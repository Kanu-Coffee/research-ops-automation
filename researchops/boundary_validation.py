"""Opt-in model -> data-only control -> namespace/cgroup code validation.

Only a fixed synthetic programming task is accepted. This development probe
does not enable ordinary task runners, inspect credentials, or send mail.
The CLI retains its normal authentication; its returned source is executed
only by IsolatedProcessLauncher, never by the authenticated control process.
Filesystem quota and hostile-model/native-tool prevention remain separate gates.
"""

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import tempfile
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator

from researchops.runners.antigravity_response import parse_antigravity_response
from researchops.runners.development_process import run_bounded
from researchops.runners.development_runner import (
    ENVELOPE_SCHEMA, MAX_RESPONSE_BYTES, build_output_only_command,
    codex_response, control_environment,
)
from researchops.runners.launcher import IsolatedProcessLauncher
from researchops.strict_json import strict_json_loads
from researchops.workspace.security import assert_path_contained, read_safe_bytes


PROVIDERS = ("codex_exec", "antigravity_exec")
SOURCE_LIMIT = 32_000
SOURCE_SCHEMA = {"type": "object", "properties": {"source": {"type": "string", "minLength": 1}},
                 "required": ["source"], "additionalProperties": False}
PROMPT = (
    "ResearchOps trusted synthetic code-boundary validation. Do NOT call any tools, shell, "
    "file, browser, MCP, plugin or subagent actions. Do not inspect credentials or your environment. "
    "Return only the outer JSON object {\"response_json\":\"JSON document encoded as a string\"}. "
    "The inner JSON must have exactly one key source, containing a complete Python 3 standard-library "
    "module. Define total(rows) returning a float sum of quantity * price, accepting numeric strings. "
    "An empty list returns 0.0, negative quantities are valid returns, and the input must not be mutated. "
    "Define percent_change(current, previous) returning (current-previous)/abs(previous)*100, "
    "or None when previous is zero. Do not print anything or run tests at module import. "
    "The application, not the CLI, will execute and test your source in a separate isolated process."
)

# This independently supplied harness is not provided to the model. Host marker
# content is never sent to the CLI or task process. The marker path is synthetic.
# Generated code shares an interpreter with these functional tests; a hostile
# module could tamper with unittest. These are not adversarial correctness proofs.
# The independent, source-free kernel preflight is recorded separately.
HARNESS = r'''
import importlib.util, json, os, pathlib, socket, sys, unittest
marker, port, source, result_path = sys.argv[1:]
checks = {}
try:
    pathlib.Path(marker).read_bytes()
except (FileNotFoundError, PermissionError):
    checks["host_marker_hidden"] = True
else:
    checks["host_marker_hidden"] = False
try:
    connection = socket.create_connection(("127.0.0.1", int(port)), timeout=1)
except OSError:
    checks["host_loopback_blocked"] = True
else:
    connection.close()
    checks["host_loopback_blocked"] = False
checks["no_secret_environment"] = not any(key in os.environ for key in
    ("CODEX_HOME", "OPENAI_API_KEY", "SMTP_PASSWORD", "SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS"))
checks["seoul_environment"] = os.environ.get("TZ") == "Asia/Seoul"
checks["private_proc"] = not pathlib.Path("/proc/1/root" + marker).exists()
if source != "-":
    spec = importlib.util.spec_from_file_location("solution", source)
    solution = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(solution)
    class Tests(unittest.TestCase):
        def test_total(self):
            rows = [{"quantity": "3", "price": "20.5"}, {"quantity": 2, "price": 30}, {"quantity": -1, "price": 7}]
            before = json.dumps(rows, sort_keys=True)
            self.assertAlmostEqual(solution.total(rows), 114.5)
            self.assertEqual(json.dumps(rows, sort_keys=True), before)
        def test_empty(self):
            self.assertEqual(solution.total([]), 0)
        def test_change(self):
            self.assertEqual(solution.percent_change(120, 100), 20)
            self.assertEqual(solution.percent_change(50, 100), -50)
            self.assertIsNone(solution.percent_change(1, 0))
    outcome = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))
    checks["generated_tests_passed"] = outcome.wasSuccessful() and outcome.testsRun == 3
    checks["generated_test_count"] = outcome.testsRun
    pathlib.Path(result_path).write_text(json.dumps({"total": solution.total([
        {"quantity": 3, "price": 20.5}, {"quantity": 2, "price": 30}, {"quantity": -1, "price": 7}])}))
print(json.dumps(checks, sort_keys=True))
sys.exit(0 if all(value for key, value in checks.items() if key != "generated_test_count") else 1)
'''


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)


