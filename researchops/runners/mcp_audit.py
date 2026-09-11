"""Bounded MCP evidence and observed Agy generated-file provenance.

These are post-execution checks, not a tool firewall or a credential reader.
Only explicit file-tool targets are inspected, using metadata rather than file
contents. Raw arguments, returned data, addresses and provider errors never
enter the public summaries.
"""

from datetime import datetime, timedelta
import os
from pathlib import Path
import re
import stat
from uuid import UUID

from researchops.errors import WorkspaceError
from researchops.strict_json import strict_json_loads
from researchops.workspace.security import assert_path_contained


_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_OBSERVED_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z")
_TIMING_FIELDS = ("observed_started_at", "observed_finished_at", "duration_ms")
_CODEX_APPROVAL_DENIAL = "MCP tool call requires approval, but approval policy is never"
# A protocol envelope is tool trace data, independent of the 1 MB final answer.
MAX_MCP_ENVELOPE_BYTES = 8 * 1024 * 1024
_FILE_TARGETS = {
    "view_file": "AbsolutePath", "view_file_outline": "AbsolutePath",
    "write_to_file": "TargetFile", "replace_file_content": "TargetFile",
    "multi_replace_file_content": "TargetFile", "list_dir": "DirectoryPath",
    "find_by_name": "SearchDirectory", "grep_search": "SearchPath",
}


def _identity(provider, details):
    if provider == "codex_exec":
        return details.get("server"), details.get("tool")
    parameters = details.get("parameters")
    if not isinstance(parameters, dict):
        return None, None
    return parameters.get("ServerName"), parameters.get("ToolName")


def _safe_identifier(value):
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else "[redacted]"


def safe_tool_timing(value):
    """Project optional application observations without copying provider data.

    Missing timing stays missing for historical traces. A completed-only call
    may have an observed finish but no start or measurable duration. Wall-clock
    adjustments do not invalidate a duration measured by the monotonic clock.
    """
    if not isinstance(value, dict) or not any(key in value for key in _TIMING_FIELDS):
        return {}
    result = {}
    for key in _TIMING_FIELDS[:2]:
        stamp = value.get(key)
        valid = isinstance(stamp, str) and len(stamp) <= 32 and _OBSERVED_UTC.fullmatch(stamp)
        if valid:
            try:
                valid = datetime.fromisoformat(stamp.replace("Z", "+00:00")).utcoffset() == timedelta(0)
            except ValueError:
                valid = False
        result[key] = stamp if valid else None
    duration = value.get("duration_ms")
    result["duration_ms"] = (duration if type(duration) is int and 0 <= duration <= 2 ** 63 - 1
        and result["observed_started_at"] and result["observed_finished_at"] else None)
    return result


def _protocol_result(value):
    """Recognize an MCP envelope, never an isError field in business data."""
    if not isinstance(value, dict) or not isinstance(value.get("content"), list):
        return None
    if set(value) - {"content", "structuredContent", "structured_content", "isError", "_meta"}:
        return None
    if any(not isinstance(item, dict) or not isinstance(item.get("type"), str) or item.get("type") not in {
            "text", "image", "audio", "resource", "resource_link"} for item in value["content"]):
        return None
    if "isError" in value and type(value["isError"]) is not bool:
        return None
    return value


def mcp_protocol_error(provider, details, *, max_bytes=MAX_MCP_ENVELOPE_BYTES):
    """Return only a protocol-level error signal from known response positions."""
    if provider == "codex_exec":
        result = details.get("result")
        # Codex's item.result is already the native MCP result envelope.
        return isinstance(result, dict) and result.get("isError") is True
    output = details.get("output")
    if isinstance(output, str):
        # Agy also emits ordinary JSON business objects here. Only a complete
        # MCP content envelope is considered a protocol result.
        try:
            output = strict_json_loads(output, max_bytes=max_bytes)
        except (ValueError, RecursionError):
            return False
    envelope = _protocol_result(output)
    return envelope is not None and envelope.get("isError") is True


