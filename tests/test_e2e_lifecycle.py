"""Full sealed-candidate -> Seoul schedule -> immutable archive -> local SMTP acceptance."""
from datetime import datetime, timezone
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from tests.delivery_fixtures import smtp_server
import yaml

from researchops.services.application import ApplicationService
from researchops.delivery.smtp_config import BuiltinDeliveryConfig, SmtpSettings
from researchops.errors import ValidationError
from tests.package_support import template_package, register_package
from tests.support import isolated_settings, fixture_runner


class TestE2ELifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = isolated_settings(self.root)
        self.app = ApplicationService(self.settings, custom_runner=fixture_runner(self.settings))

    def test_full_autonomous_lifecycle(self):
        task_id = "e2e-market-task"
        files = template_package(self.settings, task_id)
        config = yaml.safe_load(files["task.yaml"])
        config["runner"]["type"] = "fake"
        config["schedule"] = {"cron":"30 0 * * *", "timezone":"Asia/Seoul","misfire_policy":"enqueue_once"}
        config["delivery"]["mode"] = "handoff"
        files["task.yaml"] = yaml.safe_dump(config)
        sealed = register_package(self.app, files)
        with self.assertRaises(ValidationError):
            self.app.tasks.activate_version(task_id, sealed.version_hash)
        candidate = self.app.runs.enqueue_run(task_id, trigger_type="candidate_dry_run",
                                              candidate_version_hash=sealed.version_hash)
        completed = self.app.runs.execute_run(candidate.run_id, force_dry_run=True)
        self.assertEqual(completed.status, "succeeded", completed.error_message)
        self.app.tasks.activate_version(task_id, sealed.version_hash)
        self.app.tasks.set_task_enabled(task_id, True)
        smtp_config = BuiltinDeliveryConfig(enabled=True, auto_dispatch=True,
            smtp=SmtpSettings(host="smtp.example.test", sender_email="sender@example.test"),
            recipient_groups={group:["recipient@example.test"] for group in config["delivery"]["allowed_recipient_group_ids"]})
        self.app.delivery.save_delivery_config(smtp_config)
        self.settings.delivery.global_handoff_kill_switch = False
        self.app.tasks.approve_delivery(task_id)
        reference = datetime(2026,9,4,15,30,tzinfo=timezone.utc)  # Seoul Sep 5, 00:30
        results = self.app.scheduler.schedule_tick(reference)
        scheduled = next(row for row in results if row["task_id"] == task_id)
        run_id = scheduled["run_id"]
        run = self.app.run_repo.get_run(run_id)
        self.assertEqual(run.local_date, "2026-09-05")
        again = self.app.scheduler.schedule_tick(reference)
        self.assertNotEqual(again[0]["status"], "enqueued")
        self.assertEqual(self.app.worker.run_worker(once=True), 1)
        run = self.app.run_repo.get_run(run_id)
        self.assertEqual(run.status, "awaiting_receipt", run.error_message)
        self.assertFalse(self.app.workspace_mgr.is_locked(task_id)[0])
        archive = self.settings.paths.run_archive_dir / task_id / run_id
        for name in ("composition-input.json","result.json","email.html","email.txt",
                     "delivery-request.json","validation-report.json","dedupe-report.json",
                     "run-manifest.json","artifact-manifest.json","logs/research.stdout"):
            self.assertTrue((archive/name).exists(), name)
        old_bytes = (archive/"run-manifest.json").read_bytes()
        server = smtp_server()
        with patch("smtplib.SMTP", return_value=server):
            sent = self.app.delivery.dispatch_all_pending()
        self.assertEqual(len(sent),1,sent)
        self.assertTrue(sent[0][1],sent)
        self.assertEqual(self.app.run_repo.get_run(run_id).status,"succeeded")
        handoff = self.app.delivery_repo.get_handoff_for_run(run_id)
        self.assertEqual(handoff.external_delivery_status,"smtp_accepted")
        server.send.assert_called_once()
        self.assertEqual(old_bytes,(archive/"run-manifest.json").read_bytes())
        with patch("smtplib.SMTP") as transport:
            self.app.delivery.dispatch_all_pending()
            transport.assert_not_called()
        # Compose-only preserves source research and increments the message revision.
        child = self.app.runs.compose_only(run_id)
        with patch.object(self.app.orchestrator.custom_runner,"execute_research",side_effect=AssertionError("must not research")):
            result = self.app.runs.execute_run(child.run_id,force_dry_run=True)
        self.assertEqual(result.status,"succeeded",result.error_message)
        original_input = self.app.run_repo.get_composition_input(run_id)
        next_input = self.app.run_repo.get_composition_input(child.run_id)
        self.assertEqual(next_input["reportable_records"], original_input["reportable_records"])
        self.assertEqual(next_input["composition_revision"], original_input["composition_revision"]+1)
        self.assertEqual(old_bytes,(archive/"run-manifest.json").read_bytes())
