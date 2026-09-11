"""Safe response diagnostics across trace, archive queries and the Run page."""

import json
import unittest
from unittest.mock import Mock, patch

from researchops.runners.base import RunnerExecutionResult, safe_response_diagnostic
from researchops.runners.production_trace import ProductionToolTraceCollector, TraceStreamError
from researchops.runners.response_transport import FileResponseReference
from tests.web_support import AuthenticatedWebRouter as WebRouter, admin_session, authenticated_headers
from researchops.web.response_views import render_run_response_diagnostics
from tests import test_archive_queries
from tests.test_runner_tool_events import agy_events, codex_events, encode


INNER_ERROR = {"code": "inner_json_invalid", "stage": "inner_json", "line": 2, "column": 10, "offset": 11}


class ResponseTraceDiagnosticsTests(unittest.TestCase):
    def test_both_provider_traces_keep_content_free_inner_json_locations(self):
        envelope = {"response_json": '{\n"field": secret-token\n}'}
        for provider in ("codex_exec", "antigravity_exec"):
            with self.subTest(provider=provider):
                events = codex_events() if provider == "codex_exec" else agy_events()
                if provider == "codex_exec":
                    events[-2]["item"]["text"] = json.dumps(envelope)
                else:
                    events[-1]["result"]["structured_output"] = envelope
                collector = ProductionToolTraceCollector(provider)
                collector.feed(encode(events))
                trace = collector.finish()
                self.assertIsNone(trace.response)
                self.assertEqual(trace.response_diagnostic, INNER_ERROR)
                self.assertEqual(collector.diagnostics()["response_diagnostic"], INNER_ERROR)
                self.assertNotIn("secret-token", trace.response_error)
                self.assertNotIn("field", json.dumps(trace.response_diagnostic))

    def test_partial_trace_keeps_response_diagnostic_without_admitting_a_result(self):
        events = codex_events()
        events[-2]["item"]["text"] = json.dumps({"response_json": '{\n"field": secret-token\n}'})
        collector = ProductionToolTraceCollector("codex_exec")
        collector.feed(encode(events[:-1]))
        with self.assertRaisesRegex(TraceStreamError, "trace_missing_terminal"):
            collector.finish()
        trace = collector.partial_trace()
        self.assertFalse(trace.successful_terminal)
        self.assertIsNone(trace.response)
        self.assertEqual(trace.response_diagnostic, INNER_ERROR)
        self.assertEqual(collector.diagnostics()["response_diagnostic"], INNER_ERROR)

    def test_later_valid_response_clears_earlier_message_diagnostic(self):
        events = codex_events([{"id": "commentary", "type": "agent_message", "text": "secret-prose"}])
        collector = ProductionToolTraceCollector("codex_exec")
        collector.feed(encode(events))
        trace = collector.finish()
        self.assertIsNotNone(trace.response)
        self.assertIsNone(trace.response_diagnostic)
        self.assertIsNone(collector.diagnostics()["response_diagnostic"])

    def test_file_reference_stays_typed_and_has_no_parse_error(self):
        envelope = {"transport_version": 2, "response_file": "submission.json", "sha256": "a" * 64, "size_bytes": 123}
        events = codex_events()
        events[-2]["item"]["text"] = json.dumps(envelope)
        collector = ProductionToolTraceCollector("codex_exec")
        collector.feed(encode(events))
        trace = collector.finish()
        self.assertIsInstance(trace.response, FileResponseReference)
        self.assertIsNone(trace.response_diagnostic)

    def test_safe_diagnostic_removes_arbitrary_fields_and_invalid_positions(self):
        safe = safe_response_diagnostic({**INNER_ERROR, "body": "secret-token", "line": True,
            "column": "secret-token", "offset": -1})
        self.assertEqual(safe, {**INNER_ERROR, "line": None, "column": None, "offset": None})
        self.assertIsNone(safe_response_diagnostic({**INNER_ERROR, "code": "secret-token"}))
        self.assertIsNone(safe_response_diagnostic({**INNER_ERROR, "stage": "secret-token"}))
        self.assertEqual(safe_response_diagnostic({**INNER_ERROR, "offset": 0})["offset"], 0)
        self.assertIsNone(safe_response_diagnostic({**INNER_ERROR, "line": 2 ** 31})["line"])


