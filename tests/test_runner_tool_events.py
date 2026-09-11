"""Recorded protocol shapes, with synthetic payloads and no model/tool calls."""

import copy
import json
import unittest

from researchops.runners.tool_events import (
    MAX_TRACE_BYTES, WEB_SEARCH_ID_COMPAT_WARNING, parse_tool_trace,
)
from researchops.runners.response_transport import FileResponseReference, RESPONSE_DIAGNOSTIC_CODES


def encode(events):
    return ("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n").encode()


def envelope(value=None):
    return {"response_json": json.dumps(value or {"status": "ok", "timezone": "Asia/Seoul"})}


def codex_events(items=(), *, status="turn.completed"):
    events = [{"type": "thread.started", "thread_id": "synthetic-thread"},
              {"type": "turn.started"}]
    events.extend({"type": "item.completed", "item": item} for item in items)
    events.append({"type": "item.completed", "item": {
        "id": "final", "type": "agent_message", "text": json.dumps(envelope())}})
    events.append({"type": status, "usage": {"input_tokens": 12, "output_tokens": 5}})
    return events


def agy_step(index, name, *, state="DONE", error=None):
    info = {"name": name, "parameters": {}}
    if error is not None:
        info["error"] = error
    return {"event": "step_update", "step_update": {
        "step_index": index, "state": state, "step_type": "tool",
        "tool_name": name, "tool_info": info}}


def agy_events(steps=(), *, status="SUCCESS"):
    return [{"event": "init", "init": {
        "cwd": "/tmp/synthetic-workspace", "permission_mode": "request-review",
        "tools": ["write_to_file", "run_command", "search_web", "call_mcp_tool"]}},
        {"event": "step_update", "step_update": {
            "step_type": "user_input", "state": "DONE", "step_index": 0}},
        *steps,
        {"event": "result", "result": {
            "status": status, "structured_output": envelope(),
            "response": 'untrusted duplicate JSON and completion metadata, never parse this',
            "usage": {"input_tokens": 17, "output_tokens": 7}}}]


def with_raw_items(lines):
    events = codex_events()
    return encode(events[:2]) + b"\n".join(lines) + b"\n" + encode(events[2:])


