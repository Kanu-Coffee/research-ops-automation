"""Synthetic MCP response and Agy file provenance checks; no servers/models."""

import json
from pathlib import Path
import tempfile
import unittest

from researchops.runners.mcp_audit import (
    mcp_protocol_error, safe_tool_timing, summarize_mcp_tools, validate_agy_mcp_reads,
)
from researchops.runners.tool_events import ToolTrace, parse_tool_trace


CONVERSATION = "11111111-1111-4111-8111-111111111111"
OTHER_CONVERSATION = "22222222-2222-4222-8222-222222222222"


def mcp_call(provider="antigravity_exec", *, output='{"answer": 42}', index=2,
             server="future-server", tool="fetch_v2"):
    if provider == "codex_exec":
        details = {"type": "mcp_tool_call", "status": "completed", "server": server,
                   "tool": tool, "result": output}
        name = "mcp_tool_call"
    else:
        details = {"name": "call_mcp_tool", "parameters": {
            "ServerName": server, "ToolName": tool, "Arguments": {"token": "private-argument"}}}
        if output is not None:
            details["output"] = output
        name = "call_mcp_tool"
    return {"name": name, "success": True, "provider_reported_success": True,
            "state": "DONE", "details": details, "step_index": index}


def read(path, *, index=3, name="view_file", success=True):
    field = {"view_file": "AbsolutePath", "grep_search": "SearchPath", "write_to_file": "TargetFile",
             "list_dir": "DirectoryPath", "view_file_outline": "AbsolutePath"}[name]
    return {"name": name, "success": success, "state": "DONE" if success else "ERROR",
            "step_index": index, "details": {"name": name, "parameters": {field: str(path)},
                                               "output": "synthetic observed file body"}}


def trace(tools, *, conversation=CONVERSATION, denied_actions=()):
    return ToolTrace(True, True, {}, tuple(tools), bool(denied_actions), {}, 5,
                     conversation_id=conversation, cwd="/synthetic/workspace", denied_actions=denied_actions)


