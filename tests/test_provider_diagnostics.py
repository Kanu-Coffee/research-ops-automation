"""Provider failures stay primary without relaxing either lifecycle parser."""

import json
import unittest
from unittest.mock import patch

from researchops.runners.antigravity import AntigravityRunner
from researchops.runners.codex import CodexRunner
from researchops.runners.development_process import ProcessResult
from researchops.runners.production_trace import ProductionToolTraceCollector
from researchops.runners.provider_diagnostics import agy_error_diagnostic
from researchops.runners.tool_events import parse_tool_trace
from tests import test_production_runner as fixtures


QUOTA = "Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 1h40m11s."


def trace_bytes(error=QUOTA, status="ERROR", steps=()):
    events = [{"event": "init", "init": {"permission_mode": "always-proceed"}}, *steps,
              {"event": "result", "result": {"status": status, "error": error,
                  "usage": {"input_tokens": 0, "output_tokens": 0}}}]
    return ("\n".join(json.dumps(event) for event in events) + "\n").encode()


class ProviderDiagnosticsTests(unittest.TestCase):
    def test_quota_diagnostic_parity_and_safe_reset_hint(self):
        raw = trace_bytes()
        collector = ProductionToolTraceCollector("antigravity_exec")
        for start in range(0, len(raw), 13):
            collector.feed(raw[start:start + 13])
        for trace in (parse_tool_trace("antigravity_exec", raw), collector.finish()):
            self.assertTrue(trace.terminal)
            self.assertFalse(trace.successful_terminal)
            self.assertEqual(trace.provider_error_code, "quota_exhausted")
            self.assertEqual(trace.provider_diagnostic, {"code": "quota_exhausted", "reset_after_seconds": 6011})
            self.assertEqual(trace.response_diagnostic["code"], "response_missing")
        self.assertEqual(collector.diagnostics()["pending_tool_count"], 0)

    def test_quota_in_model_or_tool_text_does_not_classify_provider_failure(self):
        result = {"status": "SUCCESS", "structured_output": {"response_json": json.dumps({"text": QUOTA})}}
        self.assertIsNone(agy_error_diagnostic(result))
        self.assertEqual(agy_error_diagnostic({"status": "ERROR", "error": "User says " + QUOTA}), {"code": "provider_error"})

    def test_error_text_and_malformed_or_unbounded_reset_are_not_published(self):
        for suffix in (" secret@example.invalid token=SECRET", " Resets in 999d.", " Resets in 999999999999s.", " Resets in ."):
            value = agy_error_diagnostic({"status": "ERROR", "error": "Individual quota reached." + suffix})
            self.assertEqual(value, {"code": "quota_exhausted"})
            self.assertNotIn("SECRET", json.dumps(value))

    def test_quota_does_not_make_unfinished_action_a_valid_terminal(self):
        step = {"event": "step_update", "step_update": {"step_type": "tool", "step_index": 1,
            "state": "ACTIVE", "tool_name": "run_command", "tool_info": {"name": "run_command", "parameters": {}}}}
        raw = trace_bytes(steps=[step])
        with self.assertRaises(ValueError):
            parse_tool_trace("antigravity_exec", raw)
        collector = ProductionToolTraceCollector("antigravity_exec")
        with self.assertRaises(ValueError):
            collector.feed(raw)
        self.assertFalse(collector.partial_trace().terminal)

    def _harness(self):
        fixture = fixtures.ProductionRunnerTests("test_agy_finish_control_pair_imports_research_and_compose")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_quota_exit_one_is_primary_and_never_imports_output(self):
        fixture = self._harness()
        (fixture.input / "composition-input.json").write_text("{}")
        process = ProcessResult(1, trace_bytes(), b"", True, True)
        with patch("shutil.which", return_value="/usr/bin/agy"), patch("researchops.runners.production.run_bounded", return_value=process):
            result = fixture.execute(AntigravityRunner(), fixture.context("compose"))
        self.assertFalse(result.success)
        self.assertTrue(result.cleanup_verified)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("사용량 한도 소진", result.error_message)
        self.assertNotIn("Provider exited", result.error_message)
        self.assertFalse(result.output_files)
        self.assertEqual(result.isolation["provider_diagnostic"]["reset_after_seconds"], 6011)
        self.assertEqual(result.response_diagnostic["code"], "response_missing")

    def test_timeout_priority_and_cleanup_failure_gate_are_preserved(self):
        fixture = self._harness()
        for process in (ProcessResult(124, trace_bytes(), b"", True, True, error="timeout", timed_out=True),
                        ProcessResult(1, trace_bytes(), b"", True, False)):
            with patch("shutil.which", return_value="/usr/bin/agy"), patch("researchops.runners.production.run_bounded", return_value=process):
                result = fixture.execute(AntigravityRunner())
            self.assertFalse(result.success)
            if process.error:
                self.assertNotIn("사용량 한도 소진", result.error_message)
            else:
                self.assertIn("사용량 한도 소진", result.error_message)
                self.assertFalse(result.cleanup_verified)

    def test_codex_advertised_max_ultra_pass_to_cli_without_policy_changes(self):
        for effort in ("max", "ultra"):
            fixture = self._harness()
            context = fixture.context()
            context.model, context.reasoning_effort = "unit-model", effort
            with patch("shutil.which", return_value="/usr/bin/codex"), patch("researchops.runners.production.run_bounded",
                    return_value=fixtures.response("codex_exec", {"records": []})) as run:
                result = fixture.execute(CodexRunner(), context)
            self.assertTrue(result.success, result.error_message)
            argv = run.call_args.args[0]
            self.assertIn('model_reasoning_effort="' + effort + '"', argv)
            self.assertIn('features.multi_agent=false', argv)
            self.assertIsNone(result.isolation["provider_diagnostic"])


if __name__ == "__main__":
    unittest.main()