class TestRunnerToolEvents(unittest.TestCase):
    def test_codex_multiple_messages_and_all_completed_native_tool_kinds(self):
        items = [
            {"id": "commentary", "type": "agent_message", "text": "I will inspect the sample."},
            {"id": "reasoning", "type": "reasoning", "text": "Synthetic planning."},
            {"id": "command", "type": "command_execution", "status": "completed",
             "command": "python3 analyze.py", "exit_code": 0, "aggregated_output": '{"total":74}'},
            {"id": "file", "type": "file_change", "status": "completed",
             "changes": [{"path": "/tmp/synthetic-workspace/analyze.py", "kind": "add"}]},
            {"id": "web", "type": "web_search", "status": "completed",
             "action": {"type": "search", "query": "site:docs.python.org csv.DictReader"}},
            {"id": "mcp", "type": "mcp_tool_call", "status": "completed",
             "server": "researchops_probe", "tool": "search_documents",
             "arguments": {"query": "numeric"},
             "result": {"content": [{"type": "text", "text": "synthetic receipt"}], "isError": False}},
        ]
        events = codex_events(items)
        events.insert(2, {"type": "item.started", "item": {
            "id": "command", "type": "command_execution", "status": "in_progress"}})
        events.insert(3, {"type": "item.updated", "item": {
            "id": "command", "type": "command_execution", "status": "in_progress"}})
        trace = parse_tool_trace("codex_exec", encode(events))
        self.assertTrue(trace.terminal)
        self.assertTrue(trace.successful_terminal)
        self.assertFalse(trace.denied)
        self.assertEqual([item["name"] for item in trace.tools],
                         ["command_execution", "file_change", "web_search", "mcp_tool_call"])
        self.assertTrue(all(item["success"] for item in trace.tools))
        self.assertEqual(trace.response, {"status": "ok", "timezone": "Asia/Seoul"})
        self.assertEqual(trace.usage, {"input_tokens": 12, "output_tokens": 5})
        self.assertEqual(trace.event_count, len(events))
        self.assertIsNone(trace.response_error)
        self.assertIsNone(trace.permission_mode)
        self.assertIsNone(trace.cwd)
        self.assertIsNone(trace.conversation_id)

    def test_codex_failed_commands_files_web_and_mcp_are_not_successful(self):
        items = [
            {"id": "c1", "type": "command_execution", "status": "completed", "exit_code": 1},
            {"id": "c2", "type": "command_execution", "status": "completed", "exit_code": False},
            {"id": "c3", "type": "command_execution", "status": "in_progress", "exit_code": 0},
            {"id": "f", "type": "file_change", "status": "failed"},
            {"id": "w", "type": "web_search", "status": "failed"},
            {"id": "m1", "type": "mcp_tool_call", "status": "completed", "result": {"isError": True}},
            {"id": "m2", "type": "mcp_tool_call", "status": "completed", "result": None},
            {"id": "m3", "type": "mcp_tool_call", "status": "completed",
             "result": {"isError": False}, "error": {"message": "synthetic failure"}},
        ]
        trace = parse_tool_trace("codex_exec", encode(codex_events(items)))
        self.assertEqual(len(trace.tools), len(items))
        self.assertFalse(any(tool["success"] for tool in trace.tools))

    def test_codex_top_level_error_or_failed_terminal_cannot_be_success(self):
        events = codex_events()
        events.insert(2, {"type": "error", "message": "synthetic remote failure"})
        for data in (events, codex_events(status="turn.failed")):
            with self.subTest(data=data):
                trace = parse_tool_trace("codex_exec", encode(data))
                self.assertTrue(trace.terminal)
                self.assertFalse(trace.successful_terminal)

    def test_codex_exact_benign_skill_warning_is_preserved_not_a_tool(self):
        message = ("Skill descriptions were shortened to fit the skills context budget. "
                   "Codex can still see every skill, but some descriptions are shorter. "
                   "Disable unused skills or plugins to leave more room for the rest.")
        events = codex_events([{"id": "warning", "type": "error", "message": message}])
        trace = parse_tool_trace("codex_exec", encode(events))
        self.assertTrue(trace.successful_terminal)
        self.assertEqual(trace.tools, ())
        self.assertEqual(trace.warnings, (message,))
        for unknown in (message + " Unexpected private failure.", "private upstream failure"):
            with self.subTest(message=unknown):
                try:
                    trace = parse_tool_trace("codex_exec", encode(codex_events([
                        {"id": "error", "type": "error", "message": unknown}])))
                except ValueError as error:
                    self.assertNotIn("private", str(error))
                else:
                    self.assertFalse(trace.successful_terminal)

    def test_codex_requires_unique_ordered_session_and_terminal(self):
        valid = codex_events()
        for data in ([], valid[1:], valid[:-1], valid + [valid[-1]],
                     [valid[1], valid[0], *valid[2:]],
                     [valid[0], valid[0], *valid[1:]],
                     [*valid[:-1], {"type": "turn.failed"}, valid[-1]],
                     valid + [valid[2]]):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_tool_trace("codex_exec", encode(data))

    def test_codex_unknown_action_or_duplicate_completed_identity_is_rejected(self):
        for name in ("collab_tool_call", "future_mutation", None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                parse_tool_trace("codex_exec", encode(codex_events([{"id": "x", "type": name}])))
        for identity in (None, 1, "final"):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                parse_tool_trace("codex_exec", encode(codex_events([
                    {"id": identity, "type": "agent_message", "text": "commentary"}])))

    def test_antigravity_native_tool_lifecycle_uses_structured_output_only(self):
        steps = [agy_step(2, "write_to_file", state="ACTIVE"),
                 agy_step(2, "write_to_file"), agy_step(4, "view_file"),
                 agy_step(6, "search_web"), agy_step(8, "call_mcp_tool")]
        steps[-1]["step_update"]["tool_info"]["parameters"] = {
            "ServerName": "researchops_probe", "ToolName": "search_documents",
            "Arguments": {"query": "numeric"}}
        events = agy_events(steps)
        trace = parse_tool_trace("antigravity_exec", encode(events))
        self.assertTrue(trace.terminal)
        self.assertTrue(trace.successful_terminal)
        self.assertFalse(trace.denied)
        self.assertEqual([tool["name"] for tool in trace.tools],
                         ["write_to_file", "view_file", "search_web", "call_mcp_tool"])
        self.assertTrue(all(tool["success"] for tool in trace.tools))
        self.assertEqual(trace.response, {"status": "ok", "timezone": "Asia/Seoul"})
        self.assertEqual(trace.usage, {"input_tokens": 17, "output_tokens": 7})
        self.assertEqual(trace.event_count, len(events))

    def test_antigravity_records_effective_permission_mode_without_inference(self):
        for mode in ("request-review", "always-proceed", None):
            events = agy_events([agy_step(2, "view_file")])
            if mode is None:
                events[0]["init"].pop("permission_mode")
            else:
                events[0]["init"]["permission_mode"] = mode
            with self.subTest(mode=mode):
                trace = parse_tool_trace("antigravity_exec", encode(events))
                self.assertEqual(trace.permission_mode, mode)
                self.assertTrue(trace.successful_terminal)
                self.assertTrue(trace.tools[0]["success"])

    def test_antigravity_effective_workspace_is_preserved_or_missing_not_inferred(self):
        for cwd in ("/tmp/synthetic-workspace", "/tmp/서울 연구", None):
            events = agy_events()
            if cwd is None:
                events[0]["init"].pop("cwd")
            else:
                events[0]["init"]["cwd"] = cwd
            with self.subTest(cwd=cwd):
                trace = parse_tool_trace("antigravity_exec", encode(events))
                self.assertEqual(trace.cwd, cwd)

    def test_antigravity_invalid_workspace_metadata_is_rejected_without_payload_echo(self):
        for cwd in ("", True, 1, [], {}, "/private-workspace-fixture\x00suffix"):
            events = agy_events()
            events[0]["init"]["cwd"] = cwd
            with self.subTest(cwd=cwd), self.assertRaises(ValueError) as caught:
                parse_tool_trace("antigravity_exec", encode(events))
            self.assertNotIn("private-workspace-fixture", str(caught.exception))

    def test_antigravity_conversation_identity_and_completed_step_index_are_preserved(self):
        identity = "11111111-1111-4111-8111-111111111111"
        events = agy_events([agy_step(2, "view_file", state="ACTIVE"), agy_step(2, "view_file")])
        events[0]["conversation_id"] = identity
        for event in events[1:-1]:
            event["step_update"]["conversation_id"] = identity
        events[-1]["result"]["conversation_id"] = identity
        trace = parse_tool_trace("antigravity_exec", encode(events))
        self.assertEqual(trace.conversation_id, identity)
        self.assertEqual(trace.tools[0]["step_index"], 2)
        self.assertEqual(len(trace.tools), 1)
        self.assertIsNone(parse_tool_trace("antigravity_exec", encode(agy_events())).conversation_id)

    def test_antigravity_invalid_noncanonical_conversation_identity_is_rejected(self):
        for identity in ("", "private-conversation-fixture", "../../private-conversation-fixture",
                         "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", "11111111111141118111111111111111",
                         True, 1, [], {}):
            events = agy_events()
            events[0]["conversation_id"] = identity
            with self.subTest(identity=identity), self.assertRaises(ValueError) as caught:
                parse_tool_trace("antigravity_exec", encode(events))
            self.assertNotIn("private-conversation-fixture", str(caught.exception))

    def test_antigravity_changed_or_unanchored_step_and_terminal_identity_is_rejected(self):
        identity = "11111111-1111-4111-8111-111111111111"
        different = "22222222-2222-4222-8222-222222222222"
        for include_initial in (False, True):
            for location in ("step", "terminal"):
                events = agy_events([agy_step(2, "view_file")])
                if include_initial:
                    events[0]["conversation_id"] = identity
                target = events[2]["step_update"] if location == "step" else events[-1]["result"]
                target["conversation_id"] = different
                with self.subTest(include_initial=include_initial, location=location), self.assertRaises(ValueError):
                    parse_tool_trace("antigravity_exec", encode(events))

    def test_antigravity_unknown_permission_mode_is_rejected_without_payload_echo(self):
        for mode in ("", "private-mode-fixture", "accept-edits", "yolo", True, 1, [], {}):
            events = agy_events()
            events[0]["init"]["permission_mode"] = mode
            with self.subTest(mode=mode), self.assertRaises(ValueError) as caught:
                parse_tool_trace("antigravity_exec", encode(events))
            self.assertNotIn("private-mode-fixture", str(caught.exception))

    def test_antigravity_auto_approval_mode_does_not_override_observed_denial(self):
        for tool, action in (("run_command", "command"), ("read_url_content", "read_url"),
                             ("call_mcp_tool", "mcp")):
            events = agy_events([agy_step(2, tool)])
            events[0]["init"]["permission_mode"] = "always-proceed"
            events[-1]["result"]["denied_actions"] = [{"action": action}]
            with self.subTest(tool=tool):
                trace = parse_tool_trace("antigravity_exec", encode(events))
                self.assertEqual(trace.permission_mode, "always-proceed")
                self.assertTrue(trace.denied)
                self.assertTrue(trace.tools[0]["provider_reported_success"])
                self.assertFalse(trace.tools[0]["success"])
                self.assertFalse(trace.successful_terminal)

    def test_antigravity_success_with_structured_output_and_denial_is_not_success(self):
        events = agy_events([agy_step(2, "view_file"), agy_step(4, "run_command", state="ERROR",
                            error={"type": "TOOL_ERROR", "message": "permission denied"})])
        events[-1]["result"]["denied_actions"] = [{"action": "command", "display_name": "RunCommand"}]
        trace = parse_tool_trace("antigravity_exec", encode(events))
        self.assertTrue(trace.terminal)
        self.assertTrue(trace.denied)
        self.assertEqual(trace.denied_actions, ("command",))
        self.assertFalse(trace.successful_terminal)
        self.assertIsNotNone(trace.response)
        self.assertFalse(trace.tools[-1]["success"])

    def test_antigravity_canceled_mcp_done_still_requires_denial_and_receipt_checks(self):
        # Observed agy: MCP permission rejection can still emit a DONE tool step.
        # A consumer must not promote DONE to research success or invent receipts.
        events = agy_events([agy_step(2, "view_file"), agy_step(4, "call_mcp_tool")], status="CANCELED")
        events[-1]["result"].pop("structured_output")
        events[-1]["result"]["denied_actions"] = [{"action": "mcp", "display_name": "CallMcpTool"}]
        trace = parse_tool_trace("antigravity_exec", encode(events))
        self.assertTrue(trace.terminal)
        self.assertTrue(trace.denied)
        self.assertEqual(trace.denied_actions, ("mcp",))
        self.assertFalse(trace.successful_terminal)
        self.assertTrue(trace.tools[-1]["provider_reported_success"])
        self.assertFalse(trace.tools[-1]["success"])
        self.assertIsNone(trace.response)
        self.assertIsNotNone(trace.response_error)

    def test_antigravity_failed_tools_are_preserved_but_not_counted_successful(self):
        events = agy_events([agy_step(2, "write_to_file", state="ERROR"),
                            agy_step(4, "call_mcp_tool", error={"message": "synthetic server failure"})])
        trace = parse_tool_trace("antigravity_exec", encode(events))
        self.assertEqual(len(trace.tools), 2)
        self.assertFalse(any(tool["success"] for tool in trace.tools))

    def test_antigravity_diagnostic_error_preserves_failed_terminal_evidence(self):
        diagnostic = {"event": "step_update", "step_update": {
            "step_type": "error_message", "state": "DONE", "step_index": 3}}
        events = agy_events([agy_step(2, "call_mcp_tool", state="ERROR"), diagnostic], status="ERROR")
        events[-1]["result"]["error"] = "The stream was interrupted. Please continue the task you were working on."
        trace = parse_tool_trace("antigravity_exec", encode(events))
        self.assertTrue(trace.terminal)
        self.assertFalse(trace.successful_terminal)
        self.assertEqual(trace.terminal_status, "ERROR")
        self.assertEqual(trace.provider_error_code, "stream_interrupted")
        self.assertEqual(trace.warnings, ("ANTIGRAVITY_ERROR_MESSAGE",))
        self.assertFalse(trace.tools[0]["success"])
        # Intermediate diagnostics may recover: the provider's final result
        # decides success, not the mere presence of an earlier error message.
        events[-1]["result"]["status"] = "SUCCESS"
        events[-1]["result"].pop("error")
        self.assertTrue(parse_tool_trace("antigravity_exec", encode(events)).successful_terminal)

    def test_antigravity_diagnostic_cannot_hide_tools_or_incomplete_lifecycle(self):
        diagnostic = {"event": "step_update", "step_update": {
            "step_type": "error_message", "state": "DONE", "step_index": 3}}
        for field, value in (("state", "ACTIVE"), ("step_index", True), ("step_index", -1),
                             ("tool_name", "run_command"), ("tool_info", {})):
            modified = copy.deepcopy(diagnostic)
            modified["step_update"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                parse_tool_trace("antigravity_exec", encode(agy_events([modified])))
        for steps in ([diagnostic, diagnostic], [agy_step(2, "run_command", state="ACTIVE"), diagnostic]):
            with self.subTest(steps=steps), self.assertRaises(ValueError):
                parse_tool_trace("antigravity_exec", encode(agy_events(steps, status="ERROR")))

    def test_antigravity_unknown_provider_error_is_not_echoed(self):
        events = agy_events(status="ERROR")
        events[-1]["result"]["error"] = {"message": "private provider diagnostic"}
        trace = parse_tool_trace("antigravity_exec", encode(events))
        self.assertEqual(trace.provider_error_code, "provider_error")
        self.assertNotIn("private provider diagnostic", repr(trace))

    def test_antigravity_initial_and_unique_last_terminal_required(self):
        valid = agy_events()
        for data in ([], valid[1:], valid[:-1], valid + [valid[-1]],
                     [valid[0], valid[0], *valid[1:]], valid + [valid[1]],
                     [{"event": "init", "init": []}, valid[-1]]):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_tool_trace("antigravity_exec", encode(data))

    def test_antigravity_unknown_actions_and_invalid_tool_evidence_are_rejected(self):
        valid = agy_step(2, "view_file")
        invalid = []
        for field, value in (("step_type", "subagent"), ("state", "WAITING"),
                             ("step_index", True), ("step_index", -1), ("tool_name", "")):
            step = copy.deepcopy(valid)
            step["step_update"][field] = value
            invalid.append([step])
        step = copy.deepcopy(valid)
        step["step_update"]["tool_info"]["name"] = "different_tool"
        invalid.extend(([step], [valid, valid]))
        for steps in invalid:
            with self.subTest(steps=steps), self.assertRaises(ValueError):
                parse_tool_trace("antigravity_exec", encode(agy_events(steps)))

    def test_invalid_provider_usage_rejected_without_payload_echo(self):
        for provider, factory in (("codex_exec", codex_events), ("antigravity_exec", agy_events)):
            for usage in (None, [], {"input_tokens": True}, {"input_tokens": -1},
                          {"input_tokens": 1.5}, {"private_usage": "secret-fixture"}):
                events = factory()
                terminal = events[-1] if provider == "codex_exec" else events[-1]["result"]
                terminal["usage"] = usage
                with self.subTest(provider=provider, usage=usage), self.assertRaises(ValueError) as caught:
                    parse_tool_trace(provider, encode(events))
                self.assertNotIn("secret-fixture", str(caught.exception))

    def test_strict_event_json_rejects_duplicates_nonfinite_and_invalid_utf8(self):
        for provider in ("codex_exec", "antigravity_exec"):
            for raw in (b'{"type":"thread.started","type":"turn.started"}\n',
                        b'{"x":NaN}\n', b'{"x":1e999}\n', b'{"x":"\xff"}\n',
                        b'[]\n', b'{not-json}\n'):
                with self.subTest(provider=provider, raw=raw), self.assertRaises(ValueError):
                    parse_tool_trace(provider, raw)

    def test_invalid_structured_json_is_reported_without_using_human_response(self):
        invalid = [None, [], {"response_json": "[]"}, {"response_json": '{"x":1,"x":2}'},
                   {"response_json": '{"x":NaN}'}, {"response_json": '{"x":1e999}'},
                   {"response_json": '{"x":"\\ud800"}'},
                   {"response_json": '{"private":"secret-fixture"'},
                   {"response_json": "{}", "unexpected": True}]
        for provider, factory in (("codex_exec", codex_events), ("antigravity_exec", agy_events)):
            for value in invalid:
                events = factory()
                if provider == "codex_exec":
                    events[-2]["item"]["text"] = json.dumps(value)
                else:
                    events[-1]["result"]["structured_output"] = value
                    events[-1]["result"]["response"] = json.dumps(envelope())
                trace = parse_tool_trace(provider, encode(events))
                with self.subTest(provider=provider, value=value):
                    self.assertIsNone(trace.response)
                    self.assertIn(trace.response_diagnostic["code"], RESPONSE_DIAGNOSTIC_CODES)
                    self.assertIn(trace.response_diagnostic["code"], trace.response_error)
                    self.assertNotIn("secret-fixture", trace.response_error)
                    self.assertNotIn("secret-fixture", repr(trace.response_diagnostic))

    def test_final_response_diagnostics_distinguish_outer_inner_and_shape_errors(self):
        invalid = [("{broken", "outer_json_invalid", "outer_json"),
                   ({"response_json": "{broken"}, "inner_json_invalid", "inner_json"),
                   ({"response_json": {}}, "response_json_type_invalid", "envelope"),
                   ({"response_json": "[]"}, "inner_object_required", "inner_type"),
                   ({"response_json": "{}", "private-key": "secret-fixture"}, "envelope_shape_invalid", "envelope")]
        for provider, factory in (("codex_exec", codex_events), ("antigravity_exec", agy_events)):
            for value, code, stage in invalid:
                events = factory()
                if provider == "codex_exec":
                    events[-2]["item"]["text"] = value if isinstance(value, str) else json.dumps(value)
                else:
                    events[-1]["result"]["structured_output"] = value
                trace = parse_tool_trace(provider, encode(events))
                with self.subTest(provider=provider, code=code):
                    self.assertTrue(trace.terminal)
                    self.assertTrue(trace.model_activity_observed)
                    self.assertIsNone(trace.response)
                    self.assertEqual(trace.response_diagnostic["code"], code)
                    self.assertEqual(trace.response_diagnostic["stage"], stage)
                    self.assertNotIn("private-key", trace.response_error)
                    self.assertNotIn("secret-fixture", trace.response_error)

    def test_final_file_references_are_typed_and_require_separate_file_validation(self):
        reference = {"transport_version": 2, "response_file": "submission.json", "sha256": "a" * 64, "size_bytes": 2}
        for provider, factory in (("codex_exec", codex_events), ("antigravity_exec", agy_events)):
            events = factory()
            if provider == "codex_exec":
                events[-2]["item"]["text"] = json.dumps(reference)
            else:
                events[-1]["result"]["structured_output"] = reference
            trace = parse_tool_trace(provider, encode(events))
            with self.subTest(provider=provider):
                self.assertIsInstance(trace.response, FileResponseReference)
                self.assertEqual(trace.response.to_dict(), reference)
                self.assertIsNone(trace.response_error)
                self.assertIsNone(trace.response_diagnostic)

    def test_missing_usage_and_advertised_tools_do_not_fabricate_evidence(self):
        for provider, factory in (("codex_exec", codex_events), ("antigravity_exec", agy_events)):
            events = factory()
            terminal = events[-1] if provider == "codex_exec" else events[-1]["result"]
            terminal.pop("usage")
            trace = parse_tool_trace(provider, encode(events))
            with self.subTest(provider=provider):
                self.assertEqual(trace.usage, {})
                self.assertEqual(trace.tools, ())

    def test_unknown_provider_and_output_overflow_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_tool_trace("fake", encode(codex_events()))
        for provider in ("codex_exec", "antigravity_exec"):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                parse_tool_trace(provider, b" " * (MAX_TRACE_BYTES + 1))

    def test_codex_observed_duplicate_web_ids_are_preserved_without_last_wins(self):
        upstream = "exec-15e62396-7e44-429e-8333-014ec3701aa8"
        raw = with_raw_items([
            ('{"type":"%s","item":{"id":"item_2","type":"web_search",'
             '"id":"%s","query":"csv.DictReader","action":{"type":"search"}}}'
             % (event_type, upstream)).encode()
            for event_type in ("item.started", "item.updated", "item.completed")])
        original = bytes(raw)
        trace = parse_tool_trace("codex_exec", raw)
        self.assertTrue(trace.successful_terminal)
        self.assertEqual(len(trace.tools), 1)
        self.assertTrue(trace.tools[0]["success"])
        self.assertEqual(trace.tools[0]["details"]["id"], "item_2")
        self.assertEqual(trace.tools[0]["details"]["upstream_search_id"], upstream)
        self.assertEqual(trace.warnings, (WEB_SEARCH_ID_COMPAT_WARNING,))
        self.assertEqual(raw, original)
        self.assertEqual(raw.count(b'"id":"item_2","type":"web_search","id"'), 3)

    def test_duplicate_id_compatibility_rejects_every_other_duplicate_shape(self):
        item = ('{"id":"item_2","type":"web_search",'
                '"id":"exec-15e62396-7e44-429e-8333-014ec3701aa8","query":"csv"}')
        invalid = [
            item.replace('"web_search"', '"mcp_tool_call"'),
            item.replace('"web_search"', '"agent_message"'),
            item.replace('"exec-15e62396-7e44-429e-8333-014ec3701aa8"', '"item_2"'),
            item.replace('"item_2"', '"item-2"'),
            item.replace('"exec-15e62396-7e44-429e-8333-014ec3701aa8"', '"exec-bad-uuid"'),
            item.replace('"query":"csv"', '"query":"csv","query":"csv"'),
            item.replace('"query":"csv"', '"upstream_search_id":"collision"'),
            item.replace('"query":"csv"', '"action":{"id":"a","id":"a"}'),
            item.replace('"query":"csv"', '"id":"third"'),
            item.replace('"id":"item_2","type":"web_search"',
                         '"type":"web_search","id":"item_2"'),
        ]
        for value in invalid:
            raw = with_raw_items([('{"type":"item.completed","item":' + value + '}').encode()])
            with self.subTest(item=value), self.assertRaises(ValueError):
                parse_tool_trace("codex_exec", raw)
        for raw_item in (
                '{"type":"item.completed","type":"item.completed","item":' + item + '}',
                '{"type":"item.completed","item":' + item + ',"item":' + item + '}',
                '{"type":"item.completed","item":{"id":"mcp","type":"mcp_tool_call",'
                '"result":{"isError":false,"isError":false}}}',
                '{"type":"item.completed","item":{"id":"a","type":"agent_message",'
                '"text":"same","text":"same"}}'):
            with self.subTest(raw=raw_item), self.assertRaises(ValueError):
                parse_tool_trace("codex_exec", with_raw_items([raw_item.encode()]))

    def test_duplicate_web_id_exception_is_not_available_to_other_paths_or_provider(self):
        item = ('{"id":"item_2","type":"web_search",'
                '"id":"exec-15e62396-7e44-429e-8333-014ec3701aa8"}')
        for raw in (
                ('{"type":"item.completed","item":{"id":"a","type":"web_search",'
                 '"action":' + item + '}}').encode(),
                ('{"type":"future.item","item":' + item + '}').encode()):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_tool_trace("codex_exec", with_raw_items([raw]))
        agy = encode(agy_events())
        nested = ('{"event":"step_update","step_update":{"step_type":"tool",'
                  '"item":' + item + '}}\n').encode()
        with self.assertRaises(ValueError):
            parse_tool_trace("antigravity_exec", agy.split(b"\n", 1)[0] + b"\n" + nested + agy.split(b"\n", 1)[1])

    def test_pending_codex_tool_cannot_be_hidden_by_terminal_event(self):
        for tool in ("command_execution", "file_change", "web_search", "mcp_tool_call"):
            for status in ("turn.completed", "turn.failed"):
                events = codex_events(status=status)
                events.insert(2, {"type": "item.started", "item": {"id": "pending", "type": tool}})
                with self.subTest(tool=tool, status=status), self.assertRaisesRegex(ValueError, "unfinished"):
                    parse_tool_trace("codex_exec", encode(events))
        events = codex_events()
        events.insert(2, {"type": "item.updated", "item": {"id": "pending", "type": "web_search"}})
        with self.assertRaisesRegex(ValueError, "unfinished"):
            parse_tool_trace("codex_exec", encode(events))

    def test_pending_antigravity_tool_cannot_be_hidden_by_terminal_event(self):
        for status in ("SUCCESS", "ERROR", "CANCELED"):
            events = agy_events([agy_step(2, "call_mcp_tool", state="ACTIVE")], status=status)
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "unfinished"):
                parse_tool_trace("antigravity_exec", encode(events))
        events = agy_events([agy_step(2, "search_web", state="ACTIVE"), agy_step(3, "search_web")])
        with self.assertRaisesRegex(ValueError, "unfinished"):
            parse_tool_trace("antigravity_exec", encode(events))

    def test_tool_identity_cannot_change_between_start_and_completion(self):
        events = codex_events([{"id": "same", "type": "web_search"}])
        events.insert(2, {"type": "item.started", "item": {"id": "same", "type": "command_execution"}})
        with self.assertRaisesRegex(ValueError, "identity changed"):
            parse_tool_trace("codex_exec", encode(events))
        events = agy_events([agy_step(2, "search_web", state="ACTIVE"), agy_step(2, "run_command")])
        with self.assertRaisesRegex(ValueError, "identity changed"):
            parse_tool_trace("antigravity_exec", encode(events))

    def test_model_activity_is_independent_of_structured_response_validity(self):
        codex = codex_events()
        codex[-2]["item"]["text"] = "Real model text, but no response envelope."
        codex[-1]["usage"]["output_tokens"] = 0
        agy = agy_events()
        agy[-1]["result"].pop("structured_output")
        for provider, events in (("codex_exec", codex), ("antigravity_exec", agy)):
            trace = parse_tool_trace(provider, encode(events))
            with self.subTest(provider=provider):
                self.assertTrue(trace.model_activity_observed)
                self.assertIsNone(trace.response)
        agy = agy_events([{"event": "step_update", "step_update": {
            "step_type": "agent_response", "state": "DONE", "text_delta": "Model commentary"}}])
        agy[-1]["result"]["usage"] = {}
        self.assertTrue(parse_tool_trace("antigravity_exec", encode(agy)).model_activity_observed)
        no_activity = [{"type": "thread.started"}, {"type": "turn.started"}, {"type": "turn.failed"}]
        self.assertFalse(parse_tool_trace("codex_exec", encode(no_activity)).model_activity_observed)

    def test_event_normalization_retains_unicode_depth_and_event_count_limits(self):
        for provider in ("codex_exec", "antigravity_exec"):
            invalid = [b'{"extra":"\\ud800"}\n',
                       b'{"extra":' + b'[' * 129 + b'0' + b']' * 129 + b'}\n',
                       b'{"extra":1}\n' * 10_001]
            for raw in invalid:
                with self.subTest(provider=provider, prefix=raw[:40]), self.assertRaises(ValueError):
                    parse_tool_trace(provider, raw)

    def test_denied_action_categories_are_sanitized_and_malformed_evidence_rejected(self):
        events = agy_events()
        events[-1]["result"]["denied_actions"] = [
            {"action": "command", "display_name": "RunCommand"},
            {"action": "mcp"}, {"action": "read_url"},
            {"action": "command"}, {"action": "private-future-action"}]
        trace = parse_tool_trace("antigravity_exec", encode(events))
        self.assertEqual(trace.denied_actions, ("command", "mcp", "read_url", "unknown"))
        self.assertFalse(trace.successful_terminal)
        for value in (None, {}, "command", ["command"], [{}], [{"action": 1}], [{"action": ""}]):
            events = agy_events()
            events[-1]["result"]["denied_actions"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_tool_trace("antigravity_exec", encode(events))


if __name__ == "__main__":
    unittest.main()