def summarize_mcp_tools(provider, tools):
    """Expose server/tool identity and evidence status, never tool payloads."""
    if not isinstance(provider, str) or provider not in {"codex_exec", "antigravity_exec"}:
        raise ValueError("MCP_PROVIDER_UNSUPPORTED")
    native_name = "mcp_tool_call" if provider == "codex_exec" else "call_mcp_tool"
    summaries = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("name") != native_name:
            continue
        details = tool.get("details")
        valid = isinstance(details, dict)
        details = details if valid else {}
        server, name = _identity(provider, details)
        reported = tool.get("provider_reported_success", tool.get("success")) is True
        output = details.get("result") if provider == "codex_exec" else details.get("output")
        output_verified = (isinstance(output, dict) and (isinstance(output.get("content"), list) or
                           isinstance(output.get("structured_content"), dict) or
                           isinstance(output.get("structuredContent"), dict)) if provider == "codex_exec" else
                           isinstance(output, (dict, list)) or isinstance(output, str) and bool(output.strip()))
        output_verified = output_verified or tool.get("mcp_output_verified") is True
        state = details.get("status") if provider == "codex_exec" else tool.get("state")
        error = details.get("error")
        denied = (tool.get("denied") is True or provider == "codex_exec" and
                  isinstance(error, dict) and error.get("message") == _CODEX_APPROVAL_DENIAL)
        if not valid or state is not None and not isinstance(state, str):
            status, code = "unverified", "MCP_EVIDENCE_INVALID"
        elif denied:
            status, code = "denied", "MCP_PERMISSION_DENIED"
        elif details.get("error") or state in {"ERROR", "failed"}:
            status, code = "failed", "MCP_TOOL_ERROR"
        elif mcp_protocol_error(provider, details):
            status, code = "failed", "MCP_PROTOCOL_ERROR"
        elif not reported or tool.get("success") is not True:
            status, code = "failed", "MCP_TOOL_ERROR"
        elif not output_verified:
            status, code = "unverified", "MCP_OUTPUT_UNVERIFIED"
        else:
            status, code = "succeeded", None
        summary = {"server": _safe_identifier(server), "tool": _safe_identifier(name),
                          "status": status, "error_code": code, "success": status == "succeeded",
                          "provider_reported_success": reported, "output_verified": bool(output_verified)}
        for key in ("event_bytes", "event_sequence"):
            if type(tool.get(key)) is int and tool[key] >= 0:
                summary[key] = tool[key]
        if tool.get("response_truncated") is True:
            summary["content_truncated"] = True
        summary.update(safe_tool_timing(tool))
        summaries.append(summary)
    return summaries


def _regular_generated_file(path, root):
    try:
        assert_path_contained(path, root)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size == 0:
            raise ValueError("invalid generated file")
    except (OSError, ValueError, RuntimeError, WorkspaceError):
        raise ValueError("AGY_MCP_GENERATED_FILE_UNSAFE") from None


