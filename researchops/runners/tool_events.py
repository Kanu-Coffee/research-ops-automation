"""Observed tool evidence for trusted development probes, not a tool firewall.

An advertised tool, model assertion, successful process exit or permission
denial is never counted as successful tool execution. Raw details stay in the
private evidence directory; callers publish only bounded evidence summaries.
"""

from dataclasses import dataclass
from collections import Counter
import json
import math
import re
from uuid import UUID

from researchops.runners.mcp_audit import mcp_protocol_error
from researchops.runners.provider_diagnostics import agy_error_diagnostic
from researchops.runners.response_transport import FileResponseReference, ResponseTransportError, parse_response_transport
from researchops.strict_json import MAX_JSON_INTEGER_DIGITS, MAX_JSON_NESTING


MAX_TRACE_BYTES = 2_000_000
SKILL_BUDGET_WARNING = "Skill descriptions were shortened to fit the skills context budget. Codex can still see every skill, but some descriptions are shorter. Disable unused skills or plugins to leave more room for the rest."
WEB_SEARCH_ID_COMPAT_WARNING = "CODEX_WEB_SEARCH_DUPLICATE_ID_COMPAT"
_ITEM_EVENTS = {"item.started", "item.updated", "item.completed"}
_CODEX_TOOLS = {"command_execution", "file_change", "web_search", "mcp_tool_call"}
_DENIED_ACTION_NAMES = {"command", "mcp", "read_url", "execute_url", "read_file", "write_file", "unsandboxed"}


@dataclass(frozen=True)
class ToolTrace:
    terminal: bool
    successful_terminal: bool
    response: dict | FileResponseReference | None
    tools: tuple[dict, ...]
    denied: bool
    usage: dict
    event_count: int
    response_error: str | None = None
    warnings: tuple[str, ...] = ()
    model_activity_observed: bool = False
    denied_actions: tuple[str, ...] = ()
    permission_mode: str | None = None
    cwd: str | None = None
    conversation_id: str | None = None
    terminal_status: str | None = None
    provider_error_code: str | None = None
    response_diagnostic: dict | None = None
    provider_diagnostic: dict | None = None


class _ObjectPairs(list):
    """Retain every JSON object pair until path-specific duplicate validation."""


def _normalize_event_pairs(value, *, provider, path=(), event_kind=None, depth=1):
    """Handle one observed CLI wire defect without a last-key-wins decoder.

    Codex currently serializes its local item ID and an upstream web-search ID
    under the same key. Only the exact direct-item header shape is accepted.
    No model response, MCP result, metadata or other duplicate key is repaired.
    The caller retains original raw bytes; this is diagnostic normalization.
    """
    if isinstance(value, (list, _ObjectPairs)) and depth > MAX_JSON_NESTING:
        raise ValueError("Provider event exceeds the nesting limit")
    if isinstance(value, _ObjectPairs):
        keys = [key for key, _ in value]
        key_counts = Counter(keys)
        duplicate_keys = {key for key, count in key_counts.items() if count > 1}
        compat = False
        if duplicate_keys:
            if not (provider == "codex_exec" and path == ("item",)
                    and isinstance(event_kind, str) and event_kind in _ITEM_EVENTS and duplicate_keys == {"id"}
                    and key_counts["id"] == 2 and keys[:3] == ["id", "type", "id"]
                    and value[1][1] == "web_search" and "upstream_search_id" not in keys):
                raise ValueError("Duplicate JSON object key in provider event")
            local_id, upstream_id = value[0][1], value[2][1]
            if not (isinstance(local_id, str) and re.fullmatch(r"item_[0-9]+", local_id)
                    and isinstance(upstream_id, str) and upstream_id.startswith("exec-")):
                raise ValueError("Invalid duplicated Codex web-search identity")
            try:
                valid_uuid = str(UUID(upstream_id[5:])) == upstream_id[5:]
            except ValueError:
                valid_uuid = False
            if not valid_uuid:
                raise ValueError("Invalid duplicated Codex web-search identity")
            compat = True
        if not path:
            event_kind = next((child for key, child in value if key == "type"), None)
        normalized, warnings, seen = {}, [], set()
        for key, child in value:
            key.encode("utf-8", errors="strict")
            target = "upstream_search_id" if compat and key == "id" and key in seen else key
            seen.add(key)
            normalized_child, child_warnings = _normalize_event_pairs(
                child, provider=provider, path=path + (key,), event_kind=event_kind, depth=depth + 1)
            normalized[target] = normalized_child
            warnings.extend(child_warnings)
        if compat:
            warnings.append(WEB_SEARCH_ID_COMPAT_WARNING)
        return normalized, warnings
    if isinstance(value, list):
        normalized, warnings = [], []
        for index, child in enumerate(value):
            normalized_child, child_warnings = _normalize_event_pairs(
                child, provider=provider, path=path + (index,), event_kind=event_kind, depth=depth + 1)
            normalized.append(normalized_child)
            warnings.extend(child_warnings)
        return normalized, warnings
    if isinstance(value, str):
        value.encode("utf-8", errors="strict")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite provider event number")
    return value, []


