"""Incremental production trace validation with bounded diagnostic evidence.

Only one JSONL event is decoded at a time. Tool response bodies are discarded
after their status has been checked; original bytes belong in the caller's
protected log files. Compact lifecycle identities, generated-file targets and
denial evidence survive until the terminal event and cannot authorize import
from a partial trace.
"""

from collections import Counter
from datetime import datetime, timezone
import re
from time import monotonic_ns
from uuid import UUID

from researchops.runners.mcp_audit import (
    _CODEX_APPROVAL_DENIAL, _FILE_TARGETS, _safe_identifier,
    mcp_protocol_error, safe_tool_timing, summarize_mcp_tools,
)
from researchops.runners.tool_events import (
    SKILL_BUDGET_WARNING, ToolTrace, _CODEX_TOOLS, _denied_actions,
    _response_details, _usage, decode_provider_event, consume_agy_finish,
)
from researchops.runners.provider_diagnostics import agy_error_diagnostic


DEFAULT_TRACE_BYTES = 64 * 1024 * 1024
DEFAULT_EVENT_BYTES = 8 * 1024 * 1024
DEFAULT_TRACE_EVENTS = 10_000
DEFAULT_PREVIEW_BYTES = 64 * 1024
TRUNCATED_RESPONSE_WARNING = "MCP_RESPONSE_TRUNCATION_OBSERVED"
_TRUNCATED = re.compile(
    r"\b(?:\d+\s+)?(?:chars?|characters?|bytes|tokens)\s+(?:truncated|omitted)\b"
    r"|\b(?:output|response|content)\s+(?:was\s+)?truncated\b", re.IGNORECASE)


class TraceStreamError(ValueError):
    def __init__(self, code):
        self.trace_error_code = code
        super().__init__(code)


def _text(value, *, nonempty=True, limit=4096):
    if not isinstance(value, str) or nonempty and not value or len(value) > limit:
        raise ValueError("Invalid lifecycle metadata")
    return value


def _truncated(value):
    if isinstance(value, str):
        return bool(_TRUNCATED.search(value))
    if isinstance(value, dict):
        return any(_truncated(child) for child in value.values())
    if isinstance(value, list):
        return any(_truncated(child) for child in value)
    return False


def _compact_error(value):
    if not value:
        return None
    if isinstance(value, dict) and value.get("message") == _CODEX_APPROVAL_DENIAL:
        return {"message": _CODEX_APPROVAL_DENIAL}
    return {"message": "provider_tool_error"}


def _compact_codex(item):
    details = {key: item[key] for key in ("id", "type", "upstream_search_id") if key in item}
    if "exit_code" in item:
        details["exit_code"] = item["exit_code"] if type(item["exit_code"]) is int else None
    state = item.get("status")
    if state is not None:
        # Preserve malformed-state evidence without retaining an arbitrary body.
        details["status"] = state if isinstance(state, str) and len(state) <= 128 else False
    if item.get("type") == "mcp_tool_call":
        details.update(server=_safe_identifier(item.get("server")), tool=_safe_identifier(item.get("tool")))
        result = item.get("result")
        if isinstance(result, dict):
            compact = {"isError": result.get("isError") is True}
            if isinstance(result.get("content"), list):
                compact["content"] = []
            if isinstance(result.get("structured_content"), dict):
                compact["structured_content"] = {}
            if isinstance(result.get("structuredContent"), dict):
                compact["structuredContent"] = {}
            details["result"] = compact
        else:
            details["result"] = None
    if item.get("error"):
        details["error"] = _compact_error(item["error"])
    return details


def _compact_agy(info, *, max_bytes):
    name = info["name"]
    details = {"name": name}
    parameters = info.get("parameters")
    if isinstance(parameters, dict):
        kept = {}
        if name == "call_mcp_tool":
            kept = {key: _safe_identifier(parameters.get(key)) for key in ("ServerName", "ToolName")}
        field = _FILE_TARGETS.get(name)
        if field is not None and isinstance(parameters.get(field), str):
            kept[field] = _text(parameters[field], nonempty=False)
        details["parameters"] = kept
    if info.get("error"):
        details["error"] = _compact_error(info["error"])
    output = info.get("output")
    if name == "call_mcp_tool" and mcp_protocol_error("antigravity_exec", info, max_bytes=max_bytes):
        details["output"] = {"content": [], "isError": True}
    elif isinstance(output, (dict, list)) or isinstance(output, str) and output.strip():
        details["output"] = "[captured]"
    return details


