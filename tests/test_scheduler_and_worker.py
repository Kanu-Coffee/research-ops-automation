"""Unit and integration tests for SchedulerService, CronExpression, and WorkerService."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
import zoneinfo
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from researchops.errors import ValidationError

from researchops.config import load_settings
from researchops.domain.models import RunLease, ScheduledRun, TaskDefinition, TaskVersion
from researchops.runners.fake import FakeRunner
from researchops.services.application import ApplicationService
from researchops.services.scheduler import CronExpression, CronField, SchedulerService
from researchops.services.worker import WorkerService


class TestCronExpression(unittest.TestCase):
    def test_restricted_dom_and_dow_use_or(self):
        cron=CronExpression("0 9 1 * 1")
        self.assertTrue(cron.matches(datetime(2026,9,1,9)))
        self.assertTrue(cron.matches(datetime(2026,9,7,9)))
        self.assertFalse(cron.matches(datetime(2026,9,8,9)))

    def test_invalid_cron_does_not_silently_become_empty(self):
        for expr in ("60 * * * *","* 24 * * *","* * 0 * *","* * * 13 *","* * * * 8","*/0 * * * *","1,,2 * * * *","foo * * * *","5-1 * * * *"):
            with self.subTest(expr=expr),self.assertRaises(ValidationError):
                CronExpression(expr)
    def test_cron_field_wildcard(self):
        field = CronField("*", 0, 59)
        self.assertTrue(field.matches(0))
        self.assertTrue(field.matches(30))
        self.assertTrue(field.matches(59))
        self.assertFalse(field.matches(60))

    def test_cron_field_step(self):
        field = CronField("*/15", 0, 59)
        self.assertTrue(field.matches(0))
        self.assertTrue(field.matches(15))
        self.assertTrue(field.matches(30))
        self.assertTrue(field.matches(45))
        self.assertFalse(field.matches(10))

    def test_cron_field_range_and_list(self):
        field = CronField("1-5,10,20-22", 0, 59)
        for v in (1, 2, 3, 4, 5, 10, 20, 21, 22):
            self.assertTrue(field.matches(v), f"Expected {v} to match")
        self.assertFalse(field.matches(0))
        self.assertFalse(field.matches(6))
        self.assertFalse(field.matches(11))

    def test_cron_field_dow_sunday(self):
        # In cron, both 0 and 7 mean Sunday
        field = CronField("0", 0, 7, is_dow=True)
        self.assertTrue(field.matches(0))
        self.assertTrue(field.matches(7))

        field7 = CronField("7", 0, 7, is_dow=True)
        self.assertTrue(field7.matches(0))
        self.assertTrue(field7.matches(7))

    def test_cron_expression_matching(self):
        # 0 9 * * 1-5 (At 09:00 on every day-of-week from Monday through Friday)
        cron = CronExpression.from_string("0 9 * * 1-5")

        # 2026-09-04 is a Friday
        dt_friday_9am = datetime(2026, 9, 4, 9, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(cron.matches(dt_friday_9am))

        # 09:01 should not match
        dt_friday_901 = datetime(2026, 9, 4, 9, 1, 0, tzinfo=timezone.utc)
        self.assertFalse(cron.matches(dt_friday_901))

        # 2026-09-05 is a Saturday (dow 6)
        dt_saturday_9am = datetime(2026, 9, 5, 9, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(cron.matches(dt_saturday_9am))


class TestSchedulerAndWorker(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

        from tests.support import isolated_settings, fixture_runner, register_fixture_task
        self.settings = isolated_settings(self.root)
        self.app = ApplicationService(self.settings,custom_runner=fixture_runner(self.settings))
        self.task_version = register_fixture_task(self.app)
        self.task_def = self.task_version.definition
        self.app.task_repo.set_task_enabled("software-releases",True)


    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_schedule_tick_due_and_not_due(self):
        # 2026-09-04 09:00:00 KST is 2026-09-04 00:00:00 UTC
        ref_due = datetime(2026, 9, 4, 0, 0, 0, tzinfo=timezone.utc)
        results = self.app.scheduler.schedule_tick(now=ref_due)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "enqueued")
        run_id = results[0]["run_id"]

        run = self.app.run_repo.get_run(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run.task_id, "software-releases")
        self.assertEqual(run.status, "queued")

        # Second tick at the exact same minute slot should skip (already_scheduled)
        results2 = self.app.scheduler.schedule_tick(now=ref_due)
        self.assertEqual(len(results2), 1)
        self.assertEqual(results2[0]["status"], "skipped")
        self.assertEqual(results2[0]["reason"], "already_scheduled")

        # Tick at 09:05 KST should be not_due
        ref_not_due = datetime(2026, 9, 4, 0, 5, 0, tzinfo=timezone.utc)
        results3 = self.app.scheduler.schedule_tick(now=ref_not_due)
        self.assertEqual(len(results3), 1)
        self.assertEqual(results3[0]["status"], "not_due")

    def test_global_scheduler_disable_preserves_queue_and_watermarks(self):
        self.settings.raw_config["scheduler"] = {"enabled": False}
        now = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)
        with patch.object(self.app.task_repo, "list_tasks", side_effect=AssertionError("disabled scheduler queried tasks")):
            self.assertEqual(self.app.scheduler.schedule_tick(now=now),
                [{"status": "disabled", "reason": "scheduler_disabled"}])
        self.assertEqual(self.app.run_repo.list_runs(), [])
        self.settings.raw_config["scheduler"]["enabled"] = True
        result = self.app.scheduler.schedule_tick(now=now)
        self.assertEqual(result[0]["status"], "enqueued")

    def test_schedule_tick_queue_coalescing(self):
        # Disable original and recreate slot
        # Enqueue manual run so task is active
        active_run = self.app.runs.enqueue_run("software-releases", trigger_type="manual")
        self.assertEqual(active_run.status, "queued")

        # Now evaluate tick for another slot
        ref_due2 = datetime(2026, 9, 5, 0, 0, 0, tzinfo=timezone.utc)  # Saturday
        # Let's use Friday next week: 2026-09-11 00:00:00 UTC (09:00 KST)
        ref_due_next_week = datetime(2026, 9, 11, 0, 0, 0, tzinfo=timezone.utc)
        results = self.app.scheduler.schedule_tick(now=ref_due_next_week)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "coalesced")
        self.assertIn("already_active", results[0]["reason"])

    def test_worker_consumes_queued_run_once(self):
        # Enqueue a run
        run = self.app.runs.enqueue_run("software-releases", trigger_type="manual")
        self.assertEqual(run.status, "queued")

        # Run worker with once=True
        completed = self.app.worker.run_worker(poll_interval=0.1, once=True)
        self.assertEqual(completed, 1)

        # Verify run succeeded
        finished_run = self.app.run_repo.get_run(run.run_id)
        self.assertIsNotNone(finished_run)
        self.assertEqual(finished_run.status, "succeeded")
        self.assertEqual(finished_run.phase, "finalize")

        # Verify lease was released
        lease = self.app.run_repo.get_lease(run.run_id)
        self.assertIsNone(lease)

    def test_business_date_uses_scheduled_seoul_instant(self):
        run=self.app.runs.enqueue_run("software-releases",scheduled_for="2026-09-04T15:01:00Z")
        self.assertEqual(run.local_date,"2026-09-05")
        self.assertEqual(run.local_date_display,"2026.09.05")
        self.assertEqual(run.scheduled_for,"2026-09-04T15:01:00+00:00")
        naive=self.app.runs.enqueue_run("software-releases",scheduled_for="2026-09-05T00:01:00")
        self.assertEqual(naive.scheduled_for,run.scheduled_for)

    def test_schedule_restart_recovers_latest_missed_slot(self):
        self.app.scheduler.schedule_tick(now=datetime(2026,9,3,1,0,tzinfo=timezone.utc))
        result=self.app.scheduler.schedule_tick(now=datetime(2026,9,5,1,0,tzinfo=timezone.utc))[0]
        self.assertEqual(result["status"],"enqueued")
        run=self.app.run_repo.get_run(result["run_id"])
        self.assertEqual(run.scheduled_for,"2026-09-05T00:00:00+00:00")
        self.assertEqual(run.local_date,"2026-09-05")

    def test_concurrent_ticks_create_only_one_occurrence(self):
        now=datetime(2026,9,4,0,0,tzinfo=timezone.utc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.app.scheduler.schedule_tick(now=now),range(2)))
        self.assertEqual(sum(r[0]["status"]=="enqueued" for r in results),1)
        self.assertEqual(len(self.app.run_repo.list_runs()),1)

    def test_duplicate_command_returns_same_run_and_rejects_changed_payload(self):
        first=self.app.runs.enqueue_run("software-releases",request_key="request-one",force_dry_run=True)
        second=self.app.runs.enqueue_run("software-releases",request_key="request-one",force_dry_run=True)
        self.assertEqual(first.run_id,second.run_id)
        self.assertEqual(len(self.app.run_repo.list_runs()),1)
        self.assertEqual(self.app.run_repo.get_execution_controls(first.run_id)["force_dry_run"],1)
        with self.assertRaises(ValidationError):
            self.app.runs.enqueue_run("software-releases",request_key="request-one",force_dry_run=False)

    def test_retry_is_queued_and_candidate_mode_is_preserved(self):
        run=self.app.runs.enqueue_run("software-releases",force_dry_run=True)
        self.app.runs.cancel_run(run.run_id)
        with patch.object(self.app.orchestrator,"execute_run",side_effect=AssertionError("must not execute")):
            retry=self.app.runs.retry_run(run.run_id)
        self.assertEqual(retry.status,"queued")
        self.assertEqual(retry.parent_run_id,run.run_id)
        self.assertEqual(self.app.run_repo.get_execution_controls(retry.run_id)["force_dry_run"],1)

    def test_compose_only_creates_new_revision_without_research(self):
        source=self.app.runs.enqueue_run("software-releases",force_dry_run=True)
        finished=self.app.runs.execute_run(source.run_id)
        self.assertEqual(finished.status,"succeeded",finished.error_message)
        original=self.app.run_repo.get_composition_input(source.run_id)
        child=self.app.runs.compose_only(source.run_id)
        self.assertEqual(child.status,"queued")
        self.assertEqual(self.app.run_repo.get_execution_controls(child.run_id)["composition_revision"],2)
        with patch.object(self.app.orchestrator.custom_runner,"execute_research",side_effect=AssertionError("compose-only must reuse immutable input")):
            composed=self.app.runs.execute_run(child.run_id)
        self.assertEqual(composed.status,"succeeded",composed.error_message)
        self.assertEqual(self.app.run_repo.get_composition_input(source.run_id),original)
        self.assertEqual(self.app.run_repo.get_composition_input(child.run_id)["composition_revision"],2)

    def test_sync_is_candidate_only_and_activation_requires_same_hash_dry_run(self):
        task_md=self.settings.paths.tasks_dir/"software-releases"/"task.md"
        task_md.write_text(task_md.read_text()+"\nNew candidate instructions\n")
        candidate=self.app.tasks.sync_canonical_tasks()[0]
        self.assertFalse(candidate.is_active)
        self.assertEqual(self.app.task_repo.get_active_version("software-releases").version_hash,self.task_version.version_hash)
        with self.assertRaises(ValidationError):
            self.app.tasks.activate_version("software-releases",candidate.version_hash)
        dry=self.app.runs.enqueue_run("software-releases",candidate_version_hash=candidate.version_hash)
        finished=self.app.runs.execute_run(dry.run_id)
        self.assertEqual(finished.status,"succeeded",finished.error_message)
        self.app.tasks.activate_version("software-releases",candidate.version_hash)
        task=self.app.task_repo.get_task_status("software-releases")
        self.assertEqual(task["active_version_hash"],candidate.version_hash)
        self.assertEqual((task["enabled"],task["delivery_approved"]),(0,0))
        with self.assertRaises(ValidationError):
            self.app.task_repo.set_delivery_approved("software-releases",True,delivery_revision="config-revision")

    def test_stale_lease_recovery(self):
        # Create a run stuck in 'running' with an expired lease
        run = self.app.runs.enqueue_run("software-releases", trigger_type="manual")
        self.app.run_repo.update_run_status(run_id=run.run_id, status="running", phase="research")

        # Lock workspace
        self.app.workspace_mgr.acquire_workspace_lock("software-releases", run.run_id, "stale-fence")

        # Create expired lease
        expired_time = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        stale_lease = RunLease(
            run_id=run.run_id,
            worker_id="ghost-worker-99",
            attempt=1,
            fencing_token="stale-fence",
            claimed_at=expired_time,
            heartbeat_at=expired_time,
            lease_expires_at=expired_time
        )
        self.app.run_repo.acquire_lease(stale_lease)

        # Worker recovers stale leases
        recovered = self.app.worker.recover_stale_leases()
        self.assertEqual(recovered, 1)

        # Check run status was marked failed
        updated_run = self.app.run_repo.get_run(run.run_id)
        self.assertEqual(updated_run.status, "needs_attention")
        self.assertIn("Stale worker lease expired", updated_run.error_message)

        # Check workspace lock was cleared
        locked, _ = self.app.workspace_mgr.is_locked("software-releases")
        self.assertTrue(locked)

        # Check lease was released
        self.assertIsNotNone(self.app.run_repo.get_lease(run.run_id))


if __name__ == "__main__":
    unittest.main()
