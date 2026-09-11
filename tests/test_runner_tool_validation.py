"""Native-tool suite regressions. All provider invocations here are controlled mocks."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import shlex
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from researchops.cli.main import main
from researchops.runner_tool_validation import (
    CODE_SCENARIOS, SCENARIOS, SOURCE_CSV, _native_error_categories, _repair_test_sequence,
    build_tool_command, evaluate_tool_case, validate_runner_tools,
)
from researchops.runners.development_process import ProcessResult
from researchops.runners.research_mcp import ResearchMCPServer
from researchops.runners.tool_events import ToolTrace, parse_tool_trace


def encode_events(events):
    return b"".join((json.dumps(event, ensure_ascii=False) + "\n").encode() for event in events)


def codex_trace(*, command_exit=0, terminal="turn.completed", usage=None):
    inner = {"timezone": "Asia/Seoul", "summary": "Controlled test fixture, not a live model."}
    return encode_events([
        {"type": "thread.started", "thread_id": "mock-thread"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "files", "type": "file_change", "status": "completed"}},
        {"type": "item.completed", "item": {"id": "command", "type": "command_execution",
            "status": "completed", "exit_code": command_exit,
            "command": "python3 -m unittest -v test_analyze_sales && python3 analyze_sales.py",
            "aggregated_output": "Ran 3 tests in 0.001s\nOK\n" if command_exit == 0 else "FAILED (errors=1)"}},
        {"type": "item.completed", "item": {"id": "response", "type": "agent_message",
            "text": json.dumps({"response_json": json.dumps(inner)})}},
        {"type": terminal, "usage": {"input_tokens": 10, "output_tokens": 5} if usage is None else usage},
    ])


def agy_file_trace(work, *, permission_mode="request-review", denied=False):
    initial = {"permission_mode": permission_mode} if permission_mode is not None else {}
    initial["cwd"] = str(work)
    steps = []
    for index, name, parameters in ((1, "write_to_file", {"TargetFile": str(work / "note.txt")}),
                                    (2, "view_file", {"AbsolutePath": str(work / "note.txt")})):
        steps.append({"event": "step_update", "step_update": {
            "step_index": index, "step_type": "tool", "state": "DONE", "tool_name": name,
            "tool_info": {"name": name, "parameters": parameters}}})
    result = {"status": "SUCCESS", "structured_output": {"response_json": json.dumps({
        "timezone": "Asia/Seoul", "summary": "Controlled fixture, not a live model."})},
        "usage": {"input_tokens": 10, "output_tokens": 5}}
    if denied:
        result["denied_actions"] = [{"action": "write_file"}]
    return encode_events([{"event": "init", "init": initial}, *steps,
                          {"event": "result", "result": result}])


def write_code_fixture(work):
    (work / "analyze_sales.py").write_text(
        "import csv, json\nfrom decimal import Decimal\n"
        "def aggregate(rows):\n"
        "    totals = {}\n    count = 0\n"
        "    for row in rows:\n"
        "        totals[row['product']] = totals.get(row['product'], Decimal(0)) + Decimal(row['units']) * Decimal(row['unit_price'])\n"
        "        count += 1\n"
        "    return {'totals': {k: str(v) for k,v in totals.items()}, 'grand_total': str(sum(totals.values(), Decimal(0))), 'row_count': count}\n"
        "if __name__ == '__main__':\n"
        "    with open('sales.csv', newline='') as source:\n        result = aggregate(csv.DictReader(source))\n"
        "    with open('analysis.json', 'w') as output:\n        json.dump(result, output)\n", encoding="utf-8")
    (work / "test_analyze_sales.py").write_text(
        "import unittest\nfrom analyze_sales import aggregate\n"
        "class TestAggregate(unittest.TestCase):\n"
        "    def test_empty(self):\n        self.assertEqual(aggregate([])['row_count'], 0)\n"
        "    def test_duplicate(self):\n        self.assertEqual(aggregate([{'product':'a','units':'1','unit_price':'2'}]*2)['totals']['a'], '4')\n"
        "    def test_money(self):\n        self.assertEqual(aggregate([{'product':'a','units':'3','unit_price':'0.1'}])['grand_total'], '0.3')\n", encoding="utf-8")
    (work / "analysis.json").write_text(json.dumps({"totals": {"alpha": "62.50", "beta": "32", "gamma": "20"},
                                                   "grand_total": "114.50", "row_count": 4}), encoding="utf-8")


def mcp_tool(name, *, success=True):
    return {"name": "mcp_tool_call", "success": success,
            "details": {"server": "researchops_probe", "tool": name,
                        "status": "completed" if success else "failed"}}


def repair_tool(output, *, command="python3 -m unittest -v test_metrics", success=True):
    return {"name": "run_command", "success": success,
            "details": {"name": "run_command", "parameters": {"CommandLine": command}, "output": output}}


REPAIR_FAILED = "Controlled fixture traceback.\nRan 3 tests in 0.001s\n\nFAILED (failures=2)\n"
REPAIR_PASSED = "Controlled fixture test names.\nRan 3 tests in 0.001s\n\nOK\n"


class TestRunnerToolValidation(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="researchops-tools-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def directories(self):
        work, control = self.root / "workspace", self.root / "control"
        work.mkdir(mode=0o700)
        control.mkdir(mode=0o700)
        return work, control

    def test_no_live_blocks_entire_matrix_without_process_db_config_or_smtp(self):
        with patch.dict("os.environ", {"RESEARCHOPS_CONFIG": "/not-used/operator.yaml",
                                       "RESEARCHOPS_ROOT": "/not-used/operator-root"}), \
                patch("subprocess.Popen", side_effect=AssertionError("No process")), \
                patch("sqlite3.connect", side_effect=AssertionError("No operator database")), \
                patch("smtplib.SMTP", side_effect=AssertionError("No SMTP")), \
                patch("smtplib.SMTP_SSL", side_effect=AssertionError("No SMTP")), \
                patch("researchops.cli.main.load_settings", side_effect=AssertionError("No config")), \
                patch("researchops.cli.main.create_application_service", side_effect=AssertionError("No app")), \
                redirect_stdout(io.StringIO()) as output:
            code = main(["validate-runner-tools", "--output-parent", str(self.root), "--json"])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 78)
        self.assertEqual(report["totals"], {"passed": 0, "failed": 0, "blocked": 2 * len(SCENARIOS)})
        self.assertEqual(report["cli_invocations"], 0)
        self.assertEqual(report["model_invocations"], 0)
        self.assertEqual(report["upstream_request_count"], 0)
        self.assertFalse(report["operator_config_loaded"])
        self.assertFalse(report["production_runner_ready"])
        self.assertFalse(report["agy_dangerously_skip_permissions_approved"])
        self.assertFalse(report["agy_no_sandbox_approved"])
        self.assertEqual(report["smtp_send_attempts"], 0)
        self.assertEqual(json.loads(Path(report["report_path"]).read_text()), report)
        self.assertTrue(all(case["blocked_by"] == ["LIVE-OPT-IN-REQUIRED"] for case in report["cases"]))

    def test_cli_forwards_explicit_options_without_operator_application(self):
        with patch("researchops.runner_tool_validation.validate_runner_tools", return_value={"exit_code": 0}) as run, \
                patch("researchops.cli.main.load_settings", side_effect=AssertionError("No config")), \
                patch("researchops.cli.main.create_application_service", side_effect=AssertionError("No app")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["validate-runner-tools", "--live", "--runner", "codex_exec", "--runner", "antigravity_exec",
                "--scenario", "file-code", "--scenario", "mcp-research", "--timeout-seconds", "90", "--model", "chosen-model",
                "--output-parent", str(self.root)]), 0)
        run.assert_called_once_with(["codex_exec", "antigravity_exec"], scenarios=["file-code", "mcp-research"], live=True,
                                    output_parent=str(self.root), timeout_seconds=90, model="chosen-model",
                                    agy_dangerously_skip_permissions=False, agy_no_sandbox=False)

    def test_cli_forwards_separate_antigravity_permission_grant(self):
        with patch("researchops.runner_tool_validation.validate_runner_tools", return_value={"exit_code": 0}) as run, \
                patch("researchops.cli.main.load_settings", side_effect=AssertionError("No config")), \
                patch("researchops.cli.main.create_application_service", side_effect=AssertionError("No app")), \
                redirect_stdout(io.StringIO()):
            code = main(["validate-runner-tools", "--live", "--runner", "antigravity_exec",
                         "--agy-dangerously-skip-permissions", "--scenario", "file-write", "--json"])
        self.assertEqual(code, 0)
        run.assert_called_once_with(["antigravity_exec"], scenarios=["file-write"], live=True,
            output_parent=None, timeout_seconds=180, model=None, agy_dangerously_skip_permissions=True,
            agy_no_sandbox=False)

    def test_cli_forwards_separate_sandbox_off_grant_for_explicit_code_scenarios(self):
        with patch("researchops.runner_tool_validation.validate_runner_tools", return_value={"exit_code": 0}) as run, \
                patch("researchops.cli.main.load_settings", side_effect=AssertionError("No config")), \
                patch("researchops.cli.main.create_application_service", side_effect=AssertionError("No app")), \
                redirect_stdout(io.StringIO()):
            code = main(["validate-runner-tools", "--live", "--runner", "antigravity_exec",
                         "--agy-dangerously-skip-permissions", "--agy-no-sandbox", "--scenario", "file-code",
                         "--scenario", "code-repair", "--timeout-seconds", "90", "--json"])
        self.assertEqual(code, 0)
        run.assert_called_once_with(["antigravity_exec"], scenarios=["file-code", "code-repair"], live=True,
            output_parent=None, timeout_seconds=90, model=None, agy_dangerously_skip_permissions=True,
            agy_no_sandbox=True)

    def test_cli_rejects_sandbox_off_without_separate_scoped_grants_before_evidence(self):
        options = [
            [], ["--live"], ["--live", "--runner", "antigravity_exec", "--scenario", "file-code"],
            ["--runner", "antigravity_exec", "--agy-dangerously-skip-permissions", "--scenario", "file-code"],
            ["--live", "--agy-dangerously-skip-permissions", "--scenario", "file-code"],
            ["--live", "--runner", "codex_exec", "--agy-dangerously-skip-permissions", "--scenario", "file-code"],
            ["--live", "--runner", "antigravity_exec", "--runner", "codex_exec",
             "--agy-dangerously-skip-permissions", "--scenario", "file-code"],
            ["--live", "--runner", "antigravity_exec", "--agy-dangerously-skip-permissions"],
        ]
        options.extend(["--live", "--runner", "antigravity_exec", "--agy-dangerously-skip-permissions",
                        "--scenario", "file-code", "--scenario", scenario]
                       for scenario in set(SCENARIOS) - CODE_SCENARIOS)
        for flags in options:
            with self.subTest(flags=flags), \
                    patch("subprocess.Popen", side_effect=AssertionError("No process")), \
                    patch("researchops.runner_tool_validation.tempfile.mkdtemp", side_effect=AssertionError("No evidence")), \
                    patch("researchops.cli.main.load_settings", side_effect=AssertionError("No config")), \
                    patch("researchops.cli.main.create_application_service", side_effect=AssertionError("No app")), \
                    redirect_stderr(io.StringIO()) as error:
                code = main(["validate-runner-tools", "--agy-no-sandbox", "--output-parent", str(self.root), *flags])
            self.assertEqual(code, 2)
            self.assertIn("requires --live", error.getvalue())
            self.assertEqual(list(self.root.iterdir()), [])

    def test_cli_rejects_unscoped_permission_grant_without_model_or_evidence(self):
        for options in ([], ["--runner", "antigravity_exec"], ["--live"],
                        ["--live", "--runner", "codex_exec"],
                        ["--live", "--runner", "antigravity_exec", "--runner", "codex_exec"]):
            with self.subTest(options=options), \
                    patch("subprocess.Popen", side_effect=AssertionError("No process")), \
                    patch("researchops.cli.main.load_settings", side_effect=AssertionError("No config")), \
                    redirect_stderr(io.StringIO()) as error:
                code = main(["validate-runner-tools", "--agy-dangerously-skip-permissions",
                             "--output-parent", str(self.root), *options])
            self.assertEqual(code, 2)
            self.assertIn("requires --live and only --runner antigravity_exec", error.getvalue())
            self.assertEqual(list(self.root.iterdir()), [])

    def test_operator_config_cli_option_is_rejected_before_model_or_application(self):
        with patch("researchops.runner_tool_validation.validate_runner_tools", side_effect=AssertionError("No suite")), \
                patch("researchops.cli.main.load_settings", side_effect=AssertionError("No config")), \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["validate-runner-tools", "--config", "/not-used/operator.yaml", "--live"])
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_options_create_no_evidence_and_never_spawn(self):
        options = [{"runners": []}, {"runners": ["fake"]}, {"scenarios": []}, {"scenarios": ["../private"]},
                   {"live": "yes"}, {"timeout_seconds": 0}, {"timeout_seconds": 301}, {"timeout_seconds": True},
                   {"model": "not-approved"}, {"live": True, "model": "-flag"}, {"live": True, "model": "bad\nmodel"}]
        with patch("subprocess.Popen", side_effect=AssertionError("No model")):
            for values in options:
                with self.subTest(values=values), self.assertRaises(ValueError):
                    validate_runner_tools(output_parent=self.root, **values)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_permission_grant_requires_boolean_live_and_explicit_antigravity_only(self):
        options = [
            {"agy_dangerously_skip_permissions": True},
            {"runners": ["antigravity_exec"], "agy_dangerously_skip_permissions": True},
            {"live": True, "agy_dangerously_skip_permissions": True},
            {"live": True, "runners": ["codex_exec"], "agy_dangerously_skip_permissions": True},
            {"live": True, "runners": ["antigravity_exec", "codex_exec"], "agy_dangerously_skip_permissions": True},
        ]
        options.extend({"live": True, "runners": ["antigravity_exec"],
                        "agy_dangerously_skip_permissions": value} for value in (None, 0, 1, "true", [], {}))
        with patch("subprocess.Popen", side_effect=AssertionError("No process")), \
                patch("researchops.runner_tool_validation.tempfile.mkdtemp", side_effect=AssertionError("No evidence")):
            for values in options:
                with self.subTest(values=values), self.assertRaises(ValueError):
                    validate_runner_tools(output_parent=self.root, **values)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_sandbox_off_api_requires_boolean_and_all_explicit_scoped_grants(self):
        approved = {"live": True, "runners": ["antigravity_exec"], "scenarios": ["file-code"],
                    "agy_dangerously_skip_permissions": True, "agy_no_sandbox": True}
        options = [
            {**approved, "live": False},
            {**approved, "agy_dangerously_skip_permissions": False},
            {**approved, "runners": None},
            {**approved, "runners": ["codex_exec"]},
            {**approved, "runners": ["antigravity_exec", "codex_exec"]},
            {**approved, "scenarios": None},
            {**approved, "scenarios": []},
        ]
        options.extend({**approved, "agy_no_sandbox": value} for value in (None, 0, 1, "true", [], {}))
        for scenario in set(SCENARIOS) - CODE_SCENARIOS:
            options.extend(({**approved, "scenarios": [scenario]},
                            {**approved, "scenarios": ["file-code", scenario]}))
        with patch("subprocess.Popen", side_effect=AssertionError("No process")), \
                patch("researchops.runner_tool_validation.tempfile.mkdtemp", side_effect=AssertionError("No evidence")):
            for values in options:
                with self.subTest(values=values), self.assertRaises(ValueError):
                    validate_runner_tools(output_parent=self.root, **values)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_sandbox_off_api_forwards_each_bundled_code_scenario_without_expanding_selection(self):
        selected = sorted(CODE_SCENARIOS)

        def one(provider, scenario, root, **kwargs):
            return {"runner": provider, "scenario": scenario, "status": "passed", "cli_invocations": 0,
                    "model_invocations": 0, "terminal_turns": 0}

        with patch("researchops.runner_tool_validation._one_case", side_effect=one) as invoke, \
                patch("subprocess.Popen", side_effect=AssertionError("No real process")):
            report = validate_runner_tools(["antigravity_exec"], scenarios=selected, live=True,
                output_parent=self.root, agy_dangerously_skip_permissions=True, agy_no_sandbox=True)
        self.assertEqual(invoke.call_count, len(selected))
        self.assertEqual([call.args[1] for call in invoke.call_args_list], selected)
        self.assertTrue(all(call.kwargs["agy_no_sandbox"] is True for call in invoke.call_args_list))
        self.assertTrue(all(call.kwargs["agy_dangerously_skip_permissions"] is True for call in invoke.call_args_list))
        self.assertTrue(report["agy_no_sandbox_approved"])
        self.assertFalse(report["production_runner_ready"])

    def test_codex_build_settings_are_ephemeral_scoped_and_not_permission_bypass(self):
        work, control = self.directories()
        argv, stdin, prompt = build_tool_command("codex_exec", "/mock/codex", "combined-research", work, control,
                                                timeout_seconds=90, model="chosen-model")
        self.assertEqual(stdin, prompt.encode())
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--ignore-rules", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        self.assertNotIn("--sandbox=false", argv)
        self.assertEqual(argv[argv.index("--cd") + 1], str(work))
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        settings = {}
        for index, value in enumerate(argv[:-1]):
            if value == "-c":
                key = argv[index + 1].split("=", 1)[0]
                settings[key] = tomllib.loads("value=" + argv[index + 1].split("=", 1)[1])["value"]
        self.assertEqual(settings["approval_policy"], "never")
        self.assertFalse(settings["sandbox_workspace_write.network_access"])
        self.assertEqual(settings["shell_environment_policy.inherit"], "none")
        self.assertEqual(settings["web_search"], "live")
        self.assertEqual(settings["tools.web_search.allowed_domains"], ["docs.python.org"])
        self.assertEqual(set(settings["mcp_servers"]), {"researchops_probe"})
        server = settings["mcp_servers"]["researchops_probe"]
        self.assertEqual(server["enabled_tools"], ["search_documents", "fetch_document"])
        self.assertIn(str(control / "mcp-audit.jsonl"), server["args"])
        self.assertNotIn("--nonce", server["args"])
        self.assertNotIn("OPENAI_API_KEY", server.get("env", {}))
        self.assertFalse((work / ".agents").exists())

    def test_antigravity_uses_project_local_mcp_and_preserves_native_permission_modes(self):
        work, control = self.directories()
        argv, stdin, _ = build_tool_command("antigravity_exec", "/mock/agy", "mcp-research", work, control,
                                            timeout_seconds=90)
        self.assertIsNone(stdin)
        self.assertNotIn("--mode", argv)
        self.assertIn("--disable-slash-commands", argv)
        self.assertIn("--sandbox", argv)
        self.assertNotIn("--sandbox=false", argv)
        self.assertNotIn("--yolo", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        config = json.loads((work / ".agents" / "mcp_config.json").read_text())
        self.assertEqual(set(config["mcpServers"]), {"researchops_probe"})
        self.assertFalse((self.root / ".agents").exists())

    def test_antigravity_permission_grant_is_one_argv_flag_and_keeps_sandbox(self):
        work, control = self.directories()
        argv, stdin, prompt = build_tool_command("antigravity_exec", "/mock/agy", "mcp-research", work, control,
            timeout_seconds=90, model="chosen-model", agy_dangerously_skip_permissions=True)
        self.assertIsNone(stdin)
        self.assertEqual(argv.count("--dangerously-skip-permissions"), 1)
        self.assertEqual(argv.count("--sandbox"), 1)
        self.assertNotIn("--sandbox=false", argv)
        self.assertNotIn("--no-sandbox", argv)
        self.assertNotIn("--mode", argv)
        self.assertEqual(argv[-2:], ["--print", prompt])
        self.assertNotIn("--dangerously-skip-permissions", prompt)
        self.assertEqual(argv[argv.index("--model") + 1], "chosen-model")
        config = json.loads((work / ".agents" / "mcp_config.json").read_text())
        self.assertEqual(set(config), {"mcpServers"})
        self.assertEqual(set(config["mcpServers"]), {"researchops_probe"})

    def test_explicit_sandbox_off_command_is_per_invocation_and_preserves_other_controls(self):
        for scenario in sorted(CODE_SCENARIOS):
            work, control = self.root / (scenario + "-workspace"), self.root / (scenario + "-control")
            work.mkdir(mode=0o700)
            control.mkdir(mode=0o700)
            with self.subTest(scenario=scenario):
                argv, stdin, prompt = build_tool_command("antigravity_exec", "/mock/agy", scenario, work, control,
                    timeout_seconds=90, model="chosen-model", agy_dangerously_skip_permissions=True, agy_no_sandbox=True)
                self.assertIsNone(stdin)
                self.assertEqual(argv.count("--sandbox=false"), 1)
                self.assertNotIn("--sandbox", argv)
                self.assertNotIn("--no-sandbox", argv)
                self.assertEqual(argv.count("--dangerously-skip-permissions"), 1)
                self.assertIn("--disable-slash-commands", argv)
                self.assertEqual(argv[argv.index("--add-dir") + 1], str(work))
                self.assertEqual(argv[argv.index("--print-timeout") + 1], "90s")
                self.assertEqual(argv[argv.index("--log-file") + 1], str(control / "control.log"))
                self.assertEqual(argv[argv.index("--json-schema") + 1], str(control / "response-schema.json"))
                self.assertEqual(argv[-2:], ["--print", prompt])
                self.assertIn("WITHOUT the native terminal sandbox", prompt)
                self.assertIn("this session only", prompt)
                self.assertIn("inside the task workspace", prompt)
                self.assertIn("do not retry the known write_to_file", prompt)
                self.assertNotIn("Never request unsandboxed execution", prompt)
                self.assertIn("Never read credentials", prompt)
                self.assertIn("bypass permission denials", prompt)

    def test_default_code_prompt_keeps_native_sandbox_without_implicit_fallback(self):
        work, control = self.directories()
        for provider, grant in (("codex_exec", False), ("antigravity_exec", False), ("antigravity_exec", True)):
            with self.subTest(provider=provider, grant=grant):
                argv, _, prompt = build_tool_command(provider, "/mock/client", "file-code", work, control,
                    timeout_seconds=90, agy_dangerously_skip_permissions=grant)
                self.assertIn("--sandbox", argv)
                self.assertNotIn("--sandbox=false", argv)
                self.assertIn("Never request unsandboxed execution or retry outside the native sandbox", prompt)
                self.assertNotIn("WITHOUT the native terminal sandbox", prompt)

    def test_build_rejects_invalid_sandbox_off_combinations_before_any_file_write(self):
        work, control = self.directories()
        options = [("codex_exec", "file-code", False, True), ("codex_exec", "file-code", True, True),
                   ("antigravity_exec", "file-code", False, True)]
        options.extend(("antigravity_exec", "file-code", True, value) for value in (None, 0, 1, "true", [], {}))
        options.extend(("antigravity_exec", scenario, True, True) for scenario in set(SCENARIOS) - CODE_SCENARIOS)
        for provider, scenario, grant, no_sandbox in options:
            with self.subTest(provider=provider, scenario=scenario, grant=grant, no_sandbox=no_sandbox), self.assertRaises(ValueError):
                build_tool_command(provider, "/mock/client", scenario, work, control, timeout_seconds=90,
                    agy_dangerously_skip_permissions=grant, agy_no_sandbox=no_sandbox)
            self.assertEqual(list(work.iterdir()), [])
            self.assertEqual(list(control.iterdir()), [])

    def test_build_rejects_cross_provider_or_nonboolean_grant_before_any_file_write(self):
        work, control = self.directories()
        options = [("codex_exec", True), ("antigravity_exec", None), ("antigravity_exec", 1),
                   ("antigravity_exec", "true"), ("antigravity_exec", {})]
        for provider, grant in options:
            with self.subTest(provider=provider, grant=grant), self.assertRaises(ValueError):
                build_tool_command(provider, "/mock/client", "mcp-research", work, control,
                                   timeout_seconds=90, agy_dangerously_skip_permissions=grant)
            self.assertEqual(list(work.iterdir()), [])
            self.assertEqual(list(control.iterdir()), [])

    def test_antigravity_permission_mode_and_explicit_grant_are_recorded_per_case(self):
        for grant, mode in ((False, "request-review"), (True, "always-proceed")):
            def execute(argv, *, cwd, **kwargs):
                if argv[0] == "/mock/git":
                    return ProcessResult(0, b"", b"", True, True)
                self.assertEqual("--dangerously-skip-permissions" in argv, grant)
                self.assertIn("--sandbox", argv)
                (cwd / "note.txt").write_text("서울 연구 도구 검증\n", encoding="utf-8")
                return ProcessResult(0, agy_file_trace(cwd, permission_mode=mode), b"", True, True)
            with self.subTest(grant=grant), \
                    patch("researchops.runner_tool_validation.shutil.which", side_effect=lambda name: "/mock/" + name), \
                    patch("researchops.runner_tool_validation.control_environment", return_value={}), \
                    patch("researchops.runner_tool_validation.run_bounded", side_effect=execute) as invoke:
                report = validate_runner_tools(["antigravity_exec"], scenarios=["file-write"], live=True,
                    output_parent=self.root, agy_dangerously_skip_permissions=grant)
            self.assertEqual(invoke.call_count, 2)  # Isolated git setup, then one mocked model CLI.
            self.assertEqual(report["totals"], {"passed": 1, "failed": 0, "blocked": 0}, report["cases"])
            self.assertEqual(report["cli_invocations"], 1)
            self.assertEqual(report["agy_dangerously_skip_permissions_approved"], grant)
            self.assertFalse(report["agy_no_sandbox_approved"])
            self.assertFalse(report["production_runner_ready"])
            case = report["cases"][0]
            self.assertEqual(case["agy_dangerously_skip_permissions_approved"], grant)
            self.assertFalse(case["agy_no_sandbox_approved"])
            self.assertTrue(case["requested_terminal_sandbox"])
            self.assertIsNone(case["effective_terminal_sandbox"])
            self.assertEqual(case["requested_permission_mode"], mode)
            self.assertEqual(case["effective_permission_mode"], mode)
            self.assertTrue(case["checks"]["permission_mode_matches_request"])
            self.assertEqual(case["effective_cwd"], str(Path(case["evidence_dir"]) / "workspace"))
            self.assertTrue(case["checks"]["working_directory_matches_request"])

    def test_sandbox_request_and_approval_are_recorded_without_attesting_effective_isolation(self):
        for no_sandbox in (False, True):
            def execute(argv, *, cwd, **kwargs):
                if argv[0] == "/mock/git":
                    return ProcessResult(0, b"", b"", True, True)
                self.assertEqual("--sandbox=false" in argv, no_sandbox)
                self.assertEqual("--sandbox" in argv, not no_sandbox)
                self.assertIn("--dangerously-skip-permissions", argv)
                write_code_fixture(cwd)
                events = [json.loads(line) for line in agy_file_trace(cwd, permission_mode="always-proceed").splitlines()]
                command = {"event": "step_update", "step_update": {"step_index": 1,
                    "step_type": "tool", "state": "DONE", "tool_name": "run_command",
                    "tool_info": {"name": "run_command", "parameters": {}, "output": "Ran 3 tests in 0.001s\nOK\n"}}}
                return ProcessResult(0, encode_events([events[0], command, events[-1]]), b"", True, True)

            with self.subTest(no_sandbox=no_sandbox), \
                    patch("researchops.runner_tool_validation.shutil.which", side_effect=lambda name: "/mock/" + name), \
                    patch("researchops.runner_tool_validation.control_environment", return_value={}), \
                    patch("researchops.runner_tool_validation.run_bounded", side_effect=execute) as invoke:
                report = validate_runner_tools(["antigravity_exec"], scenarios=["file-code"], live=True,
                    output_parent=self.root, agy_dangerously_skip_permissions=True, agy_no_sandbox=no_sandbox)
            self.assertEqual(invoke.call_count, 2)
            self.assertEqual(report["totals"], {"passed": 1, "failed": 0, "blocked": 0}, report["cases"])
            self.assertEqual(report["agy_no_sandbox_approved"], no_sandbox)
            self.assertFalse(report["operator_config_loaded"])
            self.assertFalse(report["production_runner_ready"])
            self.assertEqual(report["smtp_send_attempts"], 0)
            case = report["cases"][0]
            self.assertEqual(case["agy_no_sandbox_approved"], no_sandbox)
            self.assertEqual(case["requested_terminal_sandbox"], not no_sandbox)
            self.assertIsNone(case["effective_terminal_sandbox"])
            self.assertEqual(case["effective_permission_mode"], "always-proceed")
            self.assertTrue(case["checks"]["permission_mode_matches_request"])
            self.assertTrue(case["checks"]["working_directory_matches_request"])
            self.assertEqual(json.loads(Path(report["report_path"]).read_text()), report)

    def test_sandbox_off_never_suppresses_observed_denial_or_retries_blocked_code(self):
        def execute(argv, *, cwd, **kwargs):
            if argv[0] == "/mock/git":
                return ProcessResult(0, b"", b"", True, True)
            self.assertIn("--sandbox=false", argv)
            write_code_fixture(cwd)
            events = [json.loads(line) for line in agy_file_trace(cwd, permission_mode="always-proceed").splitlines()]
            events[-1]["result"]["denied_actions"] = [{"action": "command"}]
            command = {"event": "step_update", "step_update": {"step_index": 1,
                "step_type": "tool", "state": "DONE", "tool_name": "run_command",
                "tool_info": {"name": "run_command", "parameters": {}, "output": "Ran 3 tests in 0.001s\nOK\n"}}}
            return ProcessResult(0, encode_events([events[0], command, events[-1]]), b"", True, True)

        with patch("researchops.runner_tool_validation.shutil.which", side_effect=lambda name: "/mock/" + name), \
                patch("researchops.runner_tool_validation.control_environment", return_value={}), \
                patch("researchops.runner_tool_validation.run_bounded", side_effect=execute) as invoke:
            report = validate_runner_tools(["antigravity_exec"], scenarios=["file-code", "code-repair"], live=True,
                output_parent=self.root, agy_dangerously_skip_permissions=True, agy_no_sandbox=True)
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(report["totals"], {"passed": 0, "failed": 0, "blocked": 2})
        case = report["cases"][0]
        self.assertTrue(case["agy_no_sandbox_approved"])
        self.assertTrue(case["permission_denied"])
        self.assertFalse(case["checks"]["no_permission_denials"])
        self.assertFalse(case["checks"]["native_code_execution"])
        self.assertTrue(case["tools"][0]["provider_reported_success"])
        self.assertFalse(case["tools"][0]["completed_successfully"])
        self.assertEqual(report["cases"][1]["blocked_by"], ["NATIVE-TOOL-PERMISSION-REQUIRED"])

    def test_permission_mode_mismatch_fails_despite_tools_artifact_and_success_terminal(self):
        for grant, mode in ((True, "request-review"), (False, "always-proceed"), (True, None), (False, None)):
            def execute(argv, *, cwd, **kwargs):
                if argv[0] == "/mock/git":
                    return ProcessResult(0, b"", b"", True, True)
                (cwd / "note.txt").write_text("서울 연구 도구 검증\n", encoding="utf-8")
                return ProcessResult(0, agy_file_trace(cwd, permission_mode=mode), b"", True, True)
            with self.subTest(grant=grant, mode=mode), \
                    patch("researchops.runner_tool_validation.shutil.which", side_effect=lambda name: "/mock/" + name), \
                    patch("researchops.runner_tool_validation.control_environment", return_value={}), \
                    patch("researchops.runner_tool_validation.run_bounded", side_effect=execute) as invoke:
                report = validate_runner_tools(["antigravity_exec"], scenarios=["file-write", "file-code"], live=True,
                    output_parent=self.root, agy_dangerously_skip_permissions=grant)
            self.assertEqual(invoke.call_count, 2)
            self.assertEqual(report["totals"], {"passed": 0, "failed": 1, "blocked": 1})
            self.assertEqual(report["model_invocations"], 1)
            self.assertEqual(report["terminal_turns"], 1)
            checks = report["cases"][0]["checks"]
            self.assertFalse(checks["permission_mode_matches_request"])
            self.assertTrue(all(value for name, value in checks.items() if name != "permission_mode_matches_request"))
            self.assertEqual(report["cases"][1]["blocked_by"], ["PERMISSION-MODE-MISMATCH"])

    def test_wrong_or_missing_effective_workspace_fails_and_holds_remaining_cases(self):
        for effective_cwd in (None, "/outside/private-workspace", "."):
            def execute(argv, *, cwd, **kwargs):
                if argv[0] == "/mock/git":
                    return ProcessResult(0, b"", b"", True, True)
                (cwd / "note.txt").write_text("서울 연구 도구 검증\n", encoding="utf-8")
                events = [json.loads(line) for line in agy_file_trace(cwd, permission_mode="always-proceed").splitlines()]
                if effective_cwd is None:
                    events[0]["init"].pop("cwd")
                else:
                    events[0]["init"]["cwd"] = effective_cwd
                return ProcessResult(0, encode_events(events), b"", True, True)
            with self.subTest(effective_cwd=effective_cwd), \
                    patch("researchops.runner_tool_validation.shutil.which", side_effect=lambda name: "/mock/" + name), \
                    patch("researchops.runner_tool_validation.control_environment", return_value={}), \
                    patch("researchops.runner_tool_validation.run_bounded", side_effect=execute) as invoke:
                report = validate_runner_tools(["antigravity_exec"], scenarios=["file-write", "file-code"], live=True,
                    output_parent=self.root, agy_dangerously_skip_permissions=True)
            self.assertEqual(invoke.call_count, 2)
            self.assertEqual(report["totals"], {"passed": 0, "failed": 1, "blocked": 1})
            self.assertEqual(report["cases"][0]["effective_cwd"], effective_cwd)
            checks = report["cases"][0]["checks"]
            self.assertFalse(checks["working_directory_matches_request"])
            self.assertTrue(all(value for name, value in checks.items() if name != "working_directory_matches_request"))
            self.assertEqual(report["cases"][1]["blocked_by"], ["WORKSPACE-MISMATCH"])

    def test_antigravity_web_requires_successful_page_read_as_well_as_search(self):
        work, control = self.directories()
        response = {"timezone": "Asia/Seoul", "sources": ["https://docs.python.org/3/library/csv.html"],
                    "findings": ["A controlled csv finding.", "A second controlled finding."]}
        for read_success in (None, False, True):
            tools = [{"name": "search_web", "success": True, "details": {}}]
            if read_success is not None:
                tools.append({"name": "read_url_content", "success": read_success,
                              "details": {"parameters": {"Url": response["sources"][0]}}})
            trace = ToolTrace(True, True, response, tuple(tools), False, {}, 4)
            checks, _ = evaluate_tool_case("antigravity_exec", "web-search", work, control,
                                           ProcessResult(0, b"", b"", True, True), trace)
            with self.subTest(read_success=read_success):
                self.assertTrue(checks["native_web_search"])
                self.assertTrue(checks["official_web_sources"])
                self.assertEqual(checks["native_web_page_read"], read_success is True)
                self.assertEqual(all(checks.values()), read_success is True)

    def test_antigravity_web_rejects_unapproved_observed_page_targets_even_on_failed_reads(self):
        work, control = self.directories()
        response = {"timezone": "Asia/Seoul", "sources": ["https://docs.python.org/3/library/csv.html"],
                    "findings": ["First controlled finding.", "Second controlled finding."]}
        invalid_urls = [None, True, 1, [], {}, "", "http://docs.python.org/3/library/csv.html",
            "file:///private-fixture", "https://private-fixture.example/csv", "https://docs.python.org.evil.example/csv",
            "https://private-fixture@docs.python.org/csv", "https://user:private-fixture@docs.python.org/csv",
            "https://@docs.python.org/3/library/csv.html", "https://:@docs.python.org/3/library/csv.html",
            "https://docs.python.org:80/csv", "https://docs.python.org:444/csv", "https://docs.python.org:99999/csv",
            "https://docs.python.org:private-fixture/csv", "https://docs.python.org/csv\nprivate-fixture",
            "\thttps://docs.python.org/3/library/csv.html", "https://docs.python.org/\x00private-fixture",
            "https://docs.python.org/3/library/csv.html\x7f"]
        for url in invalid_urls:
            for success in (False, True):
                tools = ({"name": "search_web", "success": True, "details": {}},
                         {"name": "read_url_content", "success": success, "details": {"parameters": {"Url": url}}})
                trace = ToolTrace(True, True, response, tools, False, {}, 4)
                with self.subTest(url=url, success=success):
                    checks, _ = evaluate_tool_case("antigravity_exec", "web-search", work, control,
                                                   ProcessResult(0, b"", b"", True, True), trace)
                    self.assertFalse(checks["observed_web_targets_scoped"])
                    self.assertFalse(checks["native_web_page_read"])
                    self.assertNotIn("private-fixture", json.dumps(checks))

    def test_antigravity_web_malformed_read_parameters_fail_closed(self):
        work, control = self.directories()
        response = {"timezone": "Asia/Seoul", "sources": ["https://docs.python.org/3/library/csv.html"],
                    "findings": ["First controlled finding.", "Second controlled finding."]}
        for parameters in (None, [], "private-fixture", {}, {"url": response["sources"][0]}):
            trace = ToolTrace(True, True, response, (
                {"name": "search_web", "success": True, "details": {}},
                {"name": "read_url_content", "success": True, "details": {"parameters": parameters}},
            ), False, {}, 4)
            with self.subTest(parameters=parameters):
                checks, _ = evaluate_tool_case("antigravity_exec", "web-search", work, control,
                                               ProcessResult(0, b"", b"", True, True), trace)
                self.assertFalse(checks["observed_web_targets_scoped"])
                self.assertFalse(checks["native_web_page_read"])

    def test_antigravity_web_successful_read_requires_matching_normalized_cited_url(self):
        work, control = self.directories()
        cases = [
            ("https://docs.python.org/3/library/csv.html", "https://docs.python.org/3/library/csv.html#reader-objects", True),
            ("https://docs.python.org:443/3/library/csv.html", "https://docs.python.org:443/3/library/csv.html", True),
            ("https://docs.python.org/3/library/csv.html", "https://docs.python.org/3/library/decimal.html", False),
            ("https://docs.python.org/3/library/csv.html?view=one", "https://docs.python.org/3/library/csv.html?view=two", False),
        ]
        for fetched, cited, matches in cases:
            response = {"timezone": "Asia/Seoul", "sources": [cited],
                        "findings": ["First controlled finding.", "Second controlled finding."]}
            trace = ToolTrace(True, True, response, (
                {"name": "search_web", "success": True, "details": {}},
                {"name": "read_url_content", "success": True, "details": {"parameters": {"Url": fetched}}},
            ), False, {}, 4)
            with self.subTest(fetched=fetched, cited=cited):
                checks, _ = evaluate_tool_case("antigravity_exec", "web-search", work, control,
                                               ProcessResult(0, b"", b"", True, True), trace)
                self.assertTrue(checks["observed_web_targets_scoped"])
                self.assertEqual(checks["native_web_page_read"], matches)
                self.assertEqual(all(checks.values()), matches)

    def web_spill_fixture(self):
        work, control = self.directories()
        identity = "11111111-1111-4111-8111-111111111111"
        spill = Path.home() / ".gemini/antigravity-cli/brain" / identity / ".system_generated/steps/4/content.md"
        source = "https://docs.python.org/3/library/csv.html"
        response = {"timezone": "Asia/Seoul", "sources": [source], "findings": ["First finding.", "Second finding."]}
        search = {"name": "search_web", "success": True, "step_index": 2, "details": {}}
        fetch = {"name": "read_url_content", "success": True, "step_index": 4,
                 "details": {"parameters": {"Url": source}}}
        read = {"name": "view_file", "success": True, "step_index": 5,
                "details": {"parameters": {"AbsolutePath": str(spill)}}}
        return work, control, identity, spill, response, search, fetch, read

    def test_web_spill_accepts_only_current_preceding_official_page_content_read(self):
        work, control, identity, spill, response, search, fetch, read = self.web_spill_fixture()
        trace = ToolTrace(True, True, response, (search, fetch, read), False, {}, 5, conversation_id=identity)
        checks, _ = evaluate_tool_case("antigravity_exec", "web-search", work, control,
            ProcessResult(0, b"", b"", True, True), trace)
        self.assertTrue(checks["observed_file_targets_scoped"])
        self.assertTrue(checks["only_scenario_tools"])
        self.assertTrue(checks["native_web_page_read"])
        self.assertTrue(all(checks.values()), checks)
        for scenario in ("web-search", "combined-research"):
            _, _, prompt = build_tool_command("antigravity_exec", "/mock/agy", scenario, work, control,
                timeout_seconds=90, agy_dangerously_skip_permissions=True)
            self.assertIn(".system_generated/steps/<step>/content.md", prompt)
            self.assertIn("current-conversation, already-completed page result", prompt)

    def test_web_spill_rejects_other_conversations_steps_files_and_future_calls(self):
        work, control, identity, spill, response, search, fetch, read = self.web_spill_fixture()
        paths = [str(spill).replace(identity, "22222222-2222-4222-8222-222222222222"),
                 str(spill.parent.parent / "6/content.md"), str(spill.with_name("output.txt")),
                 str(spill.with_name("private.txt")), str(spill.parents[3] / "scratch/private.txt")]
        variants = []
        for path in paths:
            changed = copy.deepcopy(read)
            changed["details"]["parameters"]["AbsolutePath"] = path
            variants.append((path, (search, fetch, changed), identity))
        variants.extend((("missing-conversation", (search, fetch, read), None),
                         ("future-call", (search, read, fetch), identity),
                         ("unobserved-call", (search, read), identity)))
        for label, tools, conversation in variants:
            trace = ToolTrace(True, True, response, tools, False, {}, 5, conversation_id=conversation)
            with self.subTest(label=label):
                checks, _ = evaluate_tool_case("antigravity_exec", "web-search", work, control,
                    ProcessResult(0, b"", b"", True, True), trace)
                self.assertFalse(checks["observed_file_targets_scoped"])
        for step in (None, True, -1, "4"):
            changed = copy.deepcopy(fetch)
            changed["step_index"] = step
            trace = ToolTrace(True, True, response, (search, changed, read), False, {}, 5, conversation_id=identity)
            with self.subTest(step=step):
                checks, _ = evaluate_tool_case("antigravity_exec", "web-search", work, control,
                    ProcessResult(0, b"", b"", True, True), trace)
                self.assertFalse(checks["observed_file_targets_scoped"])

    def test_web_spill_requires_successful_fetch_with_safe_official_url(self):
        work, control, identity, spill, response, search, fetch, read = self.web_spill_fixture()
        variants = []
        failed = copy.deepcopy(fetch)
        failed["success"] = False
        variants.append(("failed-fetch", failed))
        urls = [None, [], {}, "file:///private-fixture", "http://docs.python.org/3/library/csv.html",
                "https://docs.python.org.evil.example/csv", "https://private-fixture@docs.python.org/csv",
                "https://docs.python.org:444/csv", "https://docs.python.org/csv\nprivate-fixture"]
        for url in urls:
            changed = copy.deepcopy(fetch)
            changed["details"]["parameters"]["Url"] = url
            variants.append((url, changed))
        for parameters in (None, [], "private-fixture", {}):
            changed = copy.deepcopy(fetch)
            changed["details"]["parameters"] = parameters
            variants.append((parameters, changed))
        for label, changed in variants:
            trace = ToolTrace(True, True, response, (search, changed, read), False, {}, 5, conversation_id=identity)
            with self.subTest(label=label):
                checks, _ = evaluate_tool_case("antigravity_exec", "web-search", work, control,
                    ProcessResult(0, b"", b"", True, True), trace)
                self.assertFalse(checks["observed_file_targets_scoped"])
                self.assertFalse(checks["native_web_page_read"])
                self.assertNotIn("private-fixture", json.dumps(checks))

    def test_web_spill_does_not_grant_writes_directory_reads_or_nonweb_scope(self):
        work, control, identity, spill, response, search, fetch, read = self.web_spill_fixture()
        for name, field in (("write_to_file", "TargetFile"), ("replace_file_content", "TargetFile"),
                            ("multi_replace_file_content", "TargetFile"), ("list_dir", "DirectoryPath"),
                            ("find_by_name", "SearchDirectory"), ("grep_search", "SearchPath")):
            action = {"name": name, "success": True, "step_index": 5,
                      "details": {"parameters": {field: str(spill)}}}
            trace = ToolTrace(True, True, response, (search, fetch, action), False, {}, 5, conversation_id=identity)
            with self.subTest(action=name):
                checks, _ = evaluate_tool_case("antigravity_exec", "web-search", work, control,
                    ProcessResult(0, b"", b"", True, True), trace)
                self.assertFalse(checks["observed_file_targets_scoped"])
                self.assertFalse(checks["only_scenario_tools"])
        for provider, scenario in (("antigravity_exec", "file-write"), ("antigravity_exec", "mcp-public-docs"),
                                   ("antigravity_exec", "mcp-research"), ("codex_exec", "web-search")):
            trace = ToolTrace(True, True, response, (search, fetch, read), False, {}, 5, conversation_id=identity)
            with self.subTest(provider=provider, scenario=scenario):
                checks, _ = evaluate_tool_case(provider, scenario, work, control,
                    ProcessResult(0, b"", b"", True, True), trace)
                self.assertFalse(checks["observed_file_targets_scoped"])

    def test_antigravity_code_scenario_accepts_native_shell_file_creation_with_verified_artifacts(self):
        work, control = self.directories()
        (work / "sales.csv").write_text(SOURCE_CSV, encoding="utf-8")
        write_code_fixture(work)
        trace = ToolTrace(True, True, {"timezone": "Asia/Seoul"}, (
            {"name": "run_command", "success": True, "details": {"output": "Ran 3 tests in 0.001s\nOK\n"}},
        ), False, {}, 4)
        checks, _ = evaluate_tool_case("antigravity_exec", "file-code", work, control,
                                       ProcessResult(0, b"", b"", True, True), trace)
        self.assertTrue(checks["native_file_action"])
        self.assertTrue(all(checks.values()), checks)
        (work / "analysis.json").unlink()
        checks, _ = evaluate_tool_case("antigravity_exec", "file-code", work, control,
                                       ProcessResult(0, b"", b"", True, True), trace)
        self.assertFalse(checks["expected_artifacts_present"])
        self.assertFalse(checks["analysis_results"])

    def test_repair_accepts_native_agy_done_failure_output_before_later_passing_tests(self):
        work, control = self.directories()
        (work / "metrics.py").write_text("def percent_change(before, after):\n    return None if before == 0 else (after - before) / before * 100\n")
        (work / "test_metrics.py").write_text("# Controlled evaluator fixture, no real command or model invocation.\n")
        (work / "repair.json").write_text(json.dumps({"positive": 20, "negative": -50, "zero": None}))
        events = [{"event": "init", "init": {"cwd": str(work), "permission_mode": "always-proceed"}}]
        for index, output in enumerate((REPAIR_FAILED, REPAIR_PASSED), start=1):
            events.append({"event": "step_update", "step_update": {"step_index": index, "step_type": "tool",
                "state": "DONE", "tool_name": "run_command", "tool_info": repair_tool(output)["details"]}})
        events.append({"event": "result", "result": {"status": "SUCCESS", "structured_output": {
            "response_json": json.dumps({"timezone": "Asia/Seoul", "summary": "Controlled repair fixture."})}}})
        trace = parse_tool_trace("antigravity_exec", encode_events(events))
        self.assertTrue(trace.tools[0]["provider_reported_success"])
        self.assertTrue(trace.tools[0]["success"])  # DONE remains provider completion, not unittest success.
        checks, _ = evaluate_tool_case("antigravity_exec", "code-repair", work, control,
            ProcessResult(0, b"", b"", True, True), trace)
        self.assertTrue(checks["native_failure_reproduced"])
        self.assertTrue(checks["native_repair_test_sequence"])
        self.assertTrue(all(checks.values()), checks)

    def test_repair_sequence_requires_observed_failure_then_later_success(self):
        cases = [
            ("failure-then-pass", [REPAIR_FAILED, REPAIR_PASSED], (True, True)),
            ("pass-before-failure", [REPAIR_PASSED, REPAIR_FAILED], (True, False)),
            ("failure-only", [REPAIR_FAILED], (True, False)),
            ("pass-only", [REPAIR_PASSED], (False, False)),
            ("later-failure-reopens", [REPAIR_FAILED, REPAIR_PASSED, REPAIR_FAILED], (True, False)),
            ("later-malformed-result-reopens", [REPAIR_FAILED, REPAIR_PASSED, "private-fixture"], (True, False)),
            ("missing-native-tools", [], (False, False)),
        ]
        for label, outputs, expected in cases:
            trace = ToolTrace(True, True, {"timezone": "Asia/Seoul", "summary": REPAIR_FAILED + REPAIR_PASSED},
                tuple(repair_tool(output) for output in outputs), False, {}, 5)
            with self.subTest(label=label):
                self.assertEqual(_repair_test_sequence(trace), expected)

    def test_repair_ignores_wrong_commands_claims_and_uncorrelated_async_results(self):
        commands = [None, {}, "", "echo 'python3 -m unittest -v test_metrics'", "python3 -c 'print(1)'",
            "python3 -m unittest -v other_tests", "python3 -m unittest -v test_metrics --help",
            "python3 -m unittest -v test_metrics test_metrics", "python3 -m unittest -v test_metrics && echo OK",
            "python3 -m unittest -v test_metrics; echo OK", "python3 -m unittest … test_metrics", "'unclosed"]
        for command in commands:
            trace = ToolTrace(True, True, {}, (repair_tool(REPAIR_FAILED, command=command),
                repair_tool(REPAIR_PASSED)), False, {}, 5)
            with self.subTest(command=command):
                self.assertEqual(_repair_test_sequence(trace), (False, False))
            trace = ToolTrace(True, True, {}, (repair_tool(REPAIR_FAILED),
                repair_tool(REPAIR_PASSED, command=command)), False, {}, 5)
            with self.subTest(passing_command=command):
                self.assertEqual(_repair_test_sequence(trace), (True, False))
        for name in ("command_status", "send_command_input", "view_file"):
            asynchronous = repair_tool(REPAIR_PASSED)
            asynchronous["name"] = name
            trace = ToolTrace(True, True, {}, (repair_tool(REPAIR_FAILED), asynchronous), False, {}, 5)
            with self.subTest(uncorrelated=name):
                self.assertEqual(_repair_test_sequence(trace), (True, False))

    def test_repair_rejects_malformed_unittest_summaries_and_tool_failures(self):
        invalid_outputs = [None, [], {}, REPAIR_FAILED.replace("Ran 3 tests", "Ran 2 tests"),
            REPAIR_FAILED.replace("Ran 3 tests", "Ran 4 tests"),
            REPAIR_FAILED.replace("Ran 3 tests", "I claim Ran 3 tests"),
            REPAIR_FAILED.replace("0.001s", "NaNs"), REPAIR_FAILED.replace("0.001s", "-1s"),
            REPAIR_FAILED.replace("\n\nFAILED", "\nFAILED"), REPAIR_FAILED + "extra assertion\n",
            REPAIR_FAILED.replace("failures=2", "failures=0"), REPAIR_FAILED.replace("failures=2", "failures=4"),
            REPAIR_FAILED.replace("failures=2", "failures=2, errors=2"),
            REPAIR_FAILED.replace("failures=2", "failures=1, failures=1"),
            REPAIR_FAILED.replace("failures=2", "skipped=1")]
        for output in invalid_outputs:
            trace = ToolTrace(True, True, {}, (repair_tool(output), repair_tool(REPAIR_PASSED)), False, {}, 5)
            with self.subTest(output=output):
                self.assertEqual(_repair_test_sequence(trace), (False, False))
        for phase in ("failed", "passed"):
            for mode in ("native-error", "not-successful"):
                native_failure, native_pass = repair_tool(REPAIR_FAILED), repair_tool(REPAIR_PASSED)
                target = native_failure if phase == "failed" else native_pass
                if mode == "native-error":
                    target["success"] = False
                    target["details"]["error"] = {"message": "private-fixture"}
                else:
                    target["success"] = False
                trace = ToolTrace(True, True, {}, (native_failure, native_pass), False, {}, 5)
                with self.subTest(phase=phase, mode=mode):
                    self.assertEqual(_repair_test_sequence(trace), (phase == "passed", False))

    def test_repair_accepts_fixed_command_variants_and_valid_failure_counts(self):
        for command in ("python -m unittest test_metrics", "python3 -m unittest -q test_metrics.py",
                        "/usr/bin/python3.12 -m unittest test_metrics -v"):
            for summary in ("failures=1", "errors=3", "failures=1, errors=2", "errors=1, failures=2"):
                output = REPAIR_FAILED.replace("failures=2", summary).replace("\n", "\r\n")
                trace = ToolTrace(True, True, {}, (repair_tool(output, command=command),
                    repair_tool(REPAIR_PASSED, command=command)), False, {}, 5)
                with self.subTest(command=command, summary=summary):
                    self.assertEqual(_repair_test_sequence(trace), (True, True))

    def test_repair_codex_native_exit_one_then_zero_and_observed_shell_batch(self):
        batch = "python3 - <<'PY'\nprint('Controlled setup fixture')\nPY\npython3 -m unittest -v test_metrics"
        commands = ["python3 -m unittest -v test_metrics",
            "/bin/bash -lc " + shlex.quote("python3 -m unittest -v test_metrics"),
            "/bin/bash -lc " + shlex.quote(batch)]
        for command in commands:
            for failed_exit in (0, 1, 2):
                tools = tuple({"name": "command_execution", "success": exit_code == 0,
                    "details": {"command": command, "exit_code": exit_code, "aggregated_output": output}}
                    for exit_code, output in ((failed_exit, REPAIR_FAILED), (0, REPAIR_PASSED)))
                trace = ToolTrace(True, True, {}, tools, False, {}, 5)
                with self.subTest(command=command, failed_exit=failed_exit):
                    self.assertEqual(_repair_test_sequence(trace), (failed_exit == 1, failed_exit == 1))

    def test_repair_codex_rejects_unreachable_or_arbitrary_shell_wrapper_prefixes(self):
        unittest_command = "python3 -m unittest -v test_metrics"
        scripts = ["exit 1\n" + unittest_command, "exit 1; " + unittest_command,
            "printf 'Ran 3 tests in 0.001s\\n\\nFAILED (failures=1)\\n'\nexit 1\n" + unittest_command,
            "if false; then\n" + unittest_command + "\nfi",
            "printf 'setup'\n" + unittest_command,
            "python3 - <<'PY'\nprint('Controlled setup')\nPY\nexit 1\n" + unittest_command,
            "python3 - <<'PY'\nprint('Controlled setup')\nPY\npython3 - <<'OTHER'\nprint('Second setup')\nOTHER\n" + unittest_command,
            "python3 - <<'PY'\nprint('Controlled setup')\nPY\nPY\n" + unittest_command,
            "python3 - <<'lowercase'\nprint('Controlled setup')\nlowercase\n" + unittest_command]
        for script in scripts:
            command = "/bin/bash -lc " + shlex.quote(script)
            tools = tuple({"name": "command_execution", "success": exit_code == 0,
                "details": {"command": command, "exit_code": exit_code, "aggregated_output": output}}
                for exit_code, output in ((1, REPAIR_FAILED), (0, REPAIR_PASSED)))
            trace = ToolTrace(True, True, {}, tools, False, {}, 5)
            with self.subTest(script=script):
                self.assertEqual(_repair_test_sequence(trace), (False, False))

    def test_antigravity_file_write_does_not_accept_shell_write_fallback(self):
        work, control = self.directories()
        (work / "note.txt").write_text("서울 연구 도구 검증\n", encoding="utf-8")
        trace = ToolTrace(True, True, {"timezone": "Asia/Seoul"}, (
            {"name": "run_command", "success": True, "details": {}},
            {"name": "view_file", "success": True, "details": {"parameters": {"AbsolutePath": str(work / "note.txt")}}},
        ), False, {}, 4)
        checks, _ = evaluate_tool_case("antigravity_exec", "file-write", work, control,
                                       ProcessResult(0, b"", b"", True, True), trace)
        self.assertTrue(checks["file_bytes_match"])
        self.assertTrue(checks["native_read_action"])
        self.assertFalse(checks["native_file_action"])
        self.assertFalse(checks["only_scenario_tools"])

    def test_explicit_grant_never_converts_observed_denial_into_success(self):
        def execute(argv, *, cwd, **kwargs):
            if argv[0] == "/mock/git":
                return ProcessResult(0, b"", b"", True, True)
            (cwd / "note.txt").write_text("서울 연구 도구 검증\n", encoding="utf-8")
            return ProcessResult(0, agy_file_trace(cwd, permission_mode="always-proceed", denied=True), b"", True, True)
        with patch("researchops.runner_tool_validation.shutil.which", side_effect=lambda name: "/mock/" + name), \
                patch("researchops.runner_tool_validation.control_environment", return_value={}), \
                patch("researchops.runner_tool_validation.run_bounded", side_effect=execute) as invoke:
            report = validate_runner_tools(["antigravity_exec"], scenarios=["file-write", "file-code"], live=True,
                output_parent=self.root, agy_dangerously_skip_permissions=True)
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(report["totals"], {"passed": 0, "failed": 0, "blocked": 2})
        case = report["cases"][0]
        self.assertTrue(case["checks"]["permission_mode_matches_request"])
        self.assertFalse(case["checks"]["no_permission_denials"])
        self.assertFalse(case["checks"]["terminal_success"])
        self.assertEqual(case["blocked_by"], ["NATIVE-TOOL-PERMISSION-REQUIRED"])

    def test_file_write_prompt_allows_codex_native_readback_without_code_execution(self):
        work, control = self.directories()
        _, _, prompt = build_tool_command("codex_exec", "/mock/codex", "file-write", work, control, timeout_seconds=90)
        self.assertIn("read-only cat", prompt)
        self.assertIn("No code execution", prompt)
        self.assertIn("do not use shell to write", prompt)

    def test_observed_file_read_outside_workspace_is_not_accepted(self):
        work, control = self.directories()
        (work / "note.txt").write_text("서울 연구 도구 검증\n")
        trace = ToolTrace(True, True, {"timezone": "Asia/Seoul"}, (
            {"name": "write_to_file", "success": True, "details": {"parameters": {"TargetFile": str(work / "note.txt")}}},
            {"name": "view_file", "success": True, "details": {"parameters": {"AbsolutePath": "/outside/private.txt"}}},
        ), False, {}, 4)
        checks, _ = evaluate_tool_case("antigravity_exec", "file-write", work, control, ProcessResult(0, b"", b"", True, True), trace)
        self.assertFalse(checks["observed_file_targets_scoped"])

    def test_public_mcp_report_must_cite_an_actually_fetched_url(self):
        work, control = self.directories()
        response = {"timezone": "Asia/Seoul", "sources": ["https://learn.chatgpt.com/docs/not-fetched"], "findings": ["one", "two"]}
        trace = ToolTrace(True, True, response, tuple({"name": "mcp_tool_call", "success": True,
            "details": {"server": "openai_docs", "tool": name, "arguments": {"url": "https://learn.chatgpt.com/docs/web-search"}}}
            for name in ("search_openai_docs", "fetch_openai_doc")), False, {}, 5)
        checks, _ = evaluate_tool_case("codex_exec", "mcp-public-docs", work, control, ProcessResult(0, b"", b"", True, True), trace)
        self.assertTrue(checks["official_mcp_sources"])
        self.assertFalse(checks["fetched_source_cited"])

    def test_public_mcp_spill_read_requires_preceding_approved_call_in_current_conversation(self):
        work, control = self.directories()
        conversation_id = "11111111-1111-4111-8111-111111111111"
        other_conversation = "22222222-2222-4222-8222-222222222222"
        output_root = Path.home() / ".gemini/antigravity-cli/brain" / conversation_id / ".system_generated/steps"
        source = "https://developers.openai.com/learn/docs-mcp"
        response = {"timezone": "Asia/Seoul", "sources": [source], "findings": ["First finding.", "Second finding."]}
        calls = [{"name": "call_mcp_tool", "success": True, "step_index": index,
            "details": {"parameters": {"ServerName": "openai_docs", "ToolName": name, "Arguments": {"url": source}}}}
            for index, name in ((2, "search_openai_docs"), (4, "fetch_openai_doc"))]
        read = {"name": "view_file", "success": True, "step_index": 5,
                "details": {"parameters": {"AbsolutePath": str(output_root / "4/output.txt")}}}
        trace = ToolTrace(True, True, response, tuple([*calls, read]), False, {}, 5,
                          conversation_id=conversation_id)
        checks, _ = evaluate_tool_case("antigravity_exec", "mcp-public-docs", work, control,
                                       ProcessResult(0, b"", b"", True, True), trace)
        self.assertTrue(all(checks.values()), checks)
        variants = []
        for label, path in (("different-conversation", str(output_root / "4/output.txt").replace(conversation_id, other_conversation)),
                            ("unobserved-step", str(output_root / "6/output.txt")),
                            ("different-filename", str(output_root / "4/private.txt")),
                            ("arbitrary-brain-file", str(output_root.parent.parent / "scratch/private.txt"))):
            changed = copy.deepcopy(read)
            changed["details"]["parameters"]["AbsolutePath"] = path
            variants.append((label, [*calls, changed], conversation_id, "mcp-public-docs"))
        variants.extend((
            ("no-conversation", [*calls, read], None, "mcp-public-docs"),
            ("read-before-call", [read, *calls], conversation_id, "mcp-public-docs"),
            ("local-mcp-does-not-grant-brain-access", [*calls, read], conversation_id, "mcp-research"),
        ))
        for label, field, value in (("failed-call", "success", False), ("invalid-step", "step_index", True)):
            changed = copy.deepcopy(calls)
            changed[-1][field] = value
            variants.append((label, [*changed, read], conversation_id, "mcp-public-docs"))
        for field, value in (("ServerName", "unapproved_server"), ("ToolName", "unapproved_tool")):
            changed = copy.deepcopy(calls)
            changed[-1]["details"]["parameters"][field] = value
            variants.append((field, [*changed, read], conversation_id, "mcp-public-docs"))
        write = {"name": "write_to_file", "success": True, "step_index": 5,
                 "details": {"parameters": {"TargetFile": str(output_root / "4/output.txt")}}}
        variants.append(("write-not-granted", [*calls, write], conversation_id, "mcp-public-docs"))
        for label, tools, identity, scenario in variants:
            with self.subTest(label=label):
                trace = ToolTrace(True, True, response, tuple(tools), False, {}, 5, conversation_id=identity)
                checks, _ = evaluate_tool_case("antigravity_exec", scenario, work, control,
                                               ProcessResult(0, b"", b"", True, True), trace)
                self.assertFalse(checks["observed_file_targets_scoped"])

    def test_mocked_native_code_case_passes_only_with_artifacts_and_execution_evidence(self):
        def execute(argv, *, cwd, **kwargs):
            self.assertEqual((cwd / "sales.csv").read_text(), SOURCE_CSV)
            write_code_fixture(cwd)
            return ProcessResult(0, codex_trace(), b"", True, True)
        with patch("researchops.runner_tool_validation.shutil.which", return_value="/mock/codex"), \
                patch("researchops.runner_tool_validation.control_environment", return_value={"PATH": "/usr/bin:/bin"}), \
                patch("researchops.runner_tool_validation.run_bounded", side_effect=execute) as invoke:
            report = validate_runner_tools(["codex_exec"], scenarios=["file-code"], live=True, output_parent=self.root)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(report["totals"], {"passed": 1, "failed": 0, "blocked": 0}, report["cases"])
        self.assertEqual(report["cli_invocations"], 1)
        self.assertEqual(report["model_invocations"], 1)
        self.assertFalse(report["production_runner_ready"])

    def test_failed_native_command_cannot_pass_from_artifacts_or_model_claim(self):
        def execute(argv, *, cwd, **kwargs):
            write_code_fixture(cwd)
            return ProcessResult(0, codex_trace(command_exit=1), b"", True, True)
        with patch("researchops.runner_tool_validation.shutil.which", return_value="/mock/codex"), \
                patch("researchops.runner_tool_validation.control_environment", return_value={}), \
                patch("researchops.runner_tool_validation.run_bounded", side_effect=execute):
            report = validate_runner_tools(["codex_exec"], scenarios=["file-code"], live=True, output_parent=self.root)
        self.assertEqual(report["totals"], {"passed": 0, "failed": 1, "blocked": 0})
        self.assertFalse(report["cases"][0]["checks"]["native_code_execution"])
        self.assertFalse(report["cases"][0]["checks"]["native_tests_passed"])

    def test_permission_denial_holds_same_capability_not_independent_file_write(self):
        def one(provider, scenario, root, **kwargs):
            denied = scenario in {"file-code", "mcp-research"}
            return {"runner": provider, "scenario": scenario, "status": "blocked" if denied else "passed",
                    "cli_invocations": 1, "model_invocations": 0 if denied else 1, "terminal_turns": 1,
                    "permission_denied": denied, "control_process_cleanup": True, "remote_cleanup_verified": True}
        with patch("researchops.runner_tool_validation._one_case", side_effect=one) as invoke:
            report = validate_runner_tools(["antigravity_exec"], live=True,
                scenarios=["file-code", "code-repair", "mcp-research", "mcp-public-docs", "combined-research", "file-write"],
                output_parent=self.root)
        self.assertEqual(invoke.call_count, 3)
        self.assertEqual(report["cli_invocations"], 3)
        self.assertEqual(report["totals"], {"passed": 1, "failed": 0, "blocked": 5})
        for case in (report["cases"][1], report["cases"][3], report["cases"][4]):
            self.assertEqual(case["blocked_by"], ["NATIVE-TOOL-PERMISSION-REQUIRED"])
        self.assertEqual(report["cases"][-1]["status"], "passed")

    def test_native_diagnostic_categories_are_bounded_and_never_echo_error_payloads(self):
        errors = [
            {"message": "private-fixture: connecting to sandbox server: /private.sock: connection reset by peer"},
            {"message": "invalid_args: /private-fixture/note.txt is not a valid artifact path"},
            {"message": "connecting to sandbox server: connection reset by peer"},
            {"message": "private-fixture: unknown native error", "secret": "private-fixture"},
            {"message": None}, "private-fixture", None,
        ]
        trace = ToolTrace(True, True, {}, tuple({"name": "run_command", "success": False,
            "details": {"error": error}} for error in errors), False, {}, 8)
        categories = _native_error_categories(trace)
        self.assertEqual(categories, ["AGY-FILE-ARTIFACT-INCOMPATIBLE", "AGY-SANDBOX-SERVER-UNAVAILABLE"])
        self.assertNotIn("private-fixture", json.dumps(categories))
        for message in ("connection reset by peer", "connecting to sandbox server: a different error",
                        "invalid_args: a different error", "is not a valid artifact path"):
            trace = ToolTrace(True, True, {}, ({"name": "run_command", "success": False,
                "details": {"error": {"message": message}}},), False, {}, 3)
            with self.subTest(message=message):
                self.assertEqual(_native_error_categories(trace), [])

    def test_sandbox_server_failure_holds_code_but_not_web_mcp_or_file_only_cases(self):
        def one(provider, scenario, root, **kwargs):
            blocked = scenario == "file-code"
            self.assertTrue(kwargs["agy_dangerously_skip_permissions"])
            return {"runner": provider, "scenario": scenario, "status": "blocked" if blocked else "passed",
                "cli_invocations": 1, "model_invocations": 1, "terminal_turns": 1, "permission_denied": False,
                "blocked_by": ["AGY-SANDBOX-SERVER-UNAVAILABLE"] if blocked else [],
                "control_process_cleanup": True, "remote_cleanup_verified": True}
        with patch("researchops.runner_tool_validation._one_case", side_effect=one) as invoke:
            report = validate_runner_tools(["antigravity_exec"], live=True, agy_dangerously_skip_permissions=True,
                scenarios=["file-code", "code-repair", "combined-research", "web-search", "mcp-research", "mcp-public-docs", "file-write"],
                output_parent=self.root)
        self.assertEqual(invoke.call_count, 5)
        self.assertEqual(report["totals"], {"passed": 4, "failed": 0, "blocked": 3})
        self.assertEqual(report["cli_invocations"], 5)
        for case in report["cases"][:3]:
            self.assertEqual(case["blocked_by"], ["AGY-SANDBOX-SERVER-UNAVAILABLE"])
        self.assertTrue(all(case["status"] == "passed" for case in report["cases"][3:]))

    def test_sandbox_server_error_is_blocked_only_when_native_execution_has_not_recovered(self):
        for recovered in (False, True):
            def execute(argv, *, cwd, **kwargs):
                if argv[0] == "/mock/git":
                    return ProcessResult(0, b"", b"", True, True)
                write_code_fixture(cwd)
                events = [json.loads(line) for line in agy_file_trace(cwd, permission_mode="always-proceed").splitlines()]
                events.insert(-1, {"event": "step_update", "step_update": {
                    "step_index": 3, "step_type": "tool", "state": "ERROR", "tool_name": "run_command",
                    "tool_info": {"name": "run_command", "parameters": {}, "error": {
                        "message": "private-fixture: connecting to sandbox server: connection reset by peer"}}}})
                if recovered:
                    events.insert(-1, {"event": "step_update", "step_update": {
                        "step_index": 4, "step_type": "tool", "state": "DONE", "tool_name": "run_command",
                        "tool_info": {"name": "run_command", "parameters": {}, "output": "Ran 3 tests in 0.001s\nOK\n"}}})
                return ProcessResult(0, encode_events(events), b"", True, True)
            with self.subTest(recovered=recovered), \
                    patch("researchops.runner_tool_validation.shutil.which", side_effect=lambda name: "/mock/" + name), \
                    patch("researchops.runner_tool_validation.control_environment", return_value={}), \
                    patch("researchops.runner_tool_validation.run_bounded", side_effect=execute):
                report = validate_runner_tools(["antigravity_exec"], scenarios=["file-code"], live=True,
                    output_parent=self.root, agy_dangerously_skip_permissions=True)
            case = report["cases"][0]
            self.assertEqual(case["native_error_categories"], ["AGY-SANDBOX-SERVER-UNAVAILABLE"])
            self.assertFalse(case["permission_denied"])
            self.assertEqual(case["checks"]["native_code_execution"], recovered)
            self.assertEqual(case["status"], "passed" if recovered else "blocked", case)
            if not recovered:
                self.assertEqual(case["blocked_by"], ["AGY-SANDBOX-SERVER-UNAVAILABLE"])
            self.assertNotIn("private-fixture", json.dumps(report))

    def test_sandbox_recovery_requires_later_successful_test_execution_and_respects_error_order(self):
        sequences = [
            ("setup-before-error", ["setup", "sandbox-error"], False),
            ("setup-after-error", ["sandbox-error", "setup"], False),
            ("tests-before-later-error", ["tests", "sandbox-error"], False),
            ("tests-after-error", ["sandbox-error", "tests"], True),
            ("later-error-reopens-blocker", ["sandbox-error", "tests", "sandbox-error"], False),
            ("failed-tests-do-not-recover", ["sandbox-error", "failed-tests"], False),
            ("too-few-tests-do-not-recover", ["sandbox-error", "short-tests"], False),
        ]
        for label, sequence, recovered in sequences:
            def execute(argv, *, cwd, **kwargs):
                if argv[0] == "/mock/git":
                    return ProcessResult(0, b"", b"", True, True)
                write_code_fixture(cwd)
                events = [json.loads(line) for line in agy_file_trace(cwd, permission_mode="always-proceed").splitlines()]
                for index, action in enumerate(sequence, start=3):
                    info = {"name": "run_command", "parameters": {}}
                    state = "DONE"
                    if action == "sandbox-error":
                        state = "ERROR"
                        info["error"] = {"message": "connecting to sandbox server: private-fixture: connection reset by peer"}
                    elif action == "setup":
                        info["output"] = "Workspace setup completed.\n"
                    elif action == "failed-tests":
                        state = "ERROR"
                        info["output"] = "Ran 3 tests in 0.001s\nFAILED (failures=1)\n"
                    elif action == "short-tests":
                        info["output"] = "Ran 2 tests in 0.001s\nOK\n"
                    else:
                        info["output"] = "Ran 3 tests in 0.001s\nOK\n"
                    events.insert(-1, {"event": "step_update", "step_update": {"step_index": index,
                        "step_type": "tool", "state": state, "tool_name": "run_command", "tool_info": info}})
                return ProcessResult(0, encode_events(events), b"", True, True)
            with self.subTest(sequence=label), \
                    patch("researchops.runner_tool_validation.shutil.which", side_effect=lambda name: "/mock/" + name), \
                    patch("researchops.runner_tool_validation.control_environment", return_value={}), \
                    patch("researchops.runner_tool_validation.run_bounded", side_effect=execute) as invoke:
                report = validate_runner_tools(["antigravity_exec"], scenarios=["file-code", "code-repair"], live=True,
                    output_parent=self.root, agy_dangerously_skip_permissions=True)
            case = report["cases"][0]
            self.assertEqual(case["checks"]["native_sandbox_recovered"], recovered)
            self.assertEqual(case["status"], "passed" if recovered else "blocked", case)
            self.assertFalse(case["permission_denied"])
            self.assertNotIn("private-fixture", json.dumps(report))
            if not recovered:
                self.assertEqual(invoke.call_count, 2)
                self.assertEqual(case["blocked_by"], ["AGY-SANDBOX-SERVER-UNAVAILABLE"])
                self.assertEqual(report["cases"][1]["blocked_by"], ["AGY-SANDBOX-SERVER-UNAVAILABLE"])
            else:
                self.assertEqual(invoke.call_count, 4)

    def test_actual_denial_category_holds_code_even_when_file_scenario_triggered_it(self):
        def one(provider, scenario, root, **kwargs):
            return {"runner": provider, "scenario": scenario, "status": "blocked", "cli_invocations": 1,
                "model_invocations": 1, "structured_responses": 0, "terminal_turns": 1, "permission_denied": True,
                "denied_actions": ["command"], "control_process_cleanup": True, "remote_cleanup_verified": True}
        with patch("researchops.runner_tool_validation._one_case", side_effect=one) as invoke:
            report = validate_runner_tools(["antigravity_exec"], scenarios=["file-write", "file-code", "code-repair"], live=True, output_parent=self.root)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(report["model_invocations"], 1)
        self.assertEqual(report["structured_responses"], 0)
        self.assertEqual(report["totals"]["blocked"], 3)

    def test_model_activity_does_not_require_a_valid_final_envelope(self):
        events = [json.loads(line) for line in codex_trace().splitlines()]
        events[-2]["item"]["text"] = "I used the tools, but this is not the required JSON envelope."
        with patch("researchops.runner_tool_validation.shutil.which", return_value="/mock/codex"), \
                patch("researchops.runner_tool_validation.control_environment", return_value={}), \
                patch("researchops.runner_tool_validation.run_bounded", return_value=ProcessResult(0, encode_events(events), b"", True, True)):
            report = validate_runner_tools(["codex_exec"], scenarios=["file-code"], live=True, output_parent=self.root)
        self.assertEqual(report["model_invocations"], 1)
        self.assertEqual(report["structured_responses"], 0)
        self.assertEqual(report["totals"]["failed"], 1)

    def test_invalid_provider_metadata_preserves_spawn_count_and_holds_remaining_cases(self):
        process = ProcessResult(0, codex_trace(usage={"input_tokens": "private-fixture"}), b"", True, True)
        with patch("researchops.runner_tool_validation.shutil.which", return_value="/mock/codex"), \
                patch("researchops.runner_tool_validation.control_environment", return_value={}), \
                patch("researchops.runner_tool_validation.run_bounded", return_value=process) as invoke:
            report = validate_runner_tools(["codex_exec"], scenarios=["file-code", "code-repair"], live=True, output_parent=self.root)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(report["cli_invocations"], 1)
        self.assertEqual(report["model_invocations"], 0)
        self.assertEqual(report["cases"][1]["blocked_by"], ["PROVIDER-TERMINAL-UNVERIFIED"])
        self.assertNotIn("private-fixture", json.dumps(report))

    def test_post_spawn_evidence_write_failure_does_not_reset_invocation_to_zero(self):
        process = ProcessResult(0, codex_trace(), b"", True, True)
        with patch("researchops.runner_tool_validation.shutil.which", return_value="/mock/codex"), \
                patch("researchops.runner_tool_validation.control_environment", return_value={}), \
                patch("researchops.runner_tool_validation.run_bounded", return_value=process), \
                patch.object(Path, "write_bytes", side_effect=OSError("private-storage-error")):
            report = validate_runner_tools(["codex_exec"], scenarios=["file-code"], live=True, output_parent=self.root)
        self.assertEqual(report["cli_invocations"], 1)
        self.assertEqual(report["totals"], {"passed": 0, "failed": 1, "blocked": 0})
        self.assertNotIn("private-storage-error", json.dumps(report))

    def mcp_fixture(self, *, wrong_sources=False):
        work, control = self.directories()
        with ResearchMCPServer(control / "mcp-audit.jsonl") as server:
            server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
            server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
            server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            calls = [("search_documents", {"query": "numeric"}), ("fetch_document", {"document_id": "numeric-ledger"}),
                     ("search_documents", {"query": "partial"}), ("fetch_document", {"document_id": "partial-survey"})]
            if wrong_sources:
                calls = [("search_documents", {"query": "Korean"}), ("fetch_document", {"document_id": "korean-policy"})] * 2
            returned = [server.handle({"jsonrpc": "2.0", "id": index + 3, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}})["result"]["structuredContent"]
                        for index, (name, args) in enumerate(calls)]
        response = {"timezone": "Asia/Seoul", "summary": "Synthetic MCP fixture", "document_ids": ["numeric-ledger", "partial-survey"],
            "receipt_ids": [result["receipt_id"] for result in returned], "evidence_marker": returned[0]["evidence_marker"],
            "baseline": 125, "current": 150, "change": 25, "percent_change": 20, "coverage": "partial", "sample_size": None}
        trace = ToolTrace(True, True, response, tuple(mcp_tool(name) for name, _ in calls), False, {}, 7)
        return work, control, ProcessResult(0, b"", b"", True, True), trace

    def test_local_mcp_pass_requires_actual_search_fetch_receipts_and_source_values(self):
        work, control, process, trace = self.mcp_fixture()
        checks, _ = evaluate_tool_case("codex_exec", "mcp-research", work, control, process, trace)
        self.assertTrue(all(checks.values()), checks)

    def test_local_mcp_cannot_pass_with_unrelated_fetched_sources_and_guessed_facts(self):
        work, control, process, trace = self.mcp_fixture(wrong_sources=True)
        checks, _ = evaluate_tool_case("codex_exec", "mcp-research", work, control, process, trace)
        self.assertFalse(all(checks.values()), checks)

    def test_local_mcp_rejects_discovery_only_without_call_receipts(self):
        work, control, process, trace = self.mcp_fixture()
        path = control / "mcp-audit.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows if row["event"] == "protocol"))
        checks, _ = evaluate_tool_case("codex_exec", "mcp-research", work, control, process, trace)
        self.assertFalse(checks["server_side_receipts"])

    def test_local_mcp_malformed_audit_and_response_fail_closed_without_raising(self):
        work, control, process, trace = self.mcp_fixture()
        (control / "mcp-audit.jsonl").write_text("[]\n")
        trace.response["document_ids"] = [[]]
        checks, _ = evaluate_tool_case("codex_exec", "mcp-research", work, control, process, trace)
        self.assertFalse(all(checks.values()), checks)

    def test_local_mcp_failed_fetch_does_not_count_as_successful_tool(self):
        work, control, process, trace = self.mcp_fixture()
        failed = ToolTrace(True, True, trace.response,
                           (mcp_tool("search_documents"), mcp_tool("fetch_document", success=False)), False, {}, 5)
        checks, _ = evaluate_tool_case("codex_exec", "mcp-research", work, control, process, failed)
        self.assertFalse(checks["native_mcp_search_and_fetch"])


if __name__ == "__main__":
    unittest.main()