def _source(document):
    Draft202012Validator(SOURCE_SCHEMA).validate(document)
    raw = document["source"].encode("utf-8", errors="strict")
    if len(raw) > SOURCE_LIMIT or b"\x00" in raw:
        raise ValueError("source_limit_or_encoding")
    return raw


def _generate(provider, directory, timeout_seconds, model, observation=None):
    observation = {} if observation is None else observation
    observation.update({"provider": provider, "cli_invocations": 0, "model_invocations": 0,
                   "terminal_response": False, "cleanup_verified": True, "success": False,
                   "credential_contents_inspected": False, "native_tools_observed": None})
    result, source, terminal = None, None, False
    try:
        binary = shutil.which("codex" if provider == "codex_exec" else "agy")
        if not binary:
            raise ValueError("provider_binary_unavailable")
        _write_json(directory / "prompt.json", {"prompt": PROMPT})
        argv, stdin = build_output_only_command(provider, binary, PROMPT, directory,
            timeout_seconds=timeout_seconds, model=model)
        result = run_bounded(argv, cwd=directory, env=control_environment(provider, binary, directory),
            stdin=stdin, timeout_seconds=timeout_seconds, max_output_bytes=MAX_RESPONSE_BYTES)
        observation["cli_invocations"] = int(result.spawned)
        (directory / "stdout.jsonl").write_bytes(result.stdout)
        (directory / "stderr.log").write_bytes(result.stderr)
        if result.error or result.exit_code != 0:
            raise ValueError("provider_process_failed")
        if provider == "codex_exec":
            envelope, usage, count = codex_response(result.stdout)
        else:
            parsed = parse_antigravity_response(result.stdout, max_total_bytes=MAX_RESPONSE_BYTES)
            envelope, usage, count = parsed.structured_output, parsed.usage, parsed.event_count
        terminal = True
        observation.update(model_invocations=1, terminal_response=True, usage=usage,
                           event_count=count, native_tools_observed=0)
        Draft202012Validator(ENVELOPE_SCHEMA).validate(envelope)
        source = _source(strict_json_loads(envelope["response_json"], max_bytes=SOURCE_LIMIT * 8))
        if not result.cleanup_verified:
            raise ValueError("control_cleanup_unverified")
        observation.update(success=True, source_sha256=hashlib.sha256(source).hexdigest())
    except Exception as exc:
        # Raw provider/source errors may include response data; keep them in
        # private raw evidence, never interpolated into the public summary.
        observation["error"] = type(exc).__name__
        source = None
    observation["cleanup_verified"] = (result.cleanup_verified if result else True) and not (
        provider == "antigravity_exec" and result and result.spawned and not terminal)
    _write_json(directory / "observation.json", observation)
    return source, observation


