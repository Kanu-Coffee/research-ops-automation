"""Owner-scoped worker catalogs and SMTP jobs, with synthetic transport only."""

from unittest.mock import patch
import unittest

from researchops.delivery.recipient_routing import catalog_recipient_snapshot, require_current_recipient
from researchops.delivery.smtp_config import SmtpSettings, delivery_revision, save_delivery_config
from researchops.errors import DeliveryError, NotFoundError
from researchops.services.auth_service import AuthService
from researchops.services.catalog_service import CatalogService
from researchops.services.delivery_service import DeliveryService
from researchops.services.ownership import creation_owner
from tests.delivery_fixtures import DeliveryFixture, smtp_server

PASSWORD = "Synthetic owned delivery password"
NEW_PASSWORD = "Permanent owned delivery password"


class OwnedDeliveryTests(DeliveryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.catalog = CatalogService(self.settings, self.db)
        self.catalog.bootstrap_delivery(self.config)
        self.auth = AuthService(self.settings, self.db)
        self.admin = self.auth.setup(self.auth.issue_setup_token(), "mail-admin", PASSWORD)
        self.delivery = DeliveryService(self.settings, self.delivery_repo, self.state_repo,
            self.publisher, self.consumer, self.dispatcher, self.catalog)

    def account(self, name, role="user"):
        user = self.auth.create_user(self.admin.session.principal, name, name, role, PASSWORD)
        initial = self.auth.login(name, PASSWORD)
        return user, self.auth.change_password(initial.session.principal, PASSWORD, NEW_PASSWORD)

    def foreign_resources(self, principal):
        with creation_owner(principal.user_id):
            # The same label in another namespace must not make the worker's
            # exact-name resolution ambiguous or reveal the other group.
            group = self.catalog.allocate("recipient_group", "test-team")
            sender = self.catalog.allocate("sender", "Foreign Sender")
        self.config.recipient_groups[group["legacy_key"]] = ["foreign@example.test"]
        self.config.sender_profiles[sender["legacy_key"]] = SmtpSettings(
            host="smtp.other.example.test", sender_email="foreign-sender@example.test")
        save_delivery_config(self.config, self.settings.paths.delivery_config_file)
        return group["legacy_key"], sender["legacy_key"]

    def test_other_users_group_changes_preserve_queued_legacy_mail_and_worker_snapshot(self):
        handoff = self.publish()
        job_id = self.dispatcher.enqueue_handoff(handoff.handoff_id)
        original = self.dispatcher.queue.get(job_id)["config_revision"]
        self.assertEqual(original, delivery_revision(self.config))
        _, user = self.account("other-owner")
        group, _ = self.foreign_resources(user.session.principal)
        self.config.recipient_groups[group] = ["changed-foreign@example.test"]
        save_delivery_config(self.config, self.settings.paths.delivery_config_file)
        self.assertEqual(delivery_revision(self.config, db=self.db), original)
        snapshot = catalog_recipient_snapshot(self.db, self.config, task_id=self.task.id)
        self.assertEqual({row["recipient_group_id"] for row in snapshot}, {"test-team", "researchops-admins"})
        self.assertNotIn(group, repr(snapshot))
        server = smtp_server()
        with patch("smtplib.SMTP", return_value=server):
            ok, _, message = self.dispatcher._dispatch_job(job_id)
        self.assertTrue(ok, message)
        self.assertEqual([call.args[0] for call in server.rcpt.call_args_list],
            ["first@example.test", "second@example.test"])

    def test_foreign_recipient_cannot_bypass_task_scope_and_readiness_counts_are_local(self):
        _, user = self.account("other-owner")
        group, sender = self.foreign_resources(user.session.principal)
        with self.assertRaises(DeliveryError):
            require_current_recipient(self.db, self.config, group, task_id=self.task.id)
        admin_state = self.delivery.operating_status("default")
        user_state = self.delivery.operating_status(sender, owner_user_id=user.session.principal.user_id)
        self.assertEqual((admin_state["sender_profile_count"], admin_state["recipient_group_count"]), (1, 2))
        self.assertEqual((user_state["sender_profile_count"], user_state["recipient_group_count"]), (1, 1))
        with self.assertRaises(NotFoundError):
            self.delivery.operating_status("default", owner_user_id=user.session.principal.user_id)

    def test_disabled_user_owner_keeps_existing_mail_and_owned_resource_namespace(self):
        # Changing the original owner's role does not transfer their existing
        # Task. A second administrator then disables that user's browser login.
        _, second_admin = self.account("second-admin", "admin")
        principal = second_admin.session.principal
        user_id = self.admin.session.principal.user_id
        self.auth.update_user(principal, user_id, role="user")
        self.auth.update_user(principal, user_id, active=False)
        self.assertIsNone(self.auth.resolve_session(self.admin.token))
        self.assertEqual({item["recipient_group_id"] for item in
            catalog_recipient_snapshot(self.db, self.config, task_id=self.task.id)},
            {"test-team", "researchops-admins"})
        handoff = self.publish()
        server = smtp_server()
        with patch("smtplib.SMTP", return_value=server):
            ok, _, message = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok, message)
        server.send.assert_called_once()
        with creation_owner(user_id):
            entry = self.catalog.allocate("sender", "Administrator creates for disabled owner")
        self.assertEqual(entry["owner_user_id"], user_id)


if __name__ == "__main__":
    unittest.main()