def validate_agy_mcp_reads(trace, active_server_names, *, home_dir=None):
    """Validate observed generated-file accesses and annotate proven spill reads.

    view_file and grep_search may target the same exact authorized generated
    files. Directory searches and other generated-state accesses remain denied.
    An error-free, non-denied DONE call may have no inline output. A subsequent
    successful view_file of its exact, existing generated output establishes
    response evidence without changing the recorded provider success. This
    function annotates only that call with ``mcp_output_verified=True``.
    """
    home = Path(home_dir) if home_dir is not None else Path.home()
    agy_root = home / ".gemini" / "antigravity-cli"
    metadata = agy_root / "mcp"
    active = {name for name in active_server_names if _safe_identifier(name) != "[redacted]"}
    conversation = getattr(trace, "conversation_id", None)
    try:
        valid_conversation = isinstance(conversation, str) and str(UUID(conversation)) == conversation
    except ValueError:
        valid_conversation = False
    outputs = {}
    for tool in trace.tools:
        details = tool.get("details")
        if not isinstance(details, dict):
            continue
        parameters = details.get("parameters")
        parameters = parameters if isinstance(parameters, dict) else {}
        name, index = tool.get("name"), tool.get("step_index")
        denied_actions = getattr(trace, "denied_actions", ())
        denied = (tool.get("denied") is True or "unknown" in denied_actions or
                  name == "call_mcp_tool" and "mcp" in denied_actions or
                  name == "read_url_content" and "read_url" in denied_actions or
                  name in {"view_file", "view_file_outline"} and "read_file" in denied_actions)
        successful = (tool.get("success") is True and not denied and
                      not details.get("error") and tool.get("state", "DONE") == "DONE")
        if (valid_conversation and type(index) is int and index >= 0 and successful):
            base = agy_root / "brain" / conversation / ".system_generated" / "steps" / str(index)
            if name == "call_mcp_tool":
                server, tool_name = _identity("antigravity_exec", details)
                if (_safe_identifier(server) != "[redacted]" and server in active and
                        _safe_identifier(tool_name) != "[redacted]" and
                        not mcp_protocol_error("antigravity_exec", details)):
                    outputs[base / "output.txt"] = (index, tool)
            elif name == "read_url_content":
                # Native web results use the same generated-file mechanism.
                # Keep this existing capability separate from MCP promotion.
                outputs[base / "content.md"] = (index, None)
        field = _FILE_TARGETS.get(name)
        if field is None or not isinstance(parameters.get(field), str):
            continue
        raw_path = parameters[field]
        if "\x00" in raw_path:
            raise ValueError("AGY_MCP_GENERATED_PATH_INVALID")
        supplied = Path(raw_path)
        cwd = getattr(trace, "cwd", None)
        target = Path(os.path.abspath(supplied if supplied.is_absolute() else Path(cwd or "/") / supplied))
        # Inspect only explicit accesses to Agy's generated state (including
        # aliases into it); never enumerate home or read authentication files.
        try:
            relevant = (supplied.is_absolute() and supplied.is_relative_to(agy_root) or
                        target.is_relative_to(agy_root) or target.resolve().is_relative_to(agy_root))
        except (OSError, RuntimeError):
            raise ValueError("AGY_MCP_GENERATED_PATH_INVALID") from None
        if not relevant:
            continue
        if (not supplied.is_absolute() or str(supplied) != raw_path or ".." in supplied.parts or
                "\\" in raw_path or any(ord(c) < 32 or ord(c) == 127 for c in raw_path)):
            raise ValueError("AGY_MCP_GENERATED_PATH_INVALID")
        if name not in {"view_file", "grep_search"}:
            raise ValueError("AGY_MCP_GENERATED_ACCESS_DENIED")
        if target.is_relative_to(metadata):
            parts = target.relative_to(metadata).parts
            schema_file = (len(parts) == 2 and (parts[1] == "instructions.md" or
                           parts[1].endswith(".json") and _safe_identifier(parts[1][:-5]) != "[redacted]"))
            if (len(parts) != 2 or parts[0] not in active or _safe_identifier(parts[0]) == "[redacted]" or
                    not schema_file):
                raise ValueError("AGY_MCP_SCHEMA_PROVENANCE_INVALID")
            _regular_generated_file(target, metadata)
            continue
        source = outputs.get(target)
        if source is None or type(index) is not int or index <= source[0]:
            raise ValueError("AGY_MCP_SPILL_PROVENANCE_INVALID")
        _regular_generated_file(target, agy_root / "brain")
        # A grep result may contain no matches or only selected lines; it does
        # not establish the original response evidence that view_file provides.
        if name == "view_file" and successful and source[1] is not None:
            source[1]["mcp_output_verified"] = True
