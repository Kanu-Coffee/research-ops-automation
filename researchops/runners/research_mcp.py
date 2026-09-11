"""Private, read-only synthetic corpus MCP server for live runner verification.

This is a real stdio MCP connection, not an Internet search service. The server
does not read environment/configuration/credentials or caller-selected files.
Only its new private audit file is written. A fresh process marker returned by
tools and durable receipts let the verifier distinguish actual tool use from a
model merely claiming it searched. The marker is evidence, not authentication.

Protocol references: modelcontextprotocol.io/specification/2025-03-26/basic/
{lifecycle,transports} and /specification/2025-06-18/server/tools.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from typing import BinaryIO

from researchops.strict_json import strict_json_loads


SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 131_072
MAX_SESSION_INPUT_BYTES = 1_048_576
MAX_SESSION_OUTPUT_BYTES = 2_097_152
MAX_MESSAGES = 256
MAX_TOOL_CALLS = 64

_DOCUMENTS = (
    {
        "document_id": "numeric-ledger",
        "title": "Synthetic revenue ledger / 합성 매출 장부",
        "summary": "Numeric comparison of baseline and current synthetic revenue.",
        "body": "합성 매출 자료: 기준 매출은 125, 현재 매출은 150이다. "
                "증가액은 25이며 기준 대비 증가율은 20%이다. 실제 기업 자료가 아니다.",
        "facts": {"baseline": 125, "current": 150, "change": 25,
                  "percent_change": 20, "unit": "synthetic_units"},
        "source_uri": "researchops-synthetic://corpus/numeric-ledger",
    },
    {
        "document_id": "korean-policy",
        "title": "한국어 정책 안내 / Korean policy notice",
        "summary": "A synthetic Korean policy with a Seoul date and an opaque audience.",
        "body": "합성 정책 안내: 시행일은 2026-09-06이며 기준 시간대는 Asia/Seoul이다. "
                "대상 그룹은 sample-analysts이다. 실제 수신자 주소는 제공하지 않는다.",
        "facts": {"effective_date": "2026-09-06", "timezone": "Asia/Seoul",
                  "recipient_group_id": "sample-analysts"},
        "source_uri": "researchops-synthetic://corpus/korean-policy",
    },
    {
        "document_id": "partial-survey",
        "title": "Partial survey / 일부 설문 자료",
        "summary": "An incomplete synthetic survey preserving unknown sample size.",
        "body": "합성 설문은 일부 자료만 확보되었다. 응답 비율은 62%지만 표본 수는 "
                "알 수 없다. 전체 시장에 일반화하지 않으며 누락 정보를 추측하지 않는다.",
        "facts": {"coverage_status": "partial", "response_percent": 62,
                  "sample_size": None, "warnings": ["Sample size was not supplied."]},
        "source_uri": "researchops-synthetic://corpus/partial-survey",
    },
)


def _encode(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _error(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def _success(request_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _tool_definition(name: str, argument: str, description: str) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object", "properties": {
                argument: {"type": "string", "minLength": 1, "maxLength": 512}},
            "required": [argument], "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
    }


class ResearchMCPServer:
    """Bounded serial MCP session; the enclosing runner owns wall-clock timeout.

    ``audit_path`` must name a nonexistent file immediately within an existing
    owner-only directory. No existing file, symlink, directory, or hardlink is
    reused. ``nonce`` exists only for deterministic protocol tests; live callers
    should omit it, so the marker cannot already appear in the task prompt.
    """

    def __init__(self, audit_path: str | Path, *, nonce: str | None = None):
        if nonce is not None and (not isinstance(nonce, str) or
                                  re.fullmatch(r"[A-Za-z0-9_-]{8,64}", nonce) is None):
            raise ValueError("Evidence nonce must be 8 to 64 ASCII identifier characters")
        self.evidence_marker = nonce if nonce is not None else secrets.token_hex(16)
        self.protocol_version: str | None = None
        self.initialized = False
        self.tool_calls = 0
        self._audit_events = 0
        self._audit_fd = self._open_audit(Path(audit_path))

    @staticmethod
    def _open_audit(path: Path) -> int:
        if path.name in ("", ".", ".."):
            raise ValueError("Audit file name is invalid")
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parent = os.fstat(parent_fd)
            if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
                raise ValueError("Audit parent must be an owner-only directory")
            return os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                           os.O_APPEND | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)

    def close(self) -> None:
        if self._audit_fd is not None:
            os.close(self._audit_fd)
            self._audit_fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _audit_event(self, event: dict) -> None:
        encoded = _encode(event)
        if (len(encoded) > MAX_RESPONSE_BYTES or self._audit_fd is None or
                self._audit_events >= MAX_MESSAGES):
            raise OSError("Audit unavailable")
        remaining = memoryview(encoded)
        while remaining:
            count = os.write(self._audit_fd, remaining)
            if count <= 0:
                raise OSError("Audit write failed")
            remaining = remaining[count:]
        os.fsync(self._audit_fd)
        self._audit_events += 1

    def _tool_result(self, tool: str, data: dict, documents: list[dict]) -> dict:
        self.tool_calls += 1
        receipt = f"{self.evidence_marker}:{self.tool_calls}"
        payload = dict(data, source_kind="synthetic-local",
                       evidence_marker=self.evidence_marker, receipt_id=receipt)
        # Deliberately exclude arbitrary queries, request IDs and client metadata.
        # Canonical returned documents and server-generated receipts are enough
        # to prove which source material the model actually received.
        self._audit_event({"event": "tool_call", "tool": tool, "receipt_id": receipt,
                           "evidence_marker": self.evidence_marker,
                           "source_kind": "synthetic-local", "documents": documents})
        result = {"content": [{"type": "text", "text": _encode(payload).decode().rstrip("\n")}],
                  "isError": False}
        if self.protocol_version == "2025-06-18":
            result["structuredContent"] = payload
        return result

    def _call_tool(self, request_id: object, params: dict) -> dict:
        if set(params) - {"name", "arguments", "_meta"}:
            return _error(request_id, -32602, "Invalid tool parameters")
        name, arguments = params.get("name"), params.get("arguments")
        if name not in ("search_documents", "fetch_document"):
            return _error(request_id, -32602, "Unknown tool")
        argument = "query" if name == "search_documents" else "document_id"
        if not isinstance(arguments, dict) or set(arguments) != {argument}:
            return _error(request_id, -32602, "Invalid tool arguments")
        value = arguments[argument]
        if not isinstance(value, str) or not 1 <= len(value) <= 512 or not value.strip():
            return _error(request_id, -32602, "Invalid tool argument value")
        if self.tool_calls >= MAX_TOOL_CALLS:
            return _error(request_id, -32002, "Tool call limit exceeded")
        if name == "search_documents":
            terms = re.findall(r"\w+", value.casefold())
            documents = [
                {key: doc[key] for key in ("document_id", "title", "summary")}
                for doc in _DOCUMENTS
                if any(term in " ".join((doc["document_id"], doc["title"],
                                          doc["summary"], doc["body"])).casefold()
                       for term in terms)
            ]
            result = self._tool_result(name, {"documents": documents}, documents)
        else:
            doc = next((doc for doc in _DOCUMENTS if doc["document_id"] == value), None)
            if doc is None:
                return _success(request_id, {"content": [{"type": "text",
                                "text": "Document not found in the synthetic corpus."}],
                                "isError": True})
            document = copy.deepcopy(doc)
            result = self._tool_result(name, {"document": document}, [document])
        return _success(request_id, result)

    def handle(self, message: object) -> dict | None:
        """Validate one decoded message without echoing untrusted error values."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid JSON-RPC request")
        has_id = "id" in message
        request_id = message.get("id")
        if has_id and (type(request_id) not in (str, int) or
                       isinstance(request_id, str) and len(request_id) > 128):
            return _error(None, -32600, "Invalid JSON-RPC request ID")
        method = message.get("method")
        params = message.get("params", {})
        if (not isinstance(method, str) or not method or
                set(message) - {"jsonrpc", "id", "method", "params"} or
                not isinstance(params, dict)):
            return _error(request_id, -32600, "Invalid JSON-RPC request") if has_id else None
        if not has_id:
            if method == "notifications/initialized" and self.protocol_version is not None:
                self.initialized = True
            # Notifications never execute tools and never receive responses.
            return None
        if method == "ping":
            return _success(request_id, {})
        if method == "initialize":
            if self.protocol_version is not None:
                return _error(request_id, -32600, "Session already initialized")
            version = params.get("protocolVersion")
            client = params.get("clientInfo")
            if (not isinstance(version, str) or not isinstance(params.get("capabilities"), dict)
                    or not isinstance(client, dict)
                    or not isinstance(client.get("name"), str)
                    or not isinstance(client.get("version"), str)):
                return _error(request_id, -32602, "Invalid initialize parameters")
            self.protocol_version = (version if version in SUPPORTED_PROTOCOL_VERSIONS
                                     else SUPPORTED_PROTOCOL_VERSIONS[-1])
            self._audit_event({"event": "protocol", "method": "initialize",
                               "protocol_version": self.protocol_version})
            return _success(request_id, {
                "protocolVersion": self.protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "researchops-synthetic-research", "version": "1.0.0"},
                "instructions": "Read-only synthetic local corpus, not live public web research. "
                                "Search documents, fetch relevant documents, and retain tool receipts.",
            })
        if not self.initialized:
            return _error(request_id, -32000, "Session is not initialized")
        if method == "tools/list":
            if set(params) - {"_meta"}:
                return _error(request_id, -32602, "Pagination is not available")
            self._audit_event({"event": "protocol", "method": "tools/list",
                               "tools": ["search_documents", "fetch_document"]})
            return _success(request_id, {"tools": [
                _tool_definition("search_documents", "query",
                                 "Search a read-only synthetic Korean/numeric/partial corpus. "
                                 "Return source IDs and a fresh tool receipt; not Internet search."),
                _tool_definition("fetch_document", "document_id",
                                 "Fetch one synthetic document by exact ID from search_documents. "
                                 "Preserves numeric facts and missing values; returns a tool receipt."),
            ]})
        if method == "tools/call":
            return self._call_tool(request_id, params)
        return _error(request_id, -32601, "Method not found")

    def serve(self, source: BinaryIO, destination: BinaryIO) -> int:
        """Serve newline-delimited messages with per-message and session bounds."""
        input_bytes = output_bytes = messages = 0
        while True:
            raw = source.readline(MAX_REQUEST_BYTES + 1)
            if not raw:
                return 0
            messages += 1
            input_bytes += len(raw)
            fatal = (len(raw) > MAX_REQUEST_BYTES or input_bytes > MAX_SESSION_INPUT_BYTES
                     or messages > MAX_MESSAGES or not raw.endswith(b"\n"))
            if fatal:
                response = _error(None, -32002, "MCP transport limit or framing violation")
            else:
                try:
                    message = strict_json_loads(raw, max_bytes=MAX_REQUEST_BYTES)
                except ValueError:
                    response = _error(None, -32700, "Invalid JSON encoding or syntax")
                else:
                    try:
                        response = self.handle(message)
                    except OSError:
                        request_id = message.get("id") if isinstance(message, dict) else None
                        if type(request_id) not in (str, int):
                            request_id = None
                        response = _error(request_id, -32603, "MCP audit unavailable")
                        fatal = True
            if response is not None:
                encoded = _encode(response)
                if (len(encoded) > MAX_RESPONSE_BYTES or
                        output_bytes + len(encoded) > MAX_SESSION_OUTPUT_BYTES):
                    return 2
                output_bytes += len(encoded)
                try:
                    destination.write(encoded)
                    destination.flush()
                except (BrokenPipeError, OSError):
                    return 1
            if fatal:
                return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-path", required=True,
                        help="New audit JSONL file under an existing private directory")
    parser.add_argument("--nonce", help="Deterministic evidence marker for protocol tests only")
    args = parser.parse_args(argv)
    try:
        with ResearchMCPServer(args.audit_path, nonce=args.nonce) as server:
            return server.serve(sys.stdin.buffer, sys.stdout.buffer)
    except (OSError, ValueError):
        print("Research MCP could not start or preserve its private audit.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
