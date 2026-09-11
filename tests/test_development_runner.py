"""Opt-in live-runner boundary tests using only mocked CLI responses."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops.runners.base import RunnerInvocationContext
from researchops.runners.development_process import ProcessResult
from researchops.runners.development_runner import (
    MAX_RESPONSE_BYTES, OutputOnlyDevelopmentRunner, codex_response,
    control_environment, import_response,
)


def jsonl(events):
    return b"\n".join(json.dumps(item, ensure_ascii=False).encode("utf-8") for item in events) + b"\n"


def envelope(document):
    return {"response_json": json.dumps(document, ensure_ascii=False)}


def codex_events(document):
    return [{"type": "thread.started", "thread_id": "synthetic-thread"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(envelope(document))}},
            {"type": "turn.completed", "usage": {"input_tokens": 8, "output_tokens": 4}}]


def agy_events(document):
    return [{"event": "init", "init": {"permission_mode": "request-review"}},
            {"event": "step_update", "step_update": {"step_type": "agent_response", "state": "DONE"}},
            {"event": "step_update", "step_update": {"step_type": "finish", "state": "DONE"}},
            {"event": "result", "result": {"status": "SUCCESS", "structured_output": envelope(document),
             "response": "Do not import this human response", "usage": {"input_tokens": 8, "output_tokens": 4}}}]


class DevelopmentRunnerTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="researchops-dev-runner-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def runner(self, provider="antigravity_exec", **kwargs):
        with patch("researchops.runners.development_runner.shutil.which", return_value="/mock/bin/cli"):
            return OutputOnlyDevelopmentRunner(provider, "document-extract", approval=True,
                evidence_dir=self.root / "evidence", **kwargs)

    def stage(self, runner, stage="research"):
        folder = self.root / stage
        input_dir, output_dir = folder / "input", folder / "output"
        input_dir.mkdir(parents=True)
        output_dir.mkdir()
        names = set(runner.task.instructions[stage + "_files"])
        names.update(runner.task.output[key] for key in ("research_schema", "composition_schema", "composition_record_schema"))
        for name in names:
            path = input_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(runner.package_files[name], encoding="utf-8")
        context = RunnerInvocationContext(runner.task.id, "synthetic-run", 1, stage,
            network_profile="none", timezone="Asia/Seoul", local_date="2026-09-06",
            local_date_display="2026년 9월 6일", scheduled_for="2026-09-06T00:00:00+00:00")
        if stage == "compose":
            (input_dir / "composition-input.json").write_text(json.dumps({"task_id": context.task_id,
                "run_id": context.run_id, "local_date": context.local_date, "records": []}), encoding="utf-8")
        return input_dir, output_dir, context

    def execute(self, runner, files, process):
        input_dir, output_dir, context = files
        with patch("researchops.runners.development_runner.run_bounded", return_value=process) as run:
            result = getattr(runner, "execute_" + context.invocation_stage)(
                input_dir, self.root / "tmp", output_dir, self.root / "project", context)
        return result, run


class TestDevelopmentRunnerGrant(DevelopmentRunnerTestCase):
    def test_approval_supported_provider_and_bundled_sample_are_mandatory(self):
        with patch("researchops.runners.development_runner.TaskPackageLoader.load_from_dir") as load:
            for approval in (False, None, 1, "true"):
                with self.subTest(approval=approval), self.assertRaises(ValueError):
                    OutputOnlyDevelopmentRunner("codex_exec", "document-extract", approval=approval,
                                                evidence_dir=self.root / "evidence")
            for provider, sample in (("fake", "document-extract"), ("codex_exec", "software-releases"),
                                     ("antigravity_exec", "../../tasks/private")):
                with self.subTest(provider=provider, sample=sample), self.assertRaises(ValueError):
                    OutputOnlyDevelopmentRunner(provider, sample, approval=True, evidence_dir=self.root / "evidence")
            load.assert_not_called()
        self.assertFalse((self.root / "evidence").exists())

    def test_invalid_timeout_or_model_is_rejected_before_loading(self):
        for timeout in (0, 301, True, "120"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.runner(timeout_seconds=timeout)
        for model in ("", "--option", "a\nmodel", 1):
            with self.subTest(model=model), self.assertRaises(ValueError):
                self.runner(model=model)

    def test_staged_bytes_must_equal_the_bundled_sample(self):
        runner = self.runner()
        files = self.stage(runner)
        (files[0] / "source.txt").write_text("tampered synthetic instructions", encoding="utf-8")
        result, run = self.execute(runner, files, None)
        self.assertFalse(result.success)
        self.assertIn("differs from approved", result.error_message)
        self.assertFalse(result.isolation["spawned"])
        run.assert_not_called()
        self.assertEqual(list(files[1].iterdir()), [])

    def test_fixtures_and_extra_input_are_never_included_in_prompt(self):
        runner = self.runner()
        files = self.stage(runner)
        prompt = runner._prompt(files[0], files[2])
        self.assertNotIn("sample-result.json", prompt)
        payload = json.loads(prompt[prompt.index('{"context":'):])
        self.assertFalse(any(name.startswith("fixtures/") for name in payload["files"]))
        self.assertIn("2026-09-06", prompt)
        (files[0] / "private-extra.txt").write_text("fixture-only-secret", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "file set") as error:
            runner._prompt(files[0], files[2])
        self.assertNotIn("fixture-only-secret", str(error.exception))

    def test_extra_symlink_directory_is_not_an_approved_input(self):
        runner = self.runner()
        files = self.stage(runner)
        external = self.root / "not-input"
        external.mkdir()
        (files[0] / "unexpected-link").symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            runner._prompt(files[0], files[2])

    def test_context_task_network_timezone_and_compose_identity_are_checked(self):
        runner = self.runner()
        files = self.stage(runner, "compose")
        context = files[2]
        for attribute, value in (("task_id", "other-task"), ("network_profile", "research"),
                                 ("timezone", "UTC"), ("invocation_stage", "unsupported")):
            original = getattr(context, attribute)
            setattr(context, attribute, value)
            with self.subTest(attribute=attribute), self.assertRaises(ValueError):
                runner._prompt(files[0], context)
            setattr(context, attribute, original)
        (files[0] / "composition-input.json").write_text(json.dumps({"task_id": context.task_id,
            "run_id": "other-run"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "identity"):
            runner._prompt(files[0], context)

    def test_environment_has_no_arbitrary_credentials_or_agent_sockets(self):
        source = {"HOME": "/mock/control-home", "USER": "mock-user", "LOGNAME": "mock-user",
            "CODEX_HOME": "/mock/control-codex", "XDG_RUNTIME_DIR": "/run/mock", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/mock/bus",
            "OPENAI_API_KEY": "do-not-forward", "SMTP_PASSWORD": "do-not-forward", "SSH_AUTH_SOCK": "/private/agent",
            "LD_PRELOAD": "/private/hook.so", "PYTHONPATH": "/private/import", "PATH": "/private/path", "TASK_SECRET": "do-not-forward"}
        with patch.dict(os.environ, source, clear=True), patch.object(Path, "home", return_value=Path("/mock/control-home")), \
             patch.object(Path, "read_text", side_effect=AssertionError("Do not read credentials")), \
             patch.object(Path, "read_bytes", side_effect=AssertionError("Do not read credentials")):
            for provider in ("codex_exec", "antigravity_exec"):
                env = control_environment(provider, "/mock/bin/cli", self.root)
                self.assertEqual(env["TZ"], "Asia/Seoul")
                self.assertEqual(env["HOME"], "/mock/control-home")
                self.assertEqual(env["PATH"], "/mock/bin:/usr/bin:/bin")
                self.assertEqual(env["TMPDIR"], str(self.root))
                self.assertNotIn("do-not-forward", env.values())
                for key in ("SSH_AUTH_SOCK", "LD_PRELOAD", "PYTHONPATH", "TASK_SECRET"):
                    self.assertNotIn(key, env)
                self.assertEqual("CODEX_HOME" in env, provider == "codex_exec")


class TestDevelopmentRunnerResponses(DevelopmentRunnerTestCase):
    def test_actual_agy_envelope_imports_response_json_and_audits_boundary(self):
        runner = self.runner()
        files = self.stage(runner)
        document = {"task_id": runner.task.id, "records": [], "note": "합성 출력"}
        process = ProcessResult(0, jsonl(agy_events(document)), b"", True, True)
        result, run = self.execute(runner, files, process)
        self.assertTrue(result.success, result.error_message)
        self.assertTrue(result.cleanup_verified)
        self.assertEqual(set(result.output_files), {"result.json"})
        self.assertEqual(json.loads(result.output_files["result.json"].read_text()), document)
        argv = run.call_args.args[0]
        self.assertIn("--sandbox", argv)
        self.assertNotIn("--mode", argv)
        self.assertIn("--disable-slash-commands", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIsNone(run.call_args.kwargs["stdin"])
        self.assertFalse(result.isolation["production_ready"])
        self.assertFalse(result.isolation["credential_bridge"])
        self.assertEqual(result.isolation["model_invocations"], 1)
        self.assertEqual(result.isolation["filesystem_quota_scope"], "response-import-only")
        observation = json.loads((Path(result.isolation["evidence_dir"]) / "observation.json").read_text())
        self.assertEqual(len(observation["output_hashes"]["result.json"]), 64)

    def test_mock_codex_success_uses_stdin_readonly_and_disabled_tools(self):
        runner = self.runner("codex_exec", model="mock-model")
        files = self.stage(runner)
        result, run = self.execute(runner, files, ProcessResult(0, jsonl(codex_events({"records": []})), b"", True, True))
        self.assertTrue(result.success, result.error_message)
        argv = run.call_args.args[0]
        self.assertIn("read-only", argv)
        self.assertIn("features.shell_tool=false", argv)
        self.assertIn("features.apps=false", argv)
        self.assertEqual(argv[-1], "-")
        self.assertIsInstance(run.call_args.kwargs["stdin"], bytes)
        self.assertEqual(result.isolation["requested_model"], "mock-model")

    def test_compose_preserves_html_and_text_with_fixed_destination_names(self):
        runner = self.runner()
        files = self.stage(runner, "compose")
        document = {"composition_result": {"html_path": "email.html", "text_path": "email.txt", "subject": "합성"},
                    "html": "<!doctype html><p>서울 &amp; 검증</p>\n", "text": "서울 & 검증\n"}
        result, _ = self.execute(runner, files, ProcessResult(0, jsonl(agy_events(document)), b"", True, True))
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(set(result.output_files), {"composition-result.json", "email.html", "email.txt"})
        self.assertEqual((files[1] / "email.html").read_bytes(), document["html"].encode())
        self.assertEqual((files[1] / "email.txt").read_bytes(), document["text"].encode())

    def test_parse_failure_never_imports_and_agy_remote_cleanup_stays_unknown(self):
        runner = self.runner()
        files = self.stage(runner)
        result, _ = self.execute(runner, files, ProcessResult(0, b"not JSON\n", b"", True, True))
        self.assertFalse(result.success)
        self.assertFalse(result.cleanup_verified)
        self.assertFalse(result.isolation["terminal_response"])
        self.assertEqual(list(files[1].iterdir()), [])

    def test_timeout_and_cancel_codes_preserve_remote_cleanup_uncertainty(self):
        runner = self.runner()
        files = self.stage(runner)
        for process, code in ((ProcessResult(-9, b"", b"", True, True, "timeout", timed_out=True), 124),
                              (ProcessResult(-9, b"", b"", True, True, "cancelled", cancelled=True), 77)):
            with self.subTest(code=code):
                result, _ = self.execute(runner, files, process)
                self.assertFalse(result.success)
                self.assertFalse(result.cleanup_verified)
                self.assertEqual(result.exit_code, code)
                self.assertEqual(list(files[1].iterdir()), [])

    def test_cancel_before_import_and_unverified_cleanup_do_not_write_files(self):
        runner = self.runner()
        files = self.stage(runner)
        files[2].cancellation_check = lambda: True
        result, _ = self.execute(runner, files, ProcessResult(0, jsonl(agy_events({"records": []})), b"", True, True))
        self.assertFalse(result.success)
        self.assertIn("cancelled before output import", result.error_message)
        self.assertEqual(list(files[1].iterdir()), [])
        files[2].cancellation_check = None
        result, _ = self.execute(runner, files, ProcessResult(0, jsonl(agy_events({"records": []})), b"", True, False))
        self.assertFalse(result.success)
        self.assertFalse(result.cleanup_verified)
        self.assertEqual(list(files[1].iterdir()), [])

    def test_codex_action_failure_missing_final_and_duplicate_response_are_rejected(self):
        source = codex_events({"records": []})
        action = {"type": "item.completed", "item": {"type": "command_execution", "command": "private fixture"}}
        for events in (source[:-1], source + [source[-1]], source[:2] + [action] + source[2:],
                       source[:2] + [{"type": "turn.failed"}] + source[2:],
                       source[:2] + [source[2], source[2], source[-1]]):
            with self.subTest(events=events), self.assertRaises(ValueError):
                codex_response(jsonl(events))


class TestDevelopmentFixedImport(DevelopmentRunnerTestCase):
    def test_traversal_unknown_files_and_invalid_values_are_rejected(self):
        output = self.root / "output"
        output.mkdir()
        valid = {"composition_result": {"html_path": "email.html", "text_path": "email.txt"}, "html": "<p>x</p>", "text": "x"}
        invalid = [[], {**valid, "extra": "no"}, {**valid, "html": False},
                   {**valid, "composition_result": {"html_path": "../escape", "text_path": "email.txt"}},
                   {**valid, "composition_result": {"html_path": "email.html", "text_path": "/tmp/escape"}}]
        for document in invalid:
            with self.subTest(document=document), self.assertRaises(ValueError):
                import_response(document, "compose", output)
            self.assertEqual(list(output.iterdir()), [])

    def test_response_byte_quota_is_utf8_and_enforced_before_any_file_write(self):
        output = self.root / "output"
        output.mkdir()
        with self.assertRaisesRegex(ValueError, "submission_size_exceeded"):
            import_response({"large": "한" * (MAX_RESPONSE_BYTES // 3 + 1)}, "research", output)
        self.assertEqual(list(output.iterdir()), [])

    def test_existing_files_symlinks_and_symlink_directory_are_never_overwritten(self):
        output = self.root / "output"
        output.mkdir()
        existing = output / "result.json"
        existing.write_bytes(b"original")
        with self.assertRaisesRegex(ValueError, "empty"):
            import_response({}, "research", output)
        self.assertEqual(existing.read_bytes(), b"original")
        existing.unlink()
        outside = self.root / "outside.json"
        outside.write_bytes(b"external original")
        existing.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "empty"):
            import_response({}, "research", output)
        self.assertEqual(outside.read_bytes(), b"external original")
        directory_link = self.root / "output-link"
        directory_link.symlink_to(output, target_is_directory=True)
        with self.assertRaises(OSError):
            import_response({}, "research", directory_link)


if __name__ == "__main__":
    unittest.main()
