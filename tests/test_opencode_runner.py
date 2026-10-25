"""OpenCode v2 adapter contracts without a live provider or operator runtime."""

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from researchops.runners.base import RunnerExecutionResult, RunnerInvocationContext
from researchops.runners.development_process import ProcessResult
from types import SimpleNamespace

from researchops.engine.execution_plan import build_execution_plan
from researchops.engine.orchestrator import Orchestrator
from researchops.package.production_template import build_production_package
from researchops.runners.opencode import (
    OpenCodeRunner, OpenCodeClient, OpenCodeTraceBudget, _mcp_inventory, _messages, _model_ref, _parse_trace,
    _permission_rules, opencode_control_environment,
)
from researchops.runners.production import _prompt
from researchops.runner_tool_validation import (
    _opencode_case, _opencode_checks, _python_unittest_invoked,
    validate_runner_tools,
)
from researchops.runners.result_submission import SUBMISSION_DIRECTORY


SESSION_ID = "ses_TestSession123"


def process(stdout=b"", exit_code=0, cleanup=True):
    return ProcessResult(exit_code, stdout, b"", True, cleanup)


class FakeAPI:
    def __init__(self, *, model=None, final_text=None, always_running=False,
                 live_shells=None, live_ptys=None, live_terminals=None,
                 shell_list_error=False, pty_list_error=False, terminal_list_error=False,
                 tool_catalog_error=False,
                 control_cleanup=True, cancel_after_environment=False,
                 prompt_error=False, service_unavailable=False, final_finish="stop",
                 final_completed=3, final_tool=None, document=None):
        self.model = model or {"id": "model-one", "providerID": "provider-one", "variant": "high"}
        self.final_text = final_text
        self.always_running = always_running
        self.work = None
        self.envelope = None
        self.assistant_message = None
        self.get_calls = 0
        self.interrupted = False
        self.argv_seen = []
        self.create_body = None
        self.environment_body = None
        self.prompt_body = None
        self.live_shells = list(live_shells or [])
        self.live_ptys = list(live_ptys or [])
        self.live_terminals = list(live_terminals or [])
        self.shell_list_error = shell_list_error
        self.pty_list_error = pty_list_error
        self.terminal_list_error = terminal_list_error
        self.tool_catalog_error = tool_catalog_error
        self.control_cleanup = control_cleanup
        self.cancel_after_environment = cancel_after_environment
        self.prompt_error = prompt_error
        self.service_unavailable = service_unavailable
        self.final_finish = final_finish
        self.final_completed = final_completed
        self.final_tool = final_tool
        self.document = document or {"status": "success", "summary": "synthetic", "records": [], "artifacts": []}

    def __call__(self, argv, **kwargs):
        self.argv_seen.append(list(argv))
        operation = argv[-1]
        if "--version" in argv:
            return process(b"opencode v2.1.1\n", cleanup=self.control_cleanup)
        if "service" in argv and "status" in argv:
            return process(b"" if self.service_unavailable else b"http://127.0.0.1:4096\n",
                           exit_code=1 if self.service_unavailable else 0,
                           cleanup=self.control_cleanup)
        if "mcp" in argv and "list" in argv:
            return process("✓ cardrag     connected\n".encode(),
                           cleanup=self.control_cleanup)
        if operation == "model.list":
            return process(json.dumps({"location": {"directory": "/tmp"},
                                       "data": [{"id": "model-one", "modelID": "model-one",
                                                 "providerID": "provider-one", "name": "Model One",
                                                 "enabled": True, "variants": [{"id": "high"}]}]}).encode())
        if operation == "mcp.list":
            return process(json.dumps({"location": {"directory": "/tmp"},
                                       "data": [{"name": "cardrag",
                                                 "status": {"status": "connected"}}]}).encode())
        if operation == "mcp.resource.catalog":
            return process(json.dumps({"location": {"directory": "/tmp"},
                                       "data": {"resources": [], "templates": []}}).encode())
        if operation == "mcp.tool.catalog":
            if self.tool_catalog_error:
                return process(b"Operation not found: mcp.tool.catalog\n", exit_code=1,
                               cleanup=self.control_cleanup)
            return process(json.dumps({"location": {"directory": "/tmp"},
                                       "data": {"tools": [
                                           {"server": "cardrag", "name": "calculate", "capability": "query"},
                                           {"server": "cardrag", "name": "get_and_cancel", "capability": "mutation"},
                                           {"server": "cardrag", "name": "search", "capability": "query"},
                                       ]}}).encode())
        if operation == "shell.list":
            if self.shell_list_error:
                return process(b"", exit_code=1, cleanup=self.control_cleanup)
            location = self.work or Path("/tmp")
            return process(json.dumps({"location": {"directory": str(location)},
                                       "data": self.live_shells}).encode())
        if operation == "pty.list":
            if self.pty_list_error:
                return process(b"", exit_code=1, cleanup=self.control_cleanup)
            location = self.work or Path("/tmp")
            return process(json.dumps({"location": {"directory": str(location)},
                                       "data": self.live_ptys}).encode())
        if operation == "server.experimental.persistentPty.list":
            if self.terminal_list_error:
                return process(b"", exit_code=1, cleanup=self.control_cleanup)
            return process(json.dumps({"data": self.live_terminals}).encode())
        if operation == "session.create":
            body = json.loads(argv[argv.index("--data") + 1])
            self.create_body = body
            self.work = Path(body["location"]["directory"])
            return process(json.dumps({"data": {
                "id": SESSION_ID, "location": {"directory": str(self.work)},
                "model": self.model, "agent": "build", "tokens": {"input": 1, "output": 2,
                "reasoning": 0, "cache": {"read": 0, "write": 0}}}}).encode(),
                cleanup=self.control_cleanup)
        if operation == "session.environment":
            self.environment_body = json.loads(argv[argv.index("--data") + 1])
            return process(cleanup=self.control_cleanup)
        if operation == "session.prompt":
            if self.prompt_error:
                return process(b"", exit_code=1, cleanup=self.control_cleanup)
            self.prompt_body = json.loads(argv[argv.index("--data") + 1])
            document = self.document
            raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
            submission = self.work / SUBMISSION_DIRECTORY / "submission.json"
            submission.write_bytes(raw)
            self.envelope = json.dumps({"transport_version": 2, "response_file": "submission.json",
                                        "sha256": __import__("hashlib").sha256(raw).hexdigest(),
                                        "size_bytes": len(raw)})
            text = self.final_text if self.final_text is not None else self.envelope
            content = [{"type": "text", "text": text}]
            if self.final_tool is not None:
                content.insert(0, self.final_tool)
            message = {"id": "msg_assistant_1", "type": "assistant",
                       "agent": "build", "model": self.model,
                       "time": {"created": 1, "streamed": 2},
                       "content": content}
            if self.final_finish is not None:
                message["finish"] = self.final_finish
            if self.final_completed is not None:
                message["time"]["completed"] = self.final_completed
            self.assistant_message = message
            return process(json.dumps({"data": {"id": "msg_user_1", "sessionID": SESSION_ID,
                                                "type": "user"}}).encode(),
                           cleanup=self.control_cleanup)
        if operation == "session.get":
            self.get_calls += 1
            if self.always_running and not self.interrupted:
                return process(json.dumps({"data": {"id": SESSION_ID, "time": {"created": 1},
                                                    "location": {"directory": str(self.work)},
                                                    "model": self.model, "agent": "build"}}).encode(),
                cleanup=self.control_cleanup)
            outcome = "interrupted" if self.interrupted else "succeeded"
            return process(json.dumps({"data": {"id": SESSION_ID,
                "time": {"created": 1, "idle": 2}, "outcome": outcome,
                "location": {"directory": str(self.work)}, "model": self.model,
                "agent": "build",
                "tokens": {"input": 3, "output": 4, "reasoning": 0,
                           "cache": {"read": 0, "write": 0}}}}).encode(),
                cleanup=self.control_cleanup)
        if operation == "session.message.list":
            message_type = next((value.split("=", 1)[1] for value in argv
                                 if value.startswith("type=")), None)
            if message_type == "assistant":
                message = self.assistant_message or {
                    "id": "msg_assistant_1", "type": "assistant", "finish": "stop",
                    "agent": "build", "model": self.model,
                    "time": {"created": 1, "completed": 3}, "content": []}
                return process(json.dumps({"data": [message],
                                           "cursor": {"next": None}}).encode(),
                               cleanup=self.control_cleanup)
            return process(json.dumps({"data": [], "cursor": {"next": None}}).encode(),
                               cleanup=self.control_cleanup)
        if operation == "session.interrupt":
            self.interrupted = True
            return process(json.dumps({"interrupted": True}).encode(),
                           cleanup=self.control_cleanup)
        return process(b"{}")


class OpenCodeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input = self.root / "input"
        self.tmp = self.root / "tmp"
        self.output = self.root / "output"
        self.project = self.root / "project"
        self.logs = self.root / "logs"
        for directory in (self.input, self.tmp, self.output, self.project, self.logs):
            directory.mkdir()
        (self.input / "task.md").write_text("# Synthetic task\nReturn one record.\n", encoding="utf-8")

    def context(self, **overrides):
        values = {"task_id": "sample-task", "run_id": "run-1", "attempt": 1,
                  "invocation_stage": "research", "model": "provider-one/model-one",
                  "reasoning_effort": "high", "timeout_seconds": 30, "network_profile": "none",
                  "trace_log_dir": self.logs}
        values.update(overrides)
        return RunnerInvocationContext(**values)

    def run_with(self, api, context=None, stage="research"):
        with patch("researchops.runners.opencode.shutil.which", return_value="/usr/bin/opencode"), \
                patch("researchops.runners.opencode.run_bounded", side_effect=api):
            method = (OpenCodeRunner().execute_research if stage == "research"
                      else OpenCodeRunner().execute_compose)
            result = method(self.input, self.tmp, self.output, self.project,
                            context or self.context(invocation_stage=stage))
        return result

    def test_success_records_owned_session_clean_environment_and_strict_import(self):
        api = FakeAPI()
        result = self.run_with(api)
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.cleanup_verified)
        self.assertEqual((self.output / "result.json").read_text(), '{"status": "success", "summary": "synthetic", "records": [], "artifacts": []}')
        record = json.loads((self.logs / "opencode-session-research.json").read_text())
        self.assertEqual(record["session_id"], SESSION_ID)
        self.assertEqual(record["status"], "prompt_accepted")
        self.assertEqual(record["run_id"], "run-1")
        self.assertTrue((self.logs / "opencode-control-research.jsonl").is_file())
        # Worker environment is task-local and contains no operator secrets.
        variables = api.environment_body["variables"]
        self.assertTrue(variables["HOME"].startswith(str(self.tmp)))
        self.assertNotIn("USER", variables)
        self.assertNotIn("OPENAI_API_KEY", variables)
        # Session permissions deny delegation/Code Mode and the model was pinned.
        rules = api.create_body["permissions"]
        self.assertIn({"action": "subagent", "resource": "*", "effect": "deny"}, rules)
        self.assertIn({"action": "execute", "resource": "*", "effect": "allow"}, rules)
        self.assertEqual(api.create_body["model"],
                         {"providerID": "provider-one", "id": "model-one", "variant": "high"})
        self.assertIn("invocation-instructions.txt", api.prompt_body["text"])
        self.assertNotIn("Synthetic task", api.prompt_body["text"])
        # MCP status is queried through the structured API in every phase.
        self.assertIn("mcp.list", [argv[-1] for argv in api.argv_seen])

    def test_model_identity_mismatch_interrupts_and_never_imports(self):
        api = FakeAPI(model={"id": "other-model", "providerID": "provider-one", "variant": "high"})
        result = self.run_with(api)
        self.assertFalse(result.success)
        self.assertIn("MODEL_MISMATCH", result.error_message)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertTrue(result.cleanup_verified)

    def test_prose_final_response_is_rejected_without_import(self):
        api = FakeAPI(final_text="Here is the result in prose.")
        result = self.run_with(api)
        self.assertFalse(result.success)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(result.response_diagnostic["code"], "outer_json_invalid")
        self.assertTrue(result.cleanup_verified)

    def test_timeout_interrupts_only_the_owned_session(self):
        api = FakeAPI(always_running=True)
        result = self.run_with(api, self.context(timeout_seconds=2))
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 124)
        self.assertEqual(list(self.output.iterdir()), [])
        interrupt_calls = [argv for argv in api.argv_seen if argv[-1] == "session.interrupt"]
        self.assertEqual(len(interrupt_calls), 1)
        self.assertTrue(any(f"sessionID={SESSION_ID}" in value for value in interrupt_calls[0]))
        self.assertFalse(any("service" in argv and "stop" in argv for argv in api.argv_seen))
        self.assertTrue(result.isolation["partial_diagnostics"]["partial"])

    def test_missing_binary_is_a_blocked_preflight_without_spawn(self):
        with patch("researchops.runners.opencode.shutil.which", return_value=None), \
                patch("researchops.runners.opencode.run_bounded", side_effect=AssertionError("spawned")):
            result = OpenCodeRunner().execute_research(
                self.input, self.tmp, self.output, self.project, self.context())
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 78)
        self.assertTrue(result.cleanup_verified)
        self.assertIn("RUNNER-BINARY-UNAVAILABLE", result.error_message)

    def test_permission_rules_are_phase_scoped(self):
        none = _permission_rules(stage="compose", network_profile="none", work=self.root,
            project_dir=self.project, input_dir=self.input, control_dir=self.tmp, mcp_servers=["cardrag"])
        public = _permission_rules(stage="research", network_profile="public-research", work=self.root,
            project_dir=self.project, input_dir=self.input, control_dir=self.tmp, mcp_servers=["cardrag"])
        self.assertNotIn({"action": "webfetch", "resource": "*", "effect": "allow"}, none)
        self.assertIn({"action": "webfetch", "resource": "*", "effect": "allow"}, public)
        self.assertIn({"action": "cardrag_*", "resource": "*", "effect": "allow"}, public)
        self.assertIn({"action": "cardrag_*", "resource": "*", "effect": "allow"}, none)
        self.assertIn({"action": "cardrag_delete*", "resource": "*", "effect": "deny"}, none)
        self.assertIn({"action": "cardrag_send*", "resource": "*", "effect": "deny"}, none)
        rules_multi = _permission_rules(stage="compose", network_profile="none", work=self.root,
            project_dir=self.project, input_dir=self.input, control_dir=self.tmp,
            mcp_servers=["cardrag", "remote-cards.v2", "space server"])
        self.assertIn({"action": "remote-cards_v2_*", "resource": "*", "effect": "allow"}, rules_multi)
        self.assertIn({"action": "space_server_*", "resource": "*", "effect": "allow"}, rules_multi)
        self.assertIn({"action": "opencode_read_mcp_resource", "resource": "remote-cards.v2:*",
                       "effect": "allow"}, rules_multi)
        for rules in (none, public):
            self.assertIn({"action": "subagent", "resource": "*", "effect": "deny"}, rules)
            self.assertIn({"action": "execute", "resource": "*", "effect": "allow"}, rules)
            self.assertNotIn({"action": "execute", "resource": "*", "effect": "deny"}, rules)

    def test_directory_permissions_allow_exact_roots_without_granting_parents(self):
        work = Path('/synthetic/task/.runs/work')
        project = Path('/synthetic/task/project')
        rules = _permission_rules(stage='research', network_profile='none', work=work,
            project_dir=project, input_dir=Path('/synthetic/task/.runs/input'),
            control_dir=Path('/synthetic/task/.runs/control'), mcp_servers=[])
        allowed = {r['resource'] for r in rules
                   if r['action']=='external_directory' and r['effect']=='allow'}
        self.assertIn(str(project), allowed)
        self.assertIn(str(project)+'/*', allowed)
        self.assertIn(str(work), allowed)
        self.assertNotIn(str(project.parent), allowed)
        self.assertNotIn(str(project.parent)+'/*', allowed)

    def test_model_ref_preserves_nested_model_ids_and_rejects_bad_variants(self):
        self.assertEqual(_model_ref("openrouter/moonshotai/kimi-k3.1-pro", "high"),
                         {"providerID": "openrouter", "id": "moonshotai/kimi-k3.1-pro",
                          "variant": "high"})
        self.assertEqual(_model_ref("provider-one/model-one#low", None),
                         {"providerID": "provider-one", "id": "model-one", "variant": "low"})
        for model, effort in (("missing-provider", None), ("provider/", None),
                              ("provider/model", "Bad Variant"), ("provider/model#", None)):
            with self.subTest(model=model, effort=effort), self.assertRaises(ValueError):
                _model_ref(model, effort)

    def test_engine_wiring_accepts_opencode_in_plan_package_and_factory(self):
        stage = {"type": "opencode_exec", "model": "provider-one/model-one",
                 "reasoning_effort": "high"}
        plan = build_execution_plan({"research": stage, "compose": stage})
        self.assertEqual(plan["stages"]["research"], stage)
        package = build_production_package(task_id="synthetic-opencode", name="Synthetic",
            instructions="Test the OpenCode path.", runner_type="opencode_exec",
            recipient_group_id="synthetic-team", cron="0 9 * * *", schedule_enabled=False,
            stage_settings={"research": stage, "compose": stage})
        self.assertIn("type: opencode_exec", package["task.yaml"])
        settings = SimpleNamespace(runner=SimpleNamespace(codex_binary="codex",
            antigravity_binary="agy", opencode_binary="/opt/opencode", cgroup_root=None))
        orchestrator = SimpleNamespace(custom_runner=None, settings=settings)
        task = SimpleNamespace(runner={"type": "opencode_exec"})
        runner = Orchestrator.resolve_runner(orchestrator, task)
        self.assertIsInstance(runner, OpenCodeRunner)
        self.assertEqual(runner.binary, "/opt/opencode")

    def test_opencode_prompt_does_not_claim_antigravity_tool_names(self):
        prompt = _prompt(self.input, self.project, self.tmp, self.context(
            network_profile="public-research", allow_http_requests=False),
            mcp_inventory={"servers": [{"name": "cardrag", "enabled": True}]},
            provider="opencode_exec")
        self.assertNotIn("call_mcp_tool", prompt)
        self.assertNotIn("~/.gemini", prompt)
        self.assertIn("OpenCode session's actual native MCP tool names", prompt)
        self.assertIn("application-owned phase staging directory", prompt)

    def test_message_cursor_is_not_combined_with_order(self):
        class CursorClient:
            def __init__(self):
                self.calls = []

            def call(self, operation, *, params, timeout_seconds):
                self.calls.append(dict(params))
                if "cursor" not in params:
                    return {"data": [{"type": "assistant"}], "cursor": {"next": "opaque"}}
                return {"data": [], "cursor": {"next": None}}

        client = CursorClient()
        messages = _messages(client, SESSION_ID, message_type="assistant",
                             deadline=__import__("time").monotonic() + 30, max_bytes=1_000_000)
        self.assertEqual(len(messages), 1)
        self.assertIn("order", client.calls[0])
        self.assertEqual(client.calls[1].get("cursor"), "opaque")
        self.assertNotIn("order", client.calls[1])

        with patch("researchops.runners.opencode.run_bounded",
                   return_value=process(b"No MCP servers configured\n")):
            inventory = _mcp_inventory("/usr/bin/opencode", self.root, {"HOME": str(self.root)})
        self.assertEqual(inventory["servers"], [])

    def test_live_resource_probe_checks_shell_pty_and_persistent_terminals(self):
        api = FakeAPI()
        result = self.run_with(api)
        self.assertTrue(result.success, result.error_message)
        operations = [argv[-1] for argv in api.argv_seen]
        self.assertIn("shell.list", operations)
        self.assertIn("pty.list", operations)
        self.assertIn("server.experimental.persistentPty.list", operations)
        resources = result.isolation["resources"]
        self.assertTrue(resources["cleanup_verified"])
        self.assertEqual(resources["running_shells"], 0)

    def test_running_tool_in_final_projection_is_not_cleanup_evidence(self):
        running_tool = {"type": "tool", "name": "shell",
                        "state": {"status": "running", "input": {"command": "sleep 30"}}}
        api = FakeAPI(final_tool=running_tool)
        result = self.run_with(api)
        self.assertFalse(result.success)
        self.assertEqual(result.error_message, "OPENCODE_TOOL_NOT_TERMINAL")
        self.assertEqual(result.isolation["resources"]["running_shells"], 0)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_live_shell_pty_or_terminal_blocks_import_and_cleanup(self):
        for kwargs, message in (({"live_shells": [{"id": "sh1", "status": "running"}]}, "shell"),
                                ({"live_shells": [{"id": "sh1"}]}, "malformed-shell"),
                                ({"live_ptys": [{"id": "pty1", "status": "running"}]}, "pty"),
                                ({"live_terminals": [{"id": "t1", "status": "running",
                                                      "sessionID": SESSION_ID}]}, "terminal")):
            with self.subTest(message=message):
                api = FakeAPI(**kwargs)
                result = self.run_with(api)
                self.assertFalse(result.success)
                self.assertFalse(result.cleanup_verified)
                self.assertEqual(list(self.output.iterdir()), [])

    def test_resource_api_error_is_not_an_empty_list(self):
        for kwargs in ({"shell_list_error": True}, {"pty_list_error": True},
                       {"terminal_list_error": True}):
            with self.subTest(kwargs=kwargs):
                api = FakeAPI(**kwargs)
                result = self.run_with(api)
                self.assertFalse(result.success)
                self.assertFalse(result.cleanup_verified)
                self.assertEqual(list(self.output.iterdir()), [])

    def test_lost_session_create_response_does_not_claim_cleanup(self):
        api = FakeAPI()

        def lost_create(argv, **kwargs):
            if argv[-1] == "session.create":
                return process(b"", exit_code=1)
            return api(argv, **kwargs)

        with patch("researchops.runners.opencode.shutil.which", return_value="/usr/bin/opencode"), \
                patch("researchops.runners.opencode.run_bounded", side_effect=lost_create):
            result = OpenCodeRunner().execute_research(
                self.input, self.tmp, self.output, self.project, self.context())
        self.assertFalse(result.success)
        self.assertFalse(result.cleanup_verified)
        self.assertFalse(any(argv[-1] == "session.prompt" for argv in api.argv_seen))

    def test_control_cleanup_failure_is_never_hidden_by_missing_session(self):
        api = FakeAPI(control_cleanup=False)
        result = self.run_with(api)
        self.assertFalse(result.success)
        self.assertFalse(result.cleanup_verified)
        self.assertFalse(result.isolation["control_cleanup_verified"])

    def test_final_finish_completion_model_and_identity_are_required(self):
        cases = ({"final_finish": None}, {"final_completed": None})
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                api = FakeAPI(**kwargs)
                result = self.run_with(api)
                self.assertFalse(result.success)
                self.assertEqual(list(self.output.iterdir()), [])

        def different_model(argv, **kwargs):
            response = api(argv, **kwargs)
            if argv[-1] == "session.prompt":
                api.assistant_message["model"] = {"id": "other", "providerID": "provider-one"}
            return response

        api = FakeAPI()
        with patch("researchops.runners.opencode.shutil.which", return_value="/usr/bin/opencode"), \
                patch("researchops.runners.opencode.run_bounded", side_effect=different_model):
            result = OpenCodeRunner().execute_research(
                self.input, self.tmp, self.output, self.project, self.context())
        self.assertFalse(result.success)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_tool_exit_truncation_and_mcp_error_are_not_success(self):
        model = {"id": "model-one", "providerID": "provider-one"}
        session = {"outcome": "succeeded", "time": {"idle": 2}, "model": model, "agent": "build"}

        def trace(content):
            return _parse_trace([{"id": "msg_1", "type": "assistant", "finish": "stop",
                                  "agent": "build", "model": model,
                                  "time": {"created": 1, "completed": 2},
                                  "content": content}], session, max_bytes=100_000)

        failed = trace([{"type": "tool", "name": "shell",
                         "state": {"status": "completed", "input": {"command": "false"},
                                   "metadata": {"exit": 2}, "content": []}},
                        {"type": "text", "text": "{}"}])
        self.assertFalse(failed.tools[0].success)
        self.assertTrue(failed.tools[0].verified)
        self.assertTrue(failed.successful)

        ok = trace([{"type": "tool", "name": "shell",
                     "state": {"status": "completed", "input": {"command": "true"},
                               "metadata": {"exit": 0, "truncated": False}, "content": []}}])
        self.assertTrue(ok.tools[0].success)
        truncated = trace([{"type": "tool", "name": "shell",
                            "state": {"status": "completed", "input": {"command": "true"},
                                      "metadata": {"exit": 0, "truncated": True}, "content": []}}])
        self.assertFalse(truncated.tools[0].success)
        mcp_error = trace([{"type": "tool", "name": "cardrag_search",
                            "state": {"status": "completed", "input": {},
                                      "content": [{"type": "text", "text": '{"isError": true}'}]}}])
        self.assertFalse(mcp_error.tools[0].success)

    def test_duplicate_message_or_agent_change_is_rejected(self):
        model = {"id": "model-one", "providerID": "provider-one"}
        session = {"outcome": "succeeded", "time": {"idle": 2}, "model": model, "agent": "build"}
        base = {"id": "msg_1", "type": "assistant", "finish": "stop", "agent": "build",
                "model": model, "time": {"created": 1, "completed": 2},
                "content": [{"type": "text", "text": "{}"}]}
        duplicate = _parse_trace([base, {**base}], session, max_bytes=100_000)
        self.assertFalse(duplicate.successful)
        self.assertEqual(duplicate.response_error, "OPENCODE_MESSAGE_IDENTITY_INVALID")
        changed = {**base, "id": "msg_2", "agent": "other"}
        changed_trace = _parse_trace([changed], session, max_bytes=100_000)
        self.assertFalse(changed_trace.successful)
        self.assertEqual(changed_trace.response_error, "OPENCODE_FINAL_AGENT_MISMATCH")

    def test_failed_step_then_recovery_is_recorded_without_hiding_success(self):
        model = {"id": "model-one", "providerID": "provider-one"}
        session = {"outcome": "succeeded", "time": {"idle": 2}, "model": model, "agent": "build"}
        message = {"id": "msg_1", "type": "assistant", "finish": "stop", "agent": "build",
                   "model": model, "time": {"created": 1, "completed": 2},
                   "content": [
                       {"type": "tool", "name": "shell",
                        "state": {"status": "completed", "input": {"command": "false"},
                                  "metadata": {"exit": 1}, "content": []}},
                       {"type": "tool", "name": "shell",
                        "state": {"status": "completed", "input": {"command": "true"},
                                  "metadata": {"exit": 0}, "content": []}},
                       {"type": "text", "text": "{}"}]}
        trace = _parse_trace([message], session, max_bytes=100_000)
        self.assertTrue(trace.successful)
        self.assertEqual([tool.success for tool in trace.tools], [False, True])

    def test_blocked_external_file_access_can_recover_with_valid_submission(self):
        blocked = {"type": "tool", "name": "read", "state": {
            "status": "error", "input": {"path": "/tmp/opencode/inv.txt"},
            "error": {"type": "permission.rejected", "message": "Permission denied: external_directory"}}}
        result = self.run_with(FakeAPI(final_tool=blocked))
        self.assertTrue(result.success, result.error_message)
        self.assertTrue(result.cleanup_verified)
        self.assertTrue((self.output / "result.json").is_file())
        self.assertEqual(result.isolation["permission_warnings"][0]["count"], 1)
        self.assertEqual(result.events[0]["denied_actions"], ["read"])
        self.assertFalse(result.events[0]["tools"][0]["success"])

    def test_blocked_file_access_does_not_bypass_submission_validation(self):
        blocked = {"type": "tool", "name": "write", "state": {
            "status": "error", "input": {"path": "/tmp/opencode/helper.py"},
            "error": {"type": "permission.rejected", "message": "Permission denied: external_directory"}}}
        result = self.run_with(FakeAPI(final_tool=blocked, final_text="not a file reference"))
        self.assertFalse(result.success)
        self.assertNotEqual(result.error_message, "OPENCODE_PERMISSION_DENIED")
        self.assertFalse((self.output / "result.json").exists())
        self.assertEqual(result.events[0]["permission_warnings"][0]["count"], 1)

    def test_non_file_permission_denial_is_still_fatal(self):
        blocked = {"type": "tool", "name": "cardrag_delete_record", "state": {
            "status": "error", "input": {},
            "error": {"type": "permission.rejected", "message": "Permission denied: cardrag_delete_record"}}}
        result = self.run_with(FakeAPI(final_tool=blocked))
        self.assertFalse(result.success)
        self.assertEqual(result.error_message, "OPENCODE_PERMISSION_DENIED")
        self.assertFalse((self.output / "result.json").exists())

    def test_strict_trace_policy_does_not_recover_blocked_files_by_default(self):
        model = {"id": "model-one", "providerID": "provider-one"}
        message = {"id": "msg_1", "type": "assistant", "finish": "stop", "agent": "build",
            "model": model, "time": {"created": 1, "completed": 2}, "content": [
                {"type": "tool", "name": "read", "state": {"status": "error", "input": {},
                 "error": {"type": "permission.rejected", "message": "Permission denied: external_directory"}}},
                {"type": "text", "text": "{}"}]}
        session = {"outcome": "succeeded", "time": {"idle": 2}, "model": model, "agent": "build"}
        trace = _parse_trace([message], session, max_bytes=100_000)
        self.assertEqual(trace.response_error, "OPENCODE_PERMISSION_DENIED")
        self.assertEqual(trace.recovered_file_denials, 0)

    def test_quota_failure_has_actionable_safe_diagnostics(self):
        class QuotaAPI(FakeAPI):
            def __call__(self, argv, **kwargs):
                response = super().__call__(argv, **kwargs)
                if argv[-1] not in {"session.get", "session.message.list"}:
                    return response
                document = json.loads(response.stdout)
                if argv[-1] == "session.get":
                    document["data"]["outcome"] = "failed"
                elif document["data"]:
                    message = document["data"][-1]
                    message.update(finish="error", content=[], error={
                        "type": "provider.quota", "status": 429,
                        "message": "synthetic-private-provider-detail",
                        "response": {"body": "synthetic-private-provider-detail"}})
                return process(json.dumps(document).encode())
        result = self.run_with(QuotaAPI())
        self.assertFalse(result.success)
        self.assertTrue(result.cleanup_verified)
        self.assertTrue(result.error_message.startswith("OPENCODE_PROVIDER_QUOTA_EXHAUSTED:"))
        self.assertIn("다른 모델/공급자", result.error_message)
        self.assertFalse(result.isolation["provider_failure"]["retryable"])
        self.assertFalse((self.output / "result.json").exists())
        self.assertNotIn("synthetic-private-provider-detail", json.dumps(result.events))
        for path in self.logs.iterdir():
            if path.is_file():
                self.assertNotIn(b"synthetic-private-provider-detail", path.read_bytes())

    def test_transient_429_is_distinct_from_quota_exhaustion(self):
        model = {"id": "model-one", "providerID": "provider-one"}
        session = {"outcome": "failed", "time": {"idle": 2}, "model": model, "agent": "build"}
        message = {"id": "msg_error", "type": "assistant", "finish": "error",
                   "agent": "build", "model": model, "time": {"created": 1, "completed": 2},
                   "content": [], "error": {"type": "provider.rate_limit", "status": 429}}
        trace = _parse_trace([message], session, max_bytes=100_000)
        self.assertEqual(trace.response_error, "OPENCODE_PROVIDER_RATE_LIMITED")
        self.assertTrue(trace.provider_failure["retryable"])
        self.assertFalse(trace.successful)

    def test_unknown_provider_error_keeps_failed_session_gate(self):
        model = {"id": "model-one", "providerID": "provider-one"}
        session = {"outcome": "failed", "time": {"idle": 2}, "model": model, "agent": "build"}
        message = {"id": "msg_error", "type": "assistant", "finish": "error",
                   "agent": "build", "model": model, "time": {"created": 1, "completed": 2},
                   "content": [], "error": {"type": {"unexpected": "shape"}, "status": 500}}
        trace = _parse_trace([message], session, max_bytes=100_000)
        self.assertEqual(trace.response_error, "OPENCODE_SESSION_FAILED")
        self.assertIsNone(trace.provider_failure)

    def test_cancellation_before_prompt_creates_no_model_invocation(self):
        api = FakeAPI()
        result = self.run_with(api, self.context(cancellation_check=lambda: True))
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 130)
        self.assertFalse(any(argv[-1] == "session.prompt" for argv in api.argv_seen))
        self.assertFalse(any(argv[-1] == "session.create" for argv in api.argv_seen))
        self.assertTrue(result.cleanup_verified)

        api = FakeAPI()
        result = self.run_with(api, self.context(
            cancellation_check=lambda: api.environment_body is not None))
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 130)
        self.assertFalse(any(argv[-1] == "session.prompt" for argv in api.argv_seen))
        self.assertTrue(any(argv[-1] == "session.interrupt" for argv in api.argv_seen))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_deadline_expiry_before_prompt_creates_no_model_invocation(self):
        api = FakeAPI()

        def delayed(argv, **kwargs):
            if argv[-1] == "session.environment":
                time.sleep(2.5)
            return api(argv, **kwargs)

        with patch("researchops.runners.opencode.shutil.which", return_value="/usr/bin/opencode"), \
                patch("researchops.runners.opencode.run_bounded", side_effect=delayed):
            result = OpenCodeRunner().execute_research(
                self.input, self.tmp, self.output, self.project,
                self.context(timeout_seconds=2))
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 124)
        self.assertFalse(any(argv[-1] == "session.prompt" for argv in api.argv_seen))
        self.assertTrue(any(argv[-1] == "session.interrupt" for argv in api.argv_seen))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_lost_prompt_acceptance_is_not_resubmitted(self):
        api = FakeAPI(prompt_error=True)
        result = self.run_with(api)
        self.assertFalse(result.success)
        self.assertEqual(sum(argv[-1] == "session.prompt" for argv in api.argv_seen), 1)
        self.assertTrue(any(argv[-1] == "session.interrupt" for argv in api.argv_seen))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_trace_limits_are_enforced_before_prompt(self):
        for kwargs in ({"trace_max_event_bytes": 16}, {"trace_max_events": 1},
                       {"trace_max_bytes": 32}):
            with self.subTest(kwargs=kwargs):
                api = FakeAPI()
                result = self.run_with(api, self.context(**kwargs))
                self.assertFalse(result.success)
                self.assertIn("OPENCODE_TRACE_", result.error_message)
                self.assertFalse(any(argv[-1] == "session.prompt" for argv in api.argv_seen))
                self.assertEqual(list(self.output.iterdir()), [])

    def test_message_page_uses_total_budget_and_checks_individual_events(self):
        messages = [{'type': 'assistant', 'id': str(n), 'content': 'x'*700000}
                    for n in range(2)]
        raw = json.dumps({'data': messages, 'cursor': {'next': None}}).encode()
        budget = OpenCodeTraceBudget(20*1024*1024, 1024*1024, 20)
        client = OpenCodeClient('/usr/bin/opencode', self.tmp, budget=budget)
        with patch('researchops.runners.opencode.run_bounded', return_value=process(raw)) as call:
            values = _messages(client, SESSION_ID, message_type='assistant',
                               deadline=time.monotonic()+30, max_bytes=budget.max_bytes, budget=budget)
        self.assertEqual(len(values), 2)
        self.assertGreater(budget.bytes, len(raw))
        self.assertEqual(budget.events, 3)
        self.assertEqual(call.call_args.kwargs['max_output_bytes'], budget.max_bytes)
        self.assertTrue(call.call_args.kwargs['direct_stdout'])

    def test_message_page_still_rejects_one_oversized_event(self):
        raw = json.dumps({'data': [{'type': 'assistant', 'content': 'x'*2000}]}).encode()
        budget = OpenCodeTraceBudget(100000, 1000, 20)
        client = OpenCodeClient('/usr/bin/opencode', self.tmp, budget=budget)
        with patch('researchops.runners.opencode.run_bounded', return_value=process(raw)):
            with self.assertRaisesRegex(ValueError, 'OPENCODE_TRACE_EVENT_LIMIT'):
                _messages(client, SESSION_ID, message_type='assistant',
                          deadline=time.monotonic()+30, max_bytes=budget.max_bytes, budget=budget)

    def test_message_page_still_rejects_total_budget_overflow(self):
        raw = json.dumps({'data': [{'type': 'assistant', 'content': 'x'*2000}]}).encode()
        budget = OpenCodeTraceBudget(1000, 1000, 20)
        client = OpenCodeClient('/usr/bin/opencode', self.tmp, budget=budget)
        with patch('researchops.runners.opencode.run_bounded', return_value=process(raw)):
            with self.assertRaisesRegex(ValueError, 'OPENCODE_TRACE_TOTAL_LIMIT'):
                client.call('session.message.list')

    def test_service_unavailable_queries_no_mcp_model_or_session(self):
        api = FakeAPI(service_unavailable=True)
        result = self.run_with(api, self.context(network_profile="public-research"))
        self.assertFalse(result.success)
        self.assertIn("RUNNER-SERVICE-UNAVAILABLE", result.error_message)
        operations = [argv[-1] for argv in api.argv_seen]
        self.assertNotIn("mcp.list", operations)
        self.assertNotIn("model.list", operations)
        self.assertNotIn("session.create", operations)

    def test_code_repair_requires_observed_failure_then_recovery(self):
        (self.root / "metrics.py").write_text(
            "def percent_change(before, after):\n"
            "    return None if before == 0 else (after-before)/before*100\n")
        (self.root / "test_metrics.py").write_text(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            " def test_a(self): pass\n"
            " def test_b(self): pass\n"
            " def test_c(self): pass\n")
        tools = [
            SimpleNamespace(name="write", path=str(self.root / "test_metrics.py"),
                            command=None, success=True, verified=True, exit_code=None,
                            content_sha256=__import__("hashlib").sha256(
                                (self.root / "test_metrics.py").read_bytes()).hexdigest()),
            SimpleNamespace(name="shell", path=None,
                            command="python3 -m unittest -v test_metrics.py",
                            success=False, verified=True, exit_code=1,
                            unittest_failed_marker=True, unittest_ok_marker=False,
                            unittest_count=3),
            SimpleNamespace(name="write", path=str(self.root / "metrics.py"),
                            command=None, success=True, verified=True, exit_code=None),
            SimpleNamespace(name="shell", path=None,
                            command="python3 -m unittest -v test_metrics.py",
                            success=True, verified=True, exit_code=0,
                            unittest_failed_marker=False, unittest_ok_marker=True,
                            unittest_count=3),
        ]
        trace = SimpleNamespace(tools=tools, successful=True)
        checks = _opencode_checks("code-repair", self.root,
                                  {"timezone": "Asia/Seoul", "summary": "ok"}, trace)
        self.assertTrue(checks["failure_observed_by_model"])
        self.assertTrue(checks["recovery_observed_by_model"])
        self.assertTrue(checks["three_tests_present"])
        self.assertTrue(checks["metrics_modified_after_failure"])
        self.assertTrue(all(value for key, value in checks.items()
                            if key in {"failure_observed_by_model", "recovery_observed_by_model",
                                       "three_tests_present", "metrics_modified_after_failure",
                                       "test_file_stable_during_repair",
                                       "test_file_hash_matches_model_write"}))

    def test_unittest_target_is_limited_to_its_simple_command_argv(self):
        def tool(command):
            return SimpleNamespace(name="shell", command=command)

        self.assertTrue(_python_unittest_invoked(
            tool("cd /tmp/work && python3 -m unittest -v test_metrics.py"), "test_metrics.py"))
        self.assertTrue(_python_unittest_invoked(
            tool("python3 -m unittest -v test_metrics 2>&1; echo done"), "test_metrics.py"))
        for command in (
                "python3 -m unittest -v test_other ; cat test_metrics.py",
                "python3 -m unittest -v test_other && cat test_metrics.py",
                "python3 -m unittest -v test_other > test_metrics.py",
                "python3 -m unittest -v test_other; exit 1 # test_metrics.py",
                "echo python3 -m unittest test_metrics.py",
                'python3 -m unittest "test_other; test_metrics.py"',
                "python3 -m unittest -v test_metrics.py; python3 -m unittest -v test_metrics.py"):
            self.assertFalse(_python_unittest_invoked(tool(command), "test_metrics.py"), command)

    def test_realistic_wrong_target_repair_is_rejected(self):
        work = self.root
        metrics = ("def percent_change(before, after):\n"
                   "    return None if before == 0 else (after-before)/before*100\n")
        tests = ("import unittest\nclass T(unittest.TestCase):\n"
                 " def test_a(self): pass\n def test_b(self): pass\n def test_c(self): pass\n")
        (work / "metrics.py").write_text(metrics)
        (work / "test_metrics.py").write_text(tests)
        other_bad = tests.replace("def test_a(self): pass", 'def test_a(self): self.fail("x")')
        (work / "test_other.py").write_text(other_bad)

        def write(name, text):
            return {"type": "tool", "name": "write",
                    "state": {"status": "completed", "input": {"path": str(work / name),
                                                               "content": text}}}

        def shell(command):
            return {"type": "tool", "name": "shell",
                    "state": {"status": "completed", "input": {"command": command},
                              "metadata": {"exit": 1 if "test_other" in command else 0},
                              "content": [{"type": "text", "text": (
                                  "Ran 3 tests in 0.001s\\n\\nFAILED (failures=1)"
                                  if "test_other" in command else
                                  "Ran 3 tests in 0.001s\\n\\nOK")}]}}

        blocks = [write("test_metrics.py", tests),
                  shell("python3 -m unittest -v test_other ; cat test_metrics.py ; exit 1"),
                  write("metrics.py", metrics),
                  write("test_other.py", tests),
                  shell("python3 -m unittest -v test_other && cat test_metrics.py"),
                  {"type": "text", "text": "{}"}]
        model = {"id": "synthetic", "providerID": "synthetic"}
        trace = _parse_trace([{"id": "msg_1", "type": "assistant", "finish": "stop",
                               "model": model, "agent": "build",
                               "time": {"created": 1, "completed": 2},
                               "content": blocks}],
                             {"outcome": "succeeded", "time": {"idle": 2},
                              "model": model, "agent": "build"}, max_bytes=1_000_000)
        checks = _opencode_checks("code-repair", work,
                                  {"timezone": "Asia/Seoul", "summary": "synthetic"}, trace)
        self.assertTrue(trace.successful)
        self.assertFalse(checks["failure_observed_by_model"])
        self.assertFalse(checks["recovery_observed_by_model"])
        self.assertFalse(checks["metrics_modified_after_failure"])
        self.assertFalse(all(checks.values()))

    def test_code_repair_rejects_echo_only_or_wrong_test_commands(self):
        (self.root / "metrics.py").write_text(
            "def percent_change(before, after):\n"
            "    return None if before == 0 else (after-before)/before*100\n")
        (self.root / "test_metrics.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            " def test_a(self): pass\n def test_b(self): pass\n def test_c(self): pass\n")
        tools = [
            SimpleNamespace(name="write", path=str(self.root / "test_metrics.py"),
                            success=True, verified=True, content_sha256=__import__("hashlib").sha256(
                                (self.root / "test_metrics.py").read_bytes()).hexdigest()),
            SimpleNamespace(name="shell", command="echo unittest test_metrics; false",
                            success=False, verified=True, exit_code=1,
                            unittest_failed_marker=False, unittest_ok_marker=False,
                            unittest_count=None),
            SimpleNamespace(name="write", path=str(self.root / "metrics.py"),
                            success=True, verified=True),
            SimpleNamespace(name="shell", command="echo unittest test_metrics",
                            success=True, verified=True, exit_code=0,
                            unittest_failed_marker=False, unittest_ok_marker=False,
                            unittest_count=None),
        ]
        checks = _opencode_checks("code-repair", self.root,
                                  {"timezone": "Asia/Seoul", "summary": "ok"},
                                  SimpleNamespace(tools=tools))
        self.assertFalse(checks["failure_observed_by_model"])
        self.assertFalse(checks["recovery_observed_by_model"])
        self.assertFalse(checks["executed_test_count_matches"])
        self.assertFalse(all(checks.values()))

        (self.root / "test_metrics.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            " def test_a(self): pass\n def test_b(self): pass\n def test_c(self): pass\n")
        wrong = [
            SimpleNamespace(name="write", path=str(self.root / "test_metrics.py"),
                            success=True, verified=True, content_sha256=__import__("hashlib").sha256(
                                (self.root / "test_metrics.py").read_bytes()).hexdigest()),
            SimpleNamespace(name="shell", command="python3 -m unittest -v other_test.py",
                            success=True, verified=True, exit_code=0,
                            unittest_failed_marker=False, unittest_ok_marker=True,
                            unittest_count=3),
            SimpleNamespace(name="write", path=str(self.root / "test_metrics.py"),
                            success=True, verified=True),
        ]
        checks = _opencode_checks("code-repair", self.root,
                                  {"timezone": "Asia/Seoul", "summary": "ok"},
                                  SimpleNamespace(tools=wrong))
        self.assertFalse(checks["failure_observed_by_model"])
        self.assertFalse(checks["recovery_observed_by_model"])

    def test_native_case_local_control_cleanup_failure_is_blocked(self):
        api = FakeAPI(control_cleanup=False)
        with tempfile.TemporaryDirectory() as directory, \
                patch("researchops.runner_tool_validation.shutil.which",
                      return_value="/usr/bin/opencode"), \
                patch("researchops.runners.opencode.run_bounded", side_effect=api):
            case = _opencode_case("opencode_exec", "file-write", Path(directory),
                                  timeout_seconds=30, model=None)
        self.assertEqual(case["status"], "blocked")
        self.assertFalse(case["control_process_cleanup"])
        self.assertEqual(case["blocked_by"], ["OPENCODE-CONTROL-CLEANUP-UNVERIFIED"])

    def test_tool_suite_does_not_return_success_for_cleanup_blocked_case(self):
        blocked = {"runner": "opencode_exec", "scenario": "file-write",
                   "status": "blocked", "blocked_by": ["OPENCODE-CLEANUP-UNVERIFIED"],
                   "cli_invocations": 1, "model_invocations": 1,
                   "terminal_turns": 1, "structured_responses": 1,
                   "checks": {"file_bytes_exact": True},
                   "remote_cleanup_verified": False,
                   "control_process_cleanup": True}
        with tempfile.TemporaryDirectory() as directory, \
                patch("researchops.runner_tool_validation._one_case", return_value=blocked):
            report = validate_runner_tools(["opencode_exec"], scenarios=["file-write"],
                                            live=True, output_parent=directory)
        self.assertEqual(report["exit_code"], 78)
        self.assertEqual(report["totals"], {"passed": 0, "failed": 0, "blocked": 1})
        self.assertEqual(report["status"], "blocked")

    def test_file_code_requires_model_shell_execution_not_only_files(self):
        program = self.root / "analyze_sales.py"
        tests = self.root / "test_analyze_sales.py"
        analysis = self.root / "analysis.json"
        program.write_text("print('ok')\n")
        tests.write_text("import unittest\n")
        analysis.write_text(json.dumps({"totals": {"alpha": "62.5", "beta": "32", "gamma": "20"},
                                       "grand_total": "114.5", "row_count": 4}))
        tools = [SimpleNamespace(name="write", path=str(program), command=None,
                                 success=True, verified=True, exit_code=None),
                 SimpleNamespace(name="write", path=str(tests), command=None,
                                 success=True, verified=True, exit_code=None)]
        checks = _opencode_checks("file-code", self.root,
                                  {"timezone": "Asia/Seoul", "summary": "ok"},
                                  SimpleNamespace(tools=tools, successful=True))
        self.assertTrue(checks["program_written"])
        self.assertFalse(checks["program_executed_by_model"])
        self.assertFalse(checks["tests_executed_by_model"])

    def test_engine_keeps_workspace_and_lease_when_opencode_cleanup_is_unverified(self):
        from researchops.delivery.smtp_config import BuiltinDeliveryConfig, save_delivery_config
        from researchops.services.application import ApplicationService
        from tests.support import isolated_settings

        with tempfile.TemporaryDirectory() as directory:
            settings = isolated_settings(Path(directory))
            settings.environment = "production"
            app = ApplicationService(settings)
            save_delivery_config(BuiltinDeliveryConfig(recipient_groups={
                "team": ["recipient@example.test"]}), settings.paths.delivery_config_file)
            app.tasks.create_production_task(
                task_id="opencode-cleanup", name="Cleanup fixture",
                instructions="Synthetic cleanup boundary.", runner_type="opencode_exec",
                recipient_group_id="team")
            run = app.runs.enqueue_run("opencode-cleanup", force_dry_run=True)
            failed = RunnerExecutionResult(False, 77, "", "cleanup", events=[],
                                           error_message="cleanup unverified",
                                           cleanup_verified=False, isolation={})
            with patch.object(OpenCodeRunner, "execute_research", return_value=failed), \
                    patch("smtplib.SMTP") as smtp:
                result = app.runs.execute_run(run.run_id)
            smtp.assert_not_called()
            self.assertEqual(result.status, "needs_attention")
            self.assertTrue(app.workspace_mgr.is_locked("opencode-cleanup")[0])
            self.assertIsNotNone(app.run_repo.get_lease(run.run_id))
            self.assertIsNone(app.delivery_repo.get_handoff_for_run(run.run_id))


    def test_mcp_query_allowed_in_compose_but_direct_web_rejected(self):
        (self.input / "composition-input.json").write_text("{}", encoding="utf-8")
        document = {"composition_result": {"recipient_group_id": "team",
                    "recipient_group_reason": "synthetic", "subject": "synthetic",
                    "html_path": "email.html", "text_path": "email.txt",
                    "included_record_ids": []},
                    "html": "<html><head></head><body>synthetic</body></html>",
                    "text": "synthetic"}
        mcp_tool = {"type": "tool", "name": "cardrag_search",
                    "state": {"status": "completed", "input": {},
                              "content": [{"type": "text", "text": "synthetic result"}]}}
        api = FakeAPI(final_tool=mcp_tool, document=document)
        result = self.run_with(api, self.context(invocation_stage="compose",
                                                  reasoning_effort=None), stage="compose")
        self.assertTrue(result.success, result.error_message)
        self.assertIn("cardrag", [s["name"] for s in result.isolation["mcp"]["servers"]])

        web_tool = {"type": "tool", "name": "webfetch",
                    "state": {"status": "completed", "input": {"url": "https://example.test/"},
                              "content": [{"type": "text", "text": "page"}]}}
        api = FakeAPI(final_tool=web_tool, document=document)
        result = self.run_with(api, self.context(invocation_stage="compose",
                                                  reasoning_effort=None), stage="compose")
        self.assertFalse(result.success)
        self.assertIn("direct web", result.error_message)

        mutation_tool = {"type": "tool", "name": "cardrag_delete_record",
                         "state": {"status": "completed", "input": {},
                                   "content": [{"type": "text", "text": "deleted"}]}}
        api = FakeAPI(final_tool=mutation_tool, document=document)
        result = self.run_with(api, self.context(invocation_stage="compose",
                                                  reasoning_effort=None), stage="compose")
        self.assertFalse(result.success)
        self.assertIn("unauthorized MCP mutation", result.error_message)

    def test_mcp_discovery_preserves_tool_capability_rules_and_audit(self):
        """MCP tools discovered with capability metadata generate correct permission rules and audit."""
        import fnmatch
        class CustomCatalogAPI(FakeAPI):
            def __call__(self, argv, **kwargs):
                if argv[-1] == "mcp.list":
                    self.argv_seen.append(list(argv))
                    return process(json.dumps({"data": [{"name": "docs", "status": {"status": "connected"},
                        "tools": [{"name": "calculate", "capability": "query"},
                                  {"name": "get_balance", "capability": "mutation"}]}]}).encode())
                return super().__call__(argv, **kwargs)

        api = CustomCatalogAPI()
        result = self.run_with(api)
        self.assertTrue(result.success)

        def effect(action):
            res = None
            for rule in api.create_body["permissions"]:
                if rule["resource"] == "*" and fnmatch.fnmatchcase(action, rule["action"]):
                    res = rule["effect"]
            return res

        self.assertEqual(effect("docs_calculate"), "allow")
        self.assertEqual(effect("docs_get_balance"), "deny")

    def test_control_environment_does_not_forward_credentials(self):
        with patch.dict("os.environ", {"HOME": str(self.root), "SMTP_PASSWORD": "secret",
                                       "OPENAI_API_KEY": "secret", "SSH_AUTH_SOCK": "/secret",
                                       "PATH": "/host"}, clear=False):
            env = opencode_control_environment("/usr/bin/opencode", self.tmp)
        self.assertNotIn("SMTP_PASSWORD", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertEqual(env["HOME"], str(self.root))
        self.assertNotIn("/host", env["PATH"])

    def test_mcp_queries_work_without_unsupported_catalog_api(self):
        """Trusted configured MCP tools are usable without a tool catalog API."""
        import fnmatch
        api = FakeAPI(tool_catalog_error=True)
        result = self.run_with(api)
        self.assertTrue(result.success, result.error_message)
        self.assertNotIn("mcp.tool.catalog", [argv[-1] for argv in api.argv_seen])
        rules = api.create_body["permissions"]
        def effect(action):
            value = None
            for rule in rules:
                if rule["resource"] == "*" and fnmatch.fnmatchcase(action, rule["action"]):
                    value = rule["effect"]
            return value
        self.assertEqual(effect("cardrag_calculate"), "allow")
        self.assertEqual(effect("cardrag_get_and_cancel"), "deny")
        for stage in ("research", "compose"):
            policy = _permission_rules(stage=stage, network_profile="none", work=self.root,
                project_dir=self.project, input_dir=self.input, control_dir=self.tmp,
                mcp_servers=[{"name": "docs", "tools": [{"name": "calculate", "capability": "unknown"}]}, "library"])
            def permitted(action):
                value = None
                for rule in policy:
                    if rule["resource"] == "*" and fnmatch.fnmatchcase(action, rule["action"]):
                        value = rule["effect"]
                return value
            for action in ("docs_calculate", "library_resolve_library_id", "docs_read_archive", "docs_get_email_history"):
                self.assertEqual(permitted(action), "allow")
            for action in ("docs_delete_record", "library_send_message", "docs_get_and_disable"):
                self.assertEqual(permitted(action), "deny")


if __name__ == "__main__":
    unittest.main()
