"""Protocol and safe evidence regressions; subprocess test makes no model call."""

import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from researchops.runners import research_mcp
from researchops.runners.research_mcp import ResearchMCPServer


def request(method, params=None, request_id=1):
    value = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        value["params"] = params
    return value


def initialize(version="2025-06-18"):
    return request("initialize", {"protocolVersion": version, "capabilities": {},
                                 "clientInfo": {"name": "test", "version": "1.0"}})


def notification(method="notifications/initialized"):
    return {"jsonrpc": "2.0", "method": method}


def call(name, arguments, request_id=2):
    return request("tools/call", {"name": name, "arguments": arguments}, request_id)


def wire(*messages):
    return b"".join((json.dumps(message, ensure_ascii=False) + "\n").encode()
                    for message in messages)


def payload(response):
    return json.loads(response["result"]["content"][0]["text"])


class TestResearchMCP(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="researchops-mcp-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.audit = self.root / "audit.jsonl"
        self.server = ResearchMCPServer(self.audit, nonce="test-evidence-marker")
        self.addCleanup(self.server.close)

    def ready(self, version="2025-06-18"):
        response = self.server.handle(initialize(version))
        self.assertIsNone(self.server.handle(notification()))
        return response

    def audit_records(self, *, include_protocol=False):
        rows = [json.loads(line) for line in self.audit.read_text().splitlines()]
        return rows if include_protocol else [row for row in rows if row["event"] == "tool_call"]

    def test_initialize_negotiates_requested_versions_and_latest_fallback(self):
        for index, version in enumerate((*research_mcp.SUPPORTED_PROTOCOL_VERSIONS,
                                         "2099-01-01")):
            with self.subTest(version=version), ResearchMCPServer(
                    self.root / f"version-{index}.jsonl") as server:
                response = server.handle(initialize(version))
                expected = version if version in research_mcp.SUPPORTED_PROTOCOL_VERSIONS else "2025-06-18"
                self.assertEqual(response["result"]["protocolVersion"], expected)
                self.assertEqual(response["result"]["capabilities"], {"tools": {"listChanged": False}})
                self.assertNotIn(server.evidence_marker, json.dumps(response))
                self.assertEqual(server.handle(initialize(version))["error"]["code"], -32600)

    def test_lifecycle_requires_initialize_and_notification(self):
        for response in (self.server.handle(request("tools/list")),
                         self.server.handle(call("search_documents", {"query": "numeric"}))):
            self.assertEqual(response["error"]["code"], -32000)
        self.assertIsNone(self.server.handle(notification()))
        self.assertFalse(self.server.initialized)
        self.server.handle(initialize())
        self.assertIn("error", self.server.handle(request("tools/list")))
        self.server.handle(notification())
        self.assertIn("result", self.server.handle(request("tools/list")))
        self.assertEqual(self.audit_records(), [])

    def test_tools_are_read_only_closed_world_and_do_not_disclose_marker(self):
        self.ready()
        response = self.server.handle(request("tools/list"))
        tools = response["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["search_documents", "fetch_document"])
        for tool in tools:
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["destructiveHint"])
            self.assertFalse(tool["annotations"]["openWorldHint"])
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        self.assertNotIn(self.server.evidence_marker, json.dumps(response))

    def test_discovery_is_audited_but_not_mistaken_for_tool_execution(self):
        message = initialize()
        message["params"]["clientInfo"]["name"] = "private-client-metadata"
        self.server.handle(message)
        self.server.handle(notification())
        self.server.handle(request("tools/list"))
        rows = self.audit_records(include_protocol=True)
        self.assertEqual([row["event"] for row in rows], ["protocol", "protocol"])
        self.assertEqual([row["method"] for row in rows], ["initialize", "tools/list"])
        self.assertEqual(rows[1]["tools"], ["search_documents", "fetch_document"])
        self.assertNotIn("private-client-metadata", self.audit.read_text())
        self.assertEqual(self.audit_records(), [])
        with patch.object(research_mcp, "MAX_MESSAGES", 2), self.assertRaises(OSError):
            self.server.handle(request("tools/list"))

    def test_search_fetch_chain_has_matching_durable_receipts_and_source_facts(self):
        self.ready()
        search = payload(self.server.handle(call("search_documents", {"query": "numeric revenue"})))
        self.assertEqual([doc["document_id"] for doc in search["documents"]], ["numeric-ledger"])
        response = self.server.handle(call("fetch_document", {"document_id": "numeric-ledger"}, 3))
        fetched = payload(response)
        self.assertEqual(fetched, response["result"]["structuredContent"])
        self.assertEqual(fetched["document"]["facts"]["current"], 150)
        self.assertEqual(fetched["document"]["facts"]["percent_change"], 20)
        self.assertEqual(search["evidence_marker"], fetched["evidence_marker"])
        self.assertNotEqual(search["receipt_id"], fetched["receipt_id"])
        self.assertEqual(fetched["source_kind"], "synthetic-local")
        audit = self.audit_records()
        self.assertEqual([row["tool"] for row in audit], ["search_documents", "fetch_document"])
        self.assertEqual([row["receipt_id"] for row in audit], [search["receipt_id"], fetched["receipt_id"]])
        self.assertEqual(audit[1]["documents"], [fetched["document"]])

    def test_korean_and_partial_sources_preserve_unicode_unknown_values_and_warnings(self):
        self.ready()
        search = payload(self.server.handle(call("search_documents", {"query": "정책"})))
        self.assertEqual(search["documents"][0]["document_id"], "korean-policy")
        korean = payload(self.server.handle(call("fetch_document", {"document_id": "korean-policy"})))
        self.assertEqual(korean["document"]["facts"]["timezone"], "Asia/Seoul")
        partial = payload(self.server.handle(call("fetch_document", {"document_id": "partial-survey"})))
        self.assertIsNone(partial["document"]["facts"]["sample_size"])
        self.assertEqual(partial["document"]["facts"]["coverage_status"], "partial")
        self.assertTrue(partial["document"]["facts"]["warnings"])

    def test_older_protocol_uses_json_text_without_newer_structured_content(self):
        self.ready("2024-11-05")
        response = self.server.handle(call("search_documents", {"query": "survey"}))
        self.assertNotIn("structuredContent", response["result"])
        self.assertEqual(payload(response)["documents"][0]["document_id"], "partial-survey")

    def test_audit_excludes_arbitrary_arguments_and_client_supplied_request_id(self):
        self.ready()
        marker = "private_query_not_present_in_any_source_781924"
        response = self.server.handle(call("search_documents", {"query": marker}, marker))
        self.assertEqual(payload(response)["documents"], [])
        self.assertNotIn(marker, self.audit.read_text())
        self.assertEqual(stat.S_IMODE(self.audit.stat().st_mode), 0o600)

    def test_unknown_tools_methods_documents_and_invalid_arguments_are_distinct(self):
        self.ready()
        invalid = [call("read_file", {"path": "private-fixture"}),
                   call("fetch_document", {"document_id": []}),
                   call("search_documents", {"query": ""}),
                   call("search_documents", {"query": " "}),
                   call("search_documents", {"query": "x" * 513}),
                   call("search_documents", {"query": "numeric", "path": "private-fixture"}),
                   call("search_documents", []), call(None, {}), call({}, {})]
        for message in invalid:
            with self.subTest(message=message):
                response = self.server.handle(message)
                self.assertEqual(response["error"]["code"], -32602)
                self.assertNotIn("private-fixture", json.dumps(response))
        self.assertEqual(self.server.handle(request("files/read"))["error"]["code"], -32601)
        unknown = self.server.handle(call("fetch_document", {"document_id": "../../private-fixture"}))
        self.assertTrue(unknown["result"]["isError"])
        self.assertNotIn("private-fixture", json.dumps(unknown))
        self.assertEqual(self.audit_records(), [])

    def test_tool_notifications_never_execute_or_reply(self):
        self.ready()
        message = call("search_documents", {"query": "numeric"})
        del message["id"]
        self.assertIsNone(self.server.handle(message))
        self.assertIsNone(self.server.handle(notification("notifications/cancelled")))
        self.assertEqual(self.audit_records(), [])

    def test_invalid_envelope_and_initialize_parameters_are_rejected(self):
        for message in ([], None, True, {}, {"jsonrpc": "1.0"},
                        request("ping", request_id=True), request("ping", request_id=None),
                        request("ping", request_id=[]), request("ping", request_id="x" * 129),
                        request("ping", params=[]), request("initialize", {})):
            with self.subTest(message=message):
                self.assertIn("error", self.server.handle(message))
        self.assertEqual(self.server.handle(request("ping"))["result"], {})

    def test_stream_rejects_duplicate_keys_nonfinite_and_invalid_utf8_without_echo(self):
        source = io.BytesIO(b'{"jsonrpc":"2.0","jsonrpc":"private-fixture"}\n'
                            b'{"x":NaN}\n' b'{"x":1e999}\n' b'\xff\n')
        output = io.BytesIO()
        self.assertEqual(self.server.serve(source, output), 0)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([item["error"]["code"] for item in responses], [-32700] * 4)
        self.assertNotIn(b"private-fixture", output.getvalue())

    def test_oversized_and_truncated_frames_stop_session_without_tool_use(self):
        for raw in (b"x" * (research_mcp.MAX_REQUEST_BYTES + 1), b'{"jsonrpc":"2.0"}'):
            with self.subTest(size=len(raw)):
                output = io.BytesIO()
                self.assertEqual(self.server.serve(io.BytesIO(raw), output), 2)
                self.assertEqual(json.loads(output.getvalue())["error"]["code"], -32002)
        self.assertEqual(self.audit_records(), [])

    def test_session_message_input_output_and_tool_bounds_are_enforced(self):
        self.ready()
        with patch.object(research_mcp, "MAX_TOOL_CALLS", 1):
            self.assertIn("result", self.server.handle(call("search_documents", {"query": "numeric"})))
            self.assertEqual(self.server.handle(call("search_documents", {"query": "numeric"}))["error"]["code"], -32002)
        for limit_name, limit in (("MAX_MESSAGES", 1), ("MAX_SESSION_INPUT_BYTES", 1),
                                  ("MAX_SESSION_OUTPUT_BYTES", 1), ("MAX_RESPONSE_BYTES", 1)):
            with self.subTest(limit=limit_name), patch.object(research_mcp, limit_name, limit):
                self.assertEqual(self.server.serve(io.BytesIO(wire(request("ping"), request("ping"))),
                                                   io.BytesIO()), 2)

    def test_audit_must_be_new_private_regular_file_and_no_symlink_is_followed(self):
        with self.assertRaises(FileExistsError):
            ResearchMCPServer(self.audit)
        link = self.root / "audit-link"
        link.symlink_to(self.audit)
        with self.assertRaises(OSError):
            ResearchMCPServer(link)
        public = self.root / "public"
        public.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            ResearchMCPServer(public / "audit.jsonl")
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            ResearchMCPServer(linked_parent / "new.jsonl")

    def test_missing_audit_aborts_before_success_and_broken_output_exits(self):
        self.ready()
        self.server.close()
        output = io.BytesIO()
        self.assertEqual(self.server.serve(io.BytesIO(wire(call("search_documents", {"query": "numeric"}))), output), 2)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], -32603)
        self.assertEqual(json.loads(output.getvalue())["id"], 2)
        self.assertEqual(self.audit_records(), [])
        broken = unittest.mock.Mock()
        broken.write.side_effect = BrokenPipeError()
        self.assertEqual(self.server.serve(io.BytesIO(wire(request("ping"))), broken), 1)

    def test_process_marker_is_generated_fresh_and_nonce_is_bounded(self):
        with ResearchMCPServer(self.root / "fresh-1.jsonl") as one, ResearchMCPServer(
                self.root / "fresh-2.jsonl") as two:
            self.assertRegex(one.evidence_marker, r"^[0-9a-f]{32}$")
            self.assertNotEqual(one.evidence_marker, two.evidence_marker)
        for nonce in ("short", "x" * 65, "invalid/marker", 42):
            with self.subTest(nonce=nonce), self.assertRaises(ValueError):
                ResearchMCPServer(self.root / "bad-nonce.jsonl", nonce=nonce)

    def test_actual_stdio_subprocess_initializes_searches_fetches_and_stops_at_eof(self):
        audit = self.root / "subprocess.jsonl"
        repo = Path(__file__).resolve().parents[1]
        command = [sys.executable, "-m", "researchops.runners.research_mcp", "--audit-path", str(audit)]
        result = subprocess.run(command, input=wire(
            initialize(), notification(), request("tools/list", request_id=2),
            call("search_documents", {"query": "numeric"}, 3),
            call("fetch_document", {"document_id": "numeric-ledger"}, 4)),
            cwd=self.root, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(repo),
                                "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, timeout=5, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stderr, b"")
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2, 3, 4])
        audit_rows = [json.loads(line) for line in audit.read_text().splitlines()]
        rows = [row for row in audit_rows if row["event"] == "tool_call"]
        self.assertEqual([row["tool"] for row in rows], ["search_documents", "fetch_document"])
        self.assertEqual(payload(responses[-1])["receipt_id"], rows[-1]["receipt_id"])


if __name__ == "__main__":
    unittest.main()
