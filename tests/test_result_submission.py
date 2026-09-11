"""Exact JSON/HTML/text round trips and fail-closed native file submissions."""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from researchops.runners.antigravity import AntigravityRunner
from researchops.runners.codex import CodexRunner
from researchops.runners.development_process import ProcessResult
from researchops.runners.production import _resolve_submission
from researchops.runners.response_transport import FileResponseReference, ResponseTransportError
from researchops.runners.result_submission import (
    HELPER_DIRECTORY, SUBMISSION_DIRECTORY, prepare_result, stage_submission_helper, submit_result,
)
from tests import test_production_runner as production_fixtures


TEXT = '한글 😀 "따옴표" \\경로\\끝\n다음 줄\t탭\n테스트 설정: ' + json.dumps(
    {"date_basis": "출시일", "nested": {"문자": '"값"\\\n', "array": [1, False, None]}}, ensure_ascii=False)


def document(stage):
    if stage == "research":
        return {"status": "no_updates", "summary": TEXT, "records": [], "artifacts": [],
                "coverage": {"complete": True, "expected_target_count": 0, "completed_target_count": 0, "issues": []},
                "warnings": [], "nested": {"quotes": TEXT}}
    return {"composition_result": {"subject": TEXT, "html_path": "email.html", "text_path": "email.txt",
                "recipient_group_id": "test-team", "recipient_group_reason": TEXT, "included_record_ids": []},
            "html": '<html><head></head><body data-local-date="2026-09-10"><p>' + TEXT + '</p></body></html>',
            "text": TEXT}