class RunResponseDiagnosticsTests(unittest.TestCase):
    setUp = test_archive_queries.TestArchiveQueries.setUp

    def write_report(self, report):
        (self.archive / "validation-report.json").write_text(json.dumps(report))

    def test_response_diagnostic_is_visible_without_mcp_inventory(self):
        self.write_report({"executions": [{"stage": "compose", "response_diagnostic": {
            **INNER_ERROR, "body": "secret-token", "json": {"private": "secret-token"}}}]})
        summary = self.app.runs.show_run(self.run.run_id)["response_diagnostics"]
        self.assertEqual(summary, {"status": "recorded", "errors": [{"invocation_stage": "compose", **INNER_ERROR}]})
        status, _, body = WebRouter(self.app).handle_request("GET", f"/runs/{self.run.run_id}?tab=logs",
                                                          headers={"Host": "localhost"})
        self.assertEqual(status, 200)
        self.assertIn(b'id="response-diagnostics"', body)
        self.assertIn(b"inner_json_invalid", body)
        self.assertIn("행 2 · 열 10".encode(), body)
        self.assertNotIn(b"secret-token", body)
        self.assertNotIn(b'"private"', body)

    def test_old_runs_and_null_diagnostics_do_not_gain_an_error_card(self):
        for report in ({}, {"executions": [{"stage": "research"}]},
                       {"executions": [{"stage": "research", "response_diagnostic": None}]}):
            self.write_report(report)
            summary = self.app.runs.get_run_response_diagnostics(self.run.run_id)
            self.assertEqual(summary, {"status": "not_recorded", "errors": []})
            self.assertEqual(render_run_response_diagnostics(summary), "")
        (self.archive / "validation-report.json").unlink()
        self.assertEqual(self.app.runs.get_run_response_diagnostics(self.run.run_id)["status"], "not_recorded")

    def test_submission_diagnostics_do_not_expose_file_paths_or_decoder_messages(self):
        for code, stage in (("submission_file_unsafe", "submission"),
                            ("submission_json_invalid", "submission"), ("import_size_exceeded", "import")):
            with self.subTest(code=code):
                self.write_report({"executions": [{"stage": "research", "response_diagnostic": {
                    "code": code, "stage": stage, "line": None, "column": None, "offset": None,
                    "response_file": "/private/secret-token.json", "message": "secret-token"}}]})
                summary = self.app.runs.get_run_response_diagnostics(self.run.run_id)
                self.assertEqual(summary["status"], "recorded")
                self.assertNotIn("secret-token", json.dumps(summary))
                rendered = render_run_response_diagnostics(summary)
                self.assertIn(code, rendered)
                self.assertNotIn("secret-token", rendered)
                self.assertIn("위치 정보 없음", rendered)

    def test_invalid_diagnostic_or_archive_is_unavailable_without_raw_data(self):
        for report in ({"executions": [{"response_diagnostic": {**INNER_ERROR, "code": "secret-token"}}]},
                       {"executions": [{"response_diagnostic": {**INNER_ERROR, "stage": "secret-token"}}]},
                       {"executions": ["secret-token"]}, {"executions": [{}] * 101}):
            self.write_report(report)
            summary = self.app.runs.get_run_response_diagnostics(self.run.run_id)
            self.assertEqual(summary, {"status": "unavailable", "errors": []})
            rendered = render_run_response_diagnostics(summary)
            self.assertIn("응답 오류 진단을 확인할 수 없습니다", rendered)
            self.assertNotIn("secret-token", rendered)
        (self.archive / "validation-report.json").write_text('{"secret-token":')
        self.assertEqual(self.app.runs.get_run_response_diagnostics(self.run.run_id)["status"], "unavailable")

    def test_unknown_invocation_and_non_numeric_locations_do_not_leak_into_ui(self):
        self.write_report({"executions": [{"stage": "secret-token", "response_diagnostic": {
            **INNER_ERROR, "line": "secret-token", "column": False, "offset": 2 ** 80}}]})
        summary = self.app.runs.get_run_response_diagnostics(self.run.run_id)
        self.assertEqual(summary["errors"][0]["invocation_stage"], "unknown")
        self.assertNotIn("secret-token", json.dumps(summary))
        rendered = render_run_response_diagnostics(summary)
        self.assertIn("위치 정보 없음", rendered)
        self.assertNotIn("secret-token", rendered)

    def test_runner_diagnostic_reaches_archive_and_query_on_failed_fixture_run(self):
        runner = Mock()
        runner.execute_research.return_value = RunnerExecutionResult(False, 1, "", "",
            error_message="inner_json_invalid (stage=inner_json, line=2, column=10, offset=11)",
            cleanup_verified=True, response_diagnostic=INNER_ERROR)
        run = self.app.runs.enqueue_run("software-releases", force_dry_run=True)
        with patch.object(self.app.runs.orchestrator, "resolve_runner", return_value=runner):
            finished = self.app.runs.execute_run(run.run_id)
        self.assertEqual(finished.status, "failed")
        archive = self.settings.paths.run_archive_dir / run.task_id / run.run_id
        report = json.loads((archive / "validation-report.json").read_bytes())
        self.assertEqual(report["executions"][0]["response_diagnostic"], INNER_ERROR)
        self.assertEqual(self.app.runs.show_run(run.run_id)["response_diagnostics"]["errors"],
                         [{"invocation_stage": "research", **INNER_ERROR}])
        runner.execute_compose.assert_not_called()
        self.assertIsNone(self.app.delivery_repo.get_handoff_for_run(run.run_id))


if __name__ == "__main__":
    unittest.main()
