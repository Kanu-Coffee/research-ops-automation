"""Multiple protected senders; isolated databases and mock SMTP transports only."""

from dataclasses import asdict, replace
from email import policy
from email.parser import BytesParser
import hashlib
import json
import smtplib
import unittest
from unittest.mock import patch

import yaml

from researchops.delivery.smtp_config import (
    BuiltinDeliveryConfig, SmtpSettings, delivery_revision, load_delivery_config,
    save_delivery_config,
)
from researchops.errors import DeliveryError
from researchops.services.delivery_service import DeliveryService
from tests.delivery_fixtures import DeliveryFixture, smtp_server


class TestSenderProfiles(DeliveryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.settings.environment = "production"
        self.config.smtp.username = "primary@example.test"
        self.config.smtp.password = "primary-synthetic-secret"
        self.config.sender_profiles["product-team"] = SmtpSettings(
            host="smtp.product.example.test", username="product@example.test",
            password="product-synthetic-secret", sender_email="product@example.test",
            sender_name="Product Team")
        self.config.sender_profiles["research-team"] = SmtpSettings(
            host="smtp.research.example.test", username="research@example.test",
            password="research-synthetic-secret", sender_email="research@example.test",
            sender_name="Research Team")
        save_delivery_config(self.config, self.settings.paths.delivery_config_file)
        self.service = DeliveryService(self.settings, self.delivery_repo, self.state_repo,
                                      self.publisher, self.consumer, self.dispatcher)

    def server(self):
        server = smtp_server()
        server.noop.return_value = (250, b"ok")
        return server

    def select_sender(self, profile_id):
        old_archive = self.settings.paths.run_archive_dir / self.task.id / self.run.run_id
        manifest = json.loads((old_archive / "run-manifest.json").read_text())
        self.task = replace(self.task, delivery={**self.task.delivery, "sender_profile_id": profile_id})
        self.version = replace(self.version, definition=self.task, version_hash="c" * 64)
        self.task_repo.save_version(self.version)
        self.task_repo.set_active_version(self.task.id, self.version.version_hash, delivery_mode="handoff")
        self.run = self.make_run("profile-live")
        self.comp_input = self.make_input(self.run)
        self.run_repo.save_composition_input(self.comp_input, "d" * 64)
        archive = self.settings.paths.run_archive_dir / self.task.id / self.run.run_id
        archive.mkdir(parents=True)
        manifest["run_id"] = self.run.run_id
        (archive / "run-manifest.json").write_text(json.dumps(manifest))

    def test_legacy_configuration_loads_as_default_without_migration(self):
        legacy = self.config.to_dict(include_secrets=True)
        legacy.pop("sender_profiles")
        self.settings.paths.delivery_config_file.write_text(yaml.safe_dump(legacy))
        loaded = load_delivery_config(self.settings.paths.delivery_config_file)
        self.assertEqual(loaded.sender_profiles, {})
        self.assertIs(loaded.get_sender(), loaded.smtp)
        self.assertEqual(loaded.smtp.password, "primary-synthetic-secret")

    def test_default_revision_is_identical_to_pre_profile_hash(self):
        smtp = asdict(self.config.smtp)
        smtp.pop("password_configured")
        for key in ("data_command_timeout_seconds", "body_timeout_seconds", "final_reply_timeout_seconds"):
            smtp.pop(key)
        expected = hashlib.sha256(json.dumps(
            {"smtp": smtp, "recipient_groups": self.config.recipient_groups},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(delivery_revision(self.config), expected)

    def test_revisions_ignore_other_profiles_and_default_changes(self):
        default = delivery_revision(self.config)
        product = delivery_revision(self.config, "product-team")
        self.config.sender_profiles["research-team"].password = "rotated-unrelated"
        self.assertEqual(delivery_revision(self.config), default)
        self.assertEqual(delivery_revision(self.config, "product-team"), product)
        self.config.smtp.password = "rotated-default"
        self.assertNotEqual(delivery_revision(self.config), default)
        self.assertEqual(delivery_revision(self.config, "product-team"), product)
        self.config.sender_profiles["product-team"].password = "rotated-selected"
        self.assertNotEqual(delivery_revision(self.config, "product-team"), product)

    def test_public_config_redacts_every_secret_and_preserves_own_blank_password(self):
        public = self.service.get_delivery_config()
        for profile_id, smtp in public.all_senders().items():
            self.assertEqual(smtp.password, "", profile_id)
            self.assertTrue(smtp.password_configured, profile_id)
        self.assertNotIn("synthetic-secret", repr(public))
        self.assertNotIn("synthetic-secret", json.dumps(public.to_dict()))
        public.sender_profiles["product-team"].sender_name = "Renamed Product"
        self.service.save_delivery_config(public)
        stored = load_delivery_config(self.settings.paths.delivery_config_file)
        for profile_id, smtp in self.config.all_senders().items():
            self.assertEqual(stored.get_sender(profile_id).password, smtp.password)
        self.assertEqual(stored.get_sender("product-team").sender_name, "Renamed Product")

    def test_save_profile_changes_only_selected_account(self):
        public = self.service.get_delivery_config().get_sender("product-team")
        public.sender_name = "New Label"
        self.service.save_sender_profile("product-team", public)
        stored = load_delivery_config(self.settings.paths.delivery_config_file)
        self.assertEqual(stored.get_sender("product-team").password, "product-synthetic-secret")
        self.assertEqual(stored.get_sender("research-team").password, "research-synthetic-secret")
        self.assertEqual(stored.smtp.password, "primary-synthetic-secret")
        with self.assertRaises(DeliveryError):
            self.service.save_sender_profile("product-team", public, create=True)

    def test_new_blank_profile_cannot_inherit_another_accounts_secret(self):
        new = SmtpSettings(username="new@example.test", password_configured=True)
        self.service.save_sender_profile("new-team", new, create=True)
        stored = load_delivery_config(self.settings.paths.delivery_config_file)
        self.assertEqual(stored.get_sender("new-team").password, "")
        self.assertFalse(stored.get_sender("new-team").password_configured)
        self.assertFalse(self.service.operating_status("new-team")["smtp_configured"])

    def test_sender_and_global_enabled_save_together_with_strict_boolean(self):
        profile = self.service.get_delivery_config().get_sender("product-team")
        self.service.save_sender_profile("product-team", profile, enabled=False)
        self.assertFalse(load_delivery_config(self.settings.paths.delivery_config_file).enabled)
        before = self.settings.paths.delivery_config_file.read_bytes()
        with self.assertRaises(DeliveryError):
            self.service.save_sender_profile("product-team", profile, enabled="true")
        self.assertEqual(self.settings.paths.delivery_config_file.read_bytes(), before)

    def test_service_without_dispatcher_preserves_all_credentials(self):
        self.service.smtp_dispatcher = None
        public = self.service.get_delivery_config()
        self.service.save_delivery_config(public)
        stored = load_delivery_config(self.settings.paths.delivery_config_file)
        for profile_id, smtp in self.config.all_senders().items():
            self.assertEqual(stored.get_sender(profile_id).password, smtp.password)

    def test_invalid_missing_and_reserved_profile_ids_are_rejected(self):
        for profile_id in ("", "../smtp", "UPPER", None, "x"):
            with self.subTest(profile_id=profile_id), self.assertRaises(DeliveryError):
                self.service.save_sender_profile(profile_id, SmtpSettings(), create=True)
        with self.assertRaises(DeliveryError):
            self.service.save_sender_profile("default", SmtpSettings(), create=True)
        with self.assertRaises(DeliveryError):
            self.service.save_sender_profile("missing-team", SmtpSettings())
        with self.assertRaises(DeliveryError):
            self.config.get_sender("missing-team")

    def test_task_profile_controls_host_auth_from_and_private_envelope(self):
        self.select_sender("product-team")
        handoff = self.publish()
        server = self.server()
        with patch("smtplib.SMTP", return_value=server) as connect:
            ok, receipt, message = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok, message)
        self.assertEqual(receipt.status, "smtp_accepted")
        connect.assert_called_once_with("smtp.product.example.test", 587, timeout=15)
        server.login.assert_called_once_with("product@example.test", "product-synthetic-secret")
        server.mail.assert_called_once_with("product@example.test")
        job = self.dispatcher.queue.get(handoff.handoff_id)
        self.assertEqual(json.loads(job["envelope_json"])["sender_profile_id"], "product-team")
        mime = BytesParser(policy=policy.default).parsebytes(job["mime_bytes"])
        self.assertEqual(str(mime["From"]), "Product Team <product@example.test>")
        self.assertNotIn("synthetic-secret", json.dumps(self.service.show_smtp_job(handoff.handoff_id)))
        self.assertNotIn("product@example.test", json.dumps(handoff.delivery_request))
        self.assertNotIn("synthetic-secret", json.dumps(handoff.delivery_request))

    def test_another_task_sender_uses_different_from_and_auth(self):
        self.select_sender("research-team")
        handoff = self.publish()
        server = self.server()
        with patch("smtplib.SMTP", return_value=server) as connect:
            ok, _, message = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok, message)
        connect.assert_called_once_with("smtp.research.example.test", 587, timeout=15)
        server.login.assert_called_once_with("research@example.test", "research-synthetic-secret")
        server.mail.assert_called_once_with("research@example.test")

    def test_named_profile_sends_when_unused_default_gmail_is_unconfigured(self):
        self.select_sender("product-team")
        self.config.smtp = SmtpSettings()
        save_delivery_config(self.config, self.settings.paths.delivery_config_file)
        handoff = self.publish()
        with patch("smtplib.SMTP", return_value=self.server()):
            ok, _, message = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok, message)

    def test_unrelated_account_rotation_does_not_block_queued_or_pre_data_send(self):
        self.select_sender("product-team")
        handoff = self.publish()
        self.dispatcher.enqueue_handoff(handoff.handoff_id)
        self.config.smtp.password = "primary-rotation"
        save_delivery_config(self.config, self.settings.paths.delivery_config_file)
        server = self.server()

        def recipient(_):
            self.config.sender_profiles["research-team"].password = "research-rotation"
            save_delivery_config(self.config, self.settings.paths.delivery_config_file)
            return (250, b"ok")

        server.rcpt.side_effect = recipient
        with patch("smtplib.SMTP", return_value=server):
            ok, _, message = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok, message)
        server.send.assert_called_once()

    def test_selected_profile_rotation_blocks_before_network(self):
        self.select_sender("product-team")
        handoff = self.publish()
        self.dispatcher.enqueue_handoff(handoff.handoff_id)
        self.config.sender_profiles["product-team"].password = "rotated"
        save_delivery_config(self.config, self.settings.paths.delivery_config_file)
        with patch("smtplib.SMTP") as connect:
            ok, _, _ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertFalse(ok)
        connect.assert_not_called()

    def test_selected_profile_rotation_during_rcpt_prevents_data(self):
        self.select_sender("product-team")
        handoff = self.publish()
        server = self.server()

        def recipient(_):
            self.config.sender_profiles["product-team"].password = "rotated"
            save_delivery_config(self.config, self.settings.paths.delivery_config_file)
            return (250, b"ok")

        server.rcpt.side_effect = recipient
        with patch("smtplib.SMTP", return_value=server):
            ok, receipt, _ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertFalse(ok)
        self.assertEqual(receipt.status, "failed")
        server.send.assert_not_called()

    def test_missing_selected_profile_cannot_fall_back_to_default(self):
        self.select_sender("product-team")
        handoff = self.publish()
        self.dispatcher.enqueue_handoff(handoff.handoff_id)
        del self.config.sender_profiles["product-team"]
        save_delivery_config(self.config, self.settings.paths.delivery_config_file)
        with patch("smtplib.SMTP") as connect:
            ok, _, message = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertFalse(ok)
        self.assertIn("profile", message)
        connect.assert_not_called()

    def test_legacy_pending_job_without_profile_remains_default(self):
        handoff = self.publish()
        self.dispatcher.enqueue_handoff(handoff.handoff_id)
        job = self.dispatcher.queue.get(handoff.handoff_id)
        envelope = json.loads(job["envelope_json"])
        envelope.pop("sender_profile_id")
        with self.db.transaction() as conn:
            conn.execute("UPDATE smtp_attempts SET envelope_json=? WHERE job_id=?",
                         (json.dumps(envelope), handoff.handoff_id))
        self.assertEqual(self.dispatcher.enqueue_handoff(handoff.handoff_id), handoff.handoff_id)
        self.config.sender_profiles["research-team"].password = "unrelated-rotation"
        save_delivery_config(self.config, self.settings.paths.delivery_config_file)
        server = self.server()
        with patch("smtplib.SMTP", return_value=server):
            ok, _, message = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok, message)
        server.login.assert_called_once_with("primary@example.test", "primary-synthetic-secret")
        self.assertEqual(self.service.show_smtp_job(handoff.handoff_id)["sender_profile_id"], "default")

    def test_profile_diagnostic_and_explicit_test_use_selected_transport(self):
        with patch("smtplib.SMTP", return_value=self.server()) as connect:
            ok, message = self.service.test_smtp_connection(sender_profile_id="research-team")
            self.assertTrue(ok, message)
            ok, message = self.service.send_test_email("explicit@example.test", sender_profile_id="research-team")
            self.assertTrue(ok, message)
            connect.assert_not_called()
            results = self.dispatcher.dispatch_all_pending()
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result[1] for result in results), results)
        self.assertEqual(connect.call_count, 2)
        for call in connect.call_args_list:
            self.assertEqual(call.args[0], "smtp.research.example.test")

    def test_named_profile_post_data_uncertainty_never_retries(self):
        self.select_sender("product-team")
        handoff = self.publish()
        server = self.server()
        server.getreply.side_effect = [(354, b"send body"), smtplib.SMTPServerDisconnected("lost response")]
        with patch("smtplib.SMTP", return_value=server) as connect:
            ok, receipt, _ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
            self.assertFalse(ok)
            self.assertEqual(receipt.status, "uncertain")
            self.assertEqual(self.dispatcher.dispatch_all_pending(), [])
            self.assertFalse(self.dispatcher.dispatch_handoff(handoff.handoff_id)[0])
            connect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
