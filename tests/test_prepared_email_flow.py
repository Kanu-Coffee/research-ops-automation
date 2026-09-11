"""Live-policy recovery with isolated runners, archives and SMTP doubles."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml

from researchops.delivery.smtp_config import BuiltinDeliveryConfig, SmtpSettings, save_delivery_config
from researchops.errors import DeliveryError, ValidationError
from researchops.services.application import ApplicationService
from tests.delivery_fixtures import smtp_server
from tests.support import isolated_settings, fixture_runner, register_fixture_task


class PreparedEmailFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = isolated_settings(Path(self.tmp.name))
        self.settings.environment = "production"
        self.settings.delivery.global_handoff_kill_switch = False
        task_path = self.settings.paths.tasks_dir / "software-releases/task.yaml"
        definition = yaml.safe_load(task_path.read_text())
        definition["delivery"]["mode"] = "handoff"
        definition.pop("alerting", None)
        task_path.write_text(yaml.safe_dump(definition))
        self.runner = fixture_runner(self.settings)
        self.app = ApplicationService(self.settings, custom_runner=self.runner)
        self.version = register_fixture_task(self.app)
        self.app.task_repo.set_production_delivery_enabled(self.version.task_id, self.version.version_hash, True)
        self.config = BuiltinDeliveryConfig(enabled=True,
            smtp=SmtpSettings(host="smtp.example.test", sender_email="sender@example.test"),
            recipient_groups={group: ["recipient@example.test"]
                for group in self.version.definition.delivery["allowed_recipient_group_ids"]})
        save_delivery_config(self.config, self.settings.paths.delivery_config_file)

    def failed_message(self):
        run = self.app.runs.enqueue_run(self.version.task_id,
            scheduled_for="2026-09-09T15:30:00+00:00")
        with patch.object(self.app.orchestrator.handoff_publisher, "create_and_publish_handoff",
                side_effect=DeliveryError("Synthetic publication interruption")):
            failed = self.app.runs.execute_run(run.run_id)
        self.assertEqual(failed.status, "failed", failed.error_message)
        self.assertIsNotNone(self.app.run_repo.get_composition_result(run.run_id), failed.error_message)
        self.assertIsNone(self.app.delivery_repo.get_handoff_for_run(run.run_id))
        return failed

    def change_task(self, *, delivery_change=False):
        definition = copy.deepcopy(self.version.definition)
        definition.runner["model"] = "new-model"
        if delivery_change:
            definition.delivery["sender_profile_id"] = "changed-sender"
        files = dict(self.version.package_files)
        files["task.md"] += "\nUpdated research instructions.\n"
        version = replace(self.version, definition=definition, package_files=files,
            version_hash=hashlib.sha256(json.dumps(definition.to_dict(), sort_keys=True).encode()).hexdigest())
        self.app.task_repo.save_version(version)
        self.app.task_repo.set_active_version(version.task_id, version.version_hash)
        self.app.task_repo.set_production_delivery_enabled(version.task_id, version.version_hash, True)
        return version

    def archive(self, run):
        return self.settings.paths.run_archive_dir / run.task_id / run.run_id

    def test_send_saved_email_after_task_change_without_any_model_calls(self):
        source = self.failed_message()
        archive = self.archive(source)
        original = {str(path.relative_to(archive)): path.read_bytes()
            for path in archive.rglob("*") if path.is_file()}
        self.change_task()
        self.assertTrue(self.app.runs.prepared_email_status(source.run_id)["eligible"])
        child = self.app.runs.send_prepared_email(source.run_id, request_key="send-one")
        with patch.object(self.runner, "execute_research", side_effect=AssertionError("no research")), \
                patch.object(self.runner, "execute_compose", side_effect=AssertionError("no compose")):
            completed = self.app.runs.execute_run(child.run_id)
        self.assertEqual(completed.status, "awaiting_receipt", completed.error_message)
        self.assertEqual(child.task_version_hash, source.task_version_hash)
        self.assertEqual((child.local_date, child.scheduled_for), (source.local_date, source.scheduled_for))
        target = self.archive(child)
        for name in ("email.html", "email.txt", "composition-result.json"):
            self.assertEqual((target / name).read_bytes(), original[name])
        self.assertEqual(json.loads((target / "validation-report.json").read_text())["executions"], [])
        server = smtp_server()
        with patch("smtplib.SMTP", return_value=server):
            handoff = self.app.delivery_repo.get_handoff_for_run(child.run_id)
            ok, receipt, reason = self.app.delivery.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok, reason)
        self.assertEqual(receipt.status, "smtp_accepted")
        server.send.assert_called_once()
        self.assertEqual(original, {str(path.relative_to(archive)): path.read_bytes()
            for path in archive.rglob("*") if path.is_file()})
        self.assertEqual(self.app.run_repo.get_run(source.run_id).status, "failed")
        self.assertFalse(self.app.runs.prepared_email_status(source.run_id)["eligible"])
        self.assertEqual(self.app.runs.send_prepared_email(source.run_id, request_key="send-one").run_id, child.run_id)
        with self.assertRaises((DeliveryError, ValidationError)):
            self.app.runs.send_prepared_email(source.run_id, request_key="send-again")

    def test_compose_after_task_change_reaches_smtp_double(self):
        source = self.failed_message()
        self.change_task()
        child = self.app.runs.compose_only(source.run_id)
        with patch.object(self.runner, "execute_research", side_effect=AssertionError("reuse research")):
            completed = self.app.runs.execute_run(child.run_id)
        self.assertEqual(completed.status, "awaiting_receipt", completed.error_message)
        server = smtp_server()
        with patch("smtplib.SMTP", return_value=server):
            handoff = self.app.delivery_repo.get_handoff_for_run(child.run_id)
            ok, _, reason = self.app.delivery.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok, reason)

    def test_parallel_clicks_and_cross_action_key_conflict(self):
        source = self.failed_message()
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(lambda _: self.app.runs.send_prepared_email(source.run_id,
                request_key="same-button"), range(2)))
        self.assertEqual(rows[0].run_id, rows[1].run_id)
        self.assertEqual(len(self.app.runs.list_runs()), 2)
        with self.assertRaises(ValidationError):
            self.app.runs.compose_only(source.run_id, request_key="same-button")

    def test_preparation_failure_retry_remains_delivery_only(self):
        source = self.failed_message()
        child = self.app.runs.send_prepared_email(source.run_id)
        with patch.object(self.app.run_repo, "save_composition_input", side_effect=ValidationError("interrupted")):
            failed = self.app.runs.execute_run(child.run_id)
        self.assertEqual(failed.status, "failed")
        retry = self.app.runs.retry_run(child.run_id)
        self.assertEqual(self.app.runs.get_execution_plan(retry.run_id)["scope"], "delivery_only")
        with patch.object(self.runner, "execute_research", side_effect=AssertionError("no research")), \
                patch.object(self.runner, "execute_compose", side_effect=AssertionError("no compose")):
            result = self.app.runs.execute_run(retry.run_id)
        self.assertEqual(result.status, "awaiting_receipt", result.error_message)

    def test_changed_delivery_contract_rejected_before_any_new_model(self):
        source = self.failed_message()
        self.change_task(delivery_change=True)
        self.assertFalse(self.app.runs.prepared_email_status(source.run_id)["eligible"])
        with self.assertRaises((DeliveryError, ValidationError)):
            self.app.runs.send_prepared_email(source.run_id)
        with self.assertRaises((DeliveryError, ValidationError)):
            self.app.runs.compose_only(source.run_id)
        self.assertEqual(len(self.app.runs.list_runs()), 1)

    def test_source_tamper_before_execute_does_not_publish(self):
        source = self.failed_message()
        child = self.app.runs.send_prepared_email(source.run_id)
        (self.archive(source) / "email.html").write_text("tampered")
        with patch.object(self.runner, "execute_research", side_effect=AssertionError("no research")), \
                patch.object(self.runner, "execute_compose", side_effect=AssertionError("no compose")):
            result = self.app.runs.execute_run(child.run_id)
        self.assertEqual(result.status, "failed")
        self.assertIsNone(self.app.delivery_repo.get_handoff_for_run(child.run_id))

    def test_non_handoff_source_cannot_be_promoted_to_sending(self):
        definition = copy.deepcopy(self.version.definition)
        definition.delivery["mode"] = "dry_run"
        self.version = replace(self.version, definition=definition,
            version_hash=hashlib.sha256(b"synthetic-dry-run-task").hexdigest())
        self.app.task_repo.save_version(self.version)
        self.app.task_repo.set_active_version(self.version.task_id, self.version.version_hash)
        source = self.failed_message()
        self.assertFalse(self.app.runs.prepared_email_status(source.run_id)["eligible"])
        with self.assertRaises(ValidationError):
            self.app.runs.send_prepared_email(source.run_id)
        self.assertEqual(len(self.app.runs.list_runs()), 1)
