"""OpenCode v2 session control for trusted operator production tasks.

The adapter uses the official ``opencode api`` client from an authenticated
control process.  Credentials remain in the operator's existing OpenCode
installation.  A worker session receives only a task-local HOME/TMPDIR and the
explicitly staged task input; no provider token, server password, SMTP setting
or host credential is copied into the session environment.

This is not a hostile-task sandbox.  Native session permissions constrain the
OpenCode file/web/MCP tools, while ordinary shell execution retains the host
user's authority.  The adapter never stops the shared OpenCode server, never
deletes a user session, and never reuses a session from another invocation.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional

from researchops.config import bundled_path
from researchops.errors import WorkspaceError
from researchops.package.loader import TaskPackageLoader
from researchops.runners.base import BaseRunner, RunnerExecutionResult, safe_response_diagnostic
from researchops.runners.development_process import run_bounded
from researchops.runners.development_runner import (
    MAX_RESPONSE_BYTES, OutputOnlyDevelopmentRunner, SAMPLE_NAMES, import_response,
)
from researchops.runners.native_mcp import inspect_native_mcp
from researchops.runners.production import (
    MAX_INPUT_BYTES, _import, _prompt, _resolve_submission, _write_output,
)
from researchops.runners.response_transport import (
    FileResponseReference, ResponseTransportError, parse_response_transport,
)
from researchops.runners.result_submission import HELPER_DIRECTORY, stage_submission_helper
from researchops.strict_json import strict_json_loads
from researchops.workspace.security import assert_path_contained


OPENCODE_PROVIDER = "opencode_exec"
MAX_API_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_API_CALLS = 20_000
MAX_MESSAGE_PAGES = 10
MAX_MESSAGES = 2_000
MAX_SESSION_TITLE = 120
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,199}\Z")
_VARIANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_OPERATION = re.compile(r"[a-z][A-Za-z0-9.]{1,80}\Z")
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\s-]{0,127}\Z")


def _normalize_mcp_action_prefix(server: str) -> str:
    """Normalize MCP server name to OpenCode tool action prefix.

    Hyphens are preserved per OpenCode v2 specification.
    Other non-alphanumeric characters (like dots, spaces) are normalized to underscores.
    """
    return re.sub(r"[^A-Za-z0-9_-]", "_", server)
_CONTROL_ENV_NAMES = ("USER", "LOGNAME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
_FORBIDDEN_NETWORK_TOOLS = {"webfetch", "websearch", "fetch", "browser", "search_web", "read_url_content"}
_FORBIDDEN_DELEGATION_TOOLS = {"task", "subagent", "agent", "question"}


class OpenCodeControlError(ValueError):
    """A fixed, content-free control or lifecycle failure code."""

    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and code else "OPENCODE_CONTROL_FAILED"
        super().__init__(self.code)


class OpenCodeTraceBudget:
    """Shared byte/event budget for polling, pages and control output."""

    def __init__(self, max_bytes: int, max_event_bytes: int, max_events: int):
        if any(type(value) is not int or value <= 0 for value in
               (max_bytes, max_event_bytes, max_events)):
            raise ValueError("OpenCode trace limits must be positive integers")
        self.max_bytes, self.max_event_bytes, self.max_events = max_bytes, max_event_bytes, max_events
        self.bytes = self.events = 0

    def add(self, size: int, kind: str = "event") -> None:
        if type(size) is not int or size < 0:
            raise OpenCodeControlError("OPENCODE_TRACE_INVALID")
        if size > self.max_event_bytes:
            raise OpenCodeControlError("OPENCODE_TRACE_EVENT_LIMIT")
        if self.events + 1 > self.max_events:
            raise OpenCodeControlError("OPENCODE_TRACE_EVENT_COUNT")
        if self.bytes + size > self.max_bytes:
            raise OpenCodeControlError("OPENCODE_TRACE_TOTAL_LIMIT")
        self.events += 1
        self.bytes += size

    def check_event_size(self, size: int) -> None:
        if type(size) is not int or size < 0 or size > self.max_event_bytes:
            raise OpenCodeControlError("OPENCODE_TRACE_EVENT_LIMIT")

    def add_event_count(self) -> None:
        if self.events + 1 > self.max_events:
            raise OpenCodeControlError("OPENCODE_TRACE_EVENT_COUNT")
        self.events += 1

    def add_overhead(self, size: int) -> None:
        if type(size) is not int or size < 0:
            raise OpenCodeControlError("OPENCODE_TRACE_INVALID")
        if self.bytes + size > self.max_bytes:
            raise OpenCodeControlError("OPENCODE_TRACE_TOTAL_LIMIT")
        self.bytes += size


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _phase_guard(context, deadline: float) -> None:
    check = getattr(context, "cancellation_check", None)
    if check is not None:
        try:
            cancelled = bool(check())
        except Exception:
            raise OpenCodeControlError("OPENCODE_CANCELLATION_CHECK_FAILED") from None
        if cancelled:
            raise OpenCodeControlError("OPENCODE_CANCELLED")
    if time.monotonic() >= deadline:
        raise OpenCodeControlError("OPENCODE_TIMEOUT")


def _usable(value) -> bool:
    return (isinstance(value, str) and bool(value) and "\x00" not in value
            and len(value) <= 4096 and not any(ord(c) < 32 for c in value))


def opencode_control_environment(binary, control_dir, *, environ=None) -> Dict[str, str]:
    """Use the operator's existing login without copying its credential files.

    The environment is deliberately allowlisted.  Provider API keys, SMTP
    settings, SSH agent sockets, task-local HOME values and arbitrary variables
    are not forwarded to the control CLI.
    """
    source = os.environ if environ is None else environ
    home = Path(source.get("HOME") or str(Path.home()))
    if not home.is_absolute():
        home = Path.home()
    candidates = [Path(binary).parent if isinstance(binary, str) and Path(binary).is_absolute() else None,
                  home / ".local/bin", home / ".npm-global/bin", Path("/usr/local/bin"),
                  Path("/usr/bin"), Path("/bin")]
    path = ":".join(dict.fromkeys(str(item) for item in candidates if item is not None))
    env = {"HOME": str(home), "LANG": "C.UTF-8", "TZ": "Asia/Seoul",
           "TMPDIR": str(control_dir), "PATH": path}
    for name in _CONTROL_ENV_NAMES:
        if _usable(source.get(name)):
            env[name] = source[name]
    return env


def _model_ref(model, effort) -> Optional[Dict[str, str]]:
    """Parse the app's canonical ``provider/model`` storage into v2 Model.Ref."""
    if model is None and effort is None:
        return None
    if not isinstance(model, str) or not model.strip():
        raise ValueError("A model is required when a variant is selected")
    model = model.strip()
    variant = None
    if effort is not None and (not isinstance(effort, str) or not _VARIANT.fullmatch(effort)):
        raise ValueError("Invalid OpenCode model variant")
    if effort:
        variant = effort
    if "#" in model:
        base, suffix = model.rsplit("#", 1)
        if not suffix or not _VARIANT.fullmatch(suffix) or (variant and variant != suffix):
            raise ValueError("Invalid OpenCode model variant")
        model, variant = base, suffix or variant
    provider, separator, model_id = model.partition("/")
    if (not separator or not provider or not model_id or not _MODEL_ID.fullmatch(model)
            or any(ord(c) < 32 or ord(c) == 127 for c in model)):
        raise ValueError("OpenCode model must be provider/model")
    result = {"providerID": provider, "id": model_id}
    if variant:
        result["variant"] = variant
    return result


_MCP_MUTATION_VERBS = (
    "delete", "remove", "destroy", "drop", "purge", "send", "mail", "email",
    "transfer", "charge", "pay", "buy", "commit", "archive", "cancel", "update",
    "post", "publish", "create", "insert", "edit", "modify", "patch", "write",
    "mutate", "execute", "exec", "disable",
)


def _mcp_mutation_patterns():
    return tuple(pattern for verb in _MCP_MUTATION_VERBS
                 for pattern in (verb + "*", "*_and_" + verb + "*", "*_then_" + verb + "*"))


def _permission_rules(*, stage: str, network_profile: str, work: Path, project_dir: Path,
                      input_dir: Path, control_dir: Path, mcp_servers: Iterable[str]) -> List[Dict[str, str]]:
    """Build ordered session rules; the last matching rule wins.

    The first rule denies every action.  Only the explicit ResearchOps tool
    surface is re-allowed.  ``--auto`` is not used, and a session rule cannot
    override an installation/managed deny.
    """
    rules: List[Dict[str, str]] = [{"action": "*", "resource": "*", "effect": "deny"}]
    for action in ("read", "edit", "glob", "grep", "shell", "execute"):
        rules.append({"action": action, "resource": "*", "effect": "allow"})
    for directory in (work, project_dir, input_dir):
        boundary = str(directory).rstrip("/") + "/*"
        rules.append({"action": "external_directory", "resource": str(directory), "effect": "allow"})
        rules.append({"action": "external_directory", "resource": boundary, "effect": "allow"})
        rules.append({"action": "read", "resource": boundary, "effect": "allow"})
        rules.append({"action": "edit", "resource": boundary, "effect": "allow"})
    control_boundary = str(control_dir).rstrip("/") + "/*"
    rules.append({"action": "external_directory", "resource": str(control_dir), "effect": "allow"})
    rules.append({"action": "external_directory", "resource": control_boundary, "effect": "allow"})
    rules.append({"action": "read", "resource": control_boundary, "effect": "allow"})
    rules.append({"action": "edit", "resource": control_boundary, "effect": "deny"})
    if stage == "research" and network_profile == "public-research":
        rules.extend((
            {"action": "webfetch", "resource": "*", "effect": "allow"},
            {"action": "websearch", "resource": "*", "effect": "allow"},
        ))
    # Operator-registered MCP servers are read/query capable in both phases.
    # Direct web and shell HTTP remain governed by the phase network policy.
    # This is a best-effort policy for trusted configured servers, not isolation.
    for entry in mcp_servers:
        server = entry.get("name") if isinstance(entry, dict) else entry
        if not isinstance(server, str) or not _SAFE_NAME.fullmatch(server):
            continue
        prefix = _normalize_mcp_action_prefix(server)
        # Trusted internal MVP: do not hide query tools solely because of their
        # names. Explicit mutation metadata/known mutation names still deny.
        rules.append({"action": f"{prefix}_*", "resource": "*", "effect": "allow"})
        rules.append({"action": "opencode_list_mcp_resources", "resource": server, "effect": "allow"})
        rules.append({"action": "opencode_read_mcp_resource", "resource": server + ":*", "effect": "allow"})
        for forbidden in _mcp_mutation_patterns():
            rules.append({"action": f"{prefix}_{forbidden}", "resource": "*", "effect": "deny"})
        if isinstance(entry, dict):
            for tool in entry.get("tools", []):
                t_name = tool.get("name") if isinstance(tool, dict) else tool
                cap = tool.get("capability") if isinstance(tool, dict) else "unknown"
                if not t_name or not isinstance(t_name, str):
                    continue
                if cap in ("query", "read", "allow"):
                    rules.append({"action": f"{prefix}_{t_name}", "resource": "*", "effect": "allow"})
                elif cap in ("mutation", "deny"):
                    rules.append({"action": f"{prefix}_{t_name}", "resource": "*", "effect": "deny"})
    # Credential-like paths stay unreadable even inside otherwise allowed roots.
    for pattern in ("*.env", "*.env.*", "**/*.env", "**/*.env.*", "**/.ssh/**",
                    "**/.config/opencode/**", "**/.local/share/opencode/**"):
        rules.append({"action": "read", "resource": pattern, "effect": "deny"})
    # Keep delegation/persona expansion disabled.  Code Mode execute stays
    # allowed for nested MCP queries; its utility/fetch surface is documented.
    # The built-in general agent also denies questions.
    for action in ("subagent", "question", "skill"):
        rules.append({"action": action, "resource": "*", "effect": "deny"})
    return rules


