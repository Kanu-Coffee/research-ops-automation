"""Production adapter regression checks with bounded fake CLI responses."""

import json
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops.runners.antigravity import AntigravityRunner
from researchops.runners.base import RunnerInvocationContext
from researchops.runners.codex import CodexRunner
from researchops.runners.development_process import ProcessResult
from researchops.runners.production import _prompt, build_production_command


def response(provider, document, tools=(), *, denied=False, permission="always-proceed"):
    envelope = {"response_json": json.dumps(document)}
    if provider == "codex_exec":
        events = [{"type": "thread.started", "thread_id": "unit"}, {"type": "turn.started"}]
        events += [{"type": "item.completed", "item": item} for item in tools]
        events += [{"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": json.dumps(envelope)}},
                   {"type": "turn.completed", "usage": {"output_tokens": 42}}]
    else:
        events = [{"event": "init", "init": {"permission_mode": permission}},
                  {"event": "result", "result": {"status": "SUCCESS", "structured_output": envelope,
                   "usage": {"output_tokens": 42}, "denied_actions": [{"action": "command"}] if denied else []}}]
    return ProcessResult(0, ("\n".join(json.dumps(item) for item in events) + "\n").encode(), b"", True, True)


class ProductionRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.environment = patch.dict(os.environ, {"HOME": str(self.home), "CODEX_HOME": str(self.home / ".codex")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.input, self.tmp, self.output, self.project = [self.root / name for name in ("input", "tmp", "output", "project")]
        for path in (self.input, self.tmp, self.output, self.project):
            path.mkdir()
        (self.input / "task.md").write_text("Actual operator task instructions.")

    def context(self, stage="research", network="public-research"):
        return RunnerInvocationContext("operator-task", "actual-run", 1, stage,
            timeout_seconds=7200, network_profile=network, local_date="2026-09-07")

    def execute(self, runner, context=None):
        context = context or self.context()
        return getattr(runner, "execute_" + context.invocation_stage)(self.input, self.tmp, self.output, self.project, context)

    def test_codex_executes_native_tools_and_imports_real_task_without_sample_gate(self):
        result_doc = {"status": "success", "records": [], "artifacts": []}
        tool = {"id": "web", "type": "web_search", "status": "completed", "action": {"type": "search", "query": "official source"}}
        with patch("shutil.which", return_value="/usr/bin/codex"), patch("researchops.runners.production.run_bounded", return_value=response("codex_exec", result_doc, [tool])) as run:
            result = self.execute(CodexRunner())
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(json.loads((self.output / "result.json").read_text()), result_doc)
        self.assertTrue(result.cleanup_verified)
        observed = result.events[0]["tools"]
        self.assertEqual(len(observed), 1)
        self.assertEqual((observed[0]["name"], observed[0]["success"]), ("web_search", True))
        self.assertIsNone(observed[0]["observed_started_at"])
        self.assertIsNone(observed[0]["duration_ms"])
        self.assertRegex(observed[0]["observed_finished_at"], r"^\d{4}-\d{2}-\d{2}T.*Z$")
        self.assertEqual(result.isolation["model_invocations"], 1)
        self.assertFalse(result.isolation["hostile_process_isolation"])
        argv = run.call_args.args[0]
        self.assertIn('web_search="live"', argv)
        self.assertIn('approval_policy="on-request"', argv)
        self.assertIn('approvals_reviewer="auto_review"', argv)
        self.assertFalse(any(value.startswith("auto_review.policy=") for value in argv))
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(run.call_args.kwargs["timeout_seconds"], 7200)
        self.assertIn(b"Actual operator task instructions", run.call_args.kwargs["stdin"])

    def test_agy_finish_control_pair_imports_research_and_compose(self):
        from tests.test_runner_tool_events import agy_step
        (self.input / "composition-input.json").write_text('{}')
        compose = {"composition_result": {"html_path": "email.html", "text_path": "email.txt", "subject": "test", "recipient_group_id": "team"}, "html": "<p>test</p>", "text": "test"}
        for stage, document in (('research', {'status':'success','records':[]}), ('compose', compose)):
            process=response('antigravity_exec',document)
            events=[json.loads(line) for line in process.stdout.splitlines()]
            events[1:1]=[agy_step(112,'finish',state='ACTIVE'),
                {'event':'step_update','step_update':{'step_type':'finish','state':'DONE','step_index':112}}]
            process=replace(process, stdout=('\n'.join(json.dumps(e) for e in events)+'\n').encode())
            with patch('shutil.which',return_value='/usr/bin/agy'),patch('researchops.runners.production.run_bounded',return_value=process):
                result=self.execute(AntigravityRunner(),self.context(stage))
            self.assertTrue(result.success,result.error_message)
            self.assertTrue(result.cleanup_verified)
            self.assertTrue(result.isolation['remote_turn_completed'])

    def test_streaming_tool_timing_reaches_native_event_and_mcp_summary(self):
        tool = {"id": "mcp", "type": "mcp_tool_call", "server": "synthetic", "tool": "read_data",
                "status": "completed", "result": {"content": [], "isError": False}}
        events = response("codex_exec", {"records": []}, [tool]).stdout.splitlines()
        started = json.dumps({"type": "item.started", "item": {
            **tool, "status": "in_progress"}}).encode()

        def capture(*args, **kwargs):
            consume = kwargs["stdout_consumer"]
            consume(b"\n".join(events[:2]) + b"\n")
            with patch("researchops.runners.production_trace._observe_time",
                       return_value=("2026-09-10T08:00:00.000000Z", 1_000_000_000)):
                consume(started + b"\n")
            with patch("researchops.runners.production_trace._observe_time",
                       return_value=("2026-09-10T08:00:00.123456Z", 1_123_456_789)):
                consume(events[2] + b"\n")
            consume(b"\n".join(events[3:]) + b"\n")
            return ProcessResult(0, b"", b"", True, True)

        with patch("shutil.which", return_value="/usr/bin/codex"), patch(
                "researchops.runners.production.run_bounded", side_effect=capture):
            result = self.execute(CodexRunner())
        self.assertTrue(result.success, result.error_message)
        for observed in (result.events[0]["tools"][0], result.events[0]["mcp_tools"][0],
                         result.isolation["mcp_tools"][0]):
            self.assertEqual(observed["observed_started_at"], "2026-09-10T08:00:00.000000Z")
            self.assertEqual(observed["observed_finished_at"], "2026-09-10T08:00:00.123456Z")
            self.assertEqual(observed["duration_ms"], 123)
            self.assertTrue(observed["success"])
        self.assertTrue(result.cleanup_verified)

    def test_native_codex_mcp_added_in_user_config_is_available_without_task_change(self):
        config = self.home / ".codex"
        config.mkdir()
        (config / "config.toml").write_text('[mcp_servers.future_server]\nurl="https://example.invalid/mcp"\n')
        tool = {"id": "mcp", "type": "mcp_tool_call", "server": "future_server", "tool": "read_data",
                "status": "completed", "arguments": {"private": "not-in-summary"},
                "result": {"content": [{"type": "text", "text": "42"}], "isError": False}}
        with patch("shutil.which", return_value="/usr/bin/codex"), patch("researchops.runners.production.run_bounded", return_value=response("codex_exec", {"records": []}, [tool])) as run:
            result = self.execute(CodexRunner())
        self.assertTrue(result.success, result.error_message)
        argv = run.call_args.args[0]
        for override in ("--ignore-user-config", "mcp_servers={}", "features.apps=false", "features.plugins=false"):
            self.assertNotIn(override, argv)
        self.assertEqual(result.isolation["mcp"]["mode"], "native")
        self.assertEqual(result.isolation["mcp"]["servers"][0]["name"], "future_server")
        self.assertEqual(result.isolation["mcp_tools"][0]["server"], "future_server")
        self.assertEqual(result.isolation["mcp_tools"][0]["tool"], "read_data")
        self.assertNotIn("not-in-summary", json.dumps(result.isolation))
        self.assertNotIn("example.invalid", json.dumps(result.events))

    def test_codex_mcp_auth_environment_never_enters_shell_or_prompt(self):
        config = self.home / ".codex"
        config.mkdir()
        (config / "config.toml").write_text('[mcp_servers.future_server]\nurl="https://example.invalid/mcp"\nbearer_token_env_var="MCP_TEST_BEARER"\n')
        with patch.dict(os.environ, {"MCP_TEST_BEARER": "synthetic-private-marker"}), patch("shutil.which", return_value="/usr/bin/codex"), patch("researchops.runners.production.run_bounded", return_value=response("codex_exec", {"records": []})) as run:
            result = self.execute(CodexRunner())
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(run.call_args.kwargs["env"]["MCP_TEST_BEARER"], "synthetic-private-marker")
        self.assertIn('shell_environment_policy.inherit="none"', run.call_args.args[0])
        self.assertNotIn("synthetic-private-marker", json.dumps(run.call_args.args[0]))
        self.assertNotIn(b"synthetic-private-marker", run.call_args.kwargs["stdin"])
        self.assertNotIn("synthetic-private-marker", json.dumps(result.events + [result.isolation]))

    def test_antigravity_uses_operator_approved_unattended_mode_and_no_synthetic_gate(self):
        with patch("shutil.which", return_value="/usr/bin/agy"), patch("researchops.runners.production.run_bounded", return_value=response("antigravity_exec", {"records": []})) as run:
            result = self.execute(AntigravityRunner())
        self.assertTrue(result.success, result.error_message)
        self.assertIn("--sandbox=false", run.call_args.args[0])
        self.assertIn("--dangerously-skip-permissions", run.call_args.args[0])
        self.assertFalse(result.isolation["requested_terminal_sandbox"])
        self.assertIsNone(result.isolation["effective_terminal_sandbox"])
        self.assertEqual(result.isolation["effective_permission_mode"], "always-proceed")
        self.assertEqual(run.call_args.kwargs["cwd"], self.project)

    def test_declared_mcp_token_is_not_forwarded_to_compose_or_offline_research(self):
        config = self.home / ".codex"
        config.mkdir()
        (config / "config.toml").write_text('[mcp_servers.remote]\nurl="https://example.invalid/mcp"\nbearer_token_env_var="MCP_TEST_BEARER"\n')
        (self.input / "composition-input.json").write_text('{}')
        compose = {"composition_result": {"html_path": "email.html", "text_path": "email.txt", "subject": "test", "recipient_group_id": "team"}, "html": "<p>test</p>", "text": "test"}
        for stage, document in (("research", {"records": []}), ("compose", compose)):
            with self.subTest(stage=stage), patch.dict(os.environ, {"MCP_TEST_BEARER": "synthetic-private-marker"}), patch("shutil.which", return_value="/usr/bin/codex"), patch("researchops.runners.production.run_bounded", return_value=response("codex_exec", document)) as run:
                result = self.execute(CodexRunner(), self.context(stage, "none"))
            self.assertTrue(result.success, result.error_message)
            self.assertNotIn("MCP_TEST_BEARER", run.call_args.kwargs["env"])
            self.assertIn("CODEX_HOME", run.call_args.kwargs["env"])

    def test_compose_preserves_prestaged_attachment_and_exact_body(self):
        (self.input / "composition-input.json").write_text('{}')
        (self.output / "attachment.txt").write_bytes(b"immutable attachment")
        document = {"composition_result": {"html_path": "email.html", "text_path": "email.txt", "subject": "운영 결과", "recipient_group_id": "team"},
                    "html": "<p>완성 본문</p>", "text": "완성 본문\n"}
        with patch("shutil.which", return_value="/usr/bin/codex"), patch("researchops.runners.production.run_bounded", return_value=response("codex_exec", document)) as run:
            result = self.execute(CodexRunner(), self.context("compose"))
        self.assertTrue(result.success, result.error_message)
        self.assertEqual((self.output / "email.html").read_bytes(), document["html"].encode())
        self.assertEqual((self.output / "email.txt").read_bytes(), document["text"].encode())
        self.assertEqual((self.output / "attachment.txt").read_bytes(), b"immutable attachment")
        self.assertIn('web_search="disabled"', run.call_args.args[0])
        for option in ("--ignore-user-config", "mcp_servers={}", "features.apps=false", "features.plugins=false", "features.remote_plugin=false"):
            self.assertIn(option, run.call_args.args[0])

    def test_requested_remote_artifacts_are_deferred_to_application_in_both_engines(self):
        document = {"status": "success", "records": [{"record_id": "p1"}], "artifacts": [{
            "path": "attachments/original.pdf", "role": "attachment", "mime_type": "application/pdf",
            "record_ids": ["p1"], "source": {"kind": "cardrag_pdf", "connection_id": "cardrag",
                "document_id": "doc_" + "1" * 64, "issuer": "synthetic", "product_code": "1",
                "sha256": "2" * 64, "size_bytes": 100}}]}
        for provider, runner in (("codex_exec", CodexRunner()), ("antigravity_exec", AntigravityRunner())):
            for path in self.output.rglob("*"):
                if path.is_file():
                    path.unlink()
            context = self.context()
            context.artifact_connection_ids = ["cardrag"]
            with self.subTest(provider=provider), patch("shutil.which", return_value="/usr/bin/model"), \
                    patch("researchops.runners.production.run_bounded", return_value=response(provider, document)):
                result = self.execute(runner, context)
            self.assertTrue(result.success, result.error_message)
            self.assertEqual(json.loads((self.output / "result.json").read_bytes()), document)
            self.assertFalse((self.output / "attachments/original.pdf").exists())

    def test_media_prompt_exposes_only_connection_ids_and_preserves_network_policy(self):
        context = self.context()
        context.artifact_connection_ids = ["cardrag"]
        prompt = _prompt(self.input, self.project, self.tmp, context)
        self.assertIn('"artifact_connection_ids": ["cardrag"]', prompt)
        for forbidden in ("bearer_token_file", "/etc/cardrag/secrets", "Authorization: Bearer"):
            self.assertNotIn(forbidden, prompt)
        self.assertIn("on_failure", prompt)
        self.assertIn("announce_missing", prompt)
        control = self.root / "media-control"
        control.mkdir()
        argv, _ = build_production_command("codex_exec", "/usr/bin/codex", prompt, self.project, self.tmp, control, context)
        self.assertIn("sandbox_workspace_write.network_access=false", argv)

    def test_research_prompt_distinguishes_run_evidence_from_email_media(self):
        for network_profile in ("public-research", "none"):
            context = self.context()
            context.network_profile = network_profile
            prompt = _prompt(self.input, self.project, self.tmp, context)
            with self.subTest(network_profile=network_profile):
                for contract in ("scope=run and role=evidence", "source=null and record_ids=[]",
                                 "status=no_updates and records=[]", "not email attachments",
                                 "Run scope does not permit inline_image, remote source, or product record associations"):
                    self.assertIn(contract, prompt)

    def test_research_allows_exact_agy_generated_evidence_without_config_access(self):
        prompt = _prompt(self.input, self.project, self.tmp, self.context())
        self.assertIn("generated tool schema", prompt)
        self.assertIn("this conversation", prompt)
        self.assertIn("Do not read MCP configuration", prompt)

    def test_agy_search_of_current_generated_web_file_imports_but_directory_scan_does_not(self):
        from tests.test_runner_tool_events import agy_step
        conversation = "11111111-1111-4111-8111-111111111111"
        page = self.home / ".gemini/antigravity-cli/brain" / conversation / ".system_generated/steps/88/content.md"
        page.parent.mkdir(parents=True)
        page.write_text("Synthetic official product page: example-code")
        document = {"status": "success", "records": [], "artifacts": []}
        for target, expected in ((page.parent, False), (page, True)):
            with self.subTest(exact_file=expected):
                web, search = agy_step(88, "read_url_content"), agy_step(92, "grep_search")
                web["step_update"]["tool_info"].update(parameters={"Url": "https://example.test/product"}, output="Synthetic page")
                search["step_update"]["tool_info"].update(parameters={"SearchPath": str(target), "Query": "example-code"}, output="Matching excerpt")
                process = response("antigravity_exec", document)
                events = [json.loads(line) for line in process.stdout.splitlines()]
                events[0]["conversation_id"] = conversation
                events[-1]["result"]["conversation_id"] = conversation
                events[1:1] = [web, search]
                process = replace(process, stdout=("\n".join(json.dumps(item) for item in events)+"\n").encode())
                with patch("shutil.which", return_value="/usr/bin/agy"), patch(
                        "researchops.runners.production.run_bounded", return_value=process):
                    result = self.execute(AntigravityRunner())
                self.assertEqual(result.success, expected, result.error_message)
                self.assertTrue(result.cleanup_verified)
                if expected:
                    self.assertEqual(json.loads((self.output / "result.json").read_text()), document)
                else:
                    self.assertFalse((self.output / "result.json").exists())

    def test_custom_instruction_files_do_not_remove_application_mail_contract(self):
        research = "이 주제를 조사하세요.\n"
        email_spec = "세 줄 요약과 상세 표를 한국어로 작성하세요.\n"
        (self.input / "task.md").write_text(research)
        (self.input / "email_spec.md").write_text(email_spec)
        (self.input / "composition-input.json").write_text('{}')
        prompt = _prompt(self.input, self.project, self.tmp, self.context("compose"))
        for marker in ('data-local-date="YYYY-MM-DD"', "data-record-id", "included_record_ids",
                       "run.local_date_display", "no scripts", "without repairing them"):
            self.assertIn(marker, prompt)
        self.assertEqual((self.input / "task.md").read_text(), research)
        self.assertEqual((self.input / "email_spec.md").read_text(), email_spec)
        # Mail-specific rules never require the research phase to compose or send.
        research_prompt = _prompt(self.input, self.project, self.tmp, self.context())
        self.assertNotIn('data-local-date="YYYY-MM-DD"', research_prompt)

    def test_artifact_bytes_are_imported_from_phase_workspace(self):
        def execute(argv, **kwargs):
            work = Path(argv[argv.index("--add-dir") + 1])
            (work / "chart.svg").write_bytes(b"<svg/>")
            return response("codex_exec", {"records": [], "artifacts": [{"path": "chart.svg"}]})
        with patch("shutil.which", return_value="/usr/bin/codex"), patch("researchops.runners.production.run_bounded", side_effect=execute):
            result = self.execute(CodexRunner())
        self.assertTrue(result.success, result.error_message)
        self.assertEqual((self.output / "chart.svg").read_bytes(), b"<svg/>")

    def test_unsafe_artifact_paths_and_links_do_not_import_result(self):
        for name in ("../secret", "/secret", "result.json", "link.txt"):
            with self.subTest(name=name):
                def execute(argv, **kwargs):
                    work = Path(argv[argv.index("--add-dir") + 1])
                    if name == "link.txt":
                        (work / name).symlink_to(self.input / "task.md")
                    return response("codex_exec", {"records": [], "artifacts": [{"path": name}]})
                with patch("shutil.which", return_value="/usr/bin/codex"), patch("researchops.runners.production.run_bounded", side_effect=execute):
                    result = self.execute(CodexRunner())
                self.assertFalse(result.success)
                self.assertFalse((self.output / "result.json").exists())

    def test_timeout_cancel_cleanup_failure_and_missing_terminal_never_import(self):
        processes = [ProcessResult(137, b"", b"", True, True, "timeout", timed_out=True),
                     ProcessResult(137, b"", b"", True, True, "cancelled", cancelled=True),
                     ProcessResult(0, b"", b"", True, False), ProcessResult(0, b"{}\n", b"", True, True)]
        for process, code in zip(processes, (124, 130, 1, 1)):
            with self.subTest(code=code), patch("shutil.which", return_value="/usr/bin/codex"), patch("researchops.runners.production.run_bounded", return_value=process):
                result = self.execute(CodexRunner())
            self.assertFalse(result.success)
            self.assertEqual(result.exit_code, code)
            self.assertEqual(result.cleanup_verified, process.cleanup_verified)
            self.assertEqual(list(self.output.iterdir()), [])

    def test_denial_or_unapplied_agy_permission_mode_fails(self):
        for process in (response("antigravity_exec", {}, denied=True), response("antigravity_exec", {}, permission="request-review")):
            with patch("shutil.which", return_value="/usr/bin/agy"), patch("researchops.runners.production.run_bounded", return_value=process):
                result = self.execute(AntigravityRunner())
            self.assertFalse(result.success)
            self.assertEqual(list(self.output.iterdir()), [])

    def test_agy_timeout_keeps_workspace_held_without_remote_terminal_evidence(self):
        timed_out = ProcessResult(137, b"", b"", True, True, "timeout", timed_out=True)
        with patch("shutil.which", return_value="/usr/bin/agy"), patch("researchops.runners.production.run_bounded", return_value=timed_out):
            result = self.execute(AntigravityRunner())
        self.assertFalse(result.success)
        self.assertFalse(result.cleanup_verified)
        self.assertTrue(result.isolation["remote_turn_completion_unverified"])

    def test_agy_stream_interruption_is_failed_but_verified_cleanup_not_locked(self):
        events = [json.loads(line) for line in response("antigravity_exec", {"records": []}).stdout.splitlines()]
        events.insert(1, {"event": "step_update", "step_update": {
            "step_type": "error_message", "step_index": 2, "state": "DONE"}})
        events[-1]["result"].update(status="ERROR", error="The stream was interrupted. Please continue the task you were working on.")
        raw = ("\n".join(json.dumps(event) for event in events) + "\n").encode()
        # Exit zero was observed in the incident; a nonzero exit with the
        # same complete terminal evidence must not invent a remote leak either.
        for code, process_error in ((0, None), (7, None), (137, "timeout")):
            process = ProcessResult(code, raw, b"", True, True, process_error)
            with self.subTest(code=code), patch("shutil.which", return_value="/usr/bin/agy"), patch("researchops.runners.production.run_bounded", return_value=process):
                result = self.execute(AntigravityRunner())
            self.assertFalse(result.success)
            self.assertTrue(result.cleanup_verified)
            self.assertTrue(result.isolation["control_process_cleanup_verified"])
            self.assertTrue(result.isolation["remote_turn_completed"])
            self.assertEqual(result.isolation["provider_error_code"], "stream_interrupted")
            self.assertEqual(result.isolation["model_invocations"], 1)
            self.assertNotIn("remote_turn_completion_unverified", result.isolation)
            self.assertEqual(list(self.output.iterdir()), [])
            if code == 0:
                self.assertIn("stream interrupted", result.error_message)

    def test_agy_terminal_result_does_not_override_failed_local_cleanup(self):
        valid = response("antigravity_exec", {"records": []})
        failed = ProcessResult(0, valid.stdout, b"", True, False, "process_group_cleanup_unverified")
        with patch("shutil.which", return_value="/usr/bin/agy"), patch("researchops.runners.production.run_bounded", return_value=failed):
            result = self.execute(AntigravityRunner())
        self.assertFalse(result.success)
        self.assertFalse(result.cleanup_verified)
        self.assertFalse(result.isolation["control_process_cleanup_verified"])
        self.assertTrue(result.isolation["remote_turn_completed"])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_agy_unknown_provider_failure_does_not_expose_raw_error(self):
        events = [json.loads(line) for line in response("antigravity_exec", {}).stdout.splitlines()]
        events[-1]["result"].update(status="ERROR", error={"message": "private provider diagnostic"})
        process = ProcessResult(0, ("\n".join(json.dumps(event) for event in events) + "\n").encode(), b"", True, True)
        with patch("shutil.which", return_value="/usr/bin/agy"), patch("researchops.runners.production.run_bounded", return_value=process):
            result = self.execute(AntigravityRunner())
        self.assertFalse(result.success)
        self.assertTrue(result.cleanup_verified)
        self.assertEqual(result.error_message, "Antigravity provider returned ERROR")
        self.assertNotIn("private provider diagnostic", json.dumps(result.events + [result.isolation]))

    def test_none_profile_disables_codex_web_and_omits_sensitive_environment(self):
        with patch.dict(os.environ, {"SMTP_PASSWORD": "must-not-forward", "OPENAI_API_KEY": "must-not-forward", "SSH_AUTH_SOCK": "must-not-forward"}), patch("shutil.which", return_value="/usr/bin/codex"), patch("researchops.runners.production.run_bounded", return_value=response("codex_exec", {})) as run:
            result = self.execute(CodexRunner(), self.context(network="none"))
        self.assertTrue(result.success, result.error_message)
        self.assertIn('web_search="disabled"', run.call_args.args[0])
        self.assertNotIn("must-not-forward", json.dumps(run.call_args.kwargs["env"]))
        self.assertNotIn(b"must-not-forward", run.call_args.kwargs["stdin"])

    def test_large_agy_input_uses_task_prompt_file_instead_of_oversized_argv(self):
        control = self.tmp / "control"
        control.mkdir()
        argv, stdin = build_production_command("antigravity_exec", "/bin/agy", "x" * 100_000,
                                               self.project, self.tmp, control, self.context())
        self.assertIsNone(stdin)
        self.assertLess(len(argv[-1]), 1000)
        self.assertEqual((control / "invocation-prompt.txt").read_text(), "x" * 100_000)

    def test_observed_web_action_with_none_profile_is_not_success(self):
        tool = {"id": "web", "type": "web_search", "status": "completed"}
        with patch("shutil.which", return_value="/usr/bin/codex"), patch("researchops.runners.production.run_bounded", return_value=response("codex_exec", {}, [tool])):
            result = self.execute(CodexRunner(), self.context(network="none"))
        self.assertFalse(result.success)
        self.assertIn("Observed network tool", result.error_message)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_symlink_input_and_invalid_profile_are_rejected_before_spawn(self):
        (self.input / "secret.txt").symlink_to(self.root / "missing-secret")
        with patch("shutil.which", return_value="/usr/bin/codex"), patch("researchops.runners.production.run_bounded") as run:
            result = self.execute(CodexRunner())
        self.assertFalse(result.success)
        run.assert_not_called()

    def test_controller_does_not_overwrite_existing_mail_file(self):
        (self.input / "composition-input.json").write_text('{}')
        (self.output / "email.html").write_text("existing")
        document = {"composition_result": {"html_path": "email.html", "text_path": "email.txt"}, "html": "replacement", "text": "replacement"}
        with patch("shutil.which", return_value="/usr/bin/codex"), patch("researchops.runners.production.run_bounded", return_value=response("codex_exec", document)):
            result = self.execute(CodexRunner(), self.context("compose"))
        self.assertFalse(result.success)
        self.assertEqual((self.output / "email.html").read_text(), "existing")


class ProductionCleanupLifecycleTests(unittest.TestCase):
    def test_complete_provider_error_releases_claim_but_missing_terminal_keeps_lock(self):
        from researchops.delivery.smtp_config import BuiltinDeliveryConfig, save_delivery_config
        from researchops.services.application import ApplicationService
        from tests.support import isolated_settings

        for complete in (True, False):
            with self.subTest(complete=complete), tempfile.TemporaryDirectory() as directory:
                settings = isolated_settings(Path(directory))
                settings.environment = "production"
                app = ApplicationService(settings)
                save_delivery_config(BuiltinDeliveryConfig(recipient_groups={
                    "team": ["recipient@example.test"]}), settings.paths.delivery_config_file)
                app.tasks.create_production_task(task_id="stream-failure", name="Failure fixture",
                    instructions="Do not call any external system.", runner_type="antigravity_exec",
                    recipient_group_id="team")
                events = [json.loads(line) for line in response("antigravity_exec", {"records": []}).stdout.splitlines()]
                events.insert(1, {"event": "step_update", "step_update": {
                    "step_type": "error_message", "step_index": 2, "state": "DONE"}})
                events[-1]["result"].update(status="ERROR", error="The stream was interrupted. Please continue the task you were working on.")
                raw = ("\n".join(json.dumps(event) for event in (events if complete else events[:-1])) + "\n").encode()
                process = ProcessResult(0, raw, b"", True, True)
                # Exercise provider cleanup with live delivery explicitly off;
                # a disabled SMTP fixture must not fail the earlier live gate.
                run = app.runs.enqueue_run("stream-failure", force_dry_run=True)
                with patch("shutil.which", return_value="/usr/bin/agy"), patch("researchops.runners.production.run_bounded", return_value=process) as bounded, patch("smtplib.SMTP") as smtp:
                    result = app.runs.execute_run(run.run_id)
                bounded.assert_called_once()
                smtp.assert_not_called()
                self.assertEqual(result.status, "failed" if complete else "needs_attention")
                self.assertEqual(app.workspace_mgr.is_locked("stream-failure")[0], not complete)
                self.assertEqual(app.run_repo.get_lease(run.run_id) is None, complete)
                self.assertEqual(bool(app.run_repo.get_execution_controls(run.run_id)["child_cleanup_verified"]), complete)
                self.assertIsNone(app.delivery_repo.get_handoff_for_run(run.run_id))
                if complete:
                    self.assertIn("stream interrupted", result.error_message)
                    archive = settings.paths.run_archive_dir / "stream-failure" / run.run_id
                    self.assertEqual((archive / "logs/research.stdout").read_bytes(), raw)