class MCPAuditTests(unittest.TestCase):
    def test_timing_projection_preserves_absence_and_nullable_observations(self):
        self.assertEqual(safe_tool_timing({}), {})
        self.assertEqual(safe_tool_timing(None), {})
        call = mcp_call()
        call.update(observed_started_at=None, observed_finished_at="2026-09-10T08:00:02.123456Z", duration_ms=None)
        summary = summarize_mcp_tools("antigravity_exec", [call])[0]
        self.assertIsNone(summary["observed_started_at"])
        self.assertEqual(summary["observed_finished_at"], call["observed_finished_at"])
        self.assertIsNone(summary["duration_ms"])
        call.update(observed_started_at="2026-09-10T08:00:00+00:00", duration_ms=2123)
        self.assertEqual(safe_tool_timing(call)["duration_ms"], 2123)

    def test_malformed_timing_cannot_expose_payloads_or_create_duration(self):
        for stamp in ("private-token", "2026-09-10T08:00:00+09:00", "2026-02-31T00:00:00Z",
                      "2026-09-10T08:00:00Z\nprivate", "x" * 1000, None, 42, {}):
            with self.subTest(stamp=stamp):
                call = mcp_call()
                call.update(observed_started_at=stamp, observed_finished_at=stamp, duration_ms=100)
                timing = safe_tool_timing(call)
                self.assertEqual(timing, {"observed_started_at": None, "observed_finished_at": None, "duration_ms": None})
                self.assertNotIn("private", json.dumps(summarize_mcp_tools("antigravity_exec", [call])))
        for duration in (-1, True, "100", 1.5, 2 ** 63, {"private": "payload"}):
            self.assertIsNone(safe_tool_timing({"observed_started_at": "2026-09-10T08:00:00Z",
                "observed_finished_at": "2026-09-10T08:00:01Z", "duration_ms": duration})["duration_ms"])

    def test_future_server_and_tool_have_payload_free_bounded_summary(self):
        for provider, output in (("antigravity_exec", '{"secret":"private-response"}'),
                                 ("codex_exec", {"content": [{"type": "text", "text": "private-response"}]})):
            call = mcp_call(provider, output=output, tool="future.fetch-v2")
            summary = summarize_mcp_tools(provider, [call])[0]
            self.assertEqual(summary, {"server": "future-server", "tool": "future.fetch-v2",
                "status": "succeeded", "error_code": None, "success": True,
                "provider_reported_success": True, "output_verified": True})
            encoded = json.dumps(summary)
            self.assertNotIn("private-", encoded)
            self.assertNotIn("Arguments", encoded)

    def test_invalid_or_sensitive_looking_identity_is_redacted(self):
        for identity in (None, [], "https://private.example/token", "person@example.test", "../private",
                         "private\ncredential", "x" * 129):
            summary = summarize_mcp_tools("antigravity_exec", [mcp_call(server=identity, tool=identity)])[0]
            self.assertEqual((summary["server"], summary["tool"]), ("[redacted]", "[redacted]"))

    def test_done_without_output_is_unverified_not_reclassified_terminal_failure(self):
        call = mcp_call(output=None)
        summary = summarize_mcp_tools("antigravity_exec", [call])[0]
        self.assertEqual(summary["status"], "unverified")
        self.assertEqual(summary["error_code"], "MCP_OUTPUT_UNVERIFIED")
        self.assertTrue(summary["provider_reported_success"])
        self.assertFalse(summary["success"])
        self.assertTrue(call["success"])
        for output in (None, {}, {"isError": False}):
            summary = summarize_mcp_tools("codex_exec", [mcp_call("codex_exec", output=output)])[0]
            self.assertEqual(summary["status"], "unverified")

    def test_error_denial_and_missing_evidence_have_fixed_codes(self):
        failed, denied, malformed = [mcp_call() for _ in range(3)]
        failed["details"]["error"] = {"message": "private-credential https://private.example"}
        denied.update(success=False, denied=True)
        malformed["details"] = None
        summaries = summarize_mcp_tools("antigravity_exec", [failed, denied, malformed])
        self.assertEqual([item["status"] for item in summaries], ["failed", "denied", "unverified"])
        self.assertEqual([item["error_code"] for item in summaries],
                         ["MCP_TOOL_ERROR", "MCP_PERMISSION_DENIED", "MCP_EVIDENCE_INVALID"])
        self.assertNotIn("private", json.dumps(summaries))

    def test_malformed_provider_state_is_bounded_invalid_evidence(self):
        for provider in ("codex_exec", "antigravity_exec"):
            for value in ({"private": "state"}, ["private-state"], True, 12):
                call = mcp_call(provider, output={"content": []})
                if provider == "codex_exec":
                    call["details"]["status"] = value
                else:
                    call["state"] = value
                summary = summarize_mcp_tools(provider, [call])[0]
                self.assertEqual(summary["status"], "unverified")
                self.assertEqual(summary["error_code"], "MCP_EVIDENCE_INVALID")
                self.assertNotIn("private", json.dumps(summary))

    def test_codex_approval_never_denial_has_distinct_safe_code(self):
        message = "MCP tool call requires approval, but approval policy is never"
        call = mcp_call("codex_exec", output=None)
        call.update(success=False, provider_reported_success=False)
        call["details"].update(status="failed", error={"message": message})
        summary = summarize_mcp_tools("codex_exec", [call])[0]
        self.assertEqual(summary["status"], "denied")
        self.assertEqual(summary["error_code"], "MCP_PERMISSION_DENIED")
        self.assertFalse(summary["success"])
        self.assertNotIn(message, json.dumps(summary))
        call["details"]["error"]["message"] = message + " private-extra"
        summary = summarize_mcp_tools("codex_exec", [call])[0]
        self.assertEqual(summary["status"], "failed")
        self.assertNotIn("private-extra", json.dumps(summary))
        call = mcp_call("codex_exec", output={"content": [{"type": "text", "text": message}]})
        self.assertEqual(summarize_mcp_tools("codex_exec", [call])[0]["status"], "succeeded")

    def test_protocol_error_is_not_a_nested_or_business_iserror_field(self):
        protocol = {"content": [{"type": "text", "text": "private failure"}], "isError": True}
        for output in (protocol, json.dumps(protocol)):
            call = mcp_call(output=output)
            self.assertTrue(mcp_protocol_error("antigravity_exec", call["details"]))
            self.assertEqual(summarize_mcp_tools("antigravity_exec", [call])[0]["error_code"], "MCP_PROTOCOL_ERROR")
        for business in ({"isError": True, "answer": 42}, {"isError": True},
                         {"content": "business prose", "isError": True},
                         {"content": [], "isError": True, "business_status": "expected"},
                         {"content": [{"type": "text", "text": '{"isError":true}'}]},
                         {"content": [], "structuredContent": {"isError": True}},
                         {"content": [{"type": {"private": "kind"}}], "isError": True},
                         {"content": [], "isError": "true"}):
            for output in (business, json.dumps(business)):
                call = mcp_call(output=output)
                self.assertFalse(mcp_protocol_error("antigravity_exec", call["details"]))
                self.assertTrue(summarize_mcp_tools("antigravity_exec", [call])[0]["success"])

    def test_agy_parser_preserves_recoverable_protocol_failure_and_provider_state(self):
        info = mcp_call(output={"content": [], "isError": True})["details"]
        events = [{"event": "init", "conversation_id": CONVERSATION, "init": {}},
                  {"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE",
                   "step_index": 2, "tool_name": "call_mcp_tool", "tool_info": info}},
                  {"event": "result", "result": {"status": "SUCCESS", "structured_output": {
                   "response_json": '{"records":[],"warnings":["partial source unavailable"]}'}}}]
        parsed = parse_tool_trace("antigravity_exec", "\n".join(json.dumps(e) for e in events).encode())
        self.assertTrue(parsed.successful_terminal)
        self.assertIsNotNone(parsed.response)
        self.assertTrue(parsed.tools[0]["provider_reported_success"])
        self.assertFalse(parsed.tools[0]["success"])
        self.assertEqual(parsed.tools[0]["state"], "DONE")
        self.assertEqual(summarize_mcp_tools("antigravity_exec", parsed.tools)[0]["error_code"], "MCP_PROTOCOL_ERROR")


class AgyMCPReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="researchops-mcp-audit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.agy = self.home / ".gemini/antigravity-cli"
        self.schema = self.agy / "mcp/future-server/fetch_v2.json"
        self.spill = self.agy / "brain" / CONVERSATION / ".system_generated/steps/2/output.txt"
        for path in (self.schema, self.spill):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic generated data")

    def validate(self, tools, *, conversation=CONVERSATION, active=("future-server",), denied_actions=()):
        parsed = trace(tools, conversation=conversation, denied_actions=denied_actions)
        validate_agy_mcp_reads(parsed, active, home_dir=self.home)
        return parsed

    def test_active_server_schema_and_prior_done_spill_prove_missing_output(self):
        call = mcp_call(output=None)
        self.validate([read(self.schema, index=1), call, read(self.spill)])
        self.assertTrue(call["mcp_output_verified"])
        self.assertTrue(call["provider_reported_success"])
        self.assertEqual(summarize_mcp_tools("antigravity_exec", [call])[0]["status"], "succeeded")

    def test_unknown_tools_are_allowed_only_with_active_server_schema(self):
        unknown = self.schema.with_name("future-tool.2027.json")
        unknown.write_text("synthetic schema")
        self.validate([read(unknown)])
        for path, active in ((self.schema, ()), (self.schema.parent, ("future-server",)),
                             (self.schema.with_name("credentials.txt"), ("future-server",))):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate([read(path)], active=active)

    def test_exact_native_server_instructions_file_shares_schema_provenance(self):
        instructions = self.schema.with_name("instructions.md")
        instructions.write_text("Synthetic MCP server instructions returned at initialization.")
        self.validate([read(instructions)])
        for path, active in ((instructions, ()), (self.schema.with_name("README.md"), ("future-server",)),
                             (self.schema.with_name("other-instructions.md"), ("future-server",))):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate([read(path)], active=active)
        instructions.unlink()
        instructions.symlink_to(self.spill)
        with self.assertRaisesRegex(ValueError, "^AGY_MCP_GENERATED_FILE_UNSAFE$"):
            self.validate([read(instructions)])

    def test_spill_requires_current_conversation_prior_step_and_observed_call(self):
        other = Path(str(self.spill).replace(CONVERSATION, OTHER_CONVERSATION))
        variants = [([read(self.spill)], CONVERSATION), ([read(self.spill), mcp_call()], CONVERSATION),
                    ([mcp_call(), read(self.spill, index=2)], CONVERSATION),
                    ([mcp_call(), read(self.spill)], None), ([mcp_call(), read(other)], CONVERSATION),
                    ([mcp_call(), read(self.spill.with_name("private.txt"))], CONVERSATION),
                    ([mcp_call(), read(self.spill.parent.parent / "4/output.txt")], CONVERSATION)]
        for tools, conversation in variants:
            with self.subTest(conversation=conversation), self.assertRaises(ValueError):
                self.validate(tools, conversation=conversation)

    def test_failed_denied_unknown_server_and_protocol_error_cannot_grant_spill(self):
        failed, denied, error, protocol, unsafe = [mcp_call() for _ in range(5)]
        failed.update(success=False, state="ERROR")
        denied.update(success=False, denied=True)
        error["details"]["error"] = {"message": "private"}
        protocol["details"]["output"] = {"content": [], "isError": True}
        unsafe["details"]["parameters"]["ServerName"] = []
        for call in (failed, denied, error, protocol, unsafe, mcp_call(server="unknown")):
            with self.subTest(call=call), self.assertRaises(ValueError):
                self.validate([call, read(self.spill)])
        with self.assertRaises(ValueError):
            self.validate([mcp_call(), read(self.spill)], denied_actions=("mcp",))

    def test_failed_view_file_does_not_promote_output_evidence(self):
        call = mcp_call(output=None)
        self.validate([call, read(self.spill, success=False)])
        self.assertNotIn("mcp_output_verified", call)

    def test_exact_generated_search_shares_file_provenance_without_promoting_mcp_output(self):
        call = mcp_call(output=None)
        self.validate([read(self.schema, index=1, name="grep_search"), call,
                       read(self.spill, name="grep_search")])
        self.assertNotIn("mcp_output_verified", call)
        self.assertEqual(summarize_mcp_tools("antigravity_exec", [call])[0]["error_code"], "MCP_OUTPUT_UNVERIFIED")
        self.validate([call, read(self.spill, name="grep_search"), read(self.spill, index=4)])
        self.assertTrue(call["mcp_output_verified"])

    def test_native_web_file_can_be_viewed_searched_and_viewed_again(self):
        # Incident sequence: successful read_url_content at 88, view at 90,
        # exact-file grep at 92, and another view at 94.
        web = {"name": "read_url_content", "success": True, "state": "DONE", "step_index": 88,
               "details": {"parameters": {"Url": "https://example.test/public"}, "output": "provider result"}}
        content = self.spill.parent.parent / "88/content.md"
        content.parent.mkdir()
        content.write_text("Synthetic product 09137")
        self.validate([web, read(content, index=90), read(content, name="grep_search", index=92),
                       read(content, index=94)])
        self.assertNotIn("mcp_output_verified", web)

    def test_generated_search_cannot_scan_directories_or_other_conversations(self):
        other = Path(str(self.spill).replace(CONVERSATION, OTHER_CONVERSATION))
        for path in (self.schema.parent, self.spill.parent, self.agy, other,
                     self.agy / "auth.json", self.spill.with_name("private.txt")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate([mcp_call(), read(path, name="grep_search")])
        with self.assertRaises(ValueError):
            self.validate([read(self.spill, name="grep_search"), mcp_call()])
        with self.assertRaises(ValueError):
            self.validate([mcp_call(), read(self.spill, index=2, name="grep_search")])
        with self.assertRaises(ValueError):
            self.validate([read(self.schema, name="grep_search")], active=())

    def test_generated_search_rejects_failed_producer_and_unsafe_files(self):
        failed = mcp_call()
        failed.update(success=False, state="ERROR")
        with self.assertRaises(ValueError):
            self.validate([failed, read(self.spill, name="grep_search")])
        for path in (str(self.schema.parent / "../future-server/fetch_v2.json"),
                     str(self.schema).replace("/mcp/", "/mcp//")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate([read(path, name="grep_search")])
        self.schema.unlink()
        for mode in ("symlink", "hardlink", "empty", "directory"):
            if mode == "symlink":
                self.schema.symlink_to(self.spill)
            elif mode == "hardlink":
                self.schema.hardlink_to(self.spill)
            elif mode == "empty":
                self.schema.write_bytes(b"")
            else:
                self.schema.mkdir()
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "^AGY_MCP_GENERATED_FILE_UNSAFE$"):
                self.validate([read(self.schema, name="grep_search")])
            self.schema.rmdir() if self.schema.is_dir() else self.schema.unlink()

    def test_generated_writes_directory_scans_and_arbitrary_brain_reads_are_denied(self):
        for target in (read(self.spill, name="write_to_file"),
                       read(self.spill, name="view_file_outline"),
                       read(self.schema.parent, name="list_dir"),
                       read(self.agy / "brain" / CONVERSATION / "private.md"),
                       read(self.agy / "auth.json")):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.validate([mcp_call(), target])

    def test_traversal_alias_symlink_and_empty_or_hardlinked_files_are_rejected(self):
        for path in (str(self.schema.parent / "../future-server/fetch_v2.json"),
                     str(self.schema).replace("/mcp/", "/mcp//"),
                     str(self.agy / "../../../.codex/auth.json")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate([read(path)])
        self.schema.unlink()
        self.schema.symlink_to(self.spill)
        with self.assertRaisesRegex(ValueError, "^AGY_MCP_GENERATED_FILE_UNSAFE$"):
            self.validate([read(self.schema)])
        self.schema.unlink()
        self.schema.write_text("")
        with self.assertRaises(ValueError):
            self.validate([read(self.schema)])
        self.schema.unlink()
        self.schema.hardlink_to(self.spill)
        with self.assertRaises(ValueError):
            self.validate([read(self.schema)])

    def test_external_symlink_alias_cannot_bypass_generated_provenance(self):
        alias = self.root / "alias"
        alias.symlink_to(self.schema)
        with self.assertRaises(ValueError):
            self.validate([read(alias)])

    def test_symlinked_generated_directory_is_rejected(self):
        actual = self.agy / "actual-metadata"
        self.schema.parent.rename(actual)
        self.schema.parent.symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "^AGY_MCP_GENERATED_FILE_UNSAFE$"):
            self.validate([read(self.schema)])

    def test_ordinary_workspace_files_are_not_read_or_required_to_exist(self):
        self.validate([read(self.root / "workspace/not-yet-existing.txt")])

    def test_prior_native_web_content_read_preserves_existing_capability(self):
        web = {"name": "read_url_content", "success": True, "state": "DONE", "step_index": 2,
               "details": {"parameters": {"Url": "https://example.test/public"}, "output": "provider result"}}
        content = self.spill.with_name("content.md")
        content.write_text("synthetic public page")
        self.validate([web, read(content)])
        self.assertNotIn("mcp_output_verified", web)
        with self.assertRaises(ValueError):
            self.validate([web, read(self.spill)])


if __name__ == "__main__":
    unittest.main()
