"""Numeric task catalog identities and reversible lifecycle; no model/SMTP calls."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops.delivery.smtp_config import BuiltinDeliveryConfig, save_delivery_config
from researchops.delivery.queue import SmtpQueue
from researchops.domain.models import DeliveryHandoff
from researchops.errors import ValidationError
from researchops.services.application import ApplicationService
from researchops.services.scheduler import CronExpression
from tests.support import isolated_settings


class TestTaskCatalog(unittest.TestCase):
    def setUp(self):
        runtime = tempfile.TemporaryDirectory()
        self.addCleanup(runtime.cleanup)
        self.settings = isolated_settings(Path(runtime.name))
        self.settings.environment = "production"
        self.app = ApplicationService(self.settings)
        save_delivery_config(BuiltinDeliveryConfig(recipient_groups={
            "research-team": ["recipient@example.test"]}), self.settings.paths.delivery_config_file)
        self.app.catalog.bootstrap_delivery()

    def create(self, **changes):
        fields = dict(name="조사 업무", instructions="공식 자료 조사", recipient_group_id="research-team")
        fields.update(changes)
        return self.app.tasks.create_production_task(**fields)

    def test_new_task_automatically_allocates_numeric_identity_and_preserves_key(self):
        first = self.create()
        second = self.create(task_id="")
        self.assertNotEqual(first.task_id, second.task_id)
        for version in (first, second):
            entity = self.app.catalog.get("task", version.task_id)
            self.assertIsInstance(entity["entity_id"], int)
            self.assertEqual(version.task_id, f"task-{entity['entity_id']}")
            self.assertEqual(version.definition.id, version.task_id)
            self.assertEqual(self.app.tasks.show_task(version.task_id)["status"]["entity_id"], entity["entity_id"])

    def test_auto_creation_request_key_is_idempotent_and_conflict_does_not_overwrite(self):
        first = self.create(request_key="_form-key")
        repeated = self.create(request_key="_form-key")
        self.assertEqual(first.version_hash, repeated.version_hash)
        self.assertEqual(len(self.app.tasks.list_tasks()), 1)
        with self.assertRaises(ValidationError):
            self.create(request_key="_form-key", instructions="Different instructions")
        self.assertEqual(self.app.task_repo.get_active_version(first.task_id).version_hash, first.version_hash)

    def test_concurrent_auto_creation_same_request_key_creates_one_task(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            versions = list(workers.map(lambda _: self.create(request_key="same-form"), range(2)))
        self.assertEqual(versions[0].task_id, versions[1].task_id)
        self.assertEqual(len(self.app.tasks.list_tasks()), 1)

    def test_legacy_explicit_task_key_remains_supported(self):
        version = self.create(task_id="existing-legacy-id")
        entity = self.app.catalog.get("task", version.task_id)
        self.assertEqual(entity["legacy_key"], "existing-legacy-id")
        self.assertEqual(version.definition.id, "existing-legacy-id")
        self.assertEqual(self.app.tasks.list_tasks()[0]["display_name"], "조사 업무")

    def test_rename_only_changes_display_not_immutable_history_or_id(self):
        old = self.create()
        entity = self.app.catalog.get("task", old.task_id)
        self.app.catalog.rename("task", old.task_id, "새로운 표시 이름")
        shown = self.app.tasks.list_tasks()[0]
        self.assertEqual(shown["name"], "새로운 표시 이름")
        self.assertEqual(shown["entity_id"], entity["entity_id"])
        self.assertEqual(self.app.tasks.get_task_editor(old.task_id)["name"], "새로운 표시 이름")
        self.assertEqual(self.app.task_repo.get_active_version(old.task_id), old)

    def test_task_edit_updates_catalog_display_name_atomically(self):
        version = self.create()
        form = self.app.tasks.get_task_editor(version.task_id)
        fields = {key: form[key] for key in ("instructions", "runner_type", "recipient_group_id",
            "email_spec_md", "sender_profile_id", "cron", "schedule_enabled", "model", "expected_updated_at")}
        new = self.app.tasks.update_production_task(version.task_id, form["expected_version_hash"],
                                                    name="수정한 Task", **fields)
        self.assertEqual(self.app.catalog.get("task", version.task_id)["display_name"], "수정한 Task")
        self.assertEqual(new.task_id, version.task_id)

    def test_delete_is_reversible_and_keeps_version_and_workspace(self):
        version = self.create(schedule_enabled=True)
        workspace = self.app.workspace_mgr.init_task_workspace(version.task_id)
        marker = workspace / "project" / "retained.txt"
        marker.write_text("operator data")
        deleted = self.app.tasks.delete_task(version.task_id)
        self.assertTrue(deleted["deleted_at"])
        self.assertEqual(self.app.tasks.list_tasks(), [])
        listed = self.app.tasks.list_tasks(include_deleted=True)
        self.assertEqual(listed[0]["entity_id"], deleted["entity_id"])
        self.assertFalse(self.app.task_repo.get_task_status(version.task_id)["enabled"])
        self.assertEqual(marker.read_text(), "operator data")
        self.assertEqual(self.app.task_repo.get_version(version.version_hash), version)
        restored = self.app.tasks.restore_task(version.task_id)
        self.assertIsNone(restored["deleted_at"])
        self.assertEqual(restored["entity_id"], deleted["entity_id"])
        self.assertFalse(self.app.task_repo.get_task_status(version.task_id)["enabled"])
        self.assertEqual(len(self.app.tasks.list_tasks()), 1)

    def test_restore_replay_does_not_disable_schedule_enabled_after_restore(self):
        version = self.create()
        self.app.tasks.delete_task(version.task_id)
        self.app.tasks.restore_task(version.task_id)
        self.app.tasks.set_task_enabled(version.task_id, True)
        self.app.tasks.restore_task(version.task_id)
        self.assertTrue(self.app.task_repo.get_task_status(version.task_id)["enabled"])

    def test_deleted_task_rejects_run_enable_activate_edit_and_resurrection_by_create(self):
        version = self.create()
        editor = self.app.tasks.get_task_editor(version.task_id)
        self.app.tasks.delete_task(version.task_id)
        operations = [
            lambda: self.app.runs.enqueue_run(version.task_id),
            lambda: self.app.tasks.set_task_enabled(version.task_id, True),
            lambda: self.app.task_repo.set_task_enabled(version.task_id, True),
            lambda: self.app.tasks.activate_version(version.task_id, version.version_hash),
            lambda: self.app.tasks.approve_delivery(version.task_id, True),
            lambda: self.app.tasks.get_task_editor(version.task_id),
            lambda: self.create(task_id=version.task_id),
            lambda: self.app.tasks.update_production_task(version.task_id, editor["expected_version_hash"],
                name="Changed", instructions="Updated", runner_type="codex_exec", recipient_group_id="research-team"),
        ]
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(ValidationError):
                operation()

    def test_task_delete_rejects_queued_execution_then_succeeds_after_cancel(self):
        version = self.create()
        run = self.app.runs.enqueue_run(version.task_id)
        with self.assertRaises(ValidationError):
            self.app.tasks.delete_task(version.task_id)
        self.app.run_repo.request_cancel(run.run_id)
        self.app.tasks.delete_task(version.task_id)
        self.assertEqual(self.app.run_repo.get_run(run.run_id).status, "cancelled")
        with self.assertRaises(ValidationError):
            self.app.runs.retry_run(run.run_id)

    def test_cleanup_claim_and_pending_receipt_block_task_delete(self):
        version = self.create()
        run = self.app.runs.enqueue_run(version.task_id)
        _, lease = self.app.run_repo.claim_next_run("catalog-test", run_id=run.run_id)
        self.app.run_repo.update_run_status(run.run_id, "needs_attention", "finalize", fencing_token=lease.fencing_token)
        with self.assertRaises(ValidationError):
            self.app.tasks.delete_task(version.task_id)
        self.assertIsNone(self.app.catalog.get("task", version.task_id)["deleted_at"])

    def test_pending_smtp_receipt_blocks_delete_even_after_model_cleanup(self):
        version = self.create()
        run = self.app.runs.enqueue_run(version.task_id)
        _, lease = self.app.run_repo.claim_next_run("catalog-test", run_id=run.run_id)
        self.app.run_repo.update_run_status(run.run_id, "awaiting_receipt", "finalize", fencing_token=lease.fencing_token)
        self.app.run_repo.mark_cleanup_verified(run.run_id, lease.fencing_token)
        self.app.run_repo.release_lease(run.run_id, lease.fencing_token)
        with self.assertRaises(ValidationError):
            self.app.tasks.delete_task(version.task_id)

    def test_uncertain_smtp_after_cleanup_blocks_delete_without_rewriting_outcome(self):
        version = self.create(schedule_enabled=True)
        run = self.app.runs.enqueue_run(version.task_id)
        _, lease = self.app.run_repo.claim_next_run("catalog-test", run_id=run.run_id)
        self.app.run_repo.update_run_status(run.run_id, "awaiting_receipt", "finalize", fencing_token=lease.fencing_token)
        self.app.run_repo.mark_cleanup_verified(run.run_id, lease.fencing_token)
        self.app.run_repo.release_lease(run.run_id, lease.fencing_token)
        handoff = DeliveryHandoff(handoff_id="uncertain-handoff", idempotency_key="uncertain-key",
            run_id=run.run_id, task_id=version.task_id, task_version_hash=version.version_hash,
            message_revision=1, message_type="research_digest", recipient_group_id="research-team",
            mode="handoff", status="published", delivery_request={}, delivery_request_sha256="e" * 64)
        self.app.delivery_repo.save_handoff(handoff)
        queue = SmtpQueue(self.app.db)
        queue.enqueue(handoff.handoff_id, handoff.handoff_id, "<uncertain@example.test>", b"isolated message",
                      {"sender":"sender@example.test", "recipients":["recipient@example.test"]}, "isolated-revision")
        token = queue.claim(handoff.handoff_id)
        queue.start_data(handoff.handoff_id, token, production=True)
        queue.finish(handoff.handoff_id, token, "uncertain", error="Connection lost after DATA")
        with self.assertRaisesRegex(ValidationError, "uncertain SMTP"):
            self.app.tasks.delete_task(version.task_id)
        self.assertIsNone(self.app.catalog.get("task", version.task_id)["deleted_at"])
        self.assertTrue(self.app.task_repo.get_task_status(version.task_id)["enabled"])
        self.assertEqual(self.app.run_repo.get_run(run.run_id).status, "needs_attention")
        self.assertEqual(queue.get(handoff.handoff_id)["status"], "uncertain")
        self.assertEqual(self.app.delivery_repo.get_handoff(handoff.handoff_id).status, "uncertain")
        self.assertEqual(queue.pending(), [])

    def test_deleted_task_cannot_publish_version_directly_via_repository(self):
        version = self.create()
        self.app.tasks.delete_task(version.task_id)
        with self.assertRaises(ValidationError):
            self.app.task_repo.save_version(version)
        with self.assertRaises(ValidationError):
            self.app.task_repo.set_active_version(version.task_id, version.version_hash)
        with self.assertRaises(ValidationError):
            self.app.task_repo.publish_new_production_task(version)

    def test_deleted_task_advanced_edits_are_rejected_and_restore_keeps_version(self):
        version = self.create(task_id="advanced-target")
        form = self.app.tasks.get_task_advanced_editor(version.task_id)
        self.app.tasks.delete_task(version.task_id)
        self.assertEqual(self.app.tasks.list_tasks(), [])
        self.assertEqual(self.app.tasks.list_tasks(include_deleted=True)[0]["task_id"], version.task_id)
        with self.assertRaises(ValidationError):
            self.app.tasks.get_task_advanced_editor(version.task_id)
        with self.assertRaises(ValidationError):
            self.app.tasks.update_production_task_advanced(version.task_id, form["expected_version_hash"],
                expected_updated_at=form["expected_updated_at"], config_yaml=form["config_yaml"],
                task_md="Forbidden deleted edit", email_spec_md=form["email_spec_md"])
        self.app.tasks.restore_task(version.task_id)
        self.assertEqual(self.app.tasks.get_task_advanced_editor(version.task_id)["expected_version_hash"], version.version_hash)
        self.assertFalse(self.app.task_repo.get_task_status(version.task_id)["enabled"])

    def test_delete_racing_between_version_read_and_enqueue_cannot_queue(self):
        version = self.create()
        original = self.app.run_repo.create_run

        def interleave(run, **kwargs):
            self.app.tasks.delete_task(version.task_id)
            return original(run, **kwargs)

        with patch.object(self.app.run_repo, "create_run", side_effect=interleave), self.assertRaises(ValidationError):
            self.app.runs.enqueue_run(version.task_id)
        self.assertEqual(self.app.runs.list_runs(task_id=version.task_id), [])

    def test_delete_and_enqueue_race_has_only_one_winner(self):
        version = self.create()

        def perform(action):
            try:
                return action()
            except ValidationError:
                return None

        with ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(perform, [lambda: self.app.tasks.delete_task(version.task_id),
                                      lambda: self.app.runs.enqueue_run(version.task_id)]))
        deleted = self.app.catalog.get("task", version.task_id)["deleted_at"] is not None
        queued = any(run.status == "queued" for run in self.app.runs.list_runs(task_id=version.task_id))
        self.assertNotEqual(deleted, queued)

    def test_scheduler_rechecks_deletion_in_transaction_with_stale_task_snapshot(self):
        version = self.create(schedule_enabled=True)
        self.app.tasks.delete_task(version.task_id)
        result = self.app.scheduler._tick_task(version.task_id, version, CronExpression("0 9 * * *"),
            "enqueue_once", datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.app.task_repo.list_tasks(enabled_only=True), [])
        self.assertEqual(self.app.runs.list_runs(task_id=version.task_id), [])

    def test_restored_task_cannot_run_with_deleted_recipient_group(self):
        version = self.create()
        self.app.tasks.delete_task(version.task_id)
        self.app.catalog.delete("recipient_group", "research-team")
        self.app.tasks.restore_task(version.task_id)
        with self.assertRaises(ValidationError):
            self.app.runs.enqueue_run(version.task_id)
        with self.assertRaises(ValidationError):
            self.app.tasks.set_task_enabled(version.task_id, True)


if __name__ == "__main__":
    unittest.main()