def _event_integer(value):
    if len(value.lstrip("-")) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError("Provider integer exceeds the digit limit")
    return int(value)


def decode_provider_event(provider, raw):
    """Decode one original-byte-budgeted event without ASCII reserialization."""
    try:
        pairs = json.loads(raw.decode("utf-8"), object_pairs_hook=_ObjectPairs,
                           parse_int=_event_integer)
        event, warnings = _normalize_event_pairs(pairs, provider=provider)
        if not isinstance(event, dict):
            raise ValueError("Provider event must be an object")
        kind = event.get("type")
        if kind is not None and (not isinstance(kind, str) or not kind):
            raise ValueError("Event type must be nonempty text when present")
        return event, warnings
    except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError("Invalid strict provider event data") from exc


def _event_records(provider, raw):
    if not isinstance(raw, bytes):
        raise ValueError("Provider trace must be raw bytes")
    if len(raw) > MAX_TRACE_BYTES:
        raise ValueError("Provider trace exceeds its byte limit")
    warnings, records = [], []
    count = 0
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        count += 1
        if count > 10_000 or len(line) > MAX_TRACE_BYTES:
            raise ValueError("Provider trace exceeds its event or line limit")
        event, line_warnings = decode_provider_event(provider, line)
        records.append(event)
        warnings.extend(line_warnings)
    return records, tuple(dict.fromkeys(warnings))


def _response_details(value):
    try:
        return parse_response_transport(value), None
    except ResponseTransportError as exc:
        return None, exc


def _response(value):
    """Keep the existing tuple contract for callers that only need a safe string."""
    document, error = _response_details(value)
    return document, str(error) if error is not None else None


def _usage(value):
    if not isinstance(value, dict) or any(type(n) is not int or n < 0 for n in value.values()):
        raise ValueError("Invalid provider usage counters")
    return value


def _denied_actions(value):
    if not isinstance(value, list):
        raise ValueError("Invalid provider denied-action evidence")
    names = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("action"), str) or not item["action"]:
            raise ValueError("Invalid provider denied-action evidence")
        names.append(item["action"] if item["action"] in _DENIED_ACTION_NAMES else "unknown")
    return tuple(dict.fromkeys(names))


def parse_tool_trace(provider, raw):
    if provider not in {"codex_exec", "antigravity_exec"}:
        raise ValueError("Unsupported tool provider")
    events, warnings = _event_records(provider, raw)
    if provider == "codex_exec":
        return _codex(events, warnings)
    return _antigravity(events)


