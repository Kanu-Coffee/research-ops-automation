"""Explicit old-version replay authorization and SMTP revocation races."""

from dataclasses import replace
import copy
import json
import unittest
from unittest.mock import patch

from researchops.delivery.authorization import (
    build_delivery_authorization, require_run_delivery_authorization)
from researchops.delivery.policy import require_approval
from researchops.delivery.retry_guard import require_retry_family_clear
from researchops.delivery.smtp_config import save_delivery_config
from researchops.errors import DeliveryError, ValidationError
from tests.delivery_fixtures import DeliveryFixture, smtp_server


class TestRunDeliveryAuthorization(DeliveryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.settings.environment = "production"

    def current_version(self, *, digest="c", delivery=None):
        task = replace(self.task, runner={"type": "codex_exec"},
            delivery=copy.deepcopy(delivery if delivery is not None else self.task.delivery))
        version = replace(self.version, version_hash=digest * 64, definition=task)
        self.task_repo.save_version(version)
        self.task_repo.set_active_version(self.task.id, version.version_hash, delivery_mode=task.delivery.get("mode", "dry_run"))
        return version

    def authorization(self, trigger="compose_only"):
        return build_delivery_authorization(self.settings, self.db, task_id=self.task.id,
            source_version_hash=self.version.version_hash, trigger_type=trigger)

    def authorized_child(self):
        self.current_version()
        source = self.run
        with self.db.transaction() as conn:
            conn.execute("UPDATE scheduled_runs SET status='failed' WHERE run_id=?", (source.run_id,))
        authorization = self.authorization()
        child = replace(source, run_id="authorized-child", trigger_type="compose_only", parent_run_id=source.run_id)
        self.run_repo.create_run(child, delivery_authorization=authorization)
        source_archive = self.settings.paths.run_archive_dir / source.task_id / source.run_id
        manifest = json.loads((source_archive / "run-manifest.json").read_text())
        archive = source_archive.parent / child.run_id
        archive.mkdir()
        manifest["run_id"] = child.run_id
        (archive / "run-manifest.json").write_text(json.dumps(manifest))
        self.run = child
        self.comp_input = self.make_input(child)
        self.run_repo.save_composition_input(self.comp_input, "b" * 64)
        return child

    def test_same_delivery_old_version_recompose_publishes_and_sends(self):
        child = self.authorized_child()
        require_run_delivery_authorization(self.settings, self.db, child)
        self.assertEqual(self.run_repo.get_delivery_authorization(child.run_id)["active_task_version_hash"], "c" * 64)
        handoff = self.publish()
        server = smtp_server()
        with patch("smtplib.SMTP", return_value=server):
            ok, receipt, message = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok, message)
        self.assertEqual(receipt.status, "smtp_accepted")
        self.assertEqual(self.run_repo.get_run(child.run_id).task_version_hash, self.version.version_hash)
        server.send.assert_called_once()

    def test_old_ordinary_run_without_authorization_remains_blocked(self):
        self.current_version()
        with self.assertRaisesRegex(DeliveryError, "no longer active"):
            self.publish()
        self.assertIsNone(self.delivery_repo.get_handoff_for_run(self.run.run_id))
        with self.assertRaisesRegex(DeliveryError, "no longer active"):
            self.authorization(trigger="manual")

    def test_different_delivery_contract_cannot_be_authorized(self):
        for index, changed in enumerate(({"sender_profile_id": "other"},
                {"allowed_recipient_group_ids": ["researchops-admins"]}, {"message_type": "different"})):
            with self.subTest(changed=changed):
                self.current_version(digest=hex(index + 12)[2:], delivery={**self.task.delivery, **changed})
                with self.assertRaisesRegex(DeliveryError, "delivery settings differ"):
                    self.authorization()

    def test_active_version_race_rolls_back_run_authorization_and_command(self):
        self.current_version()
        with self.db.transaction() as conn:
            conn.execute("UPDATE scheduled_runs SET status='failed' WHERE run_id=?", (self.run.run_id,))
        authorization = self.authorization()
        self.current_version(digest="d")
        child = replace(self.run, run_id="not-created", trigger_type="retry", parent_run_id=self.run.run_id)
        with self.assertRaisesRegex(DeliveryError, "Task changed"):
            self.run_repo.create_run(child, request_key="raced", request_payload={"authorization": authorization},
                                     delivery_authorization=authorization)
        self.assertIsNone(self.run_repo.get_run(child.run_id))
        self.assertIsNone(self.run_repo.get_run_command(child.task_id, "raced"))

    def test_cancel_before_data_keeps_authorized_mail_unsent(self):
        self.authorized_child()
        handoff = self.publish()
        server = smtp_server()
        original = self.dispatcher.queue.start_data
        def race(job_id, token, **kwargs):
            self.run_repo.request_cancel(self.run.run_id)
            return original(job_id, token, **kwargs)
        with patch("smtplib.SMTP", return_value=server), \
                patch.object(self.dispatcher.queue, "start_data", side_effect=race):
            ok, _, _ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertFalse(ok)
        server.send.assert_not_called()

    def test_active_pin_is_checked_again_inside_data_transaction(self):
        self.authorized_child()
        handoff = self.publish()
        server = smtp_server()
        original = self.dispatcher.queue.start_data
        def race(job_id, token, **kwargs):
            self.current_version(digest="d")
            return original(job_id, token, **kwargs)
        with patch("smtplib.SMTP", return_value=server), \
                patch.object(self.dispatcher.queue, "start_data", side_effect=race):
            ok, _, _ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertFalse(ok)
        server.send.assert_not_called()

    def test_source_current_mode_and_live_config_still_required(self):
        self.assertIsNone(self.authorization())
        self.config.enabled = False
        save_delivery_config(self.config, self.settings.paths.delivery_config_file)
        with self.assertRaisesRegex(DeliveryError, "disabled"):
            self.authorization()
        self.config.enabled = True
        save_delivery_config(self.config, self.settings.paths.delivery_config_file)
        self.current_version(delivery={**self.task.delivery, "mode": "dry_run"})
        with self.assertRaisesRegex(DeliveryError, "Live mode|handoff mode"):
            self.authorization()

    def test_deleted_sender_task_and_unmapped_recipient_still_block(self):
        self.authorized_child()
        self.config.recipient_groups.pop("test-team")
        with self.assertRaises(DeliveryError):
            require_approval(self.settings, self.db, self.config, self.task.id, self.version.version_hash,
                             "test-team", run_id=self.run.run_id)
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO entity_catalog(kind,legacy_key,display_name,created_at,updated_at,deleted_at) VALUES('sender','default','Default','now','now','now')")
        with self.assertRaisesRegex(DeliveryError, "sender is deleted"):
            require_run_delivery_authorization(self.settings, self.db, self.run)
        with self.db.transaction() as conn:
            conn.execute("UPDATE entity_catalog SET deleted_at='now' WHERE kind='task' AND legacy_key=?", (self.task.id,))
        with self.assertRaisesRegex(DeliveryError, "task is deleted"):
            require_run_delivery_authorization(self.settings, self.db, self.run)

    def test_no_dry_run_or_development_exception_and_no_auth_tampering(self):
        self.current_version()
        auth = self.authorization()
        with self.assertRaises(ValidationError):
            self.run_repo.create_run(replace(self.run, run_id="dry-auth", trigger_type="retry"),
                                     force_dry_run=True, delivery_authorization=auth)
        self.settings.environment = "test"
        with self.assertRaisesRegex(DeliveryError, "no longer active"):
            self.authorization()
        self.settings.environment = "production"
        child = self.authorized_child()
        with self.db.transaction() as conn:
            conn.execute("UPDATE run_delivery_authorizations SET authorization_sha256=? WHERE run_id=?", ("0" * 64, child.run_id))
        with self.assertRaisesRegex(DeliveryError, "integrity mismatch"):
            require_run_delivery_authorization(self.settings, self.db, child)

    def test_historical_accepted_or_uncertain_attempt_blocks_prepared_email(self):
        handoff = self.publish()
        self.dispatcher.enqueue_handoff(handoff.handoff_id)
        with self.db.transaction() as conn:
            conn.execute("UPDATE delivery_handoffs SET status='failed',external_delivery_status=NULL WHERE handoff_id=?", (handoff.handoff_id,))
            conn.execute("UPDATE scheduled_runs SET status='failed' WHERE run_id=?", (self.run.run_id,))
        for status in ("smtp_accepted", "uncertain"):
            with self.subTest(status=status), self.db.transaction() as conn:
                conn.execute("UPDATE smtp_attempts SET status=? WHERE handoff_id=?", (status, handoff.handoff_id))
                with self.assertRaisesRegex(DeliveryError, "accepted or its delivery is uncertain"):
                    require_retry_family_clear(conn, self.run.run_id, prepared_email=True)

    def test_business_authorization_does_not_extend_to_old_version_system_alert(self):
        self.authorized_child()
        with self.assertRaisesRegex(DeliveryError, "no longer active"):
            require_approval(self.settings, self.db, self.config, self.task.id, self.version.version_hash,
                "researchops-admins", system_alert=True, run_id=self.run.run_id)

    def test_current_version_cancel_alert_keeps_existing_permission(self):
        self.run_repo.request_cancel(self.run.run_id)
        require_approval(self.settings, self.db, self.config, self.task.id, self.version.version_hash,
            "researchops-admins", system_alert=True, run_id=self.run.run_id)


if __name__ == "__main__":
    unittest.main()