def _observe_time():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"), monotonic_ns()


class ProductionToolTraceCollector:
    def __init__(self, provider, *, max_total_bytes=DEFAULT_TRACE_BYTES,
                 max_line_bytes=DEFAULT_EVENT_BYTES, max_events=DEFAULT_TRACE_EVENTS,
                 capture_timing=False):
        if provider not in {"codex_exec", "antigravity_exec"}:
            raise ValueError("Unsupported tool provider")
        if any(type(n) is not int or n <= 0 for n in (max_total_bytes, max_line_bytes, max_events)):
            raise ValueError("Trace limits must be positive integers")
        if type(capture_timing) is not bool:
            raise ValueError("Timing capture must be a boolean")
        self.provider = provider
        # Opt-in only for live capture: parsing an old archive cannot recover
        # receipt timestamps from a provider stream that did not contain them.
        self.capture_timing = capture_timing
        self._event_observation = None
        self.max_total_bytes, self.max_line_bytes, self.max_events = max_total_bytes, max_line_bytes, max_events
        self._buffer = bytearray()
        self._total = self._parsed_bytes = self._count = self._largest = 0
        self._closed = self._complete = False
        self._error = None
        self._lifecycle_error_code = None
        self._stage = 0
        self._tools, self._seen, self._pending, self._warnings = [], set(), {}, []
        self._terminal = self._terminal_success = self._failed = self._activity = False
        self._response = None
        self._response_error = "Missing or invalid structured response envelope"
        self._response_diagnostic = None
        self._usage = {}
        self._denied_actions = ()
        self._permission_mode = self._cwd = self._conversation_id = None
        self._terminal_status = self._provider_error_code = None
        self._provider_diagnostic = None

    def _fail(self, code):
        self._error = self._error or code
        self._closed = True
        self._buffer.clear()
        raise TraceStreamError(self._error)

    def feed(self, chunk):
        if self._closed:
            raise TraceStreamError(self._error or "trace_closed")
        if not isinstance(chunk, bytes):
            raise TypeError("Trace chunks must be bytes")
        remaining = self.max_total_bytes - self._total
        accepted = chunk[:remaining]
        self._total += len(accepted)
        # Process newline-delimited slices; even a caller supplying one huge
        # chunk cannot make the pending line grow beyond its explicit budget.
        start = 0
        while start < len(accepted):
            newline = accepted.find(b"\n", start)
            end = len(accepted) if newline < 0 else newline
            if len(self._buffer) + end - start > self.max_line_bytes:
                self._fail("trace_event_limit_exceeded")
            self._buffer.extend(accepted[start:end])
            if newline < 0:
                break
            raw = bytes(self._buffer)
            self._buffer.clear()
            self._line(raw, newline=True)
            start = newline + 1
        if len(chunk) > remaining:
            self._fail("trace_total_limit_exceeded")

    def _line(self, raw, *, newline):
        if not raw.strip():
            self._parsed_bytes += len(raw) + int(newline)
            return
        if self._count >= self.max_events:
            self._fail("trace_event_count_exceeded")
        # Observe once when the complete JSONL event is received, before JSON
        # decoding or inspecting a potentially large tool result body.
        self._event_observation = _observe_time() if self.capture_timing else None
        try:
            event, warnings = decode_provider_event(self.provider, raw)
        except ValueError:
            self._fail("trace_invalid_event" if newline else "trace_incomplete_event")
        try:
            if self.provider == "codex_exec":
                self._codex(event, len(raw) + int(newline))
            else:
                self._agy(event, len(raw) + int(newline))
        except (ValueError, TypeError, KeyError) as exc:
            self._lifecycle_error_code = {
                'Antigravity terminal event has unfinished tool actions': 'unfinished_tool_actions',
                'Invalid Antigravity finish completion': 'invalid_finish_completion',
                'Invalid or duplicate Antigravity finish index': 'invalid_finish_index',
                'Antigravity finish cannot complete another tool': 'finish_tool_identity_mismatch',
            }.get(str(exc), 'invalid_lifecycle_event')
            self._fail("trace_invalid_lifecycle")
        self._count += 1
        self._largest = max(self._largest, len(raw) + int(newline))
        self._parsed_bytes += len(raw) + int(newline)
        self._warnings.extend(item for item in warnings if item not in self._warnings)

    def _add_tool(self, tool, event_bytes, output):
        tool.update(event_bytes=event_bytes, event_sequence=self._count + 1)
        if tool["name"] in {"mcp_tool_call", "call_mcp_tool"} and _truncated(output):
            tool["response_truncated"] = True
            if TRUNCATED_RESPONSE_WARNING not in self._warnings:
                self._warnings.append(TRUNCATED_RESPONSE_WARNING)
        self._tools.append(tool)
        self._activity = True

    def _pending_timing(self, previous, *, started):
        if not self.capture_timing:
            return {}
        if previous is not None:
            return {key: previous[key] for key in ("observed_started_at", "_started_monotonic_ns",
                    "observed_finished_at", "duration_ms")}
        stamp, elapsed = self._event_observation
        return {"observed_started_at": stamp if started else None,
                "_started_monotonic_ns": elapsed if started else None,
                "observed_finished_at": None, "duration_ms": None}

    def _completed_timing(self, previous):
        if not self.capture_timing:
            return {}
        stamp, elapsed = self._event_observation
        started = previous.get("_started_monotonic_ns") if previous else None
        return {"observed_started_at": previous.get("observed_started_at") if previous else None,
                "observed_finished_at": stamp,
                "duration_ms": max(0, (elapsed - started) // 1_000_000) if started is not None else None}

    def _read_usage(self, value):
        usage = _usage(value)
        if len(usage) > 128 or any(not isinstance(k, str) or len(k) > 128 for k in usage):
            raise ValueError("Invalid provider usage metadata")
        return usage

    def _codex(self, event, event_bytes):
        kind = event.get("type")
        if self._stage < 2:
            if kind != ("thread.started" if self._stage == 0 else "turn.started"):
                raise ValueError("Expected one ordered Codex thread and turn")
            self._stage += 1
            return
        if self._terminal:
            raise ValueError("Event after terminal")
        if kind in {"turn.completed", "turn.failed"}:
            if self._pending:
                raise ValueError("Codex terminal event has unfinished tool actions")
            usage = self._read_usage(event.get("usage", {}))
            self._terminal, self._terminal_success = True, kind == "turn.completed" and not self._failed
            self._terminal_status, self._usage = kind, usage
            self._activity = self._activity or usage.get("output_tokens", 0) > 0
            return
        if kind == "error":
            self._failed = True
            return
        if kind not in {"item.started", "item.updated", "item.completed"} or not isinstance(event.get("item"), dict):
            raise ValueError("Unsupported Codex lifecycle event")
        item = event["item"]
        name = _text(item.get("type"), limit=128)
        if name == "error" and item.get("message") == SKILL_BUDGET_WARNING:
            if kind == "item.completed" and SKILL_BUDGET_WARNING not in self._warnings:
                self._warnings.append(SKILL_BUDGET_WARNING)
            return
        if name not in {"agent_message", "reasoning", *_CODEX_TOOLS}:
            raise ValueError("Unapproved Codex action")
        identity = item.get("id")
        if name in _CODEX_TOOLS:
            identity = _text(identity)
            if identity in self._seen:
                raise ValueError("Duplicate Codex tool identity")
            upstream = item.get("upstream_search_id")
            if upstream is not None:
                _text(upstream)
            current = (name, upstream)
            previous = self._pending.get(identity)
            if previous is not None and (previous["name"] != name or previous.get("upstream_search_id") is not None
                    and upstream is not None and previous["upstream_search_id"] != upstream):
                raise ValueError("Codex tool identity changed")
            if kind != "item.completed":
                if kind == "item.started" and previous is not None:
                    raise ValueError("Duplicate Codex tool start")
                self._pending[identity] = {"identity": identity, "name": current[0],
                    "upstream_search_id": upstream, "server": _safe_identifier(item.get("server")),
                    "tool": _safe_identifier(item.get("tool")),
                    **self._pending_timing(previous, started=kind == "item.started")}
                self._activity = True
                return
        if kind != "item.completed":
            return
        identity = _text(identity, nonempty=False)
        if identity in self._seen:
            raise ValueError("Missing or duplicate completed Codex identity")
        if identity in self._pending and name not in _CODEX_TOOLS:
            raise ValueError("Codex tool identity changed to a non-tool item")
        compact = _compact_codex(item) if name in _CODEX_TOOLS else None
        self._seen.add(identity)
        previous = self._pending.pop(identity, None)
        if name == "agent_message":
            text = item.get("text")
            self._activity = self._activity or isinstance(text, str) and bool(text.strip())
            self._response, response_error = _response_details(text)
            self._response_error = str(response_error) if response_error else None
            self._response_diagnostic = response_error.to_dict() if response_error else None
        elif name != "reasoning":
            success = item.get("status") == "completed"
            if name == "web_search":
                success = item.get("status", "completed") == "completed"
            elif name == "command_execution":
                success = success and type(item.get("exit_code")) is int and item["exit_code"] == 0
            elif name == "mcp_tool_call":
                result = item.get("result")
                success = success and isinstance(result, dict) and not item.get("error") and not result.get("isError")
            self._add_tool({"name": name, "success": bool(success), "details": compact,
                **self._completed_timing(previous)}, event_bytes, item.get("result"))

    def _agy(self, event, event_bytes):
        kind = event.get("event")
        if self._stage == 0:
            if kind != "init" or not isinstance(event.get("init"), dict):
                raise ValueError("Expected Antigravity initial event")
            initial = event["init"]
            mode = initial.get("permission_mode")
            if mode is not None and mode not in ("request-review", "always-proceed"):
                raise ValueError("Unknown Antigravity permission mode")
            cwd = initial.get("cwd")
            if cwd is not None and (not _text(cwd) or "\x00" in cwd):
                raise ValueError("Invalid Antigravity working directory")
            conversation = event.get("conversation_id")
            if conversation is not None and (not isinstance(conversation, str) or str(UUID(conversation)) != conversation):
                raise ValueError("Invalid Antigravity conversation identity")
            self._permission_mode, self._cwd, self._conversation_id = mode, cwd, conversation
            self._stage = 1
            return
        if self._terminal:
            raise ValueError("Event after terminal")
        if kind == "result":
            result = event.get("result")
            if not isinstance(result, dict) or result.get("status") not in {"SUCCESS", "ERROR", "CANCELED", "CANCELLED", "TIMEOUT", "FAILURE"}:
                raise ValueError("Unknown Antigravity terminal status")
            if result.get("conversation_id") is not None and result["conversation_id"] != self._conversation_id:
                raise ValueError("Antigravity terminal conversation identity changed")
            if self._pending:
                raise ValueError("Antigravity terminal event has unfinished tool actions")
            actions = _denied_actions(result.get("denied_actions", []))
            usage = self._read_usage(result.get("usage", {}))
            response, response_error = _response_details(result.get("structured_output"))
            self._denied_actions, self._usage = actions, usage
            self._response, self._response_error = response, str(response_error) if response_error else None
            self._response_diagnostic = response_error.to_dict() if response_error else None
            action_types = {"run_command": "command", "call_mcp_tool": "mcp", "read_url_content": "read_url",
                "view_file": "read_file", "view_file_outline": "read_file", "write_to_file": "write_file",
                "replace_file_content": "write_file", "multi_replace_file_content": "write_file"}
            for tool in self._tools:
                if "unknown" in actions or action_types.get(tool["name"]) in actions:
                    tool.update(success=False, denied=True)
            self._activity = self._activity or usage.get("output_tokens", 0) > 0
            self._terminal, self._terminal_status = True, result["status"]
            self._terminal_success = result["status"] == "SUCCESS" and not result.get("error") and not actions
            self._provider_diagnostic = agy_error_diagnostic(result)
            self._provider_error_code = self._provider_diagnostic["code"] if self._provider_diagnostic else None
            return
        if kind != "step_update" or not isinstance(event.get("step_update"), dict):
            raise ValueError("Unsupported Antigravity lifecycle event")
        step = event["step_update"]
        if step.get("conversation_id") is not None and step["conversation_id"] != self._conversation_id:
            raise ValueError("Antigravity conversation identity changed")
        name = _text(step.get("step_type"), limit=128)
        if consume_agy_finish(step, self._pending, self._seen):
            return
        if name in {"user_input", "agent_response", "checkpoint"}:
            if name == "agent_response":
                self._activity = self._activity or any(isinstance(step.get(key), str) and bool(step[key].strip())
                                                       for key in ("text", "text_delta"))
            return
        state, index = step.get("state"), step.get("step_index")
        if name == "error_message":
            if state != "DONE" or type(index) is not int or index < 0 or index in self._seen or index in self._pending:
                raise ValueError("Invalid Antigravity diagnostic step")
            if "tool_name" in step or "tool_info" in step:
                raise ValueError("Antigravity diagnostic step contains tool evidence")
            self._seen.add(index)
            if "ANTIGRAVITY_ERROR_MESSAGE" not in self._warnings:
                self._warnings.append("ANTIGRAVITY_ERROR_MESSAGE")
            return
        if name != "tool":
            raise ValueError("Unapproved Antigravity action")
        if state not in {"ACTIVE", "DONE", "ERROR"} or type(index) is not int or index < 0 or index in self._seen:
            raise ValueError("Invalid or duplicate Antigravity tool step")
        info, name = step.get("tool_info"), _text(step.get("tool_name"), limit=128)
        if not isinstance(info, dict) or info.get("name") != name:
            raise ValueError("Invalid Antigravity tool evidence")
        if index in self._pending and self._pending[index]["name"] != name:
            raise ValueError("Antigravity tool identity changed")
        compact = _compact_agy(info, max_bytes=self.max_line_bytes)
        if state == "ACTIVE":
            parameters = compact.get("parameters", {})
            self._pending[index] = {"identity": index, "name": name,
                "server": parameters.get("ServerName"), "tool": parameters.get("ToolName"),
                **self._pending_timing(self._pending.get(index), started=True)}
            self._activity = True
            return
        previous = self._pending.pop(index, None)
        self._seen.add(index)
        provider_success = state == "DONE" and not info.get("error")
        success = provider_success and not (name == "call_mcp_tool" and
                                            mcp_protocol_error("antigravity_exec", compact))
        self._add_tool({"name": name, "success": bool(success), "provider_reported_success": bool(provider_success),
            "state": state, "details": compact, "step_index": index,
            **self._completed_timing(previous)}, event_bytes, info.get("output"))

    def finish(self):
        if self._error:
            raise TraceStreamError(self._error)
        if self._closed:
            if self._complete:
                return self.partial_trace()
            raise TraceStreamError("trace_closed")
        if self._buffer:
            raw = bytes(self._buffer)
            self._buffer.clear()
            self._line(raw, newline=False)
        if not self._terminal:
            self._fail("trace_missing_terminal")
        self._closed = self._complete = True
        return self.partial_trace()

    def partial_trace(self):
        return ToolTrace(self._terminal, self._complete and self._terminal_success,
            self._response if self._complete else None, tuple(self._tools), bool(self._denied_actions), self._usage,
            self._count, self._error or self._response_error, tuple(self._warnings), self._activity,
            self._denied_actions, self._permission_mode, self._cwd, self._conversation_id,
            self._terminal_status, self._provider_error_code,
            response_diagnostic=dict(self._response_diagnostic) if self._response_diagnostic else None,
            provider_diagnostic=dict(self._provider_diagnostic) if self._provider_diagnostic else None)

    def diagnostics(self, termination_reason=None):
        summaries = summarize_mcp_tools(self.provider, self._tools)
        pending = [{**{key: value if key == "identity" and type(value) is int else _safe_identifier(value)
                       for key, value in item.items() if key in {"identity", "name", "server", "tool"}},
                    **safe_tool_timing(item)} for item in list(self._pending.values())[:500]]
        return {"complete": self._complete, "validation_error": self._error,
            "lifecycle_error_code": self._lifecycle_error_code,
            "termination_reason": termination_reason, "parsed_events": self._count,
            "parsed_bytes": self._parsed_bytes, "received_stdout_bytes": self._total,
            "unparsed_bytes": self._total - self._parsed_bytes, "largest_event_bytes": self._largest,
            "completed_tool_count": len(self._tools), "completed_mcp_count": len(summaries),
            "pending_tool_count": len(self._pending), "pending_tools": pending,
            "terminal_observed": self._terminal, "model_activity_observed": self._activity,
            "mcp_status_counts": dict(Counter(item["status"] for item in summaries)),
            "warnings": list(self._warnings),
            "response_diagnostic": dict(self._response_diagnostic) if self._response_diagnostic else None}


def parse_production_trace_file(provider, path, **limits):
    """Read-only re-diagnosis helper; the caller keeps the original run state."""
    collector = ProductionToolTraceCollector(provider, **limits)
    try:
        with open(path, "rb") as stream:
            while chunk := stream.read(65_536):
                collector.feed(chunk)
        collector.finish()
    except TraceStreamError:
        pass
    return collector.partial_trace(), collector.diagnostics()
