"""Production authorization without candidate-run prerequisites; no external mail."""

import unittest
from unittest.mock import patch

from researchops.delivery.policy import require_approval
from researchops.delivery.smtp_config import SmtpSettings, save_delivery_config
from researchops.errors import DeliveryError
from tests.delivery_fixtures import DeliveryFixture, smtp_server


class TestProductionDelivery(DeliveryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.settings.environment = "production"
        with self.db.transaction() as conn:
            conn.execute("""UPDATE tasks SET delivery_approved=0, approved_version_hash=NULL,
                approved_delivery_revision=NULL, approval_dry_run_id=NULL WHERE task_id=?""", (self.task.id,))

    def server(self):
        server = smtp_server()
        return server

    def test_published_production_task_sends_without_trial_or_approval(self):
        handoff = self.publish()
        server = self.server()
        with patch("smtplib.SMTP", return_value=server):
            ok, receipt, message = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok, message)
        self.assertEqual(receipt.status, "smtp_accepted")
        server.send.assert_called_once()
        self.assertEqual(self.run_repo.get_run(self.run.run_id).status, "succeeded")

    def test_new_group_does_not_require_existing_task_reapproval(self):
        self.config.recipient_groups["another-team"] = ["another@example.test"]
        save_delivery_config(self.config, self.settings.paths.delivery_config_file)
        require_approval(self.settings, self.db, self.config, self.task.id,
                         self.version.version_hash, "test-team", run_id=self.run.run_id)

    def test_unmapped_or_unselected_group_still_cannot_receive(self):
        for group in ("absent-team", "researchops-admins"):
            with self.assertRaises(DeliveryError):
                require_approval(self.settings, self.db, self.config, self.task.id,
                                 self.version.version_hash, group, run_id=self.run.run_id)

    def test_cancel_before_data_still_prevents_production_email(self):
        handoff = self.publish()
        server = self.server()
        original = self.dispatcher.queue.start_data

        def race(job_id, token, **kwargs):
            self.run_repo.request_cancel(self.run.run_id)
            return original(job_id, token, **kwargs)

        with patch("smtplib.SMTP", return_value=server), patch.object(self.dispatcher.queue, "start_data", side_effect=race):
            ok, _, _ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertFalse(ok)
        server.send.assert_not_called()

    def test_gmail_requires_actual_account_and_app_password(self):
        with self.assertRaisesRegex(DeliveryError, "app password"):
            SmtpSettings(sender_email="operator@example.test").validate(sending=True)
