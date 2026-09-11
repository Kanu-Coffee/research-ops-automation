"""Validation CLI is safe even when operator environment points at production."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops.cli.main import main
from researchops.runner_validation import validate_runners
from researchops.runners.fake import FakeRunner
from researchops.runners.base import RunnerInvocationContext


class TestRunnerValidationCommand(unittest.TestCase):
    def test_matrix_has_fake_evidence_and_blocks_actual_model_calls(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict("os.environ", {"RESEARCHOPS_CONFIG": "/nonexistent/production.yaml",
                                          "RESEARCHOPS_ROOT": "/nonexistent/production-root"}), \
                patch("subprocess.Popen", side_effect=AssertionError("No provider spawn")), \
                patch("smtplib.SMTP", side_effect=AssertionError("No SMTP")), \
                patch("smtplib.SMTP_SSL", side_effect=AssertionError("No SMTP")):
            report = validate_runners(output_parent=directory)
            self.assertEqual(report["totals"], {"passed": 4, "failed": 0, "blocked": 8}, report["cases"])
            self.assertEqual(report["exit_code"], 78)
            self.assertEqual(report["model_invocations"], 0)
            self.assertFalse(report["operator_config_loaded"])
            self.assertEqual(json.loads(Path(report["report_path"]).read_text()), report)
            for case in report["cases"]:
                if case["runner"] == "fake":
                    self.assertTrue(all(case["checks"].values()), case)
                    self.assertTrue(Path(case["archive"]).is_dir())
                else:
                    self.assertEqual(case["evidence_type"], "preflight_only")
                    self.assertIn("LIVE-VALIDATION-NOT-REQUESTED", case["blocked_by"])

    def test_cli_fake_only_never_initializes_default_application(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output, \
                patch("researchops.cli.main.load_settings", side_effect=AssertionError("No default config")), \
                patch("researchops.cli.main.create_application_service", side_effect=AssertionError("No default DB")):
            result = main(["validate-runners", "--runner", "fake", "--output-parent", directory, "--json"])
            self.assertEqual(result, 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["totals"], {"passed": 4, "failed": 0, "blocked": 0})

    def test_invalid_selection_creates_no_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                validate_runners(["unknown"], output_parent=directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_setup_failure_retains_report_and_evidence_location(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("researchops.runner_validation.ApplicationService", side_effect=RuntimeError("setup rejected")):
            report = validate_runners(["fake"], output_parent=directory)
            self.assertEqual(report["exit_code"], 1)
            self.assertEqual(report["cases"][0]["evidence_type"], "suite_setup")
            self.assertTrue(Path(report["report_path"]).is_file())
            self.assertEqual(report["cases"][0]["error"], "setup rejected")

    def test_sample_selection_and_opt_in_validation(self):
        with tempfile.TemporaryDirectory() as directory, patch("subprocess.Popen", side_effect=AssertionError("No model")):
            report = validate_runners(["fake"], samples=["numeric-compare"], output_parent=directory)
            self.assertEqual(report["totals"], {"passed": 1, "failed": 0, "blocked": 0})
            self.assertFalse(report["live_approved"])
            self.assertEqual(report["model_invocations"], 0)
            for options in ({"samples": ["../outside"]}, {"timeout_seconds": 301, "live": True},
                            {"model": "explicit-model-without-approval"}):
                with self.assertRaises(ValueError):
                    validate_runners(["codex_exec"], output_parent=directory, **options)

    def test_cli_forwards_explicit_live_grant_without_operator_application(self):
        with redirect_stdout(io.StringIO()), \
                patch("researchops.runner_validation.validate_runners", return_value={"exit_code": 0}) as validate, \
                patch("researchops.cli.main.create_application_service", side_effect=AssertionError("No operator DB")):
            self.assertEqual(main(["validate-runners", "--live", "--runner", "codex_exec",
                "--sample", "no-updates", "--timeout-seconds", "60", "--model", "chosen-model"]), 0)
            validate.assert_called_once_with(["codex_exec"], output_parent=None, live=True,
                samples=["no-updates"], timeout_seconds=60, model="chosen-model")

    def test_unverified_provider_failure_holds_remaining_live_samples(self):
        from researchops.runners.development_process import ProcessResult
        failure = ProcessResult(1, b"", b"provider failure", True, True)
        with tempfile.TemporaryDirectory() as directory, \
                patch("researchops.runners.development_runner.shutil.which", return_value="/mock/codex"), \
                patch("researchops.runners.development_runner.run_bounded", return_value=failure) as invoke:
            report = validate_runners(["codex_exec"], live=True, output_parent=directory)
            self.assertEqual(report["totals"], {"passed": 0, "failed": 1, "blocked": 3})
            self.assertEqual(invoke.call_count, 1)
            self.assertEqual(report["model_invocations"], 0)
            self.assertEqual(report["cli_invocations"], 1)
            self.assertTrue(all(case["blocked_by"] == ["PROVIDER-INVOCATION-UNVERIFIED"] for case in report["cases"][1:]))


class TestGenericFakeComposition(unittest.TestCase):
    def compose(self, directory, **kwargs):
        root = Path(directory)
        (root / "input").mkdir()
        (root / "input/composition-input.json").write_text(json.dumps({
            "allowed_recipient_group_ids": ["sample-team"], "reportable_records": [],
            "run": {"local_date": "2026-09-06"}}))
        FakeRunner(**kwargs).execute_compose(root / "input", root / "tmp", root / "output", root / "project",
            RunnerInvocationContext("sample", "run", 1, "compose"))
        return json.loads((root / "output/composition-result.json").read_text()), (root / "output/email.txt").read_text()

    def test_empty_generic_fake_uses_allowlisted_group_and_neutral_text(self):
        with tempfile.TemporaryDirectory() as directory:
            composition, text = self.compose(directory)
            self.assertEqual(composition["recipient_group_id"], "sample-team")
            self.assertNotIn("카드", composition["subject"] + text)
            self.assertEqual(composition["included_record_ids"], [])

    def test_empty_fixture_subject_and_text_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "fixtures"
            fixture.mkdir()
            (fixture / "sample-composition-result.json").write_text(json.dumps({
                "recipient_group_id": "sample-team", "subject": "No changes in documents",
                "included_record_ids": []}))
            (fixture / "sample-email-preview.txt").write_text("Documents unchanged.")
            composition, text = self.compose(directory, fixtures_dir=fixture)
            self.assertEqual(composition["subject"], "No changes in documents")
            self.assertEqual(text, "Documents unchanged.")
