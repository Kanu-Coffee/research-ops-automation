"""Database-backed Run time details, legacy gaps and safe polling regressions."""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops.domain.events import AuditEvent
from researchops.services.application import ApplicationService
from tests.web_support import AuthenticatedWebRouter as WebRouter, admin_session, authenticated_headers
from researchops.web.views import render_run_detail
from researchops.web.timeline_views import format_duration, format_seoul, render_run_timeline
from tests.support import isolated_settings, fixture_runner, register_fixture_task


CREATED = "2026-09-10T00:00:00+00:00"
START = "2026-09-10T00:00:05+00:00"
END = "2026-09-10T00:01:05+00:00"
NOW = datetime(2026, 9, 10, 0, 2, tzinfo=timezone.utc)


class RunTimelineWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = isolated_settings(Path(self.temp.name))
        self.app = ApplicationService(self.settings, custom_runner=fixture_runner(self.settings))
        register_fixture_task(self.app)
        self.run = self.app.runs.enqueue_run("software-releases")
        self.run_id = self.run.run_id
        self.router = WebRouter(self.app)
        self._state()

    def _state(self, status="running", phase="research", started=START, finished=None):
        with self.app.db.transaction() as conn:
            conn.execute("UPDATE scheduled_runs SET status=?,phase=?,attempt=1,created_at=?,started_at=?,finished_at=? WHERE run_id=?",
                         (status, phase, CREATED, started, finished, self.run_id))

    def _event(self, phase="research", *, state="running", start=START, finish=None,
               duration=None, step_id=None, summary=None, **overrides):
        details = {"schema_version": 1, "step_id": step_id or f"a1-{phase}-0123456789ab", "phase": phase,
            "label": phase, "state": state, "started_at": start, "finished_at": finish,
            "duration_ms": duration, "attempt": 1, "summary": summary or {}}
        details.update(overrides)
        event = AuditEvent(entity_type="run", entity_id=self.run_id,
            event_type="run_step_started" if state == "running" else "run_step_finished",
            occurred_at=finish or start or END, details=details)
        self.app.state_repo.save_audit_event(event)
        return event

    def _html(self, tab="overview"):
        status, _, body = self.router.handle_request("GET", f"/runs/{self.run_id}?tab={tab}", headers={"Host": "localhost"})
        self.assertEqual(status, 200)
        return body.decode()

    def test_recorded_steps_use_observed_duration_and_seoul_timestamps(self):
        self._event()
        self._event(state="succeeded", finish=END, duration=58_765,
                    summary={"stdout_bytes": 2_400_000, "record_count": 15, "cleanup_verified": 1})
        self._state("succeeded", "finalize", finished=END)
        timeline = self.app.runs.get_run_timeline(self.run_id, now=NOW)
        self.assertEqual(timeline["run"]["queue_duration_ms"], 5000)
        self.assertEqual(timeline["run"]["duration_ms"], 60000)
        self.assertEqual(timeline["run"]["total_duration_ms"], 65000)
        self.assertEqual(timeline["steps"][0]["duration_ms"], 58765)
        page = self._html()
        self.assertIn("2026-09-10 09:00:05.000", page)
        self.assertIn("58.765초", page)
        self.assertIn("2.4 MB (정확히 2,400,000 B)", page)
        self.assertIn('data-step-phase="research"', page)
        self.assertNotIn("EXECUTION TIMELINE PHASES", page)

    def test_unfinished_step_is_not_inferred_successful_on_terminal_run(self):
        self._event()
        self._state("failed", "finalize", finished=END)
        step = self.app.runs.get_run_timeline(self.run_id)["steps"][0]
        self.assertFalse(step["completion_recorded"])
        self.assertFalse(step["active"])
        self.assertIsNone(step["duration_ms"])
        page = self._html()
        self.assertIn("종료 미기록", page)
        self.assertNotIn('data-step-phase="compose"', page)

    def test_later_milestone_does_not_leave_previous_missing_completion_active(self):
        self._event()
        self._event("validate", start=END)
        steps = self.app.runs.get_run_timeline(self.run_id)["steps"]
        self.assertFalse(steps[0]["active"])
        self.assertTrue(steps[1]["active"])
        self._event("validate", start=END, state="succeeded", finish="2026-09-10T00:01:06+00:00", duration=1000)
        self.assertFalse(any(step["active"] for step in self.app.runs.get_run_timeline(self.run_id)["steps"]))

    def test_active_step_uses_event_order_when_wall_clock_moves_backwards(self):
        self._event(start=END)
        self._event("validate", start=START)
        steps = {step["phase"]: step for step in self.app.runs.get_run_timeline(self.run_id)["steps"]}
        self.assertFalse(steps["research"]["active"])
        self.assertTrue(steps["validate"]["active"])

    def test_truncated_step_list_never_claims_its_last_visible_step_is_still_active(self):
        self._event()
        snapshot = self.app.run_repo.get_run_event_snapshot(self.run_id)
        snapshot["step_total_count"] = 2001
        with patch.object(self.app.run_repo, "get_run_event_snapshot", return_value=snapshot):
            timeline = self.app.runs.get_run_timeline(self.run_id)
        self.assertEqual(timeline["status"], "partial")
        self.assertEqual(timeline["omitted_event_count"], 2000)
        self.assertFalse(timeline["steps"][0]["active"])
        page = render_run_timeline(timeline, {"run_id": self.run_id}, [])
        self.assertIn("단계 이벤트 2000건이 생략", page)
        self.assertIn("종료 미기록", page)

    def test_legacy_run_never_invents_stages_or_missing_end(self):
        self._state("failed", "compose", finished=None)
        timeline = self.app.runs.get_run_timeline(self.run_id, now=NOW)
        self.assertEqual(timeline["status"], "not_recorded")
        self.assertEqual(timeline["steps"], [])
        self.assertTrue(timeline["run"]["end_missing"])
        self.assertIsNone(timeline["run"]["duration_ms"])
        self.assertIsNone(timeline["run"]["total_duration_ms"])
        page = self._html()
        self.assertIn("상세 단계 시간은 기록되지 않았습니다", page)
        self.assertIn("종료 시각 미기록", page)
        self.assertNotIn('<details class="run-step"', page)

    def test_missing_legacy_start_is_not_reinterpreted_as_queue_time(self):
        for status, finished in (("succeeded", END), ("running", None), ("awaiting_receipt", None)):
            with self.subTest(status=status):
                self._state(status, started=None, finished=finished)
                timing = self.app.runs.get_run_timeline(self.run_id, now=NOW)["run"]
                self.assertFalse(timing["queue_active"])
                self.assertIsNone(timing["queue_duration_ms"])
                self.assertIsNone(timing["duration_ms"])
                self.assertEqual(timing["total_duration_ms"], 65000 if finished else 120000)
        self._state("queued", "queued", started=None)
        timing = self.app.runs.get_run_timeline(self.run_id, now=NOW)["run"]
        self.assertTrue(timing["queue_active"])
        self.assertEqual(timing["queue_duration_ms"], 120000)

    def test_bad_step_diagnostics_never_expose_body_or_unbounded_values(self):
        self._event(summary={"record_count": 1, "body": "secret-body@example.test"})
        self._event("compose", summary={"stdout_bytes": True})
        self._event("validate", summary={"record_count": 2**63})
        timeline = self.app.runs.get_run_timeline(self.run_id)
        self.assertEqual(timeline["status"], "partial")
        self.assertEqual(timeline["invalid_event_count"], 3)
        self.assertEqual(timeline["steps"], [])
        page = self._html()
        self.assertNotIn("secret-body", page)
        self.assertIn("일부 단계 기록을 확인할 수 없어", page)

    def test_management_events_are_separate_bounded_and_secret_free(self):
        self._event()
        for index in range(55):
            self.app.state_repo.save_audit_event(AuditEvent(entity_type="run", entity_id=self.run_id,
                event_type="worker_claimed_lease", details={"fencing_token": "secret-lease-token",
                    "worker_id": "secret-worker", "body": "private@example.test", "attempt_count": index}))
        self.app.state_repo.save_audit_event(AuditEvent(entity_type="task", entity_id=self.run_id,
            event_type="wrong_entity", details={"secret": "other-private"}))
        data = self.app.runs.show_run(self.run_id)
        self.assertEqual(data["audit_events_meta"], {"total_count": 56, "shown_count": 50, "omitted_count": 6})
        self.assertEqual(len(self.app.runs.get_run_events(self.run_id)), 50)
        self.assertEqual(data["timeline"]["event_count"], 1)
        self.assertNotIn("secret", json.dumps(data["audit_events"]))
        page = self._html("logs")
        self.assertIn("관리 기록 (50)", page)
        self.assertIn("이전 6건 생략", page)
        for value in ("secret-lease-token", "secret-worker", "private@example.test", "other-private"):
            self.assertNotIn(value, page)

    def test_lightweight_status_detects_same_phase_event_and_does_not_read_archives(self):
        route = f"/api/runs/{self.run_id}/status"
        with patch.object(self.app.runs, "show_run", side_effect=AssertionError("full query")), \
             patch.object(self.app.runs, "read_run_archive_file", side_effect=AssertionError("archive read")):
            _, _, body = self.router.handle_request("GET", route, headers={"Host": "localhost"})
            before = json.loads(body)
            self._event()
            status, _, body = self.router.handle_request("GET", route, headers={"Host": "localhost"})
            after = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(before["phase"], after["phase"])
        self.assertNotEqual(before["timeline_revision"], after["timeline_revision"])
        self.assertIsNone(after["finished_at"])
        self.assertIn("timing", after)
        page = self._html()
        self.assertIn("url.searchParams.set('partial','1')", page)
        self.assertIn("current.replaceWith(next)", page)
        self.assertNotIn("location.reload()", page)

    def test_awaiting_receipt_remains_live_until_actual_delivery_finishes(self):
        self._state("awaiting_receipt", "finalize", finished=None)
        page = self._html()
        self.assertIn("진행 중 · 종료되지 않음", page)
        self.assertIn('data-refresh-active="true"', page)
        self.assertIn("setTimeout(refresh,4000)", page)

    def test_smtp_order_and_missing_old_start_are_recorded_without_guessing(self):
        self._event("finalize", state="succeeded", finish=END, duration=60000)
        smtp_end = "2026-09-10T00:01:30+00:00"
        self._event("smtp", step_id="smtp-0123456789abcdef", start=None,
                    state="succeeded", finish=smtp_end, duration=None, summary={"mime_bytes": 4096})
        self._state("succeeded", "finalize", finished=smtp_end)
        timeline = self.app.runs.get_run_timeline(self.run_id)
        self.assertEqual([step["phase"] for step in timeline["steps"]], ["finalize", "smtp"])
        self.assertTrue(timeline["steps"][-1]["completion_recorded"])
        self.assertIsNone(timeline["steps"][-1]["started_at"])
        self.assertIsNone(timeline["steps"][-1]["duration_ms"])
        page = self._html()
        self.assertIn("메일 전달", page)
        self.assertIn("4.1 KB (정확히 4,096 B)", page)
        self.assertIn("시작: 미기록", page)

    def test_smtp_completion_survives_wall_clock_rollback_without_inventing_duration(self):
        self._event("smtp", step_id="smtp-0123456789abcdef", start=END,
                    state="succeeded", finish=START, duration=None)
        self._state("succeeded", "finalize", finished=START)
        timeline = self.app.runs.get_run_timeline(self.run_id)
        self.assertEqual(timeline["status"], "recorded")
        self.assertTrue(timeline["steps"][0]["completion_recorded"])
        self.assertIsNone(timeline["steps"][0]["duration_ms"])

    def test_step_links_use_only_fixed_available_archive_files_and_existing_anchors(self):
        self._event()
        timeline = self.app.runs.get_run_timeline(self.run_id)
        page = render_run_timeline(timeline, {"run_id": self.run_id},
            [{"filename": "logs/research.stdout"}, {"filename": "logs/research.stderr"},
             {"filename": "secret-token"}], mcp=True, response=True)
        self.assertIn(f'/runs/{self.run_id}/artifacts/logs/research.stdout', page)
        self.assertIn('href="#mcp-call-results"', page)
        self.assertIn('href="#response-diagnostics"', page)
        self.assertNotIn("secret-token", page)

    def test_mcp_timing_projection_rejects_payloads_and_preserves_missing_timings(self):
        archive = self.settings.paths.run_archive_dir / "software-releases" / self.run_id
        archive.mkdir(parents=True)
        tools = [{"server": "sample", "tool": "lookup", "status": "succeeded", "success": True,
            "output_verified": True, "observed_started_at": START, "observed_finished_at": END,
            "duration_ms": 58765, "body": "secret-body"},
            {"server": "sample", "tool": "lookup", "observed_started_at": "secret-time", "duration_ms": True},
            {"server": "sample", "tool": "old"}]
        (archive / "validation-report.json").write_text(json.dumps({"executions": [{"stage": "research",
            "isolation": {"mcp": {"provider": "codex_exec"}, "mcp_tools": tools}}]}))
        observed = self.app.runs.get_run_mcp_audit(self.run_id)["phases"][0]["tools"]
        self.assertEqual(observed[0]["duration_ms"], 58765)
        self.assertIsNone(observed[1]["observed_started_at"])
        self.assertIsNone(observed[1]["duration_ms"])
        self.assertNotIn("duration_ms", observed[2])
        page = self._html("logs")
        self.assertIn("관찰 시작: 2026-09-10 09:00:05.000", page)
        self.assertIn("호출 시각·소요 미기록", page)
        self.assertNotIn("secret-time", page)
        self.assertNotIn("secret-body", page)

    def test_time_formatters_do_not_render_arbitrary_values(self):
        self.assertEqual(format_seoul("secret@example.test"), "미기록")
        self.assertEqual(format_seoul("2026-09-10T09:00:00"), "미기록")
        self.assertEqual(format_duration(True), "미기록")
        self.assertEqual(format_duration(-1), "미기록")
        self.assertEqual(format_duration(3_600_001), "1시간 0분 0.001초")

    def test_research_counter_uses_query_count_and_does_not_invent_zero(self):
        data = self.app.runs.show_run(self.run_id)
        data["research"] = {"record_count": 15, "status": "success", "summary": "sample"}
        self.assertIn("15개 항목", render_run_detail(data, [], []))
        data["research"].pop("record_count")
        self.assertIn("미기록개 항목", render_run_detail(data, [], []))


if __name__ == "__main__":
    unittest.main()
