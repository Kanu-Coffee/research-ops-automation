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

    def test_compose_html_tag_balance_presubmit_validation(self):
        compose_base = document("compose")

        # 1. 5 Hyundai items followed by extra closing </div> tags
        hyundai_extra_div = (
            '<html><head></head><body data-local-date="2026-09-10">\n'
            '<div class="card-list">\n'
            '  <div class="card-1">현대 181109</div>\n'
            '  <div class="card-2">현대 181110</div>\n'
            '  <div class="card-3">현대 181111</div>\n'
            '  <div class="card-4">현대 181112</div>\n'
            '  <div class="card-5">현대 181113</div>\n'
            '</div></div>\n'
            '</body></html>'
        )
        bad_doc = {**compose_base, "html": hyundai_extra_div}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ResponseTransportError) as caught:
                submit_result(bad_doc, "compose", tmp)
            error = caught.exception
            self.assertEqual(error.code, "submission_html_unbalanced")
            self.assertEqual(error.stage, "compose_html")
            self.assertEqual(error.line, 8)
            self.assertEqual(error.column, 7)
            self.assertEqual(hyundai_extra_div[error.offset:error.offset + 6], "</div>")
            self.assertNotIn("현대", str(error))
            self.assertNotIn("card", str(error))
            self.assertEqual(list(Path(tmp).iterdir()), [])

        # 2. Last KB item with unclosed start tag at EOF
        kb_unclosed_eof = (
            '<html><head></head><body data-local-date="2026-09-10">\n'
            '<div class="card-list">\n'
            '  <div class="card-hyundai">현대 181109</div>\n'
            '</div>\n'
            '<div class="card-kb">\n'
            '  <p>KB 국민카드 안내</p>'
        )
        bad_kb_doc = {**compose_base, "html": kb_unclosed_eof}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ResponseTransportError) as caught:
                submit_result(bad_kb_doc, "compose", tmp)
            error = caught.exception
            self.assertEqual(error.code, "submission_html_unbalanced")
            self.assertEqual(error.stage, "compose_html")
            self.assertEqual(error.line, 5)
            self.assertEqual(error.column, 1)
            self.assertEqual(kb_unclosed_eof[error.offset:error.offset + 4], "<div")
            self.assertNotIn("국민카드", str(error))
            self.assertNotIn("card-kb", str(error))
            self.assertEqual(list(Path(tmp).iterdir()), [])

        # 3. Mismatched nesting
        mismatched_html = '<html><head></head><body data-local-date="2026-09-10"><div><p>내용</div></p></body></html>'
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ResponseTransportError) as caught:
                submit_result({**compose_base, "html": mismatched_html}, "compose", tmp)
            error = caught.exception
            self.assertEqual(error.code, "submission_html_unbalanced")
            self.assertEqual(error.stage, "compose_html")
            self.assertEqual(mismatched_html[error.offset:error.offset + 6], "</div>")

        # 4. Void tags and self-closing tags pass cleanly
        void_html = (
            '<html><head><meta charset="utf-8"></head>'
            '<body data-local-date="2026-09-10">'
            '<img src="cid:card-plate.png" alt="카드 앞면">'
            '<br><hr>'
            '<div />'
            '<p>설명<br/>끝</p>'
            '</body></html>'
        )
        raw, files = prepare_result({**compose_base, "html": void_html}, "compose")
        self.assertIn(b"cid:card-plate.png", files["email.html"])

        # 5. Normal nested tables and CID images pass cleanly
        table_html = (
            '<html><head></head><body data-local-date="2026-09-10">'
            '<table border="0"><tr><td>'
            '<img src="cid:plate.png" alt="플레이트"><br>'
            '<span>혜택 안내</span>'
            '</td></tr></table>'
            '</body></html>'
        )
        raw, files = prepare_result({**compose_base, "html": table_html}, "compose")
        self.assertIn("혜택 안내".encode("utf-8"), files["email.html"])

        # 6. Single-line HTML with Korean text and tags
        single_line_bad = '<html><body><span>안내: 현대카드 &amp; KB국민카드</span></div></body></html>'
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ResponseTransportError) as caught:
                submit_result({**compose_base, "html": single_line_bad}, "compose", tmp)
            error = caught.exception
            self.assertEqual(error.code, "submission_html_unbalanced")
            self.assertEqual(error.stage, "compose_html")
            self.assertEqual(error.line, 1)
            self.assertEqual(single_line_bad[error.offset:error.offset + 6], "</div>")
            self.assertNotIn("현대카드", str(error))
            self.assertNotIn("KB국민카드", str(error))

        # 7. Research stage does not check HTML tag balance
        research_doc = document("research")
        research_doc["summary"] = "<div>여분 태그가 있어도 Research는 통과</div></div>"
        raw, files = prepare_result(research_doc, "research")
        self.assertIn("여분 태그가 있어도".encode("utf-8"), raw)

    def test_compose_html_diagnostic_offsets_across_line_terminators(self):
        compose_base = document("compose")
        # Reviewer exact reproduction cases: offset must be 15, line must be 2, col must be 1
        repro_cr = "<html>\r<div>한글\n</span></div></html>"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ResponseTransportError) as caught:
                submit_result({**compose_base, "html": repro_cr}, "compose", tmp)
            err = caught.exception
            self.assertEqual(err.code, "submission_html_unbalanced")
            self.assertEqual(err.stage, "compose_html")
            self.assertEqual(err.line, 2)
            self.assertEqual(err.column, 1)
            self.assertEqual(err.offset, 15)
            self.assertEqual(repro_cr[err.offset:err.offset + 7], "</span>")
            self.assertEqual(repro_cr.split("\n")[err.line - 1][err.column - 1:err.column - 1 + 7], "</span>")
            self.assertEqual(list(Path(tmp).iterdir()), [])

        repro_u2028 = "<html>\u2028<div>한글\n</span></div></html>"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ResponseTransportError) as caught:
                submit_result({**compose_base, "html": repro_u2028}, "compose", tmp)
            err = caught.exception
            self.assertEqual(err.code, "submission_html_unbalanced")
            self.assertEqual(err.stage, "compose_html")
            self.assertEqual(err.line, 2)
            self.assertEqual(err.column, 1)
            self.assertEqual(err.offset, 15)
            self.assertEqual(repro_u2028[err.offset:err.offset + 7], "</span>")
            self.assertEqual(repro_u2028.split("\n")[err.line - 1][err.column - 1:err.column - 1 + 7], "</span>")
            self.assertEqual(list(Path(tmp).iterdir()), [])

        # Comprehensive matrix of line terminators: \n, \r\n, \r, \u2028
        expected_mismatch = {
            "\n": (3, 1, 21),
            "\r\n": (3, 1, 22),
            "\r": (2, 1, 21),
            "\u2028": (2, 1, 21),
        }
        expected_unclosed = {
            "\n": (2, 1, 7),
            "\r\n": (2, 1, 8),
            "\r": (1, 8, 7),
            "\u2028": (1, 8, 7),
        }

        for sep in ("\n", "\r\n", "\r", "\u2028"):
            with self.subTest(terminator=repr(sep)):
                exp_line, exp_col, exp_off = expected_mismatch[sep]
                # Case A: Mismatched end tag with Korean text preceding the tag
                html_mismatch = f"<html>{sep}<div>한글 카드 안내\n</span></div></html>"
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(ResponseTransportError) as caught:
                        submit_result({**compose_base, "html": html_mismatch}, "compose", tmp)
                    err = caught.exception
                    self.assertEqual(err.code, "submission_html_unbalanced")
                    self.assertEqual(err.stage, "compose_html")
                    self.assertEqual(err.line, exp_line)
                    self.assertEqual(err.column, exp_col)
                    self.assertEqual(err.offset, exp_off)
                    self.assertEqual(html_mismatch[err.offset:err.offset + 7], "</span>")
                    lines = html_mismatch.split("\n")
                    self.assertEqual(lines[err.line - 1][err.column - 1:err.column - 1 + 7], "</span>")
                    self.assertNotIn("한글", str(err))
                    self.assertNotIn("카드", str(err))
                    self.assertEqual(list(Path(tmp).iterdir()), [])

                # Case B: Unclosed start tag with Korean text
                exp_u_line, exp_u_col, exp_u_off = expected_unclosed[sep]
                html_unclosed = f"<html>{sep}<div class=\"unclosed\">한글 카드 안내"
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(ResponseTransportError) as caught:
                        submit_result({**compose_base, "html": html_unclosed}, "compose", tmp)
                    err = caught.exception
                    self.assertEqual(err.code, "submission_html_unbalanced")
                    self.assertEqual(err.stage, "compose_html")
                    self.assertEqual(err.line, exp_u_line)
                    self.assertEqual(err.column, exp_u_col)
                    self.assertEqual(err.offset, exp_u_off)
                    self.assertEqual(html_unclosed[err.offset:err.offset + 22], "<div class=\"unclosed\">")
                    lines_u = html_unclosed.split("\n")
                    self.assertEqual(lines_u[err.line - 1][err.column - 1:err.column - 1 + 22], "<div class=\"unclosed\">")
                    self.assertNotIn("한글", str(err))
                    self.assertNotIn("카드", str(err))
                    self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_in_session_repair_and_resubmission_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = stage_submission_helper(root)
            submission_dir = root / SUBMISSION_DIRECTORY
            bad_doc = document("compose")
            bad_doc["html"] = (
                '<html><head></head><body data-local-date="2026-09-10">\n'
                '<div><p>미종결 태그\n'
                '</body></html>'
            )
            code = (
                "import sys, json, os\n"
                "from pathlib import Path\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from researchops.runners.result_submission import submit_result\n"
                "from researchops.runners.response_transport import ResponseTransportError\n"
                "doc = json.load(sys.stdin)\n"
                "sub_dir = sys.argv[2]\n"
                "sub_file = Path(sub_dir) / 'submission.json'\n"
                "try:\n"
                "    submit_result(doc, 'compose', sub_dir)\n"
                "    sys.exit(10)\n"
                "except ResponseTransportError as exc:\n"
                "    if exc.code != 'submission_html_unbalanced' or exc.stage != 'compose_html':\n"
                "        sys.exit(11)\n"
                "if sub_file.exists():\n"
                "    sys.exit(12)\n"
                "# Repair the HTML in the same worker session\n"
                "doc['html'] = '<html><head></head><body data-local-date=\"2026-09-10\"><div><p>수정 완료</p></div></body></html>'\n"
                "envelope = submit_result(doc, 'compose', sub_dir)\n"
                "print(envelope)\n"
            )
            result = subprocess.run([sys.executable, "-I", "-c", code, str(helper), str(submission_dir)],
                                    input=json.dumps(bad_doc, ensure_ascii=False).encode("utf-8"),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=root)
            self.assertEqual(result.returncode, 0, f"Subprocess failed:\nstdout: {result.stdout.decode()}\nstderr: {result.stderr.decode()}")
            envelope = json.loads(result.stdout)
            self.assertEqual(envelope["transport_version"], 2)
            self.assertEqual(envelope["response_file"], "submission.json")
            sub_file = submission_dir / "submission.json"
            self.assertTrue(sub_file.exists())
            raw_bytes = sub_file.read_bytes()
            self.assertEqual(hashlib.sha256(raw_bytes).hexdigest(), envelope["sha256"])
            self.assertEqual(len(raw_bytes), envelope["size_bytes"])
            saved_doc = json.loads(raw_bytes.decode("utf-8"))
            self.assertEqual(saved_doc["html"], '<html><head></head><body data-local-date="2026-09-10"><div><p>수정 완료</p></div></body></html>')

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