def _write_private_json(path: Path, value: Any) -> None:
    """Replace one app-owned JSON record without following links."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    assert_path_contained(path, path.parent)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                     separators=(",", ":")).encode("utf-8")
    fd, name = tempfile.mkstemp(prefix=".opencode-record-", dir=path.parent)
    temporary = Path(name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _append_jsonl(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    assert_path_contained(path, path.parent)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "ab") as stream:
        stream.write(raw)


def _safe_error_type(value) -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > 80:
        return None
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", value):
        return None
    return value


def _iso_millis(value) -> Optional[str]:
    if type(value) not in (int, float) or value < 0 or value > 2 ** 63:
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


@dataclass
class OpenCodeTool:
    name: str
    status: str
    success: bool
    denied: bool = False
    error_type: Optional[str] = None
    observed_started_at: Optional[str] = None
    observed_finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    command: Optional[str] = None
    path: Optional[str] = None
    target: Optional[str] = None
    content_sha256: Optional[str] = None
    exit_code: Optional[int] = None
    truncated: bool = False
    verified: bool = False
    provider_reported_success: bool = False
    output_sha256: Optional[str] = None
    output_bytes: int = 0
    unittest_failed_marker: bool = False
    unittest_ok_marker: bool = False
    unittest_count: Optional[int] = None


@dataclass
class OpenCodeTrace:
    terminal: bool
    successful: bool
    outcome: Optional[str]
    idle: bool
    final_text: Optional[str]
    final_message_finish: Optional[str]
    tools: List[OpenCodeTool] = field(default_factory=list)
    denied_actions: List[str] = field(default_factory=list)
    recovered_file_denials: int = 0
    provider_failure: Optional[Dict[str, Any]] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    response_error: Optional[str] = None
    assistant_message_count: int = 0
    message_bytes: int = 0
    final_message_id: Optional[str] = None
    final_model: Optional[Dict[str, Any]] = None
    final_agent: Optional[str] = None
    final_completed_at: Optional[str] = None


class OpenCodeClient:
    """Bounded wrapper around the official v2 ``opencode api`` command."""

    def __init__(self, binary: str, control_dir: Path, *, environ=None, log_path: Optional[Path] = None,
                 max_output_bytes: Optional[int] = None, budget: Optional[OpenCodeTraceBudget] = None,
                 cancellation_check=None, deadline: Optional[float] = None):
        self.binary = binary
        self.control_dir = Path(control_dir)
        self.env = opencode_control_environment(binary, self.control_dir, environ=environ)
        self.log_path = Path(log_path) if log_path is not None else self.control_dir / "opencode-control.jsonl"
        self.max_output_bytes = (max_output_bytes if max_output_bytes is not None else
                                 budget.max_bytes if budget is not None else MAX_API_OUTPUT_BYTES)
        self.budget = budget
        self.cancellation_check = cancellation_check
        self.deadline = deadline
        self.calls = 0
        self.control_cleanup_verified = True
        self.control_cleanup_evidence: List[Dict[str, Any]] = []

    def _guard(self) -> None:
        if self.cancellation_check is not None:
            try:
                cancelled = bool(self.cancellation_check())
            except Exception:
                raise OpenCodeControlError("OPENCODE_CANCELLATION_CHECK_FAILED") from None
            if cancelled:
                raise OpenCodeControlError("OPENCODE_CANCELLED")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise OpenCodeControlError("OPENCODE_TIMEOUT")

    def _remaining_timeout(self, timeout_seconds: int) -> int:
        if self.deadline is None:
            return timeout_seconds
        remaining = int(self.deadline - time.monotonic())
        if remaining < 1:
            raise OpenCodeControlError("OPENCODE_TIMEOUT")
        return max(1, min(timeout_seconds, remaining))

    def _record_local(self, operation: str, result, elapsed_ms: int) -> None:
        evidence = {"operation": operation, "elapsed_ms": elapsed_ms,
                    "exit_code": result.exit_code, "stdout_bytes": result.stdout_bytes,
                    "stderr_bytes": result.stderr_bytes,
                    "cleanup_verified": result.cleanup_verified}
        self.control_cleanup_evidence.append(evidence)
        if not result.cleanup_verified:
            self.control_cleanup_verified = False
        if self.budget is not None:
            size = len(result.stdout) + len(result.stderr)
            if operation == "session.message.list":
                # A page contains multiple events. Charge the raw page once;
                # _messages checks each message's event limit separately.
                self.budget.add_overhead(size)
                self.budget.add_event_count()
            else:
                self.budget.add(size, operation)
        log_entry = {**evidence, "started_at": _utcnow(),
                     "outcome": "ok" if not result.error and result.exit_code == 0
                     and result.cleanup_verified else "error"}
        if self.budget is not None:
            self.budget.add_overhead(len(json.dumps(log_entry, ensure_ascii=False,
                                                    separators=(",", ":")).encode("utf-8")))
        _append_jsonl(self.log_path, log_entry)

    def local(self, argv: List[str], *, operation: str, timeout_seconds: int = 10,
              max_output_bytes: int = 65_536):
        """Run a non-API control command with the same cleanup/guard evidence."""
        if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or "\x00" in item
                                                        for item in argv):
            raise ValueError("Invalid OpenCode local command")
        self._guard()
        if self.calls >= MAX_API_CALLS:
            raise OpenCodeControlError("OPENCODE_API_CALL_LIMIT")
        self.calls += 1
        started = time.monotonic()
        try:
            result = run_bounded(argv, cwd=self.control_dir, env=self.env,
                                 timeout_seconds=self._remaining_timeout(timeout_seconds),
                                 max_output_bytes=max_output_bytes,
                                 cancellation_check=self.cancellation_check,
                                 direct_stdout=True)
        except OpenCodeControlError:
            raise
        except (OSError, ValueError):
            _append_jsonl(self.log_path, {"operation": operation, "started_at": _utcnow(),
                                          "outcome": "control_error"})
            raise OpenCodeControlError("OPENCODE_CONTROL_UNAVAILABLE") from None
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._record_local(operation, result, elapsed_ms)
        if result.cancelled:
            raise OpenCodeControlError("OPENCODE_CANCELLED")
        if result.timed_out:
            raise OpenCodeControlError("OPENCODE_API_TIMEOUT")
        if not result.cleanup_verified:
            raise OpenCodeControlError("OPENCODE_CONTROL_CLEANUP_UNVERIFIED")
        return result

    def call(self, operation: str, *, body: Any = None, params: Optional[Dict[str, str]] = None,
             timeout_seconds: int = 30) -> Dict[str, Any]:
        if not isinstance(operation, str) or not _OPERATION.fullmatch(operation):
            raise ValueError("Invalid OpenCode API operation")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 86_400:
            raise ValueError("Invalid OpenCode API timeout")
        self._guard()
        if self.calls >= MAX_API_CALLS:
            raise OpenCodeControlError("OPENCODE_API_CALL_LIMIT")
        argv = [self.binary, "api"]
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False,
                                 separators=(",", ":")).encode("utf-8")
            if len(payload) > 1_000_000:
                raise OpenCodeControlError("OPENCODE_API_BODY_TOO_LARGE")
            argv += ["--data", payload.decode("utf-8")]
        for key, value in (params or {}).items():
            if not isinstance(key, str) or not isinstance(value, str) or "\x00" in key or "\x00" in value:
                raise ValueError("Invalid OpenCode API parameter")
            argv += ["--param", key + "=" + value]
        argv.append(operation)
        self.calls += 1
        started = time.monotonic()
        try:
            result = run_bounded(argv, cwd=self.control_dir, env=self.env,
                                 timeout_seconds=self._remaining_timeout(timeout_seconds),
                                 max_output_bytes=self.max_output_bytes,
                                 cancellation_check=self.cancellation_check,
                                 direct_stdout=True)
        except OpenCodeControlError:
            raise
        except (OSError, ValueError):
            _append_jsonl(self.log_path, {"operation": operation, "started_at": _utcnow(),
                                          "outcome": "control_error"})
            raise OpenCodeControlError("OPENCODE_CONTROL_UNAVAILABLE") from None
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._record_local(operation, result, elapsed_ms)
        if result.cancelled:
            raise OpenCodeControlError("OPENCODE_CANCELLED")
        if result.timed_out:
            raise OpenCodeControlError("OPENCODE_API_TIMEOUT" if self.deadline is None
                                       or time.monotonic() < self.deadline else "OPENCODE_TIMEOUT")
        if result.error or result.exit_code != 0:
            output_str = (result.stdout + b" " + result.stderr).decode("utf-8", "replace")
            if "operation not found" in output_str.lower() or "not supported" in output_str.lower():
                raise OpenCodeControlError("OPENCODE_API_OPERATION_NOT_FOUND")
            raise OpenCodeControlError("OPENCODE_API_FAILED")
        if not result.cleanup_verified:
            raise OpenCodeControlError("OPENCODE_CONTROL_CLEANUP_UNVERIFIED")
        if not result.stdout.strip():
            return {}
        try:
            document = strict_json_loads(result.stdout, max_bytes=self.max_output_bytes)
        except (ValueError, UnicodeError, RecursionError):
            raise OpenCodeControlError("OPENCODE_RESPONSE_INVALID") from None
        if not isinstance(document, dict):
            raise OpenCodeControlError("OPENCODE_RESPONSE_INVALID")
        if "error" in document and "data" not in document:
            err_msg = str(document.get("error", ""))
            if "operation not found" in err_msg.lower() or "not supported" in err_msg.lower():
                raise OpenCodeControlError("OPENCODE_API_OPERATION_NOT_FOUND")
            raise OpenCodeControlError("OPENCODE_API_ERROR")
        return document


def _service_available(binary: str, control_dir: Path, env: Dict[str, str], *,
                       client: Optional[OpenCodeClient] = None) -> bool:
    """Check the shared service without starting or restarting it."""
    argv = [binary, "service", "status"]
    if client is not None:
        result = client.local(argv, operation="service.status", timeout_seconds=10)
    else:
        result = run_bounded(argv, cwd=control_dir, env=env,
                             timeout_seconds=10, max_output_bytes=65_536)
        if not result.cleanup_verified:
            return False
    if result.error or result.exit_code != 0:
        return False
    try:
        text = result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeError:
        return False
    return bool(re.match(r"https?://", text))


def _version(binary: str, control_dir: Path, env: Dict[str, str], *,
             client: Optional[OpenCodeClient] = None) -> Optional[str]:
    argv = [binary, "--version"]
    if client is not None:
        result = client.local(argv, operation="version", timeout_seconds=10)
    else:
        result = run_bounded(argv, cwd=control_dir, env=env,
                             timeout_seconds=10, max_output_bytes=65_536)
        if not result.cleanup_verified:
            return None
    if result.error or result.exit_code != 0:
        return None
    try:
        text = result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeError:
        return None
    match = re.fullmatch(r"opencode\s+v?([0-9][0-9A-Za-z.+-]{0,63})", text)
    return match.group(1) if match else None


def _mcp_inventory(binary: str, control_dir: Path, env: Dict[str, str], *,
                   client: Optional[OpenCodeClient] = None,
                   location: Optional[Path] = None) -> Dict[str, Any]:
    """Read structured configured/connected MCP metadata without credentials."""
    inventory = {"checked": True, "servers": [], "error_code": None,
                 "resource_catalog_checked": False, "resource_count": 0,
                 "template_count": 0, "resource_names": [],
                 "limitations": ["MCP status/resource metadata only; no tool call or authentication test was performed."]}
    if client is not None and location is not None:
        try:
            document = client.call("mcp.list", params={"location[directory]": str(location)})
            data = document.get("data")
            if not isinstance(data, list):
                raise OpenCodeControlError("OPENCODE_MCP_STATUS_INVALID")
            servers = []
            for item in data:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    raise OpenCodeControlError("OPENCODE_MCP_STATUS_INVALID")
                name = item["name"]
                if not _SAFE_NAME.fullmatch(name):
                    raise OpenCodeControlError("OPENCODE_MCP_STATUS_INVALID")
                status_value = item.get("status")
                status = status_value.get("status") if isinstance(status_value, dict) else None
                if status not in {"connected", "pending", "disabled", "failed", "needs_auth"}:
                    status = "unknown"
                server_info: Dict[str, Any] = {"name": name, "cli_status": status,
                                               "enabled": status == "connected"}
                raw_tools = item.get("tools")
                if isinstance(raw_tools, list):
                    tools = []
                    for t in raw_tools:
                        if isinstance(t, dict) and isinstance(t.get("name"), str) and _SAFE_NAME.fullmatch(t["name"]):
                            cap = t.get("capability")
                            if cap not in ("query", "read", "allow", "mutation", "deny"):
                                cap = "unknown"
                            tools.append({"name": t["name"], "capability": cap})
                        elif isinstance(t, str) and _SAFE_NAME.fullmatch(t):
                            tools.append({"name": t, "capability": "unknown"})
                    server_info["tools"] = tools
                servers.append(server_info)
            if not servers:
                # A fresh temporary location may not expose global CLI entries
                # through the location API. Fall through to the read-only CLI
                # status summary rather than treating it as "no MCP".
                raise OpenCodeControlError("OPENCODE_MCP_EMPTY_LOCATION")
            inventory["servers"] = servers
            try:
                catalog = client.call("mcp.resource.catalog",
                                      params={"location[directory]": str(location)})
                resources = (catalog.get("data") or {}).get("resources") if isinstance(catalog.get("data"), dict) else None
                templates = (catalog.get("data") or {}).get("templates") if isinstance(catalog.get("data"), dict) else None
                if isinstance(resources, list) and isinstance(templates, list):
                    inventory["resource_catalog_checked"] = True
                    inventory["resource_count"] = len(resources)
                    inventory["template_count"] = len(templates)
                    inventory["resource_names"] = [
                        {"server": item.get("server"), "name": item.get("name")}
                        for item in resources[:100] if isinstance(item, dict)
                        and isinstance(item.get("server"), str) and isinstance(item.get("name"), str)]
                    inventory["template_names"] = [
                        {"server": item.get("server"), "name": item.get("name")}
                        for item in templates[:100] if isinstance(item, dict)
                        and isinstance(item.get("server"), str) and isinstance(item.get("name"), str)]
            except OpenCodeControlError as exc:
                inventory["resource_catalog_error_code"] = exc.code
            inventory["tool_policy"] = "trusted-registered-mcp-with-mutation-name-denies"
            inventory["limitations"].append(
                "MVP permits registered MCP tools except known mutation names; read-only behavior is not guaranteed."
            )
            return inventory
        except OpenCodeControlError as exc:
            inventory["error_code"] = exc.code
            # Fall through to the text-only CLI fallback.
    argv = [binary, "mcp", "list"]
    if client is not None:
        result = client.local(argv, operation="mcp.list", timeout_seconds=15,
                              max_output_bytes=MAX_API_OUTPUT_BYTES)
    else:
        result = run_bounded(argv, cwd=control_dir, env=env,
                             timeout_seconds=15, max_output_bytes=MAX_API_OUTPUT_BYTES)
    inventory = {"checked": True, "servers": [], "error_code": None,
                 "resource_catalog_checked": False, "resource_count": 0,
                 "template_count": 0, "resource_names": [],
                 "limitations": ["CLI status summary only; no MCP tool call or authentication test was performed."]}
    if result.error or result.exit_code != 0 or not result.cleanup_verified:
        inventory.update(checked=False, error_code="OPENCODE_MCP_STATUS_UNAVAILABLE")
        return inventory
    try:
        lines = result.stdout.decode("utf-8", errors="strict").splitlines()
    except UnicodeError:
        inventory.update(checked=False, error_code="OPENCODE_MCP_STATUS_INVALID")
        return inventory
    servers = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^[^A-Za-z0-9_.-]*([A-Za-z0-9][A-Za-z0-9_.-]{0,127})\s+(.+)$", line)
        if not match:
            continue
        name, status = match.group(1), match.group(2).strip()
        normalized = status.lower()
        if normalized not in {"connected", "failed", "disabled", "needs auth", "needs_auth"}:
            continue
        servers.append({"name": name, "cli_status": normalized,
                        "enabled": normalized == "connected"})
        if len(servers) >= 128:
            break
    inventory["servers"] = servers
    return inventory


def _safe_tool_name(value) -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    return value


def _usage(value) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ("input", "output", "reasoning"):
        count = value.get(key)
        if type(count) in (int, float) and count >= 0:
            result[key] = int(count)
    cache = value.get("cache")
    if isinstance(cache, dict):
        result["cache"] = {key: int(cache[key]) for key in ("read", "write")
                           if type(cache.get(key)) in (int, float) and cache[key] >= 0}
    return result


def _message_model(value) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    provider, model_id = value.get("providerID"), value.get("id")
    if not isinstance(provider, str) or not provider or not isinstance(model_id, str) or not model_id:
        return None
    result = {"providerID": provider, "id": model_id}
    variant = value.get("variant")
    if isinstance(variant, str) and variant:
        result["variant"] = variant
    return result


def _mcp_protocol_error(content) -> bool:
    if not isinstance(content, list):
        return False
    text = "\n".join(item.get("text", "") for item in content
                     if isinstance(item, dict) and item.get("type") == "text"
                     and isinstance(item.get("text"), str))
    return bool(re.search(r'"isError"\s*:\s*true', text, re.IGNORECASE))


def _provider_failure(error) -> Optional[Dict[str, Any]]:
    """Keep actionable classification, never provider bodies/headers/credentials."""
    if not isinstance(error, dict):
        return None
    kind = error.get("type") if isinstance(error.get("type"), str) else None
    status = error.get("status")
    if kind == "provider.quota":
        code, retryable = "OPENCODE_PROVIDER_QUOTA_EXHAUSTED", False
    elif kind in {"provider.rate_limit", "provider.rate_limited"} or status == 429:
        code, retryable = "OPENCODE_PROVIDER_RATE_LIMITED", True
    else:
        return None
    return {"code": code, "http_status": status if type(status) is int else None,
            "retryable": retryable}


def _parse_trace(messages: List[Any], session: Dict[str, Any], *, max_bytes: int,
                 expected_model: Optional[Dict[str, Any]] = None,
                 expected_agent: Optional[str] = None,
                 recover_blocked_file_access: bool = False) -> OpenCodeTrace:
    """Project final assistant text and bounded, identity-checked tool evidence.

    Malformed identity/content is preserved as a failed trace with partial tool
    evidence instead of raising, so callers can retain bounded diagnostics while
    still refusing import.
    """
    total = 0
    tools: List[OpenCodeTool] = []
    denied = []
    fatal_denied = []
    recovered_file_denials = 0
    assistant_count = 0
    last_assistant = None
    seen_ids = set()
    identity_error = parse_error = False
    session_model = _message_model(session.get("model"))
    session_agent = session.get("agent") if isinstance(session.get("agent"), str) else None
    required_model = _message_model(expected_model) if expected_model is not None else session_model
    required_agent = expected_agent or session_agent
    for message in messages:
        if not isinstance(message, dict) or message.get("type") != "assistant":
            continue
        try:
            raw = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ValueError("OpenCode message projection is invalid") from None
        total += len(raw)
        if total > max_bytes:
            raise ValueError("OpenCode message projection exceeds the log budget")
        assistant_count += 1
        message_id = message.get("id")
        if (not isinstance(message_id, str) or not message_id or len(message_id) > 128
                or message_id in seen_ids
                or any(ord(char) < 32 or ord(char) == 127 for char in message_id)):
            identity_error = True
        else:
            seen_ids.add(message_id)
        model = _message_model(message.get("model"))
        agent = message.get("agent")
        if model is None or not isinstance(agent, str) or not agent:
            identity_error = True
        if session_model is not None and model is not None and model != session_model:
            identity_error = True
        if session_agent is not None and isinstance(agent, str) and agent != session_agent:
            identity_error = True
        content = message.get("content")
        if not isinstance(content, list):
            parse_error = True
            continue
        texts = []
        for block in content:
            if not isinstance(block, dict):
                parse_error = True
                continue
            if block.get("type") == "text":
                if isinstance(block.get("text"), str):
                    texts.append(block["text"])
                continue
            if block.get("type") != "tool":
                continue
            name = _safe_tool_name(block.get("name"))
            if name is None:
                parse_error = True
                continue
            state = block.get("state")
            if not isinstance(state, dict):
                parse_error = True
                continue
            status = state.get("status")
            if status not in {"streaming", "running", "completed", "error"}:
                parse_error = True
                continue
            error = state.get("error") if isinstance(state.get("error"), dict) else {}
            error_type = _safe_error_type(error.get("type"))
            denied_action = bool(error_type and ("permission" in error_type.lower()
                                                 or "denied" in error_type.lower()))
            data = state.get("input") if isinstance(state.get("input"), dict) else {}
            metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
            tool_content = state.get("content") if isinstance(state.get("content"), list) else []
            truncated = metadata.get("truncated") is True
            command = data.get("command") if isinstance(data.get("command"), str) else None
            path = data.get("path") if isinstance(data.get("path"), str) else None
            if path is None and isinstance(data.get("filePath"), str):
                path = data["filePath"]
            target = data.get("url") if isinstance(data.get("url"), str) else None
            if target is None and isinstance(data.get("query"), str):
                target = data["query"]
            raw_content = data.get("content") if isinstance(data.get("content"), str) else None
            if raw_content is None and isinstance(data.get("newString"), str):
                raw_content = data["newString"]
            exit_code = metadata.get("exit")
            if type(exit_code) not in (int, float) or exit_code < 0:
                exit_code = None
            else:
                exit_code = int(exit_code)
            lowered = name.lower()
            provider_success = status == "completed"
            output_text = "\n".join(item.get("text", "") for item in tool_content
                                    if isinstance(item, dict) and item.get("type") == "text"
                                    and isinstance(item.get("text"), str))
            unittest_failed_marker = bool(re.search(r"^FAILED\b", output_text, re.MULTILINE))
            unittest_ok_marker = bool(re.search(r"^OK(?:\s|$)", output_text, re.MULTILINE))
            count_match = re.search(r"^Ran\s+(\d+)\s+tests?\s+in\s+", output_text, re.MULTILINE)
            unittest_count = int(count_match.group(1)) if count_match else None
            if lowered == "shell":
                verified = provider_success and exit_code is not None and not truncated
                success = verified and exit_code == 0 and not unittest_failed_marker
            elif lowered in {"write", "edit", "patch"}:
                verified = provider_success and path is not None and not truncated
                success = verified
            elif lowered in {"webfetch", "websearch"}:
                verified = provider_success and target is not None and not truncated
                success = verified
            else:
                protocol_error = _mcp_protocol_error(tool_content)
                verified = (provider_success and not truncated and not protocol_error
                            and bool(tool_content))
                success = verified
            timing = block.get("time") if isinstance(block.get("time"), dict) else {}
            started = _iso_millis(timing.get("ran") or timing.get("created"))
            finished = _iso_millis(timing.get("completed"))
            duration = None
            if isinstance(timing.get("ran"), (int, float)) and isinstance(timing.get("completed"), (int, float)):
                delta = timing["completed"] - timing["ran"]
                if 0 <= delta <= 86_400_000:
                    duration = int(delta)
            tool = OpenCodeTool(
                name=name, status=status, success=success, denied=denied_action,
                error_type=error_type, observed_started_at=started,
                observed_finished_at=finished, duration_ms=duration,
                command=command[:4096] if command else None,
                path=path[:4096] if path else None,
                target=target[:4096] if target else None,
                content_sha256=(hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
                                if raw_content is not None else None),
                exit_code=exit_code, truncated=truncated, verified=verified,
                provider_reported_success=provider_success,
                output_sha256=hashlib.sha256(output_text.encode("utf-8")).hexdigest() if output_text else None,
                output_bytes=len(output_text.encode("utf-8")),
                unittest_failed_marker=unittest_failed_marker,
                unittest_ok_marker=unittest_ok_marker,
                unittest_count=unittest_count)
            tools.append(tool)
            if denied_action and name not in denied:
                denied.append(name)
            if denied_action:
                # A native external-directory rejection prevented the file
                # operation. Production may recover inside its workspace;
                # final submission, model, cleanup and message gates still apply.
                recovered = (recover_blocked_file_access and status == "error"
                             and name.lower() in {"read", "write", "edit"}
                             and error_type == "permission.rejected"
                             and error.get("message") == "Permission denied: external_directory")
                if recovered:
                    recovered_file_denials += 1
                elif name not in fatal_denied:
                    fatal_denied.append(name)
        last_assistant = {"text": "\n".join(texts), "finish": message.get("finish"),
                          "error": isinstance(message.get("error"), dict),
                          "provider_failure": _provider_failure(message.get("error")),
                          "id": message_id, "model": model, "agent": agent,
                          "time": message.get("time") if isinstance(message.get("time"), dict) else {}}
    final_text = None
    final_finish = None
    final_model = None
    final_agent = None
    final_completed_at = None
    final_time = {}
    if last_assistant is not None:
        final_text = last_assistant["text"] or None
        final_finish = last_assistant["finish"] if isinstance(last_assistant["finish"], str) else None
        final_model = last_assistant["model"]
        final_agent = last_assistant["agent"]
        final_time = last_assistant["time"]
        completed = final_time.get("completed")
        final_completed_at = _iso_millis(completed) if type(completed) in (int, float) else None
    outcome = session.get("outcome") if isinstance(session.get("outcome"), str) else None
    idle = isinstance(session.get("time"), dict) and session["time"].get("idle") is not None
    terminal = bool(outcome and idle)
    response_error = None
    provider_failure = last_assistant.get("provider_failure") if last_assistant else None
    if not terminal:
        response_error = "OPENCODE_SESSION_NOT_TERMINAL"
    elif outcome != "succeeded":
        response_error = (provider_failure["code"] if provider_failure is not None
                          else "OPENCODE_SESSION_" + outcome.upper())
    elif last_assistant is None or not final_text:
        response_error = "OPENCODE_FINAL_TEXT_MISSING"
    elif last_assistant.get("error"):
        response_error = (provider_failure["code"] if provider_failure is not None
                          else "OPENCODE_FINAL_TURN_ERROR")
    elif final_finish is None:
        response_error = "OPENCODE_FINAL_FINISH_MISSING"
    elif final_finish != "stop":
        response_error = "OPENCODE_FINAL_TURN_INCOMPLETE"
    elif (type(final_time.get("created")) not in (int, float)
          or type(final_time.get("completed")) not in (int, float)
          or final_time["completed"] < final_time["created"]):
        response_error = "OPENCODE_FINAL_COMPLETION_MISSING"
    elif required_model is not None and (
            final_model is None
            or final_model.get("providerID") != required_model.get("providerID")
            or final_model.get("id") != required_model.get("id")
            or (required_model.get("variant")
                and final_model.get("variant") != required_model.get("variant"))):
        response_error = "OPENCODE_FINAL_MODEL_MISMATCH"
    elif required_agent is not None and final_agent != required_agent:
        response_error = "OPENCODE_FINAL_AGENT_MISMATCH"
    elif any(tool.status in {"running", "streaming"} for tool in tools):
        response_error = "OPENCODE_TOOL_NOT_TERMINAL"
    elif identity_error:
        response_error = "OPENCODE_MESSAGE_IDENTITY_INVALID"
    elif parse_error:
        response_error = "OPENCODE_MESSAGE_INVALID"
    elif fatal_denied:
        response_error = "OPENCODE_PERMISSION_DENIED"
    return OpenCodeTrace(terminal=terminal, successful=response_error is None, outcome=outcome,
                         idle=idle, final_text=final_text, final_message_finish=final_finish,
                         tools=tools, denied_actions=denied,
                         recovered_file_denials=recovered_file_denials,
                         provider_failure=provider_failure,
                         usage=_usage(session.get("tokens")),
                         response_error=response_error, assistant_message_count=assistant_count,
                         message_bytes=total,
                         final_message_id=last_assistant["id"] if last_assistant else None,
                         final_model=final_model, final_agent=final_agent,
                         final_completed_at=final_completed_at)

def _is_mcp_mutation_action(subname: str) -> bool:
    """Best-effort naming policy shared by native rules and audit."""
    return any(fnmatchcase(subname.lower(), pattern) for pattern in _mcp_mutation_patterns())


def _summarize_mcp_tools(mcp: Dict[str, Any], trace: OpenCodeTrace) -> List[Dict[str, Any]]:
    """Attribute observed OpenCode MCP actions without exposing payloads.

    Connection status alone is never a call receipt. The action name is matched
    against the operator's connected server names; no output body is used to
    claim a verified MCP result.
    """
    server_tools: Dict[str, Dict[str, str]] = {}
    for item in (mcp.get("servers") or []):
        if isinstance(item, dict) and item.get("enabled") and _SAFE_NAME.fullmatch(str(item.get("name"))):
            s_name = str(item.get("name"))
            t_map: Dict[str, str] = {}
            for t in item.get("tools", []):
                if isinstance(t, dict) and t.get("name"):
                    t_map[t["name"].lower()] = t.get("capability", "unknown")
            server_tools[s_name] = t_map

    summaries = []
    for tool in trace.tools:
        lowered = tool.name.lower()
        for server, tool_caps in server_tools.items():
            prefix = _normalize_mcp_action_prefix(server.lower()) + "_"
            if lowered.startswith(prefix) and len(tool.name) > len(prefix):
                subname = lowered[len(prefix):]
                if subname in tool_caps:
                    declared_cap = tool_caps[subname]
                    if declared_cap in ("query", "read", "allow"):
                        capability = "query"
                        is_mutation = False
                    elif declared_cap in ("mutation", "deny"):
                        capability = "mutation"
                        is_mutation = True
                    else:
                        capability = "unknown"
                        is_mutation = True
                else:
                    is_mutation = _is_mcp_mutation_action(subname)
                    capability = "mutation" if is_mutation else "query"

                summaries.append({"server": server, "tool": tool.name[len(prefix):],
                                  "status": "unverified" if not tool.success else "completed",
                                  "capability": capability,
                                  "error_code": tool.error_type,
                                  "success": tool.success and not is_mutation and capability == "query",
                                  "denied": tool.denied,
                                  "provider_reported_success": tool.provider_reported_success,
                                  "output_verified": tool.verified and not is_mutation and capability == "query"})
                break
        if len(summaries) >= 500:
            break
    return summaries


def _session_data(document: Dict[str, Any]) -> Dict[str, Any]:
    data = document.get("data")
    if not isinstance(data, dict):
        raise OpenCodeControlError("OPENCODE_SESSION_RESPONSE_INVALID")
    return data


def _messages(client: OpenCodeClient, session_id: str, *, message_type: str,
              deadline: float, max_bytes: int,
              budget: Optional[OpenCodeTraceBudget] = None) -> List[Dict[str, Any]]:
    values: List[Dict[str, Any]] = []
    cursor = None
    total = 0
    pages = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OpenCodeControlError("OPENCODE_SESSION_DEADLINE")
        params = {"sessionID": session_id, "limit": "200", "type": message_type}
        if cursor:
            # The v2 message cursor already carries its order/direction.
            # Combining it with an explicit order is rejected (HTTP 400).
            params["cursor"] = cursor
        else:
            params["order"] = "asc"
        document = client.call("session.message.list", params=params,
                               timeout_seconds=max(1, min(30, int(remaining))))
        data = document.get("data")
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise OpenCodeControlError("OPENCODE_MESSAGES_INVALID")
        values.extend(data)
        pages += 1
        if len(values) > MAX_MESSAGES or pages > MAX_MESSAGE_PAGES:
            raise OpenCodeControlError("OPENCODE_MESSAGE_LIMIT")
        try:
            page_raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            total += len(page_raw)
            if budget is not None:
                for item in data:
                    item_raw = json.dumps(item, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    budget.check_event_size(len(item_raw))
                    budget.add_event_count()
        except OpenCodeControlError:
            raise
        except (TypeError, ValueError, UnicodeError):
            raise OpenCodeControlError("OPENCODE_MESSAGES_INVALID") from None
        if total > max_bytes:
            raise OpenCodeControlError("OPENCODE_MESSAGE_LIMIT")
        cursor = (document.get("cursor") or {}).get("next") if isinstance(document.get("cursor"), dict) else None
        if not cursor:
            return values
        if not isinstance(cursor, str) or len(cursor) > 4096:
            raise OpenCodeControlError("OPENCODE_MESSAGES_INVALID")


def _partial_message_diagnostics(client: OpenCodeClient, session_id: str) -> Dict[str, Any]:
    """Best-effort, bounded evidence after timeout/cancel; never promotes success."""
    old_deadline, old_cancel = client.deadline, client.cancellation_check
    client.deadline, client.cancellation_check = time.monotonic() + 2, None
    try:
        messages = _messages(client, session_id, message_type="assistant",
                             deadline=time.monotonic() + 2, max_bytes=1_000_000)
        names = []
        for message in messages[:2000]:
            for block in (message.get("content") or []) if isinstance(message, dict) else []:
                if isinstance(block, dict) and block.get("type") == "tool":
                    name = _safe_tool_name(block.get("name"))
                    if name and name not in names:
                        names.append(name)
        return {"complete": False, "partial": True, "assistant_messages": len(messages),
                "tool_names": names[:100]}
    except Exception:
        return {"complete": False, "partial": True, "collect_error": True}
    finally:
        client.deadline, client.cancellation_check = old_deadline, old_cancel


def _wait_terminal(client: OpenCodeClient, session_id: str, *, deadline: float,
                   cancellation_check) -> Dict[str, Any]:
    failures = 0
    while True:
        if cancellation_check is not None and cancellation_check():
            raise OpenCodeControlError("OPENCODE_CANCELLED")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OpenCodeControlError("OPENCODE_TIMEOUT")
        try:
            session = _session_data(client.call("session.get", params={"sessionID": session_id},
                                                timeout_seconds=max(1, min(15, int(remaining)))))
            failures = 0
        except OpenCodeControlError as exc:
            if exc.code in {"OPENCODE_CANCELLED", "OPENCODE_TIMEOUT", "OPENCODE_CANCELLATION_CHECK_FAILED"} \
                    or exc.code.startswith("OPENCODE_TRACE_"):
                raise
            failures += 1
            if failures >= 3:
                raise
            time.sleep(min(1.0, max(0.0, remaining)))
            continue
        outcome = session.get("outcome") if isinstance(session.get("outcome"), str) else None
        idle = isinstance(session.get("time"), dict) and session["time"].get("idle") is not None
        if outcome and idle:
            return session
        time.sleep(min(1.0, max(0.05, remaining)))


def _resource_snapshot(client: OpenCodeClient, session_id: str, location: Path) -> Dict[str, Any]:
    """Inspect live shells/PTYs belonging to this invocation's location/session.

    ``shell.list`` contains only currently running shells. ``pty.list`` includes
    exited PTYs, so only running entries count. Persistent PTYs are scoped to the
    recorded session. Any API error, malformed response or location/session
    mismatch leaves ``cleanup_verified`` false; it is never treated as empty.
    """
    result = {"shell_list_checked": False, "running_shells": None,
              "pty_list_checked": False, "running_ptys": None,
              "persistent_terminals_checked": False, "running_terminals": None,
              "errors": []}

    def location_matches(document):
        location_value = ((document.get("location") or {}).get("directory")
                          if isinstance(document.get("location"), dict) else None)
        return isinstance(location_value, str) and Path(location_value).resolve() == Path(location).resolve()

    try:
        document = client.call("shell.list", params={"location[directory]": str(location)})
        if not location_matches(document) or not isinstance(document.get("data"), list):
            raise OpenCodeControlError("OPENCODE_SHELL_LIST_INVALID")
        if any(not isinstance(item, dict)
               or item.get("status") not in {"running", "exited", "timeout", "killed"}
               for item in document["data"]):
            raise OpenCodeControlError("OPENCODE_SHELL_LIST_INVALID")
        result["shell_list_checked"] = True
        result["running_shells"] = sum(1 for item in document["data"]
                                       if item.get("status") == "running")
    except OpenCodeControlError as exc:
        result["errors"].append(exc.code)

    try:
        document = client.call("pty.list", params={"location[directory]": str(location)})
        if not location_matches(document) or not isinstance(document.get("data"), list):
            raise OpenCodeControlError("OPENCODE_PTY_LIST_INVALID")
        if any(not isinstance(item, dict) or item.get("status") not in {"running", "exited"}
               for item in document["data"]):
            raise OpenCodeControlError("OPENCODE_PTY_LIST_INVALID")
        result["pty_list_checked"] = True
        result["running_ptys"] = sum(1 for item in document["data"]
                                     if item.get("status") == "running")
    except OpenCodeControlError as exc:
        result["errors"].append(exc.code)

    try:
        document = client.call("server.experimental.persistentPty.list",
                               params={"sessionID": session_id})
        data = document.get("data")
        if not isinstance(data, list):
            raise OpenCodeControlError("OPENCODE_TERMINAL_LIST_INVALID")
        if any(not isinstance(item, dict) or item.get("sessionID") != session_id
               or item.get("status") not in {"running", "exited"} for item in data):
            raise OpenCodeControlError("OPENCODE_TERMINAL_IDENTITY_INVALID")
        result["persistent_terminals_checked"] = True
        result["running_terminals"] = sum(1 for item in data if item.get("status") == "running")
    except OpenCodeControlError as exc:
        result["errors"].append(exc.code)

    result["cleanup_verified"] = bool(
        result["shell_list_checked"] and result["running_shells"] == 0
        and result["pty_list_checked"] and result["running_ptys"] == 0
        and result["persistent_terminals_checked"] and result["running_terminals"] == 0
        and not result["errors"])
    return result


def _cleanup_snapshot(client: OpenCodeClient, session_id: str, location: Path) -> Dict[str, Any]:
    """Use a bounded cleanup grace without letting the phase deadline mask cleanup."""
    old_deadline, old_cancel = client.deadline, client.cancellation_check
    client.deadline = time.monotonic() + 5
    client.cancellation_check = None
    try:
        return _resource_snapshot(client, session_id, location)
    finally:
        client.deadline, client.cancellation_check = old_deadline, old_cancel


def _interrupt_and_observe(client: OpenCodeClient, session_id: str, location: Path) -> Dict[str, Any]:
    """Interrupt only the recorded session; never stop the shared server."""
    old_deadline, old_cancel = client.deadline, client.cancellation_check
    client.deadline = time.monotonic() + 5
    client.cancellation_check = None
    interrupted = None
    try:
        try:
            response = client.call("session.interrupt", params={"sessionID": session_id},
                                   timeout_seconds=15)
            interrupted = response.get("interrupted") is True
        except OpenCodeControlError:
            interrupted = None
        deadline = time.monotonic() + 5
        session = {}
        while time.monotonic() < deadline:
            try:
                session = _session_data(client.call("session.get", params={"sessionID": session_id},
                                                    timeout_seconds=5))
            except OpenCodeControlError:
                break
            outcome = session.get("outcome") if isinstance(session.get("outcome"), str) else None
            idle = isinstance(session.get("time"), dict) and session["time"].get("idle") is not None
            if outcome and idle:
                break
            time.sleep(0.25)
        resources = _resource_snapshot(client, session_id, location)
    finally:
        client.deadline, client.cancellation_check = old_deadline, old_cancel
    terminal = bool(session.get("outcome") and isinstance(session.get("time"), dict)
                    and session["time"].get("idle") is not None)
    return {"interrupted": interrupted, "terminal": terminal, "resources": resources,
            "cleanup_verified": bool(terminal and resources["cleanup_verified"]),
            "session": session}


class OpenCodeRunner(BaseRunner):
    """Trusted production runner backed by a fresh OpenCode v2 session."""

    def __init__(self, opencode_binary: str = "opencode", *, environ=None, settings=None):
        self.binary = opencode_binary
        self.environ = environ
        self.settings = settings

    def preflight(self, *, deep: bool = False):
        binary_path = shutil.which(self.binary)
        info = {
            "provider": OPENCODE_PROVIDER, "tier": "trusted-operator-production",
            "available": bool(binary_path), "binary_path": binary_path, "ready": bool(binary_path),
            "version": None, "authenticated": None, "authentication_status": "not_checked",
            "credential_contents_inspected": False, "spawned": False,
            "hostile_process_isolation": False, "service_available": None,
            "model_catalog_available": None, "blocker": None, "blockers": [],
            "internal_missing": [], "operator_missing": [],
            "notes": ("OpenCode v2 session API control uses the operator's existing login. "
                      "No provider token or server password is copied into a worker session. "
                      "Native permissions are not a hostile-task sandbox."),
        }
        if not binary_path:
            info.update(ready=False, blocker="RUNNER-BINARY-UNAVAILABLE",
                        blockers=[{"id": "RUNNER-BINARY-UNAVAILABLE", "owner": "operator",
                                   "category": "configuration", "scope": OPENCODE_PROVIDER,
                                   "reason": "Configured OpenCode executable is unavailable",
                                   "resume_condition": "Configure an installed OpenCode v2 executable path"}])
            info["operator_missing"] = ["RUNNER-BINARY-UNAVAILABLE"]
            return info
        if not deep:
            # Executable discovery is not service/authentication readiness. A
            # shallow probe never starts the shared server or creates a session.
            info.update(ready=False, blocker="RUNNER-CAPABILITY-NOT-CHECKED",
                        blockers=[{"id": "RUNNER-CAPABILITY-NOT-CHECKED", "owner": "operator",
                                   "category": "configuration", "scope": OPENCODE_PROVIDER,
                                   "reason": "OpenCode service/API readiness was not checked",
                                   "resume_condition": "Run the deep preflight or an explicit invocation"}])
            info["operator_missing"] = ["RUNNER-CAPABILITY-NOT-CHECKED"]
            return info
        control = Path(tempfile.mkdtemp(prefix="opencode-preflight-"))
        try:
            client = OpenCodeClient(binary_path, control, environ=self.environ,
                                    log_path=control / "preflight.jsonl")
            env = opencode_control_environment(binary_path, control, environ=self.environ)
            info["version"] = _version(binary_path, control, env)
            info["service_available"] = _service_available(binary_path, control, env)
            if not info["service_available"]:
                info.update(ready=False, blocker="RUNNER-SERVICE-UNAVAILABLE",
                            blockers=[{"id": "RUNNER-SERVICE-UNAVAILABLE", "owner": "operator",
                                       "category": "configuration", "scope": OPENCODE_PROVIDER,
                                       "reason": "The existing OpenCode background service is unavailable",
                                       "resume_condition": "Start the operator's existing OpenCode service without changing its configuration"}])
                info["operator_missing"] = ["RUNNER-SERVICE-UNAVAILABLE"]
                return info
            try:
                document = client.call("model.list", params={"location[directory]": str(control)},
                                       timeout_seconds=15)
                models = document.get("data")
                if not isinstance(models, list):
                    raise OpenCodeControlError("OPENCODE_MODEL_LIST_INVALID")
                info["model_catalog_available"] = True
                info["cli_invocations"] = client.calls
            except OpenCodeControlError:
                info.update(ready=False, blocker="RUNNER-API-UNAVAILABLE",
                            blockers=[{"id": "RUNNER-API-UNAVAILABLE", "owner": "operator",
                                       "category": "configuration", "scope": OPENCODE_PROVIDER,
                                       "reason": "OpenCode model/API capability is unavailable",
                                       "resume_condition": "Confirm the OpenCode service API and model catalog"}])
                info["operator_missing"] = ["RUNNER-API-UNAVAILABLE"]
            return info
        finally:
            shutil.rmtree(control, ignore_errors=True)

    def execute_research(self, input_dir, tmp_dir, output_dir, project_dir, context):
        return self._execute("research", input_dir, tmp_dir, output_dir, project_dir, context)

    def execute_compose(self, input_dir, tmp_dir, output_dir, project_dir, context):
        return self._execute("compose", input_dir, tmp_dir, output_dir, project_dir, context)

    def _execute(self, stage, input_dir, tmp_dir, output_dir, project_dir, context):
        started = time.monotonic()
        deadline = started + context.timeout_seconds
        budget = OpenCodeTraceBudget(context.trace_max_bytes, context.trace_max_event_bytes,
                                     context.trace_max_events)
        info = self.preflight()
        info.update(cli_invocations=0, model_invocations=0, upstream_request_count=None,
                    requested_model=context.model, requested_reasoning_effort=context.reasoning_effort,
                    observed_model=None, observed_agent=None, observed_variant=None,
                    credential_bridge=False, cleanup_scope="owned-session-and-location",
                    network_profile=context.network_profile,
                    allow_http_requests=(stage == "research" and context.network_profile == "public-research"
                                         and context.allow_http_requests is True),
                    requested_permission_mode="native-session-rules",
                    egress_isolation=False,
                    filesystem_quota_scope="bounded-output-import-only",
                    network_policy_enforcement="native-session-permissions-and-observed-tools")
        event = {"type": "runner_execution", "provider": OPENCODE_PROVIDER,
                 "task_id": context.task_id, "run_id": context.run_id, "stage": stage,
                 "attempt": context.attempt, "fencing_token": context.fencing_token,
                 "authentication_status": "not_checked", "spawned": False,
                 "requested_model": context.model, "requested_reasoning_effort": context.reasoning_effort,
                 "model_invocations": 0, "upstream_request_count": None,
                 "allow_http_requests": info["allow_http_requests"],
                 "observed_model": None, "observed_agent": None, "observed_variant": None}
        log_dir = context.trace_log_dir
        session_id = None
        session_create_attempted = False
        session_record = None
        client = None
        work = None
        control = None
        image_broker = None
        acquisition = context.acquisition_session
        acquisition_started = acquisition_closed = False
        acquisition_cleanup = acquisition is None
        broker_clean = True
        trace = None
        outputs: Dict[str, Path] = {}
        error = None
        response_diagnostic = None
        log_files: Dict[str, Path] = {}
        final_code = None
        interruption = None
        mcp = {}

        def close_acquisition():
            nonlocal acquisition_closed, acquisition_cleanup
            if acquisition is None or acquisition_closed:
                return
            acquisition_closed = True
            try:
                acquisition.close()
                acquisition_cleanup = acquisition.cleanup_verified is True
            except Exception:
                acquisition_cleanup = False
            info["research_acquisition"] = {"enabled": True, "started": acquisition_started,
                                            "cleanup_verified": acquisition_cleanup}
            event["research_acquisition"] = dict(info["research_acquisition"])

        try:
            _phase_guard(context, deadline)
            if not info["available"]:
                raise ValueError("RUNNER-BINARY-UNAVAILABLE: Configured OpenCode executable is unavailable")
            if context.invocation_stage != stage or stage not in {"research", "compose"}:
                raise ValueError("Invocation stage mismatch")
            if context.network_profile not in {"none", "public-research"}:
                raise ValueError("Unsupported OpenCode network profile")
            if context.timezone != "Asia/Seoul":
                raise ValueError("Invalid task timezone")
            directories = [Path(item) for item in (input_dir, tmp_dir, output_dir, project_dir)]
            for directory in directories:
                if not directory.is_absolute() or directory == Path("/") or not directory.is_dir():
                    raise ValueError("Invocation directories must be existing absolute directories")
                assert_path_contained(directory, directory)
            if len(set(directories)) != 4 or any(a in b.parents for a in directories for b in directories if a != b):
                raise ValueError("Invocation directories must be distinct and non-overlapping")
            if log_dir is None:
                raise ValueError("OpenCode invocation requires an application-owned log directory")
            log_dir = Path(log_dir)
            log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            assert_path_contained(log_dir, log_dir)
            if acquisition is not None and (stage != "research" or context.network_profile != "public-research"):
                raise ValueError("Research file acquisition is not permitted in this invocation phase")

            control = Path(tempfile.mkdtemp(prefix="opencode-control-", dir=tmp_dir))
            work = Path(tempfile.mkdtemp(prefix="opencode-work-", dir=tmp_dir))
            worker_home = Path(tempfile.mkdtemp(prefix="opencode-home-", dir=tmp_dir))
            worker_tmp = Path(tempfile.mkdtemp(prefix="opencode-tmp-", dir=tmp_dir))
            for directory in (control, work, worker_home, worker_tmp):
                os.chmod(directory, 0o700)
            image_settings = context.image_generation if isinstance(context.image_generation, dict) else {}
            include_imagegen = (stage in ("research", "compose") and
                (image_settings.get("enabled") is True
                 if image_settings else (stage == "research" and context.network_profile == "public-research")))
            image_broker = None
            if include_imagegen:
                from researchops.runners.image_broker import ImageBroker
                image_broker = ImageBroker(work, image_settings, self.settings,
                                           cancellation_check=context.cancellation_check,
                                           timeout_seconds=context.timeout_seconds)
                image_broker.start()
            stage_submission_helper(work, include_imagegen=include_imagegen)
            if info["allow_http_requests"]:
                from researchops.runners import public_http, research_fetch
                from researchops.runners.http_routing import validate_profiles
                profile = context.http_proxy_profile
                if profile:
                    validate_profiles({profile["name"]: {key: profile[key]
                                                         for key in ("proxy_url", "host_patterns")}})
                helper = Path(HELPER_DIRECTORY) / "researchops/runners"
                _write_output(work, str(helper / "public_http.py"), Path(public_http.__file__).read_bytes())
                _write_output(work, str(helper / "research_fetch.py"), Path(research_fetch.__file__).read_bytes())
                _write_output(work, str(helper / "http-profile.json"), json.dumps(profile).encode())
                info["http_proxy_profile"] = {"name": profile.get("name"),
                                               "host_patterns": profile.get("host_patterns", []),
                                               "policy_sha256": hashlib.sha256(
                                                   json.dumps(profile, sort_keys=True).encode()).hexdigest()}
            if acquisition is not None:
                try:
                    acquisition.start(work)
                    acquisition_started = True
                except Exception:
                    raise ValueError("Research file acquisition could not be started") from None

            env = opencode_control_environment(info["binary_path"], control, environ=self.environ)
            client = OpenCodeClient(info["binary_path"], control, environ=self.environ,
                                    log_path=log_dir / ("opencode-control-" + stage + ".jsonl"),
                                    budget=budget, cancellation_check=context.cancellation_check,
                                    deadline=deadline)
            info["version"] = _version(info["binary_path"], control, env, client=client)
            if not _service_available(info["binary_path"], control, env, client=client):
                info.update(service_available=False, ready=False, blocker="RUNNER-SERVICE-UNAVAILABLE",
                            blockers=[{"id": "RUNNER-SERVICE-UNAVAILABLE", "owner": "operator",
                                       "category": "configuration", "scope": OPENCODE_PROVIDER,
                                       "reason": "The existing OpenCode background service is unavailable",
                                       "resume_condition": "Start the operator's existing OpenCode service without changing its configuration"}],
                            operator_missing=["RUNNER-SERVICE-UNAVAILABLE"])
                raise ValueError("RUNNER-SERVICE-UNAVAILABLE: The existing OpenCode service is unavailable")
            info["service_available"] = True
            try:
                model_document = client.call("model.list", params={"location[directory]": str(work)},
                                             timeout_seconds=max(1, min(15, int(deadline - time.monotonic()))))
                if not isinstance(model_document.get("data"), list):
                    raise OpenCodeControlError("OPENCODE_MODEL_LIST_INVALID")
                info.update(model_catalog_available=True, service_available=True, ready=True,
                            blocker=None, blockers=[], operator_missing=[])
            except OpenCodeControlError:
                info.update(model_catalog_available=False, ready=False, blocker="RUNNER-API-UNAVAILABLE",
                            blockers=[{"id": "RUNNER-API-UNAVAILABLE", "owner": "operator",
                                       "category": "configuration", "scope": OPENCODE_PROVIDER,
                                       "reason": "OpenCode model/API capability is unavailable",
                                       "resume_condition": "Confirm the OpenCode service API and model catalog"}],
                            operator_missing=["RUNNER-API-UNAVAILABLE"])
                raise ValueError("RUNNER-API-UNAVAILABLE: OpenCode model/API capability is unavailable") from None

            mcp_native = inspect_native_mcp(OPENCODE_PROVIDER, info["binary_path"],
                                            project_dir=Path(project_dir))
            direct_web_allowed = stage == "research" and context.network_profile == "public-research"
            mcp_status = _mcp_inventory(info["binary_path"], control, env, client=client,
                                        location=work)
            mcp = {**mcp_native, "mode": "native-mcp-read",
                   "phase_policy": "operator-registered-mcp-query-allowed",
                   "direct_web_allowed": direct_web_allowed,
                   "cli_status": mcp_status, "connections_checked": False}
            mcp["servers"] = list(mcp_status.get("servers", []))
            info["mcp"] = mcp
            event["mcp"] = mcp

            prompt = _prompt(Path(input_dir), Path(project_dir), work, context, mcp,
                             provider=OPENCODE_PROVIDER)
            prompt_raw = prompt.encode("utf-8")
            if len(prompt_raw) > MAX_INPUT_BYTES + 1_000_000:
                raise ValueError("OpenCode invocation input exceeds the prompt budget")
            prompt_path = control / "invocation-instructions.txt"
            _write_output(control, "invocation-instructions.txt", prompt_raw)
            prompt_sha256 = hashlib.sha256(prompt_raw).hexdigest()
            bootstrap = (
                "You are the ResearchOps OpenCode task worker. Read the complete invocation "
                "instructions from " + str(prompt_path) + " (" + str(len(prompt_raw)) + " bytes, "
                "sha256 " + prompt_sha256 + "). This is an explicitly supplied application input, "
                "not a credential. Read the entire file using multiple reads if necessary; do not "
                "truncate it or stop after an excerpt. Follow its phase, workspace, submission and "
                "permission contracts exactly. Write task outputs only below " + str(work) +
                " and " + str(project_dir) + ". The submission helper is at " +
                str(work / HELPER_DIRECTORY) + ". Return exactly the file-reference envelope "
                "printed by submit_result, with no prose or markdown fences."
            )
            if len(bootstrap.encode("utf-8")) > 32_000:
                raise ValueError("OpenCode bootstrap prompt is too large")

            model_ref = _model_ref(context.model, context.reasoning_effort)
            title = ("ResearchOps " + stage)[:MAX_SESSION_TITLE]
            rules = _permission_rules(stage=stage, network_profile=context.network_profile,
                                      work=work, project_dir=Path(project_dir),
                                      input_dir=Path(input_dir), control_dir=control,
                                      mcp_servers=mcp.get("servers", []))
            body = {"title": title, "location": {"directory": str(work)}, "permissions": rules}
            if model_ref is not None:
                body["model"] = model_ref
            session_create_attempted = True
            created = _session_data(client.call("session.create", body=body,
                                                timeout_seconds=max(1, min(30, int(deadline - time.monotonic())))))
            session_id = created.get("id")
            if not isinstance(session_id, str) or not re.fullmatch(r"ses_[A-Za-z0-9]+", session_id):
                raise OpenCodeControlError("OPENCODE_SESSION_ID_INVALID")
            location = ((created.get("location") or {}).get("directory")
                        if isinstance(created.get("location"), dict) else None)
            if not isinstance(location, str) or Path(location).resolve() != work.resolve():
                raise OpenCodeControlError("OPENCODE_SESSION_LOCATION_MISMATCH")
            info.update(spawned=True, cli_invocations=client.calls, session_id=session_id,
                        location=str(work), permissions_rule_count=len(rules))
            event.update(spawned=True, session_id=session_id, location=str(work),
                         permissions_rule_count=len(rules))
            observed_model = created.get("model") if isinstance(created.get("model"), dict) else {}
            info["observed_model"] = observed_model
            info["observed_agent"] = created.get("agent")
            info["observed_variant"] = observed_model.get("variant")
            event.update(observed_model=observed_model, observed_agent=created.get("agent"),
                         observed_variant=observed_model.get("variant"))
            session_record = {
                "schema_version": 1, "provider": OPENCODE_PROVIDER,
                "task_id": context.task_id, "run_id": context.run_id,
                "attempt": context.attempt, "stage": stage,
                "fencing_token": context.fencing_token, "session_id": session_id,
                "location": str(work), "agent": created.get("agent"),
                "model": observed_model, "created_at": _utcnow(),
            }
            _write_private_json(log_dir / ("opencode-session-" + stage + ".json"), session_record)

            variables = {
                "HOME": str(worker_home), "TMPDIR": str(worker_tmp),
                "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "Asia/Seoul",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            client.call("session.environment", body={"variables": variables},
                        params={"sessionID": session_id},
                        timeout_seconds=max(1, min(15, int(deadline - time.monotonic()))))
            prompt_response = client.call("session.prompt", body={"text": bootstrap},
                                          params={"sessionID": session_id},
                                          timeout_seconds=max(1, min(30, int(deadline - time.monotonic()))))
            input_data = prompt_response.get("data")
            if not isinstance(input_data, dict) or input_data.get("sessionID") != session_id:
                raise OpenCodeControlError("OPENCODE_PROMPT_NOT_ACCEPTED")
            input_id = input_data.get("id")
            if not isinstance(input_id, str) or not input_id:
                raise OpenCodeControlError("OPENCODE_PROMPT_NOT_ACCEPTED")
            session_record.update(input_id=input_id, status="prompt_accepted", updated_at=_utcnow())
            _write_private_json(log_dir / ("opencode-session-" + stage + ".json"), session_record)
            event["input_id"] = input_id
            info["authentication_status"] = "invocation_started"

            requested_model = _model_ref(context.model, context.reasoning_effort)
            requested_provider = requested_model.get("providerID") if requested_model else None
            requested_id = requested_model.get("id") if requested_model else None
            requested_variant = requested_model.get("variant") if requested_model else None
            if requested_model is not None:
                if (observed_model.get("providerID") != requested_provider
                        or observed_model.get("id") != requested_id
                        or (requested_variant and observed_model.get("variant") != requested_variant)):
                    raise OpenCodeControlError("OPENCODE_SESSION_MODEL_MISMATCH")

            try:
                session = _wait_terminal(client, session_id, deadline=deadline,
                                         cancellation_check=context.cancellation_check)
            except OpenCodeControlError as exc:
                interruption = _interrupt_and_observe(client, session_id, work)
                info["interruption"] = {key: interruption[key] for key in
                                        ("interrupted", "terminal", "resources", "cleanup_verified")}
                event["interruption"] = dict(info["interruption"])
                if exc.code == "OPENCODE_TIMEOUT":
                    final_code, error = 124, "OpenCode phase deadline exceeded"
                elif exc.code == "OPENCODE_CANCELLED":
                    final_code, error = 130, "OpenCode invocation cancelled"
                else:
                    final_code, error = 1, "OpenCode session control failed"
                raise ValueError(error) from None
            session_location = ((session.get("location") or {}).get("directory")
                                if isinstance(session.get("location"), dict) else None)
            if not isinstance(session_location, str) or Path(session_location).resolve() != work.resolve():
                raise OpenCodeControlError("OPENCODE_SESSION_LOCATION_MISMATCH")
            observed_model = session.get("model") if isinstance(session.get("model"), dict) else observed_model
            info.update(observed_model=observed_model, observed_agent=session.get("agent"),
                        observed_variant=observed_model.get("variant"),
                        session_outcome=session.get("outcome"),
                        session_idle=isinstance(session.get("time"), dict) and session["time"].get("idle") is not None)
            event.update(observed_model=observed_model, observed_agent=session.get("agent"),
                         observed_variant=observed_model.get("variant"),
                         terminal=info["session_idle"] and bool(session.get("outcome")),
                         provider_terminal_status=session.get("outcome"))
            if requested_model is not None:
                if (observed_model.get("providerID") != requested_provider
                        or observed_model.get("id") != requested_id
                        or (requested_variant and observed_model.get("variant") != requested_variant)):
                    raise OpenCodeControlError("OPENCODE_SESSION_MODEL_MISMATCH")

            max_message_bytes = context.trace_max_bytes
            assistant_messages = _messages(client, session_id, message_type="assistant",
                                           deadline=deadline, max_bytes=max_message_bytes,
                                           budget=budget)
            trace = _parse_trace(assistant_messages, session, max_bytes=max_message_bytes,
                                 expected_model=requested_model or _message_model(observed_model),
                                 expected_agent=session.get("agent"),
                                 recover_blocked_file_access=True)
            resources = _cleanup_snapshot(client, session_id, work)
            info["resources"] = resources
            info["session_cleanup_verified"] = bool(trace.terminal and resources["cleanup_verified"])
            if trace.tools:
                event["tools"] = [{"name": item.name, "success": item.success,
                                   "provider_reported_success": item.provider_reported_success,
                                   "verified": item.verified, "status": item.status,
                                   "denied": item.denied,
                                   **({"exit_code": item.exit_code} if item.exit_code is not None else {}),
                                   **({"truncated": True} if item.truncated else {}),
                                   **({"duration_ms": item.duration_ms} if item.duration_ms is not None else {})}
                                  for item in trace.tools[:500]]
            if trace.denied_actions:
                event["denied_actions"] = list(trace.denied_actions[:100])
            if trace.recovered_file_denials:
                warnings = [{"code": "OPENCODE_BLOCKED_FILE_ACCESS",
                             "count": trace.recovered_file_denials,
                             "message": "Blocked file operations; final submission still requires validation."}]
                event["permission_warnings"] = warnings
                info["permission_warnings"] = warnings
            if trace.provider_failure is not None:
                event["provider_failure"] = dict(trace.provider_failure)
                info["provider_failure"] = dict(trace.provider_failure)
            info["usage"] = trace.usage
            event["usage"] = trace.usage
            mcp_tools = _summarize_mcp_tools(mcp, trace)
            info["mcp_tools"] = mcp_tools
            info["mcp_tool_count"] = len(mcp_tools)
            event["mcp_tools"] = mcp_tools
            info["model_invocations"] = int(trace.terminal and trace.assistant_message_count > 0)
            event["model_invocations"] = info["model_invocations"]
            info["trace_diagnostics"] = {"complete": trace.terminal,
                                         "parsed_events": trace.assistant_message_count,
                                         "model_activity_observed": trace.assistant_message_count > 0,
                                         "message_bytes": trace.message_bytes}
            _write_private_json(log_dir / ("opencode-messages-" + stage + ".json"), {
                "schema_version": 1, "session_id": session_id, "stage": stage,
                "assistant_message_count": trace.assistant_message_count,
                "message_bytes": trace.message_bytes,
                "final_message_finish": trace.final_message_finish,
                "provider_failure": trace.provider_failure,
                "final_text_sha256": (hashlib.sha256(trace.final_text.encode("utf-8")).hexdigest()
                                      if trace.final_text else None),
                "final_text_bytes": len(trace.final_text.encode("utf-8")) if trace.final_text else 0,
                "tools": [{"name": item.name, "status": item.status, "success": item.success,
                           "verified": item.verified, "denied": item.denied,
                           "provider_reported_success": item.provider_reported_success,
                           "exit_code": item.exit_code, "truncated": item.truncated,
                           "error_type": item.error_type}
                          for item in trace.tools[:500]],
                "resources": resources,
            })
            if trace.response_error:
                hints = {
                    "OPENCODE_PROVIDER_QUOTA_EXHAUSTED":
                        "공급자 사용량 한도가 소진되었습니다. 다른 모델/공급자를 선택하거나 한도 복구 후 전체 재시도하세요.",
                    "OPENCODE_PROVIDER_RATE_LIMITED":
                        "공급자가 요청을 일시 제한했습니다. 잠시 후 수동 재시도하세요.",
                }
                hint = hints.get(trace.response_error)
                raise ValueError(trace.response_error + (": " + hint if hint else ""))
            if not resources["cleanup_verified"]:
                raise ValueError("OPENCODE_LIVE_RESOURCE_REMAINS")
            if not trace.successful or trace.final_text is None:
                raise ValueError("OPENCODE_SESSION_NOT_SUCCESSFUL")
            for tool in trace.tools:
                lowered = tool.name.lower()
                if lowered in _FORBIDDEN_DELEGATION_TOOLS:
                    raise ValueError("OpenCode used a prohibited delegation/question tool")
                if (context.network_profile != "public-research" or stage != "research") and (
                        lowered in _FORBIDDEN_NETWORK_TOOLS):
                    raise ValueError("OpenCode used a direct web tool in a phase configured without web research")
                mcp_servers = [item.get("name") for item in (mcp.get("servers") or []) if item.get("name")]
                for s in mcp_servers:
                    prefix = _normalize_mcp_action_prefix(s.lower()) + "_"
                    if lowered.startswith(prefix) and len(tool.name) > len(prefix):
                        sub = lowered[len(prefix):]
                        if _is_mcp_mutation_action(sub) and not tool.denied and tool.provider_reported_success:
                            raise ValueError(f"OpenCode executed an unauthorized MCP mutation tool: {tool.name}")
            reference = parse_response_transport(trace.final_text, max_bytes=MAX_RESPONSE_BYTES)
            if not isinstance(reference, FileResponseReference):
                raise ValueError("OpenCode final response must use the file submission envelope")
            document, submission = _resolve_submission(reference, work, log_dir, stage,
                                                       context.cancellation_check)
            info["response_submission"] = submission
            event["response_submission"] = submission
            if not info["session_cleanup_verified"]:
                raise ValueError("OpenCode session cleanup could not be verified")
            outputs = _import(document, stage, work, Path(output_dir), control,
                              context.cancellation_check)
            close_acquisition()
            if not acquisition_cleanup:
                raise ValueError("Research file acquisition cleanup could not be verified")
            event.update(terminal=True, successful_terminal=True,
                         authentication_status="invocation_succeeded")
            info.update(authentication_status="invocation_succeeded")
        except ResponseTransportError as exc:
            response_diagnostic, error = exc.to_dict(), str(exc)
            event["error"] = error
        except Exception as exc:
            error = str(exc) if str(exc) else type(exc).__name__
            event["error"] = error
            if error.startswith("OPENCODE_TRACE_"):
                info["trace_diagnostics"] = {"complete": False, "error_code": error,
                                             "limit_exceeded": True}
        finally:
            if image_broker is not None:
                broker_clean = bool(image_broker.close())
                if not broker_clean:
                    detail = "Image broker cleanup could not be verified"
                    if error is None:
                        error = detail
                    elif detail not in error:
                        error = f"{error}; {detail}"
            close_acquisition()
            if not acquisition_cleanup:
                detail = "Research file acquisition cleanup could not be verified"
                if error is None:
                    error = detail
                elif detail not in error:
                    error += "; " + detail
                event["error"] = error
            if session_id is not None and interruption is None and error is not None and client is not None:
                try:
                    interruption = _interrupt_and_observe(client, session_id, work)
                    info["interruption"] = {key: interruption[key] for key in
                                            ("interrupted", "terminal", "resources", "cleanup_verified")}
                    event["interruption"] = dict(info["interruption"])
                except Exception:
                    info["interruption"] = {"interrupted": None, "terminal": False,
                                            "resources": None, "cleanup_verified": False}
                    event["interruption"] = dict(info["interruption"])
            if trace is None and session_id is not None and client is not None:
                info["partial_diagnostics"] = _partial_message_diagnostics(client, session_id)
                event["partial_diagnostics"] = dict(info["partial_diagnostics"])

        if response_diagnostic:
            event["response_diagnostic"] = response_diagnostic
            info["response_diagnostic"] = response_diagnostic
        if trace is not None:
            event.setdefault("terminal", trace.terminal)
            event.setdefault("successful_terminal", trace.successful)
            event.setdefault("tools", [{"name": item.name, "success": item.success, "status": item.status,
                                        "denied": item.denied} for item in trace.tools[:500]])
            event.setdefault("usage", trace.usage)
        control_cleanup = client.control_cleanup_verified if client is not None else True
        cleanup = bool(control_cleanup and acquisition_cleanup and broker_clean)
        if session_id is not None:
            if interruption is not None:
                cleanup = cleanup and interruption.get("cleanup_verified") is True
            else:
                cleanup = cleanup and info.get("session_cleanup_verified") is True
        elif session_create_attempted:
            # The create request may have reached the server even though its
            # response (and session ID) was lost. Remote cleanup is unverified.
            cleanup = False
        info["control_cleanup_verified"] = control_cleanup
        info["control_cleanup_evidence"] = list(client.control_cleanup_evidence)[-20:] if client else []
        if error is None and not cleanup:
            error = "OpenCode session cleanup could not be verified"
            event["error"] = error
        if final_code is None:
            if error is None:
                final_code = 0
            elif error == "OPENCODE_CANCELLED":
                final_code = 130
            elif error in {"OPENCODE_TIMEOUT", "OPENCODE_API_TIMEOUT"}:
                final_code = 124
            elif error.startswith("RUNNER-") or error.startswith("OPENCODE_SESSION_MODEL_MISMATCH"):
                final_code = 78
            elif info.get("session_cleanup_verified") is False or (
                    interruption is not None and interruption.get("cleanup_verified") is False
                    and info.get("session_outcome") in {None, "succeeded"}):
                final_code = 77
            elif trace is not None and trace.outcome == "interrupted":
                final_code = 130
            else:
                final_code = 1
        if error is not None and final_code == 0:
            final_code = 1
        info["cli_invocations"] = client.calls if client is not None else 0
        info["trace_limits"] = {"total_bytes": context.trace_max_bytes,
                                "event_bytes": context.trace_max_event_bytes,
                                "events": context.trace_max_events,
                                "preview_bytes": context.trace_preview_bytes}
        summary = {"provider": OPENCODE_PROVIDER, "stage": stage,
                   "session_id": session_id, "terminal": bool(trace and trace.terminal),
                   "successful_terminal": bool(trace and trace.successful),
                   "tool_count": len(trace.tools) if trace else 0,
                   "model_invocations": info.get("model_invocations", 0),
                   "error": error}
        if log_dir is not None:
            stdout_path = log_dir / (stage + ".stdout")
            stderr_path = log_dir / (stage + ".stderr")
            try:
                if not stdout_path.exists():
                    _write_output(log_dir, stage + ".stdout",
                                  (json.dumps(summary, ensure_ascii=False) + "\n").encode("utf-8"))
                if not stderr_path.exists():
                    _write_output(log_dir, stage + ".stderr", b"")
                log_files = {"stdout": stdout_path, "stderr": stderr_path}
            except (OSError, WorkspaceError):
                log_files = {}
        return RunnerExecutionResult(
            success=error is None, exit_code=(final_code or 1) if error is not None else 0,
            stdout=json.dumps(summary, ensure_ascii=False), stderr="", events=[event],
            output_files=outputs, error_message=error, cleanup_verified=cleanup, isolation=info,
            log_files=log_files,
            log_sizes={name: (path.stat().st_size if path.is_file() else 0)
                       for name, path in log_files.items()},
            response_diagnostic=safe_response_diagnostic(response_diagnostic),
            image_ledger=image_broker.snapshot() if image_broker is not None and cleanup else None)


class OpenCodeOutputOnlyRunner(OutputOnlyDevelopmentRunner):
    """Explicit synthetic validation through the same v2 session endpoint.

    Only bundled samples are accepted. The session has no tools, receives only
    the sample's public files, and must return the existing legacy
    ``response_json`` envelope. This is a transport validation, not a claim of
    hostile isolation or real task quality.
    """

    def __init__(self, provider, sample, *, approval=False, evidence_dir,
                 timeout_seconds=120, model=None, binary=None):
        if approval is not True:
            raise ValueError("Actual model validation requires explicit approval")
        if provider != OPENCODE_PROVIDER or sample not in SAMPLE_NAMES:
            raise ValueError("Only bundled synthetic samples and the OpenCode provider are allowed")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
            raise ValueError("Development invocation timeout must be between 1 and 300 seconds")
        if model is not None and (not isinstance(model, str) or not model or model.startswith("-")
                                  or any(ord(c) < 32 for c in model)):
            raise ValueError("Invalid explicit model")
        root = Path(__file__).resolve().parents[2]
        schemas = bundled_path(root, "schemas")
        self.task, self.package_files, self.version_hash = TaskPackageLoader(schemas).load_from_dir(
            bundled_path(root, "examples/runner-validation") / sample)
        self.provider, self.sample, self.model = provider, sample, model
        self.timeout_seconds, self.evidence_dir = timeout_seconds, Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.binary = shutil.which(binary or "opencode")
        self.calls = []

    def _execute(self, input_dir, output_dir, context):
        info = {"tier": "trusted-development-output-only", "production_ready": False,
                "provider": OPENCODE_PROVIDER, "spawned": False, "model_invocations": 0,
                "cli_invocations": 0, "upstream_request_count": None,
                "credential_bridge": False, "filesystem_quota_scope": "response-import-only",
                "hostile_process_isolation": False, "shell_tools_disabled": True,
                "terminal_response": False, "cleanup_verified": False,
                "tool_policy": "all-tools-denied; not-hostile-OS-isolation"}
        broker = Path(tempfile.mkdtemp(prefix="opencode-development-", dir=self.evidence_dir))
        session_id = None
        session_create_attempted = False
        client = None
        trace = None
        imported = {}
        error = None
        response_diagnostic = None
        deadline = time.monotonic() + self.timeout_seconds
        budget = OpenCodeTraceBudget(context.trace_max_bytes, context.trace_max_event_bytes,
                                     context.trace_max_events)
        try:
            _phase_guard(context, deadline)
            if not self.binary:
                raise ValueError("Configured OpenCode executable is unavailable")
            env = opencode_control_environment(self.binary, broker)
            client = OpenCodeClient(self.binary, broker,
                                    log_path=broker / "opencode-control.jsonl",
                                    budget=budget, cancellation_check=context.cancellation_check,
                                    deadline=deadline)
            if not _service_available(self.binary, broker, env, client=client):
                raise ValueError("OpenCode background service is unavailable")
            prompt = self._prompt(input_dir, context)
            model_ref = _model_ref(self.model, None)
            body = {"title": "ResearchOps synthetic validation",
                    "location": {"directory": str(broker)},
                    "permissions": [{"action": "*", "resource": "*", "effect": "deny"}]}
            if model_ref is not None:
                body["model"] = model_ref
            session_create_attempted = True
            created = _session_data(client.call("session.create", body=body,
                                                timeout_seconds=max(1, min(30, int(deadline - time.monotonic())))))
            session_id = created.get("id")
            if not isinstance(session_id, str) or not re.fullmatch(r"ses_[A-Za-z0-9]+", session_id):
                raise OpenCodeControlError("OPENCODE_SESSION_ID_INVALID")
            if not isinstance(created.get("location"), dict) or Path(
                    created["location"].get("directory", "")).resolve() != broker.resolve():
                raise OpenCodeControlError("OPENCODE_SESSION_LOCATION_MISMATCH")
            if model_ref is not None:
                observed = created.get("model") if isinstance(created.get("model"), dict) else {}
                if (observed.get("providerID") != model_ref["providerID"]
                        or observed.get("id") != model_ref["id"]
                        or (model_ref.get("variant") and observed.get("variant") != model_ref["variant"])):
                    raise OpenCodeControlError("OPENCODE_SESSION_MODEL_MISMATCH")
            info["spawned"] = True
            worker_home = broker / "home"
            worker_tmp = broker / "tmp"
            worker_home.mkdir(mode=0o700)
            worker_tmp.mkdir(mode=0o700)
            client.call("session.environment", body={"variables": {
                "HOME": str(worker_home), "TMPDIR": str(worker_tmp),
                "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "Asia/Seoul",
                "PYTHONDONTWRITEBYTECODE": "1"}},
                params={"sessionID": session_id},
                timeout_seconds=max(1, min(15, int(deadline - time.monotonic()))))
            client.call("session.prompt", body={"text": prompt}, params={"sessionID": session_id},
                        timeout_seconds=max(1, min(30, int(deadline - time.monotonic()))))
            session = _wait_terminal(client, session_id, deadline=deadline,
                                     cancellation_check=context.cancellation_check)
            session_location = ((session.get("location") or {}).get("directory")
                                if isinstance(session.get("location"), dict) else None)
            if not isinstance(session_location, str) or Path(session_location).resolve() != broker.resolve():
                raise OpenCodeControlError("OPENCODE_SESSION_LOCATION_MISMATCH")
            assistant = _messages(client, session_id, message_type="assistant",
                                  deadline=deadline, max_bytes=context.trace_max_bytes, budget=budget)
            trace = _parse_trace(assistant, session, max_bytes=context.trace_max_bytes,
                                 expected_model=model_ref, expected_agent=session.get("agent"))
            resources = _cleanup_snapshot(client, session_id, broker)
            if trace.tools:
                raise ValueError("Output-only OpenCode response used a tool")
            if not resources["cleanup_verified"]:
                raise ValueError("Output-only OpenCode live resource remains")
            if not trace.successful or trace.final_text is None:
                raise ValueError(trace.response_error or "OpenCode synthetic response was incomplete")
            document = parse_response_transport(trace.final_text, max_bytes=MAX_RESPONSE_BYTES)
            if not isinstance(document, dict):
                raise ValueError("OpenCode synthetic response must be a JSON object")
            imported = import_response(document, context.invocation_stage, output_dir,
                                       context.cancellation_check)
            info.update(cleanup_verified=bool(trace.idle and resources["cleanup_verified"]),
                        resources=resources,
                        model_invocations=int(trace.assistant_message_count > 0),
                        observed_model=session.get("model"), usage=trace.usage)
        except ResponseTransportError as exc:
            response_diagnostic, error = exc.to_dict(), str(exc)
        except Exception as exc:
            error = str(exc) if str(exc) else type(exc).__name__
        finally:
            if session_id is not None and error is not None and client is not None:
                interruption = _interrupt_and_observe(client, session_id, broker)
                info["interruption"] = {key: interruption[key] for key in
                                        ("interrupted", "terminal", "resources", "cleanup_verified")}
                info["cleanup_verified"] = interruption.get("cleanup_verified") is True
            if trace is None and session_id is not None and client is not None:
                info["partial_diagnostics"] = _partial_message_diagnostics(client, session_id)
        control_cleanup = client.control_cleanup_verified if client is not None else True
        if session_id is None:
            info["cleanup_verified"] = bool(control_cleanup and not session_create_attempted)
        elif info.get("cleanup_verified") is None:
            info["cleanup_verified"] = False
        else:
            info["cleanup_verified"] = bool(info["cleanup_verified"] and control_cleanup)
        info["control_cleanup_verified"] = control_cleanup
        info["control_cleanup_evidence"] = list(client.control_cleanup_evidence)[-20:] if client else []
        info["cli_invocations"] = client.calls if client is not None else 0
        if trace is not None:
            info.update(terminal_response=trace.terminal, observed_tool_count=len(trace.tools))
        if response_diagnostic:
            info["response_diagnostic"] = response_diagnostic
        observation = {**info, "stage": context.invocation_stage, "success": error is None,
                       "error": error,
                       "output_hashes": {name: hashlib.sha256(path.read_bytes()).hexdigest()
                                         for name, path in imported.items()}}
        self.calls.append(observation)
        _write_private_json(broker / "observation.json", observation)
        code = (124 if error == "OPENCODE_TIMEOUT" else 77 if error == "OPENCODE_CANCELLED"
                else 78 if error and (error.startswith("RUNNER-") or "SERVICE" in error)
                else 1 if error else 0)
        return RunnerExecutionResult(success=error is None, exit_code=code, stdout="", stderr="",
            events=[{"type": "development_boundary", **observation}], output_files=imported,
            error_message=error, cleanup_verified=info.get("cleanup_verified") is True,
            isolation=info, response_diagnostic=response_diagnostic)