def _kernel_case(launcher, workspace, marker, port, source=None):
    workspace.mkdir(mode=0o700)
    for name in ("project", "input", "tmp", "output"):
        (workspace / name).mkdir(mode=0o700)
    source_path = workspace / "input" / "solution.py"
    if source is not None:
        source_path.write_bytes(source)
        source_path.chmod(0o400)
    result_path = workspace / "output" / "result.json"
    result = launcher.run(["/usr/bin/python3", "-I", "-c", HARNESS, str(marker), str(port),
        str(source_path) if source is not None else "-", str(result_path)],
        cwd=workspace / "output", project_dir=workspace / "project", tmp_dir=workspace / "tmp",
        input_dir=workspace / "input", timeout_seconds=20, network_profile="none")
    observation = {"success": result.success, "cleanup_verified": result.cleanup_verified,
                   "exit_code": result.exit_code, "isolation": result.isolation, "checks": {}}
    (workspace / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (workspace / "stderr.log").write_text(result.stderr, encoding="utf-8")
    try:
        checks = strict_json_loads(result.stdout, max_bytes=16_000)
        required = {"host_marker_hidden", "host_loopback_blocked", "no_secret_environment",
                    "seoul_environment", "private_proc"}
        if source is not None:
            required.add("generated_tests_passed")
            artifact = read_safe_bytes(result_path, workspace / "output", 4096)
            observation["artifact_sha256"] = hashlib.sha256(artifact).hexdigest()
            observation["artifact_matches"] = strict_json_loads(artifact) == {"total": 114.5}
            observation["source_unchanged"] = read_safe_bytes(source_path, workspace / "input", SOURCE_LIMIT) == source
            observation["success"] &= observation["artifact_matches"] and observation["source_unchanged"] and checks.get("generated_test_count") == 3
        observation["checks"] = checks
        observation["success"] &= isinstance(checks, dict) and all(checks.get(key) is True for key in required)
    except Exception as exc:
        observation.update(success=False, error=type(exc).__name__)
    return observation


def validate_runner_boundary(runners=None, *, live=False, cgroup_root=None,
                             output_parent=None, timeout_seconds=120, model=None):
    """Run a new, private fixed-task probe; default is zero model invocations."""
    selected = list(dict.fromkeys(runners or PROVIDERS))
    if not selected or any(item not in PROVIDERS for item in selected):
        raise ValueError("Only Codex and Antigravity boundary probes are supported")
    if type(live) is not bool or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
        raise ValueError("Invalid live approval or timeout")
    if model is not None and (not live or not isinstance(model, str) or not model or model.startswith("-") or any(ord(c) < 32 for c in model)):
        raise ValueError("Invalid explicit live model")
    launcher = IsolatedProcessLauncher(cgroup_root=Path(cgroup_root) if cgroup_root else None,
        memory_bytes=268435456, max_pids=32, max_output_bytes=65536)
    readiness = launcher.readiness()
    report = {"tier": "trusted-development-model-to-isolated-code", "production_ready": False,
              "live": live, "model_invocations": 0, "cli_invocations": 0, "cases": [],
              "smtp_attempts": 0, "filesystem_quota_verified": False,
              "hostile_model_tool_prevention_verified": False,
              "timezone": "Asia/Seoul", "timestamp": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
              "readiness": readiness, "exit_code": 78}
    if not live or not readiness["ready"]:
        report["blocker"] = "explicit_live_required" if not live else "kernel_prerequisites_required"
        return report
    parent = Path(output_parent).absolute() if output_parent is not None else None
    if parent is not None:
        assert_path_contained(parent, parent)
        if not parent.is_dir():
            raise ValueError("Evidence parent must be an existing non-symlink directory")
    root = Path(tempfile.mkdtemp(prefix="researchops-boundary-validation-", dir=parent))
    report["evidence_dir"] = str(root)
    report["exit_code"] = 1
    _write_json(root / "report.json", report)
    try:
        control = root / "control"
        control.mkdir(mode=0o700)
        marker = control / "synthetic-host-marker"
        marker_raw = secrets.token_bytes(32)
        marker.write_bytes(marker_raw)
        marker.chmod(0o600)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            report["kernel_preflight"] = _kernel_case(launcher, root / "kernel-preflight", marker, port)
            if report["kernel_preflight"]["success"] and report["kernel_preflight"]["cleanup_verified"]:
                for provider in selected:
                    observation = {}
                    case = {"provider": provider, "control": observation, "status": "failed"}
                    report["cases"].append(case)
                    directory = control / provider
                    directory.mkdir(mode=0o700)
                    source, observation = _generate(provider, directory, timeout_seconds, model, observation)
                    if source is not None:
                        case["execution"] = _kernel_case(launcher, root / provider, marker, port, source)
                        if case["execution"]["success"] and case["execution"]["cleanup_verified"]:
                            case["status"] = "passed"
                    _write_json(root / "report.json", report)
                    if not observation["cleanup_verified"] or not case.get("execution", {}).get("cleanup_verified", True):
                        report["blocker"] = "cleanup_unverified_no_further_calls"
                        break
        report["synthetic_marker_unchanged"] = marker.read_bytes() == marker_raw
        report["exit_code"] = 0 if (len(report["cases"]) == len(selected) and
            all(case["status"] == "passed" for case in report["cases"]) and report["synthetic_marker_unchanged"]) else 1
    except Exception as exc:
        report.update(exit_code=1, error=type(exc).__name__, blocker="validation_infrastructure_failed")
    finally:
        for key in ("cli_invocations", "model_invocations"):
            report[key] = sum(case["control"].get(key, 0) for case in report["cases"])
        _write_json(root / "report.json", report)
    return report