def _codex(events, wire_warnings=()):
    kinds = [entry.get("type") for entry in events]
    if len(kinds) < 3 or kinds[:2] != ["thread.started", "turn.started"] or kinds.count("thread.started") != 1 or kinds.count("turn.started") != 1:
        raise ValueError("Expected one ordered Codex thread and turn")
    if kinds[-1] not in {"turn.completed", "turn.failed"} or sum(kinds.count(k) for k in ("turn.completed", "turn.failed")) != 1:
        raise ValueError("Expected one terminal Codex event")
    tools, messages, seen, warnings, pending = [], [], set(), list(wire_warnings), {}
    failed = False
    for event in events[2:-1]:
        kind = event.get("type")
        if kind == "error":
            failed = True
            continue
        if kind not in {"item.started", "item.updated", "item.completed"} or not isinstance(event.get("item"), dict):
            raise ValueError("Unsupported Codex lifecycle event")
        item = event["item"]
        name = item.get("type")
        if not isinstance(name, str):
            raise ValueError("Invalid Codex item type")
        if name == "error" and item.get("message") == SKILL_BUDGET_WARNING:
            if kind == "item.completed":
                warnings.append(SKILL_BUDGET_WARNING)
            continue
        if name not in {"agent_message", "reasoning", "command_execution", "file_change", "web_search", "mcp_tool_call"}:
            raise ValueError("Unapproved Codex action in development probe")
        identity = item.get("id")
        if name in _CODEX_TOOLS:
            if not isinstance(identity, str) or not identity or identity in seen:
                raise ValueError("Invalid Codex tool lifecycle identity")
            current = (name, item.get("upstream_search_id"))
            previous = pending.get(identity)
            if previous is not None and (previous[0] != current[0] or
                    previous[1] is not None and current[1] is not None and previous[1] != current[1]):
                raise ValueError("Codex tool identity changed during execution")
            if kind != "item.completed":
                if kind == "item.started" and previous is not None:
                    raise ValueError("Duplicate Codex tool start")
                pending[identity] = current
            else:
                pending.pop(identity, None)
        if kind != "item.completed":
            continue
        if not isinstance(identity, str) or identity in seen:
            raise ValueError("Missing or duplicate completed Codex item identity")
        seen.add(identity)
        if name == "agent_message":
            messages.append(item.get("text"))
        elif name != "reasoning":
            success = item.get("status") == "completed"
            if name == "web_search":
                success = item.get("status", "completed") == "completed"
            if name == "command_execution":
                success = success and type(item.get("exit_code")) is int and item["exit_code"] == 0
            if name == "mcp_tool_call":
                result = item.get("result")
                success = success and isinstance(result, dict) and not item.get("error") and not result.get("isError")
            tools.append({"name": name, "success": bool(success), "details": item})
    if pending:
        raise ValueError("Codex terminal event has unfinished tool actions")
    response, diagnostic = _response_details(messages[-1] if messages else None)
    error = str(diagnostic) if diagnostic is not None else None
    usage = _usage(events[-1].get("usage", {}))
    activity = any(isinstance(message, str) and bool(message.strip()) for message in messages) or usage.get("output_tokens", 0) > 0
    return ToolTrace(True, kinds[-1] == "turn.completed" and not failed, response,
        tuple(tools), False, usage, len(events), error, tuple(warnings), activity,
        response_diagnostic=diagnostic.to_dict() if diagnostic is not None else None)


def consume_agy_finish(step, pending, seen):
    """Close only a matching finish control action, never an actual tool."""
    if step.get('step_type') != 'finish':
        return False
    index = step.get('step_index')
    if step.get('state') != 'DONE' or 'tool_name' in step or 'tool_info' in step:
        raise ValueError('Invalid Antigravity finish completion')
    # Legacy standalone finish markers had no index. They close no pending work.
    if index is None:
        return True
    if type(index) is not int or index < 0 or index in seen:
        raise ValueError('Invalid or duplicate Antigravity finish index')
    prior = pending.get(index)
    if prior is not None:
        name = prior.get('name') if isinstance(prior, dict) else prior
        if name != 'finish':
            raise ValueError('Antigravity finish cannot complete another tool')
        del pending[index]
    seen.add(index)
    return True


