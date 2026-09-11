"""Opt-in output-only broker for trusted synthetic live model validation.

This is not an OS sandbox for hostile task programs. The CLI owns authentication;
no credentials or host environment are placed in prompts or worker outputs.
Only bounded response data is imported into a fixed set of application files.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from jsonschema import Draft202012Validator, ValidationError as SchemaValidationError

from researchops.config import bundled_path
from researchops.package.loader import TaskPackageLoader
from researchops.runners.base import BaseRunner, RunnerExecutionResult
from researchops.runners.development_process import run_bounded
from researchops.runners.streams import JsonLineEventCollector
from researchops.runners.response_transport import (
    ResponseTransportError, parse_response_envelope, parse_response_transport,
)
from researchops.runners.result_submission import prepare_result
from researchops.strict_json import strict_json_loads
from researchops.workspace.security import read_safe_bytes


SAMPLE_NAMES = ("document-extract", "numeric-compare", "partial-coverage", "no-updates")
ENVELOPE_SCHEMA = {"type": "object", "properties": {"response_json": {"type": "string"}},
                   "required": ["response_json"], "additionalProperties": False}
MAX_RESPONSE_BYTES = 1_000_000
MAX_INPUT_BYTES = 256_000


def control_environment(provider, binary, broker_dir):
    """Use existing CLI login in its control process, not an auth-file bridge."""
    env = {"PATH": str(Path(binary).parent) + ":/usr/bin:/bin", "HOME": str(Path.home()),
           "LANG": "C.UTF-8", "TZ": "Asia/Seoul", "TMPDIR": str(broker_dir)}
    for key in ("USER", "LOGNAME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        if key in os.environ:
            env[key] = os.environ[key]
    if provider == "codex_exec":
        env["CODEX_HOME"] = os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    # API keys, SSH agent sockets, SMTP settings and arbitrary environment are
    # deliberately not forwarded. Provider credentials are never read here.
    return env


def build_output_only_command(provider, binary, prompt, broker_dir, *, timeout_seconds=120, model=None):
    """Shared, inspected CLI contract; no claim of hostile-model isolation."""
    if provider not in {"codex_exec", "antigravity_exec"}:
        raise ValueError("Unsupported output-only provider")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
        raise ValueError("Control timeout must be between 1 and 300 seconds")
    if model is not None and (not isinstance(model, str) or not model or model.startswith("-") or any(ord(c) < 32 for c in model)):
        raise ValueError("Invalid explicit model")
    schema_path = broker_dir / "response-schema.json"
    schema_path.write_text(json.dumps(ENVELOPE_SCHEMA), encoding="utf-8")
    if provider == "codex_exec":
        argv = [binary, "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "--sandbox", "read-only", "--skip-git-repo-check", "--cd", str(broker_dir),
                "--output-schema", str(schema_path)]
        for setting in ("features.shell_tool=false", "features.unified_exec=false", "features.hooks=false",
                "features.multi_agent=false", "features.apps=false", "features.remote_plugin=false",
                'web_search="disabled"', 'model_reasoning_effort="low"', "project_doc_max_bytes=0", "mcp_servers={}"):
            argv += ["-c", setting]
        if model:
            argv += ["--model", model]
        return argv + ["-"], prompt.encode("utf-8")
    argv = [binary, "--sandbox", "--effort", "low", "--disable-slash-commands",
            "--output-format", "stream-json", "--print-timeout", f"{timeout_seconds}s",
            "--json-schema", str(schema_path), "--log-file", str(broker_dir / "control.log")]
    if model:
        argv += ["--model", model]
    return argv + ["--print", prompt], None


def codex_response(raw, *, decode_envelope=True):
    collector = JsonLineEventCollector("codex", max_total_bytes=MAX_RESPONSE_BYTES,
        max_line_bytes=MAX_RESPONSE_BYTES)
    collector.feed(raw)
    events = collector.finish()
    kinds = [entry["type"] for entry in events]
    if len(kinds) < 4 or kinds[:2] != ["thread.started", "turn.started"] or kinds.count("thread.started") != 1 or kinds.count("turn.started") != 1:
        raise ValueError("Expected one ordered Codex thread/turn start")
    documents, completed, usage = [], 0, {}
    for entry in events:
        event = entry["raw"]
        kind = event.get("type")
        if kind in {"error", "turn.failed"}:
            raise ValueError("Codex reported a failed turn")
        if kind == "turn.completed":
            completed += 1
            usage = event.get("usage", {})
        elif kind in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item", {})
            if item.get("type") not in {"agent_message", "reasoning"}:
                raise ValueError("Unexpected tool/action event in output-only Codex run")
            if kind == "item.completed" and item.get("type") == "agent_message":
                documents.append(item.get("text"))
        elif kind not in {"thread.started", "turn.started"}:
            raise ValueError("Unverified Codex lifecycle event")
    if completed != 1 or len(documents) != 1 or events[-1]["type"] != "turn.completed":
        raise ValueError("Expected one complete Codex turn and one final response")
    if not isinstance(usage, dict) or any(type(value) is not int or value < 0 for value in usage.values()):
        raise ValueError("Codex usage must contain nonnegative integer counts")
    envelope = parse_response_envelope(documents[0], max_bytes=MAX_RESPONSE_BYTES) if decode_envelope else documents[0]
    return envelope, usage, len(events)


def import_response(document, stage, output_dir, cancellation_check=None):
    """Enforce output count/bytes/names before writing; never render model HTML."""
    _, files = prepare_result(document, stage, max_bytes=MAX_RESPONSE_BYTES)
    if cancellation_check and cancellation_check():
        raise ValueError("Invocation cancelled before output import")
    directory = os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.listdir(directory):
            raise ValueError("Output directory must be empty before broker import")
        for name, raw in files.items():
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)
    return {name: output_dir / name for name in files}


class OutputOnlyDevelopmentRunner(BaseRunner):
    """Callable only with an explicit grant for one bundled synthetic package."""
    def __init__(self, provider, sample, *, approval=False, evidence_dir, timeout_seconds=120, model=None):
        if approval is not True:
            raise ValueError("Actual model validation requires explicit approval")
        if provider not in {"codex_exec", "antigravity_exec"} or sample not in SAMPLE_NAMES:
            raise ValueError("Only bundled synthetic samples and supported providers are allowed")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
            raise ValueError("Development invocation timeout must be between 1 and 300 seconds")
        if model is not None and (not isinstance(model, str) or not model or model.startswith("-") or any(ord(c)<32 for c in model)):
            raise ValueError("Invalid explicit model")
        root = Path(__file__).resolve().parents[2]
        schemas = bundled_path(root, "schemas")
        self.task, self.package_files, self.version_hash = TaskPackageLoader(schemas).load_from_dir(
            bundled_path(root, "examples/runner-validation") / sample)
        self.provider, self.sample, self.model = provider, sample, model
        self.timeout_seconds, self.evidence_dir = timeout_seconds, Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.binary = shutil.which("codex" if provider == "codex_exec" else "agy")
        self.calls = []

    def _prompt(self, input_dir, context):
        if context.task_id != self.task.id or context.network_profile != "none" or context.timezone != "Asia/Seoul":
            raise ValueError("Development grant does not match task/network/timezone")
        stage = context.invocation_stage
        if stage not in {"research", "compose"}:
            raise ValueError("Unknown development phase")
        expected_names = set(self.task.instructions[stage + "_files"])
        expected_names.update(self.task.output[key] for key in ("research_schema", "composition_schema", "composition_record_schema"))
        if stage == "compose":
            expected_names.add("composition-input.json")
        entries = list(input_dir.rglob("*"))
        if input_dir.is_symlink() or any(path.is_symlink() for path in entries):
            raise ValueError("Staged input cannot contain symlinks")
        allowed_dirs = {str(parent) for name in expected_names for parent in Path(name).parents if str(parent) != "."}
        if any(path.relative_to(input_dir).as_posix() not in allowed_dirs for path in entries if path.is_dir()):
            raise ValueError("Staged input contains an unexpected directory")
        actual = {path.relative_to(input_dir).as_posix() for path in entries if not path.is_dir()}
        if actual != expected_names:
            raise ValueError("Staged input differs from approved sample file set")
        documents = {}
        total = 0
        for name in sorted(expected_names):
            raw = read_safe_bytes(input_dir / name, input_dir, MAX_INPUT_BYTES)
            total += len(raw)
            if total > MAX_INPUT_BYTES:
                raise ValueError("Model prompt exceeds input byte limit")
            value = raw.decode("utf-8")
            if name != "composition-input.json" and value != self.package_files[name]:
                raise ValueError("Staged file differs from approved synthetic sample")
            documents[name] = value
        context_data = {"task_id": context.task_id, "run_id": context.run_id, "invocation_stage": stage,
            "timezone": context.timezone, "local_date": context.local_date,
            "local_date_display": context.local_date_display, "scheduled_for": context.scheduled_for}
        if stage == "compose":
            composition = strict_json_loads(documents["composition-input.json"], max_bytes=MAX_INPUT_BYTES)
            if composition.get("task_id") != context.task_id or composition.get("run_id") != context.run_id:
                raise ValueError("Composition identity differs from invocation")
        output_contract = ("The response_json string must contain the complete research result JSON object."
            if stage == "research" else "The response_json string must contain one JSON object with exactly composition_result (object), html (string), text (string). composition_result follows composition.schema.json. html and text are the final complete message bytes represented as strings.")
        return ("ResearchOps trusted synthetic validation. Use ONLY the supplied data. Never call any tools, shell, file, browser, network, MCP, plugin or subagent actions. Never read credentials. Do not create or modify files. "
            "Task instructions mentioning output files describe logical outputs only: return them through the response_json transport instead. "
            "Return exactly the outer object {\"response_json\":\"JSON document encoded as a string\"}, with no prose or markdown fences. "
            + output_contract + " Preserve every required record, warning, numeric sign and the supplied Seoul date. "
            + json.dumps({"context": context_data, "files": documents}, ensure_ascii=False))

    def _command(self, prompt, broker_dir):
        return build_output_only_command(self.provider, self.binary, prompt, broker_dir,
            timeout_seconds=self.timeout_seconds, model=self.model)

    def _execute(self, input_dir, output_dir, context):
        info = {"tier": "trusted-development-output-only", "production_ready": False,
                "provider": self.provider, "spawned": False, "model_invocations": 0,
                "cli_invocations": 0, "upstream_request_count": None,
                "credential_bridge": False, "filesystem_quota_scope": "response-import-only",
                "hostile_process_isolation": False, "shell_tools_disabled": self.provider == "codex_exec",
                "tool_policy": "reject-observed-actions; not-hostile-OS-isolation"}
        result, terminal, imported = None, False, {}
        broker_dir = Path(tempfile.mkdtemp(prefix="invocation-", dir=self.evidence_dir))
        error, events, usage, response_diagnostic = None, [], {}, None
        try:
            if not self.binary:
                raise ValueError("Configured provider executable is unavailable")
            prompt = self._prompt(input_dir, context)
            (broker_dir / "prompt.json").write_text(json.dumps({"prompt": prompt}, ensure_ascii=False), encoding="utf-8")
            argv, stdin = self._command(prompt, broker_dir)
            result = run_bounded(argv, cwd=broker_dir, env=control_environment(self.provider, self.binary, broker_dir),
                stdin=stdin, timeout_seconds=min(self.timeout_seconds, context.timeout_seconds),
                max_output_bytes=MAX_RESPONSE_BYTES, cancellation_check=context.cancellation_check)
            info.update(spawned=result.spawned, cli_invocations=int(result.spawned))
            (broker_dir / "stdout.jsonl").write_bytes(result.stdout)
            (broker_dir / "stderr.log").write_bytes(result.stderr)
            if result.error or result.exit_code != 0:
                raise ValueError(result.error or f"Provider exited with code {result.exit_code}")
            if self.provider == "codex_exec":
                envelope, usage, event_count = codex_response(result.stdout, decode_envelope=False)
            else:
                from researchops.runners.antigravity_response import parse_antigravity_response
                response = parse_antigravity_response(result.stdout, max_total_bytes=MAX_RESPONSE_BYTES)
                envelope, usage, event_count = response.structured_output, response.usage, response.event_count
            terminal = True
            document = parse_response_transport(envelope, max_bytes=MAX_RESPONSE_BYTES)
            if not result.cleanup_verified:
                raise ValueError("Control process cleanup could not be verified")
            imported = import_response(document, context.invocation_stage, output_dir, context.cancellation_check)
            events.append({"type": "model_response", "provider": self.provider,
                "event_count": event_count, "usage": usage, "observed_tool_actions": 0})
        except ResponseTransportError as exc:
            response_diagnostic, error = exc.to_dict(), str(exc)
        except SchemaValidationError:
            error = "Model transport failed JSON schema validation"
        except Exception as exc:
            error = str(exc)
        cleanup = result.cleanup_verified if result is not None else True
        if self.provider == "antigravity_exec" and result is not None and result.spawned and not terminal:
            cleanup = False  # Killing the client does not prove daemon-side turn cancellation.
        info.update(terminal_response=terminal, model_invocations=int(terminal), cleanup_verified=cleanup,
                    requested_model=self.model, requested_reasoning_effort="low", usage=usage, evidence_dir=str(broker_dir))
        if response_diagnostic:
            info["response_diagnostic"] = response_diagnostic
        observation = {**info, "stage": context.invocation_stage, "success": error is None,
            "error": error, "output_hashes": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in imported.items()}}
        self.calls.append(observation)
        (broker_dir / "observation.json").write_text(json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")
        events.append({"type": "development_boundary", **observation})
        code = 124 if result and result.timed_out else 77 if result and result.cancelled else 1 if error else 0
        return RunnerExecutionResult(success=error is None, exit_code=code,
            stdout=result.stdout.decode("utf-8", errors="replace") if result else "",
            stderr=result.stderr.decode("utf-8", errors="replace") if result else "",
            events=events, output_files=imported, error_message=error,
            cleanup_verified=cleanup, isolation=info, response_diagnostic=response_diagnostic)

    def execute_research(self, input_dir, tmp_dir, output_dir, project_dir, context):
        return self._execute(input_dir, output_dir, context)

    def execute_compose(self, input_dir, tmp_dir, output_dir, project_dir, context):
        return self._execute(input_dir, output_dir, context)