def wire(provider, envelope):
    if provider == "codex_exec":
        events = [{"type": "thread.started", "thread_id": "synthetic"}, {"type": "turn.started"},
                  {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": envelope}},
                  {"type": "turn.completed", "usage": {"output_tokens": 7}}]
    else:
        events = [{"event": "init", "init": {"permission_mode": "always-proceed"}},
                  {"event": "result", "result": {"status": "SUCCESS", "structured_output": json.loads(envelope),
                   "usage": {"output_tokens": 7}, "denied_actions": []}}]
    return ProcessResult(0, b"\n".join(json.dumps(e, ensure_ascii=False).encode() for e in events) + b"\n", b"", True, True)


class ResultSubmissionTests(unittest.TestCase):
    def test_worker_helper_is_standalone_and_preserves_research_and_compose(self):
        for stage in ("research", "compose"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                helper = stage_submission_helper(root)
                value = document(stage)
                code = ("import sys,json;sys.path.insert(0,sys.argv[1]);"
                    "from researchops.runners.result_submission import submit_result;"
                    "print(submit_result(json.load(sys.stdin),sys.argv[2],sys.argv[3]))")
                result = subprocess.run([sys.executable, "-I", "-c", code, str(helper), stage,
                    str(root / SUBMISSION_DIRECTORY)], input=json.dumps(value, ensure_ascii=False).encode(),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, cwd=root)
                envelope = json.loads(result.stdout)
                ref = FileResponseReference(**envelope)
                logs = root / "audit"
                logs.mkdir()
                restored, evidence = _resolve_submission(ref, root, logs, stage)
                self.assertEqual(restored, value)
                raw = (root / SUBMISSION_DIRECTORY / "submission.json").read_bytes()
                self.assertEqual(evidence["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual((logs / f"{stage}.submission.json").read_bytes(), raw)
                self.assertNotIn(TEXT.encode(), result.stdout)

    def test_invalid_documents_do_not_create_submission_files(self):
        for value, stage in (([], "research"), ({1: "nonstring key"}, "research"),
                ({"x": float("nan")}, "research"), ({"x": "\ud800"}, "research"),
                ({"x": (1, 2)}, "research"), ({"x": "x" * 1_000_000}, "research"),
                ({"composition_result": {}, "html": "", "text": ""}, "compose")):
            with self.subTest(stage=stage, kind=type(value).__name__), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ResponseTransportError):
                    submit_result(value, stage, tmp)
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_exact_limit_and_one_extra_byte(self):
        value = {"x": "x" * (1_000_000 - len(json.dumps({"x": ""}).encode()))}
        raw, files = prepare_result(value, "research")
        self.assertEqual(len(raw), 1_000_000)
        self.assertEqual(files["result.json"], raw)
        value["x"] += "x"
        with self.assertRaises(ResponseTransportError) as error:
            prepare_result(value, "research")
        self.assertEqual(error.exception.code, "submission_size_exceeded")

    def test_bad_json_is_not_unescaped_or_repaired_and_raw_audit_survives(self):
        for raw in (b'{"summary":"config {"key":"value"}"}', b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":'):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / SUBMISSION_DIRECTORY).mkdir()
                (root / SUBMISSION_DIRECTORY / "submission.json").write_bytes(raw)
                logs = root / "audit"
                logs.mkdir()
                ref = FileResponseReference("submission.json", hashlib.sha256(raw).hexdigest(), len(raw))
                with self.assertRaises(ResponseTransportError) as error:
                    _resolve_submission(ref, root, logs, "research")
                self.assertEqual(error.exception.code, "submission_json_invalid")
                self.assertNotIn("summary", str(error.exception))
                self.assertEqual((logs / "research.submission.json").read_bytes(), raw)

    def test_unsafe_changed_missing_and_oversized_submission_files_fail_closed(self):
        for mutation, expected in (("symlink", "submission_file_unsafe"), ("hardlink", "submission_file_unsafe"),
                ("directory_link", "submission_file_unsafe"), ("missing", "submission_file_unsafe"),
                ("size", "submission_file_changed"), ("hash", "submission_file_changed"),
                ("oversize", "submission_size_exceeded")):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = root / SUBMISSION_DIRECTORY
                target.mkdir()
                value = document("research")
                ref = FileResponseReference(**json.loads(submit_result(value, "research", target)))
                path = target / "submission.json"
                if mutation == "symlink":
                    path.rename(root / "original")
                    path.symlink_to(root / "original")
                elif mutation == "hardlink":
                    os.link(path, root / "other")
                elif mutation == "directory_link":
                    target.rename(root / "original")
                    target.symlink_to(root / "original", target_is_directory=True)
                elif mutation == "missing":
                    path.unlink()
                elif mutation == "size":
                    path.write_bytes(path.read_bytes() + b" ")
                elif mutation == "hash":
                    ref = replace(ref, sha256="0" * 64)
                else:
                    path.write_bytes(b"x" * 1_000_001)
                logs = root / "audit"
                logs.mkdir()
                with self.assertRaises(ResponseTransportError) as error:
                    _resolve_submission(ref, root, logs, "research")
                self.assertEqual(error.exception.code, expected)
                self.assertEqual(list(logs.iterdir()), [])


class NativeFileSubmissionTests(unittest.TestCase):
    setUp = production_fixtures.ProductionRunnerTests.setUp
    context = production_fixtures.ProductionRunnerTests.context
    execute = production_fixtures.ProductionRunnerTests.execute

    def test_both_providers_both_phases_round_trip_with_the_same_file_contract(self):
        for provider, runner in (("codex_exec", CodexRunner()), ("antigravity_exec", AntigravityRunner())):
            for stage in ("research", "compose"):
                with self.subTest(provider=provider, stage=stage):
                    for path in self.output.iterdir():
                        path.unlink()
                    if stage == "compose":
                        (self.input / "composition-input.json").write_text("{}")
                    value = document(stage)

                    def run(argv, **kwargs):
                        work = Path(argv[argv.index("--add-dir") + 1])
                        self.assertTrue((work / HELPER_DIRECTORY).is_dir())
                        return wire(provider, submit_result(value, stage, work / SUBMISSION_DIRECTORY))

                    with patch("shutil.which", return_value="/usr/bin/provider"), \
                            patch("researchops.runners.production.run_bounded", side_effect=run):
                        result = self.execute(runner, self.context(stage, "none"))
                    self.assertTrue(result.success, result.error_message)
                    self.assertTrue(result.cleanup_verified)
                    self.assertIsNone(result.response_diagnostic)
                    self.assertEqual(result.isolation["response_submission"]["transport_version"], 2)
                    if stage == "research":
                        self.assertEqual(json.loads((self.output / "result.json").read_bytes()), value)
                    else:
                        self.assertEqual(json.loads((self.output / "composition-result.json").read_bytes()), value["composition_result"])
                        self.assertEqual((self.output / "email.html").read_bytes(), value["html"].encode())
                        self.assertEqual((self.output / "email.txt").read_bytes(), value["text"].encode())

    def test_invalid_inner_json_reports_safe_location_without_import(self):
        envelope = json.dumps({"response_json": '{"summary":"config {"key":1}"}'})
        with patch("shutil.which", return_value="/usr/bin/provider"), \
                patch("researchops.runners.production.run_bounded", return_value=wire("codex_exec", envelope)):
            result = self.execute(CodexRunner())
        self.assertFalse(result.success)
        self.assertTrue(result.cleanup_verified)
        self.assertEqual(result.response_diagnostic["code"], "inner_json_invalid")
        self.assertGreater(result.response_diagnostic["column"], 1)
        self.assertNotIn("summary", result.error_message)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_unconfirmed_terminal_or_cleanup_never_opens_submitted_file(self):
        ref = json.dumps({"transport_version": 2, "response_file": "submission.json", "sha256": "0" * 64, "size_bytes": 2})
        process = replace(wire("codex_exec", ref), cleanup_verified=False)
        with patch("shutil.which", return_value="/usr/bin/provider"), \
                patch("researchops.runners.production.run_bounded", return_value=process), \
                patch("researchops.runners.production._resolve_submission") as resolve:
            result = self.execute(CodexRunner())
        self.assertFalse(result.success)
        resolve.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [])