def _antigravity(events):
    kinds = [entry.get("event") for entry in events]
    if len(kinds) < 2 or kinds[0] != "init" or not isinstance(events[0].get("init"), dict) or kinds[-1] != "result":
        raise ValueError("Expected Antigravity initial and terminal events")
    permission_mode = events[0]["init"].get("permission_mode")
    if permission_mode is not None and permission_mode not in ("request-review", "always-proceed"):
        raise ValueError("Unknown Antigravity permission mode")
    cwd = events[0]["init"].get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd or "\x00" in cwd):
        raise ValueError("Invalid Antigravity working directory")
    conversation_id = events[0].get("conversation_id")
    if conversation_id is not None:
        try:
            valid_id = isinstance(conversation_id, str) and str(UUID(conversation_id)) == conversation_id
        except ValueError:
            valid_id = False
        if not valid_id:
            raise ValueError("Invalid Antigravity conversation identity")
    tools, seen, pending, warnings = [], set(), {}, []
    activity = False
    for event in events[1:-1]:
        if event.get("event") != "step_update" or not isinstance(event.get("step_update"), dict):
            raise ValueError("Unsupported Antigravity lifecycle event")
        step = event["step_update"]
        if step.get("conversation_id") is not None and step["conversation_id"] != conversation_id:
            raise ValueError("Antigravity conversation identity changed")
        kind = step.get("step_type")
        if not isinstance(kind, str):
            raise ValueError("Invalid Antigravity step type")
        if consume_agy_finish(step, pending, seen):
            continue
        if kind in {"user_input", "agent_response", "checkpoint"}:
            if kind == "agent_response":
                activity = activity or any(isinstance(step.get(key), str) and bool(step[key].strip())
                                          for key in ("text", "text_delta"))
            continue
        if kind == "error_message":
            # Agy emits recoverable diagnostic steps as well as terminal
            # errors. These are lifecycle evidence, not a new tool action.
            # Keep a bounded marker; never publish raw provider error text.
            index = step.get("step_index")
            if step.get("state") != "DONE" or type(index) is not int or index < 0 or index in seen or index in pending:
                raise ValueError("Invalid Antigravity diagnostic step")
            if "tool_name" in step or "tool_info" in step:
                raise ValueError("Antigravity diagnostic step contains tool evidence")
            seen.add(index)
            warnings.append("ANTIGRAVITY_ERROR_MESSAGE")
            continue
        if kind != "tool":
            raise ValueError("Unapproved Antigravity action in development probe")
        state, index = step.get("state"), step.get("step_index")
        if state not in {"ACTIVE", "DONE", "ERROR"} or type(index) is not int or index < 0 or index in seen:
            raise ValueError("Invalid or duplicate Antigravity tool step")
        info = step.get("tool_info")
        name = step.get("tool_name")
        if not isinstance(info, dict) or not isinstance(name, str) or not name or info.get("name") != name:
            raise ValueError("Invalid Antigravity tool evidence")
        if index in pending and pending[index] != name:
            raise ValueError("Antigravity tool identity changed during execution")
        if state == "ACTIVE":
            pending[index] = name
            continue
        pending.pop(index, None)
        seen.add(index)
        provider_success = state == "DONE" and not info.get("error")
        tools.append({"name": name, "success": provider_success and not (
            name == "call_mcp_tool" and mcp_protocol_error("antigravity_exec", info)),
            "provider_reported_success": bool(provider_success), "state": state,
            "details": info, "step_index": index})
    if pending:
        raise ValueError("Antigravity terminal event has unfinished tool actions")
    result = events[-1].get("result")
    if not isinstance(result, dict) or result.get("status") not in {"SUCCESS", "ERROR", "CANCELED", "CANCELLED", "TIMEOUT", "FAILURE"}:
        raise ValueError("Unknown Antigravity terminal status")
    if result.get("conversation_id") is not None and result["conversation_id"] != conversation_id:
        raise ValueError("Antigravity terminal conversation identity changed")
    denied_actions = _denied_actions(result.get("denied_actions", []))
    denied = bool(denied_actions)
    action_types = {"run_command": "command", "call_mcp_tool": "mcp", "read_url_content": "read_url"}
    for tool in tools:
        if "unknown" in denied_actions or action_types.get(tool["name"]) in denied_actions:
            # Agy can report MCP DONE for a call it then auto-denies. Keep
            # raw/provider evidence, but never expose that as tool success.
            tool["success"] = False
            tool["denied"] = True
    response, diagnostic = _response_details(result.get("structured_output"))
    error = str(diagnostic) if diagnostic is not None else None
    usage = _usage(result.get("usage", {}))
    activity = activity or usage.get("output_tokens", 0) > 0
    provider_diagnostic = agy_error_diagnostic(result)
    provider_error_code = provider_diagnostic["code"] if provider_diagnostic else None
    return ToolTrace(True, result.get("status") == "SUCCESS" and not result.get("error") and not denied, response,
        tuple(tools), denied, usage, len(events), error, warnings=tuple(dict.fromkeys(warnings)), model_activity_observed=activity,
        denied_actions=denied_actions, permission_mode=permission_mode, cwd=cwd, conversation_id=conversation_id,
        terminal_status=result["status"], provider_error_code=provider_error_code,
        response_diagnostic=diagnostic.to_dict() if diagnostic is not None else None,
        provider_diagnostic=provider_diagnostic)
