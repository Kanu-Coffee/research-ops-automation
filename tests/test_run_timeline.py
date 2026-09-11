"""Persistent milestones follow real execution and the same ownership boundary."""

from dataclasses import replace
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from researchops.domain.events import AuditEvent
from researchops.engine.archive import RunArchive
from researchops.engine.run_timeline import RunTimeline, safe_summary, validate_step_event
from researchops.errors import ConcurrencyError, ValidationError
from researchops.runners.base import RunnerExecutionResult
from researchops.runners.fake import FakeRunner
from tests import test_orchestrator as fixtures


class RunTimelineTests(unittest.TestCase):
    setUp = fixtures.TestOrchestrator.setUp
    tearDown = fixtures.TestOrchestrator.tearDown
    _setup_task = fixtures.TestOrchestrator._setup_task
    _orchestrator = fixtures.TestOrchestrator._orchestrator

    def events(self, run_id):
        return self.run_repo.get_run_event_snapshot(run_id)["step_events"]

    def finished(self, run_id):
        return {item["details"]["phase"]: item["details"] for item in self.events(run_id)
                if item["event_type"] == "run_step_finished"}

    def claim(self):
        _, _, run = self._setup_task()
        claimed, lease = self.run_repo.claim_next_run("test", lease_seconds=3600, run_id=run.run_id)
        return run, RunTimeline(self.run_repo, run.run_id, claimed.attempt, lease.fencing_token)

    def test_running_steps_are_visible_and_archive_never_predicts_publication(self):
        _, _, run = self._setup_task()
        runner = FakeRunner()
        original = runner.execute_research
        observations = []

        def observe(**kwargs):
            current = self.events(run.run_id)
            observations.append(current)
            self.assertEqual(current[-1]["details"]["phase"], "research")
            self.assertEqual(current[-1]["details"]["state"], "running")
            self.assertIsNone(current[-1]["details"]["finished_at"])
            self.assertEqual(self.run_repo.get_run(run.run_id).status, "running")
            return original(**kwargs)

        with patch.object(runner, "execute_research", side_effect=observe):
            result = self._orchestrator(runner).execute_run(run.run_id)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(observations), 1)
        events = self.events(run.run_id)
        phases = [item["details"]["phase"] for item in events if item["event_type"] == "run_step_started"]
        self.assertEqual(phases, ["preflight", "research", "validate", "dedupe", "artifact_acquisition",
                                 "prepare_compose", "compose", "validate_message", "handoff", "finalize"])
        self.assertEqual(len(events), 20)
        for event in events:
            validate_step_event(event["event_type"], event["details"])
        self.assertTrue(all(item["state"] == "succeeded" for item in self.finished(run.run_id).values()))
        self.assertEqual(self.finished(run.run_id)["research"]["summary"]["cleanup_verified"], 1)
        archive = self.archive_dir / run.task_id / run.run_id
        raw = (archive / "timeline.json").read_bytes()
        snapshot = json.loads(raw)
        self.assertEqual(snapshot["snapshot_scope"], "before_archive_publication")
        self.assertEqual(snapshot["events"], events[:-1])
        self.assertEqual(snapshot["events"][-1]["details"]["state"], "running")
        index = json.loads((archive / "artifact-manifest.json").read_text())
        entry = next(item for item in index["artifacts"] if item["relative_path"] == "timeline.json")
        self.assertEqual(entry["sha256"], hashlib.sha256(raw).hexdigest())

    def test_research_failure_records_actual_step_only_and_never_content(self):
        _, _, run = self._setup_task()
        failure = RunnerExecutionResult(False, 124, "private model body", "private token", cleanup_verified=True,
                                        error_message="secret response error", events=[{"type": "runner"}],
                                        isolation={"trace_diagnostics": {"parsed_events": 37}})
        runner = FakeRunner()
        with patch.object(runner, "execute_research", return_value=failure):
            result = self._orchestrator(runner).execute_run(run.run_id)
        self.assertEqual(result.status, "timed_out")
        steps = self.finished(run.run_id)
        self.assertEqual(set(steps), {"preflight", "research", "finalize"})
        self.assertEqual(steps["research"]["state"], "timed_out")
        self.assertEqual(steps["research"]["summary"]["exit_code"], 124)
        self.assertEqual(steps["research"]["summary"]["event_count"], 37)
        text = json.dumps(self.events(run.run_id))
        self.assertNotIn("private", text)
        self.assertNotIn("secret", text)

    def test_cancellation_closes_research_cancelled_and_preserves_cleanup(self):
        _, _, run = self._setup_task()
        runner = FakeRunner()
        original = runner.execute_research

        def cancel(**kwargs):
            result = original(**kwargs)
            self.run_repo.request_cancel(run.run_id)
            return result

        with patch.object(runner, "execute_research", side_effect=cancel):
            result = self._orchestrator(runner).execute_run(run.run_id)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.finished(run.run_id)["research"]["state"], "cancelled")
        self.assertNotIn("compose", self.finished(run.run_id))
        self.assertIsNone(self.run_repo.get_lease(run.run_id))
        self.assertFalse(self.ws_mgr.is_locked(run.task_id)[0])

    def test_cleanup_unknown_keeps_claim_and_marks_the_worker_step_attention(self):
        _, _, run = self._setup_task()
        runner = FakeRunner()
        failure = RunnerExecutionResult(False, 1, "", "", cleanup_verified=False)
        with patch.object(runner, "execute_research", return_value=failure):
            result = self._orchestrator(runner).execute_run(run.run_id)
        self.assertEqual(result.status, "needs_attention")
        step = self.finished(run.run_id)["research"]
        self.assertEqual(step["state"], "needs_attention")
        self.assertEqual(step["summary"]["cleanup_verified"], 0)
        self.assertTrue(self.ws_mgr.is_locked(run.task_id)[0])
        self.assertIsNotNone(self.run_repo.get_lease(run.run_id))

    def test_primary_lifecycle_error_survives_unverified_remote_cleanup(self):
        _, _, run = self._setup_task()
        failure = RunnerExecutionResult(
            False, -9, "", "", cleanup_verified=False,
            error_message="trace_invalid_lifecycle: unfinished_tool_actions",
            isolation={"control_process_cleanup_verified": True,
                       "remote_turn_completion_unverified": True})
        with patch.object(FakeRunner, "execute_research", return_value=failure):
            result = self._orchestrator(FakeRunner()).execute_run(run.run_id)
        self.assertEqual(result.status, "needs_attention")
        self.assertIn("trace_invalid_lifecycle: unfinished_tool_actions", result.error_message)
        self.assertIn("Local process cleanup verified; remote completion unverified", result.error_message)
        self.assertTrue(self.ws_mgr.is_locked(run.task_id)[0])
        self.assertIsNotNone(self.run_repo.get_lease(run.run_id))

    def test_invalid_research_is_validate_failure_not_research_failure(self):
        _, _, run = self._setup_task()
        result = self._orchestrator(FakeRunner(custom_research_result={"secret": "not a result"})).execute_run(run.run_id)
        self.assertEqual(result.status, "failed")
        steps = self.finished(run.run_id)
        self.assertEqual(steps["research"]["state"], "succeeded")
        self.assertEqual(steps["validate"]["state"], "failed")
        self.assertNotIn("dedupe", steps)
        self.assertNotIn("secret", json.dumps(self.events(run.run_id)))

    def test_compose_only_records_reuse_without_inventing_research_steps(self):
        _, _, parent = self._setup_task()
        self.assertEqual(self._orchestrator().execute_run(parent.run_id).status, "succeeded")
        child = replace(parent, run_id="run-timeline-compose-only", parent_run_id=parent.run_id, trigger_type="compose_only")
        self.run_repo.create_run(child, composition_revision=2)
        runner = FakeRunner()
        with patch.object(runner, "execute_research", side_effect=AssertionError("must not invoke research")):
            result = self._orchestrator(runner).execute_run(child.run_id)
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertEqual(set(self.finished(child.run_id)),
                         {"preflight", "prepare_compose", "compose", "validate_message", "handoff", "finalize"})

    def test_explicit_empty_policy_records_skipped_compose_preparation(self):
        _, _, run = self._setup_task(send_on_empty=False)
        runner = FakeRunner(custom_research_result={"status": "no_updates", "summary": "none", "records": []})
        result = self._orchestrator(runner).execute_run(run.run_id)
        self.assertEqual(result.status, "succeeded")
        steps = self.finished(run.run_id)
        self.assertEqual(steps["prepare_compose"]["state"], "skipped")
        self.assertNotIn("compose", steps)
        self.assertNotIn("handoff", steps)

    def test_setup_failure_keeps_preflight_and_releases_the_claim(self):
        _, _, run = self._setup_task()
        with patch("researchops.engine.orchestrator.RunArchive", side_effect=OSError("disk failed")):
            with self.assertRaises(OSError):
                self._orchestrator().execute_run(run.run_id)
        self.assertEqual(self.finished(run.run_id)["preflight"]["state"], "failed")
        self.assertEqual(len(self.events(run.run_id)), 2)
        self.assertIsNone(self.run_repo.get_lease(run.run_id))

    def test_archive_failure_records_attention_without_false_published_completion(self):
        _, _, run = self._setup_task()
        with patch.object(RunArchive, "finish", side_effect=OSError("fsync failed")):
            with self.assertRaises(OSError):
                self._orchestrator().execute_run(run.run_id)
        self.assertEqual(self.finished(run.run_id)["finalize"]["state"], "needs_attention")
        self.assertEqual(self.run_repo.get_run(run.run_id).status, "needs_attention")
        pending = next((self.archive_dir / run.task_id).glob(".*.pending-*"))
        evidence = json.loads((pending / "timeline.json").read_text())
        self.assertEqual(evidence["events"][-1]["details"]["state"], "running")
        self.assertFalse((self.archive_dir / run.task_id / run.run_id).exists())

    def test_run_finish_is_observed_after_archive_publication(self):
        _, _, run = self._setup_task()
        original = RunArchive.finish
        published = []

        def observe(archive, *args, **kwargs):
            original(archive, *args, **kwargs)
            published.append(datetime.now(timezone.utc))

        with patch.object(RunArchive, "finish", observe):
            result = self._orchestrator().execute_run(run.run_id)
        ended = datetime.fromisoformat(result.finished_at)
        self.assertGreaterEqual(ended, published[0])
        self.assertEqual(result.finished_at, self.finished(run.run_id)["finalize"]["finished_at"])
        manifest = json.loads((self.archive_dir / run.task_id / run.run_id / "run-manifest.json").read_text())
        self.assertLessEqual(datetime.fromisoformat(manifest["finished_at"]), published[0])

    def test_commit_failure_after_publication_rolls_back_false_success(self):
        _, _, run = self._setup_task()
        archive = self.archive_dir / run.task_id / run.run_id
        original = self.db.transaction
        failed = []

        @contextmanager
        def fail_once_after_publication():
            with original() as conn:
                yield conn
                if archive.exists() and not failed:
                    failed.append((archive / "timeline.json").read_bytes())
                    raise sqlite3.OperationalError("simulated commit failure")

        with patch.object(self.db, "transaction", fail_once_after_publication):
            with self.assertRaisesRegex(sqlite3.OperationalError, "simulated commit failure"):
                self._orchestrator().execute_run(run.run_id)
        self.assertEqual(self.run_repo.get_run(run.run_id).status, "needs_attention")
        self.assertEqual(self.delivery_repo.get_handoff_for_run(run.run_id).status, "failed")
        self.assertEqual(self.finished(run.run_id)["finalize"]["state"], "needs_attention")
        self.assertEqual((archive / "timeline.json").read_bytes(), failed[0])
        self.assertIsNone(self.run_repo.get_lease(run.run_id))
        self.assertFalse(self.ws_mgr.is_locked(run.task_id)[0])

    def test_failed_diagnostic_writer_does_not_bypass_failure_or_cleanup(self):
        _, _, run = self._setup_task()
        with self.db.transaction() as conn:
            conn.execute("""CREATE TRIGGER fail_completion BEFORE INSERT ON audit_events
                WHEN NEW.event_type='run_step_finished' BEGIN SELECT RAISE(ABORT, 'audit write failed'); END""")
        with self.assertLogs("researchops.engine.orchestrator", level="WARNING"):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "audit write failed"):
                self._orchestrator().execute_run(run.run_id)
        self.assertEqual(self.run_repo.get_run(run.run_id).status, "needs_attention")
        self.assertEqual(len(self.events(run.run_id)), 1)
        self.assertEqual(self.events(run.run_id)[0]["details"]["state"], "running")
        self.assertIsNone(self.run_repo.get_lease(run.run_id))
        self.assertFalse(self.ws_mgr.is_locked(run.task_id)[0])

    def test_stale_owner_cannot_close_observed_step_or_advance_phase(self):
        run, timeline = self.claim()
        timeline.transition("preflight", coarse_phase="preflight")
        before = self.events(run.run_id)
        with self.db.transaction() as conn:
            conn.execute("UPDATE run_leases SET fencing_token='new-owner' WHERE run_id=?", (run.run_id,))
            conn.execute("UPDATE task_claims SET fencing_token='new-owner' WHERE run_id=?", (run.run_id,))
        with self.assertRaises(ConcurrencyError):
            timeline.transition("research", coarse_phase="research")
        with self.assertRaises(ConcurrencyError):
            timeline.finish("failed")
        self.assertEqual(self.events(run.run_id), before)
        self.assertEqual(self.run_repo.get_run(run.run_id).phase, "preflight")

    def test_phase_and_both_events_roll_back_if_second_insert_fails(self):
        run, timeline = self.claim()
        timeline.transition("preflight", coarse_phase="preflight")
        before = self.events(run.run_id)
        with self.db.transaction() as conn:
            conn.execute("""CREATE TRIGGER fail_start BEFORE INSERT ON audit_events
                WHEN NEW.event_type='run_step_started' BEGIN SELECT RAISE(ABORT, 'audit write failed'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            timeline.transition("research", coarse_phase="research")
        self.assertEqual(self.events(run.run_id), before)
        self.assertEqual(timeline.active["phase"], "preflight")
        self.assertEqual(self.run_repo.get_run(run.run_id).phase, "preflight")

    def test_event_snapshot_separates_management_and_revision_changes_with_fine_steps(self):
        run, timeline = self.claim()
        self.state_repo.save_audit_event(AuditEvent("run", run.run_id, "run_enqueued", {"source": "manual"}))
        timeline.transition("dedupe", coarse_phase="dedupe")
        before = self.run_repo.get_run_event_snapshot(run.run_id)
        timeline.transition("artifact_acquisition")
        after = self.run_repo.get_run_event_snapshot(run.run_id, step_limit=1, audit_limit=1)
        self.assertEqual(after["step_total_count"], 3)
        self.assertEqual(len(after["step_events"]), 1)
        self.assertEqual(after["audit_total_count"], 1)
        self.assertEqual(after["audit_events"][0]["event_type"], "run_enqueued")
        self.assertGreater(after["revision"], before["revision"])
        self.assertEqual(self.run_repo.get_run(run.run_id).phase, "dedupe")
        counts = self.run_repo.get_run_event_snapshot(run.run_id, step_limit=0, audit_limit=0)
        self.assertEqual(counts["revision"], after["revision"])
        self.assertEqual(counts["step_events"], [])
        self.assertEqual(counts["audit_events"], [])

    def test_step_summary_rejects_content_bools_and_unsafe_numbers(self):
        for summary in ({"token": "secret"}, {"record_count": True}, {"total_bytes": -1},
                        {"exit_code": 2**63}, {"cleanup_verified": 2}, {"record_count": "1"}):
            with self.subTest(summary=summary), self.assertRaises(ValidationError):
                safe_summary(summary)
        self.assertEqual(safe_summary({"exit_code": -9, "mime_bytes": 42}), {"exit_code": -9, "mime_bytes": 42})

    def test_smtp_unknown_start_is_accepted_only_for_delivery(self):
        _, timeline = self.claim()
        timeline.transition("research")
        details = {**timeline.active, "phase": "smtp", "label": "smtp", "state": "failed",
                   "finished_at": timeline.active["started_at"], "started_at": None, "duration_ms": None}
        validate_step_event("run_step_finished", details)
        details.update(phase="research", label="research")
        with self.assertRaises(ValidationError):
            validate_step_event("run_step_finished", details)
