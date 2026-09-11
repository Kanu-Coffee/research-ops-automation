"""No authentication/model/SMTP access in default boundary regression tests."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops.boundary_validation import (
    SOURCE_LIMIT, _generate, _source, validate_runner_boundary,
)
from researchops.cli.main import main
from researchops.runners.development_process import ProcessResult


def codex_stream(source):
    envelope = {"response_json": json.dumps({"source": source})}
    return b"\n".join(json.dumps(event).encode() for event in (
        {"type": "thread.started", "thread_id": "synthetic"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(envelope)}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    )) + b"\n"


class BoundaryValidationTests(unittest.TestCase):
    def test_default_does_not_spawn_or_create_evidence(self):
        with patch("researchops.boundary_validation.run_bounded") as spawn, patch("tempfile.mkdtemp") as mkdir:
            result = validate_runner_boundary()
        spawn.assert_not_called()
        mkdir.assert_not_called()
        self.assertEqual(result["model_invocations"], 0)
        self.assertEqual(result["exit_code"], 78)
        self.assertFalse(result["production_ready"])

    def test_live_without_kernel_prerequisites_never_calls_model(self):
        with patch("researchops.boundary_validation.run_bounded") as spawn:
            result = validate_runner_boundary(live=True)
        spawn.assert_not_called()
        self.assertEqual(result["blocker"], "kernel_prerequisites_required")

    def test_invalid_parameters_fail_before_subprocess_or_files(self):
        cases = ({"runners": ["fake"]}, {"live": 1}, {"timeout_seconds": True},
                 {"timeout_seconds": 301}, {"model": "model"}, {"live": True, "model": "-bad"})
        with patch("researchops.boundary_validation.run_bounded") as spawn, patch("tempfile.mkdtemp") as mkdir:
            for case in cases:
                with self.subTest(case=case), self.assertRaises(ValueError):
                    validate_runner_boundary(**case)
        spawn.assert_not_called()
        mkdir.assert_not_called()

    def test_strict_bounded_source_contract(self):
        self.assertEqual(_source({"source": "def total(rows): return 0\n"}), b"def total(rows): return 0\n")
        for value in ({}, {"source": ""}, {"source": "x", "path": "/host"},
                      {"source": 42}, {"source": "x\x00"}, {"source": "x" * (SOURCE_LIMIT + 1)},
                      {"source": "\ud800"}):
            with self.subTest(value=str(value)[:50]), self.assertRaises(Exception):
                _source(value)

    def test_successful_control_returns_data_without_executing_it(self):
        raw = codex_stream("raise RuntimeError('must not execute in control')")
        result = ProcessResult(0, raw, b"", True, True)
        with tempfile.TemporaryDirectory() as temp, patch("researchops.boundary_validation.shutil.which", return_value="/bin/codex"), patch("researchops.boundary_validation.run_bounded", return_value=result) as call:
            source, observed = _generate("codex_exec", Path(temp), 30, None)
            self.assertEqual(source, b"raise RuntimeError('must not execute in control')")
            self.assertTrue(observed["success"])
            self.assertEqual(observed["model_invocations"], 1)
            self.assertEqual(observed["native_tools_observed"], 0)
            self.assertNotIn("SMTP_PASSWORD", call.call_args.kwargs["env"])

    def test_unverified_control_cleanup_never_imports_source(self):
        result = ProcessResult(0, codex_stream("x=1"), b"", True, False)
        with tempfile.TemporaryDirectory() as temp, patch("researchops.boundary_validation.shutil.which", return_value="/bin/codex"), patch("researchops.boundary_validation.run_bounded", return_value=result):
            source, observed = _generate("codex_exec", Path(temp), 30, None)
        self.assertIsNone(source)
        self.assertFalse(observed["cleanup_verified"])
        self.assertEqual(observed["model_invocations"], 1)

    def test_agy_client_timeout_does_not_claim_daemon_cleanup(self):
        result = ProcessResult(124, b"", b"private provider message", True, True, "timeout", timed_out=True)
        with tempfile.TemporaryDirectory() as temp, patch("researchops.boundary_validation.shutil.which", return_value="/bin/agy"), patch("researchops.boundary_validation.run_bounded", return_value=result):
            source, observed = _generate("antigravity_exec", Path(temp), 30, None)
        self.assertIsNone(source)
        self.assertFalse(observed["cleanup_verified"])
        self.assertNotIn("private", json.dumps(observed))
        self.assertEqual(observed["cli_invocations"], 1)
        self.assertEqual(observed["model_invocations"], 0)

    def test_observed_native_action_rejects_source(self):
        raw = codex_stream("x=1").replace(b'"agent_message"', b'"command_execution"')
        result = ProcessResult(0, raw, b"", True, True)
        with tempfile.TemporaryDirectory() as temp, patch("researchops.boundary_validation.shutil.which", return_value="/bin/codex"), patch("researchops.boundary_validation.run_bounded", return_value=result):
            source, observed = _generate("codex_exec", Path(temp), 30, None)
        self.assertIsNone(source)
        self.assertFalse(observed["success"])

    def test_cli_default_does_not_initialize_operator_application(self):
        with patch("researchops.cli.main.create_application_service", side_effect=AssertionError("operator app")), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["validate-runner-boundary", "--json"]), 78)
        self.assertEqual(json.loads(output.getvalue())["smtp_attempts"], 0)

    def test_cli_operator_config_is_rejected(self):
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as raised:
            main(["validate-runner-boundary", "--config", "/must-not-open.yaml"])
        self.assertEqual(raised.exception.code, 2)

    def test_failure_after_paid_response_preserves_counts_and_report(self):
        def generated(provider, directory, timeout, model, observation):
            observation.update(success=True, cli_invocations=1, model_invocations=1,
                               cleanup_verified=True)
            return b"x=1", observation
        with tempfile.TemporaryDirectory() as parent, patch(
            "researchops.boundary_validation.IsolatedProcessLauncher.readiness", return_value={"ready": True}
        ), patch("researchops.boundary_validation._generate", side_effect=generated), patch(
            "researchops.boundary_validation._kernel_case", side_effect=[
                {"success": True, "cleanup_verified": True}, OSError("private setup details")]):
            result = validate_runner_boundary(["codex_exec"], live=True, output_parent=parent)
            saved = json.loads((Path(result["evidence_dir"]) / "report.json").read_text())
        self.assertEqual(saved["model_invocations"], 1)
        self.assertEqual(saved["cli_invocations"], 1)
        self.assertEqual(saved["exit_code"], 1)
        self.assertEqual(saved["error"], "OSError")
        self.assertNotIn("private setup details", json.dumps(saved))

    def test_failed_kernel_preflight_does_not_spend_model_call(self):
        with tempfile.TemporaryDirectory() as parent, patch(
            "researchops.boundary_validation.IsolatedProcessLauncher.readiness", return_value={"ready": True}
        ), patch("researchops.boundary_validation._generate") as generate, patch(
            "researchops.boundary_validation._kernel_case", return_value={"success": False, "cleanup_verified": True}):
            result = validate_runner_boundary(live=True, output_parent=parent)
        generate.assert_not_called()
        self.assertEqual(result["model_invocations"], 0)
        self.assertEqual(result["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
