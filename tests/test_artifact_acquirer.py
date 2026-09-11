"""Explicit media requests, protected HTTP bytes, selection and durable diagnostics."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
from pathlib import Path
import struct
import tempfile
import threading
import time
import tracemalloc
import unittest
from unittest.mock import patch
import zlib

from researchops.config import MediaProviderConfig, Settings
from researchops.engine.artifact_acquirer import ArtifactAcquirer, mime_part_bytes
from researchops.errors import HardGateError
from researchops.runners.protected_media_fetch import CardRAGPDFFetcher
from researchops.runners.research_fetch import FetchResult, ResearchFetchError, _fetch_direct


def pdf_bytes(label="Synthetic document", padding=0):
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R >>"]
    content = ("% " + label + "\n").encode()
    objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream")
    raw, offsets = b"%PDF-1.4\n", [0]
    for index, item in enumerate(objects, 1):
        offsets.append(len(raw))
        raw += f"{index} 0 obj\n".encode() + item + b"\nendobj\n"
    xref = len(raw)
    raw += b"xref\n0 5\n0000000000 65535 f \n" + b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets[1:])
    raw += f"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n{xref}\n".encode()
    return raw + (b"%" + b"p" * padding + b"\n" if padding else b"") + b"%%EOF\n"


def png_bytes():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(b"\0\xff\0\0")) + chunk(b"IEND", b""))


class MediaFixture:
    """Only a loopback synthetic service; records request paths/auth booleans."""
    def __init__(self, routes, token="synthetic-dedicated-token"):
        self.routes, self.requests, self.token = dict(routes), [], token
        for path, route in routes.items():
            if path.startswith("/sources/") and path.endswith("/pdf"):
                document_id = path.split("/")[2]
                metadata = {"document_id": document_id, "issuer": "woori", "product_code": "500095",
                            "pdf_sha256": hashlib.sha256(route[2]).hexdigest(), "pdf_size_bytes": len(route[2])}
                self.routes.setdefault("/resources/documents/" + document_id,
                                       (200, "application/json", json.dumps(metadata).encode()))

    def __enter__(self):
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def do_GET(self):
                authorized = self.headers.get("Authorization") == "Bearer " + fixture.token
                fixture.requests.append({"path": self.path, "authorized": authorized})
                route = fixture.routes.get(self.path)
                if not authorized and not self.path.startswith("/plate.png"):
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status, content_type, raw = route[:3]
                if len(route) == 4:
                    time.sleep(route[3])
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    if 300 <= status <= 399:
                        self.send_header("Location", "https://not-approved.example/secret")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *_):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)


class ArtifactCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "output"
        self.root.mkdir()
        self.token_path = Path(self.temp.name) / "media-token"
        self.token_path.write_text("synthetic-dedicated-token\n")
        self.token_path.chmod(0o600)
        self.settings = Settings()
        self.acquirer = ArtifactAcquirer(self.settings)
        self.records = [{"record_id": "r1"}, {"record_id": "r2"}]

    def tearDown(self):
        self.temp.cleanup()

    def acquire(self, artifacts, **kwargs):
        return self.acquirer.acquire(artifacts, self.records, kwargs.pop("reportable", self.records), self.root,
            run_id="run-synthetic", task_id="task-synthetic", task_version_hash="a" * 64,
            composition_revision=1, dedupe_enabled=kwargs.pop("dedupe_enabled", False), **kwargs)

    def local(self, raw=b"local text", **changes):
        artifact = {"artifact_id": "file-one", "path": "files/local.txt", "mime_type": "text/plain", "role": "attachment"}
        artifact.update(changes)
        path = self.root / artifact["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return artifact

    def remote(self, raw, digit="a", **changes):
        artifact = {"artifact_id": "pdf-" + digit, "path": "files/" + digit + ".pdf", "mime_type": "application/pdf",
                    "role": "attachment", "record_ids": ["r1"], "declared_status": "ready",
                    "source": {"kind": "cardrag_pdf", "connection_id": "cardrag", "document_id": "doc_" + digit * 64,
                               "issuer": "woori", "product_code": "500095", "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}}
        artifact.update(changes)
        return artifact

    def provider(self, base_url):
        self.settings.media.providers["cardrag"] = MediaProviderConfig(base_url, self.token_path)


class TestArtifactAcquirer(ArtifactCase):
    def test_two_real_pdf_http_transfers_and_one_official_png(self):
        first, second, png = pdf_bytes("First", 609000), pdf_bytes("Second", 320000), png_bytes()
        a, b = self.remote(first), self.remote(second, "b", record_ids=["r2"])
        image = {"artifact_id": "plate", "path": "files/plate.png", "role": "inline_image", "record_ids": ["r2"],
                 "source": {"kind": "official_image", "url": "https://pc.wooricard.com/plate.png?version=1"}}
        routes = {"/sources/" + a["source"]["document_id"] + "/pdf": (200, "application/pdf", first),
                  "/sources/" + b["source"]["document_id"] + "/pdf": (200, "application/pdf", second),
                  "/plate.png?version=1": (200, "image/png", png)}
        with MediaFixture(routes) as fixture, patch("researchops.engine.artifact_acquirer.PublicResearchFetcher") as public:
            self.provider(fixture.base_url)
            def image_fetch(url, **kwargs):
                policy = public.call_args.args[0]
                # The synthetic connection seam runs the real strict HTTP parser
                # against a local fixture; production still verifies public DNS/TLS.
                with patch("researchops.runners.research_fetch._resolve", return_value=("93.184.216.34",)), patch(
                        "researchops.runners.research_fetch._connect", side_effect=lambda *args:
                        (socket.create_connection(("127.0.0.1", fixture.server.server_port)), "93.184.216.34")):
                    return _fetch_direct(url, "GET", policy)
            public.return_value.fetch.side_effect = image_fetch
            result = self.acquire([a, b, image], network_allowed=True)
            self.assertEqual([item["size_bytes"] for item in result.attachments], [len(first), len(second)])
            self.assertEqual(result.inline_artifacts[0]["record_ids"], ["r2"])
            self.assertEqual(len(fixture.requests), 5)
            self.assertTrue(all(row["authorized"] for row in fixture.requests[:4]))
            self.assertFalse(fixture.requests[4]["authorized"])
            policy = public.call_args.args[0]
            self.assertTrue(policy.require_https)
            self.assertEqual(policy.max_body_bytes, 4 * 1024 * 1024)
            self.assertIn("pc.wooricard.com", policy.allowed_hosts)
        for item in result.attachments + result.inline_artifacts:
            self.assertEqual(hashlib.sha256(result.file_paths[item["path"]].read_bytes()).hexdigest(), item["sha256"])
            self.assertEqual(os.stat(result.file_paths[item["path"]]).st_mode & 0o777, 0o600)
        self.assertNotIn("synthetic-dedicated-token", json.dumps(result.report))
        self.assertNotIn("?version", json.dumps(result.report))
        self.assertEqual(result.report["entries"][2]["source"]["url_sha256"], hashlib.sha256(image["source"]["url"].encode()).hexdigest())

    def test_no_request_means_no_fetch_or_missing_warning(self):
        with patch.object(self.acquirer.pdf_fetcher, "fetch") as fetch:
            result = self.acquire([], network_allowed=True)
        fetch.assert_not_called()
        self.assertEqual(result.report["entries"], [])
        self.assertFalse(result.hold)

    def test_failed_fetch_continues_or_holds_without_automatic_announcement(self):
        artifact = self.remote(pdf_bytes())
        for policy in ("continue", "hold"):
            result = self.acquire([{**artifact, "on_failure": policy}], network_allowed=True)
            self.assertEqual(result.report["entries"][0]["reason_code"], "provider_unavailable")
            self.assertEqual(result.hold, policy == "hold")
            self.assertFalse(result.report["entries"][0]["announce_missing"])
            self.assertEqual(result.attachments, [])

    def test_offline_refuses_remote_and_preserves_explicit_missing_policy(self):
        artifact = self.remote(pdf_bytes(), announce_missing=True)
        with patch.object(self.acquirer.pdf_fetcher, "fetch") as fetch:
            result = self.acquire([artifact])
        fetch.assert_not_called()
        self.assertEqual(result.report["entries"][0]["reason_code"], "network_disabled")
        self.assertTrue(result.report["entries"][0]["announce_missing"])

    def test_shared_file_filtered_and_excluded_remote_never_fetched(self):
        local = self.local(record_ids=["r1", "r2"])
        remote = self.remote(pdf_bytes(), record_ids=["r1"], on_failure="hold")
        with patch.object(self.acquirer.pdf_fetcher, "fetch") as fetch:
            result = self.acquire([local, remote], reportable=[self.records[1]], dedupe_enabled=True, network_allowed=True)
        fetch.assert_not_called()
        self.assertEqual(result.attachments[0]["record_ids"], ["r2"])
        self.assertEqual(result.report["entries"][1]["status"], "excluded")
        self.assertFalse(result.hold)

    def test_legacy_only_excluded_when_dedupe_actually_removed_records(self):
        artifact = self.local()
        self.assertEqual(len(self.acquire([artifact], dedupe_enabled=True).attachments), 1)
        result = self.acquire([artifact], reportable=[self.records[0]], dedupe_enabled=True)
        self.assertEqual(result.attachments, [])
        self.assertEqual(result.report["entries"][0]["reason_code"], "legacy_unscoped_dedupe")
        self.assertEqual(result.file_paths[artifact["path"]].read_bytes(), b"local text")
        self.assertIsNotNone(result.report["entries"][0]["sha256"])

    def test_explicit_run_attachment_survives_dedupe(self):
        artifact = self.local(scope="run")
        self.assertEqual(len(self.acquire([artifact], reportable=[], dedupe_enabled=True).attachments), 1)

    def test_no_updates_run_evidence_preserves_two_json_files_without_compose_files(self):
        payloads = (b'{"status":"no_updates","records":[]}\n',
                    '{\n  "checked": true, "note": "변경 없음"\n}\n'.encode())
        artifacts = [self.local(payloads[0], artifact_id="run-state", path="state/current.json",
                                role="evidence", scope="run", mime_type="application/json"),
                     self.local(payloads[1], artifact_id="run-history", path="state/history.json",
                                role="evidence", scope="run", mime_type="application/json",
                                source=None, record_ids=[])]
        for artifact, raw in zip(artifacts, payloads):
            artifact.update(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
        research = {"status": "no_updates", "records": [], "artifacts": artifacts}
        self.records = research["records"]
        with patch.object(self.acquirer.pdf_fetcher, "fetch") as pdf_fetch, patch(
                "researchops.engine.artifact_acquirer.PublicResearchFetcher") as image_fetch:
            result = self.acquire(research["artifacts"], dedupe_enabled=True, network_allowed=True)
        pdf_fetch.assert_not_called()
        image_fetch.assert_not_called()
        self.assertEqual(result.inline_artifacts, [])
        self.assertEqual(result.attachments, [])
        self.assertFalse(result.hold)
        self.assertEqual(set(result.file_paths), {artifact["path"] for artifact in artifacts})
        for entry, artifact, raw in zip(result.report["entries"], artifacts, payloads):
            self.assertEqual((entry["scope"], entry["requested_scope"], entry["role"]), ("run", "run", "evidence"))
            self.assertEqual((entry["status"], entry["reason_code"]), ("available", "evidence_only"))
            self.assertFalse(entry["include_in_compose"])
            self.assertEqual(entry["record_ids"], [])
            self.assertIsNone(entry["source"])
            self.assertEqual((entry["sha256"], entry["size_bytes"]), (artifact["sha256"], len(raw)))
            self.assertEqual(result.file_paths[entry["path"]].read_bytes(), raw)

    def test_local_run_roles_accept_absent_or_null_source_and_absent_or_empty_links(self):
        for role in ("evidence", "attachment"):
            for source in ({}, {"source": None}):
                for links in ({}, {"record_ids": []}):
                    with self.subTest(role=role, source=source, links=links):
                        artifact = self.local(scope="run", role=role, **source, **links)
                        result = self.acquire([artifact], reportable=[], dedupe_enabled=True)
                        self.assertEqual(result.report["entries"][0]["status"], "available")
                        self.assertEqual(result.report["entries"][0]["include_in_compose"], role == "attachment")
                        self.assertEqual(len(result.attachments), int(role == "attachment"))

    def test_role_scope_errors_preserve_requested_scope_and_have_specific_codes(self):
        remote = self.remote(pdf_bytes())["source"]
        variants = [({"role": "inline_image", "scope": "run"}, "invalid_artifact_role_scope", "run"),
                    ({"role": "evidence", "scope": "run", "record_ids": ["r1"]}, "invalid_artifact_role_scope", "run"),
                    ({"role": "attachment", "scope": "run", "source": remote}, "invalid_artifact_role_scope", "run"),
                    ({"role": "evidence", "scope": "run", "source": remote}, "invalid_artifact_role_scope", "run"),
                    ({"role": "attachment", "scope": "run", "source": {"kind": "official_image", "url": "https://pc.wooricard.com/plate.png"}},
                     "invalid_artifact_role_scope", "run"),
                    ({"role": "unknown", "scope": "run"}, "invalid_artifact_role", "run"),
                    ({"role": None, "scope": "run"}, "invalid_artifact_role", "run"),
                    ({"role": "evidence", "scope": "other-scope"}, "invalid_artifact_scope", "legacy"),
                    ({"role": "evidence", "scope": "record"}, "invalid_artifact_role_scope", "record")]
        for changes, reason, canonical in variants:
            with self.subTest(changes=changes), patch.object(self.acquirer.pdf_fetcher, "fetch") as fetch:
                with self.assertRaises(HardGateError):
                    self.acquire([{"path": "state/result.json", **changes}], network_allowed=True)
                entry = self.acquirer.last_report["entries"][0]
                self.assertEqual((entry["reason_code"], entry["scope"]), (reason, canonical))
                self.assertEqual(entry["requested_scope"], changes["scope"])
                self.assertEqual(entry["status"], "failed")
                self.assertFalse(entry["include_in_compose"])
                fetch.assert_not_called()

    def test_requested_scope_keeps_only_bounded_printable_input(self):
        for scope, diagnostic in (("x" * 64, "x" * 64), ("x" * 65, None), ("", ""),
                                  ("run\nsecret", None), ("run\x7f", None), (None, None), ([], None), (2, None)):
            with self.subTest(scope=scope), self.assertRaises(HardGateError):
                self.acquire([{"path": "state/result.json", "scope": scope}])
            entry = self.acquirer.last_report["entries"][0]
            self.assertEqual(entry["requested_scope"], diagnostic)
            self.assertEqual(entry["scope"], "legacy")
            self.assertEqual(entry["reason_code"], "invalid_artifact_scope")
        result = self.acquire([self.local()])
        self.assertIsNone(result.report["entries"][0]["requested_scope"])
        self.assertEqual(result.report["entries"][0]["scope"], "legacy")

    def test_run_evidence_keeps_path_link_and_hash_safety_gates(self):
        for path in ("../escape.json", "logs/research.stdout", "artifact-report.json"):
            with self.subTest(path=path), self.assertRaises(HardGateError):
                self.acquire([{"path": path, "scope": "run", "role": "evidence"}])
            entry = self.acquirer.last_report["entries"][0]
            self.assertEqual((entry["reason_code"], entry["scope"], entry["requested_scope"]), ("unsafe_artifact", "run", "run"))
        artifact = self.local(b'{"value":1}', role="evidence", scope="run", mime_type="application/json")
        target = self.root / artifact["path"]
        target.with_name("link.json").symlink_to(target)
        for metadata in ({**artifact, "path": "files/link.json"}, {**artifact, "sha256": "a" * 64}):
            with self.subTest(metadata=metadata), self.assertRaises(HardGateError):
                self.acquire([metadata])
            self.assertEqual(self.acquirer.last_report["entries"][0]["reason_code"], "unsafe_artifact")
        os.link(target, target.with_name("hardlink.json"))
        with self.assertRaises(HardGateError):
            self.acquire([artifact])
        self.assertEqual(self.acquirer.last_report["entries"][0]["reason_code"], "unsafe_artifact")

    def test_invalid_source_shapes_and_links_fail_before_any_fetch(self):
        good = self.remote(pdf_bytes())
        variants = [dict(good, record_ids=[]), dict(good, record_ids=["missing"]), dict(good, scope="run"),
                    dict(good, record_ids=["r1", "r1"]), dict(good, mime_type="text/html"),
                    dict(good, sha256="b" * 64), dict(good, size_bytes=1),
                    dict(good, source={**good["source"], "headers": {"Authorization": "private"}}),
                    dict(good, source={**good["source"], "document_id": "../../secret"}),
                    dict(good, source={**good["source"], "product_code": "x\r\nprivate"}),
                    dict(good, source={**good["source"], "sha256": "invalid"})]
        for index, bad in enumerate(variants):
            with self.subTest(bad=bad), patch.object(self.acquirer.pdf_fetcher, "fetch") as fetch:
                with self.assertRaises(HardGateError):
                    self.acquire([good, {**bad, "artifact_id": "bad", "path": "files/bad.pdf"}], network_allowed=True)
                fetch.assert_not_called()
                reason = "invalid_artifact_role_scope" if index in (0, 2) else "unsafe_artifact"
                self.assertEqual(self.acquirer.last_report["entries"][-1]["reason_code"], reason)

    def test_reserved_traversal_symlink_and_duplicate_paths_hard_fail_even_excluded(self):
        for path in ("../secret", "/etc/passwd", "files/../x", "files//x", "logs/research.stdout", "artifact-report.json", "inputs/x", "research-artifacts/x"):
            with self.subTest(path=path), self.assertRaises(HardGateError):
                self.acquire([{"path": path, "record_ids": ["r1"]}], reportable=[])
        artifact = self.local()
        with self.assertRaises(HardGateError):
            self.acquire([artifact, {**artifact, "artifact_id": "other"}])
        (self.root / "link").symlink_to(self.root / "files", target_is_directory=True)
        with self.assertRaises(HardGateError):
            self.acquire([{**artifact, "path": "link/local.txt"}])

    def test_declared_local_hash_tampering_hard_fails_even_excluded(self):
        artifact = self.local(sha256="a" * 64, record_ids=["r1"])
        with self.assertRaises(HardGateError):
            self.acquire([artifact], reportable=[])
        self.assertEqual(self.acquirer.last_report["entries"][0]["reason_code"], "unsafe_artifact")

    def test_missing_local_and_invalid_mime_are_distinct_and_evidence_preserved(self):
        bad = self.local(b"not a PDF", mime_type="application/pdf")
        missing = {"path": "files/missing.pdf", "role": "attachment", "declared_status": "ready"}
        result = self.acquire([bad, missing])
        self.assertEqual([entry["reason_code"] for entry in result.report["entries"]], ["invalid_media", "local_file_missing"])
        self.assertIn(bad["path"], result.file_paths)
        self.assertEqual(result.report["entries"][1]["declared_status"], "ready")
        self.assertIsNone(result.report["entries"][1]["sha256"])

    def test_exact_mime_budget_and_one_byte_less(self):
        artifact = self.local()
        exact = self.settings.media.mime_reserve_bytes + mime_part_bytes(len(b"local text"))
        self.settings.delivery.max_message_bytes = exact
        self.assertEqual(len(self.acquire([artifact]).attachments), 1)
        self.settings.delivery.max_message_bytes = exact - 1
        result = self.acquire([artifact])
        self.assertEqual(result.report["entries"][0]["reason_code"], "message_size_limit")
        self.assertEqual(result.report["entries"][0]["status"], "excluded")
        self.assertIsNotNone(result.report["entries"][0]["sha256"])
        self.assertEqual(result.attachments, [])

    def test_raw_total_limit_and_count_limit_preserve_diagnostic(self):
        a = self.local(b"12345")
        b = self.local(b"67890", path="files/two.txt", artifact_id="two")
        self.settings.media.max_total_bytes = 9
        with self.assertRaises(HardGateError):
            self.acquire([a, b])
        self.assertEqual(self.acquirer.last_report["entries"][1]["reason_code"], "total_bytes_limit")
        artifacts = [{"path": f"files/{index}.txt"} for index in range(65)]
        with self.assertRaises(HardGateError):
            self.acquire(artifacts)
        self.assertEqual(len(self.acquirer.last_report["entries"]), 64)
        self.assertEqual(self.acquirer.last_report["entries"][-1]["reason_code"], "artifact_limit_exceeded")

    def test_cancel_after_first_file_preserves_previous_report_and_bytes(self):
        a = self.local()
        b = self.local(path="files/two.txt", artifact_id="two")
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            return calls == 4  # two initial local verifications, then each file
        with self.assertRaisesRegex(ResearchFetchError, "cancelled"):
            self.acquire([a, b], cancellation_check=cancel)
        self.assertTrue(self.acquirer.last_report["entries"][0]["include_in_compose"])
        self.assertEqual(self.acquirer.last_report["entries"][1]["reason_code"], "cancelled")
        self.assertIn(a["path"], self.acquirer.last_file_paths)

    def test_phase_deadline_marks_all_remaining_without_transport(self):
        artifacts = [self.remote(pdf_bytes()), self.remote(pdf_bytes(), "b")]
        with patch("researchops.engine.artifact_acquirer.time.monotonic", side_effect=[0, 121, 122]):
            result = self.acquire(artifacts, network_allowed=True)
        self.assertEqual([entry["reason_code"] for entry in result.report["entries"]], ["phase_timeout", "phase_timeout"])

    def test_remote_hash_size_type_failures_never_create_files(self):
        raw = pdf_bytes()
        artifact = self.remote(raw)
        self.provider("http://127.0.0.1:18015")
        for response, reason in ((FetchResult(200, "", {"content-type": "application/pdf"}, raw + b"x", {}), "source_hash_mismatch"),
                                 (FetchResult(200, "", {"content-type": "text/html"}, raw, {}), "mime_type_mismatch"),
                                 (FetchResult(401, "", {}, b"", {}), "source_unauthorized")):
            with self.subTest(reason=reason), patch.object(self.acquirer.pdf_fetcher, "fetch", return_value=response):
                result = self.acquire([artifact], network_allowed=True)
                self.assertEqual(result.report["entries"][0]["reason_code"], reason)
                self.assertFalse((self.root / artifact["path"]).exists())

    def test_failed_response_bytes_consume_total_budget(self):
        raw = pdf_bytes()
        artifacts = [self.remote(raw), self.remote(raw, "b")]
        self.settings.media.max_total_bytes = len(raw) * 2 - 1
        self.provider("http://127.0.0.1:18015")
        response = FetchResult(200, "", {"content-type": "text/html"}, raw, {})
        with patch.object(self.acquirer.pdf_fetcher, "fetch", return_value=response) as fetch:
            result = self.acquire(artifacts, network_allowed=True)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual([item["reason_code"] for item in result.report["entries"]], ["mime_type_mismatch", "total_bytes_limit"])

    def test_remote_first_budget_reserves_all_existing_local_files(self):
        raw = pdf_bytes()
        remote = self.remote(raw)
        local = self.local(b"x" * 700, record_ids=["r1"], role="evidence")
        self.settings.media.max_total_bytes = 1000
        self.provider("http://127.0.0.1:18015")
        with patch.object(self.acquirer.pdf_fetcher, "fetch") as fetch:
            result = self.acquire([remote, local], network_allowed=True, reportable=[self.records[1]])
        # The remote is selected below for an independent record. The original
        # excluded local evidence nevertheless reserves its existing bytes.
        self.assertEqual(fetch.call_count, 0)
        remote["record_ids"] = ["r2"]
        with patch.object(self.acquirer.pdf_fetcher, "fetch") as fetch:
            result = self.acquire([remote, local], network_allowed=True, reportable=[self.records[1]])
        fetch.assert_not_called()
        self.assertEqual(result.report["entries"][0]["reason_code"], "total_bytes_limit")
        self.assertEqual(result.report["entries"][1]["status"], "excluded")
        self.assertEqual(result.report["entries"][1]["size_bytes"], 700)
        self.assertEqual(list(result.file_paths), [local["path"]])

    def test_later_local_hash_tampering_prevents_earlier_network_request(self):
        remote = self.remote(pdf_bytes())
        local = self.local(b"original", sha256="a" * 64)
        self.provider("http://127.0.0.1:18015")
        with patch.object(self.acquirer.pdf_fetcher, "fetch") as fetch:
            with self.assertRaises(HardGateError):
                self.acquire([remote, local], network_allowed=True)
        fetch.assert_not_called()

    def test_local_change_during_remote_transfer_fails_hard(self):
        raw = pdf_bytes()
        remote = self.remote(raw)
        local = self.local(b"original")
        self.provider("http://127.0.0.1:18015")
        def remote_response(*args, **kwargs):
            (self.root / local["path"]).write_bytes(b"changed!")
            return FetchResult(200, "", {"content-type": "application/pdf"}, raw, {})
        with patch.object(self.acquirer.pdf_fetcher, "fetch", side_effect=remote_response):
            with self.assertRaises(HardGateError):
                self.acquire([remote, local], network_allowed=True)
        self.assertEqual(self.acquirer.last_report["entries"][1]["reason_code"], "unsafe_artifact")

    def test_disk_failure_removes_partial_file_and_records_failure(self):
        raw = pdf_bytes()
        artifact = self.remote(raw)
        self.provider("http://127.0.0.1:18015")
        response = FetchResult(200, "", {"content-type": "application/pdf"}, raw, {})
        with patch.object(self.acquirer.pdf_fetcher, "fetch", return_value=response), patch(
                "researchops.engine.artifact_acquirer.os.fsync", side_effect=OSError(28, "synthetic full disk")):
            result = self.acquire([artifact], network_allowed=True)
        self.assertEqual(result.report["entries"][0]["reason_code"], "artifact_write_failed")
        self.assertFalse((self.root / artifact["path"]).exists())
        self.assertEqual(result.file_paths, {})

    def test_downloaded_bytes_cannot_overwrite_worker_file(self):
        raw = pdf_bytes()
        artifact = self.remote(raw)
        (self.root / "files").mkdir()
        (self.root / artifact["path"]).write_bytes(b"worker contents")
        self.provider("http://127.0.0.1:18015")
        with patch.object(self.acquirer.pdf_fetcher, "fetch", return_value=FetchResult(
                200, "", {"content-type": "application/pdf"}, raw, {})):
            with self.assertRaises(HardGateError):
                self.acquire([artifact], network_allowed=True)
        self.assertEqual((self.root / artifact["path"]).read_bytes(), b"worker contents")

    def test_local_files_not_accumulated_in_result_memory(self):
        artifacts = [self.local(b"x" * 600_000, path=f"files/{index}.txt", artifact_id=f"file-{index}") for index in range(16)]
        tracemalloc.start()
        try:
            result = self.acquire(artifacts)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(len(result.attachments), 16)
        self.assertLess(peak, 4_000_000)
        self.assertTrue(all(isinstance(path, Path) for path in result.file_paths.values()))


class TestProtectedPDFTransport(ArtifactCase):
    def test_metadata_identity_and_hash_checked_before_pdf_download(self):
        raw = pdf_bytes()
        artifact = self.remote(raw)
        doc = artifact["source"]["document_id"]
        for field, bad_value in (("document_id", "doc_" + "b" * 64), ("issuer", "shinhan"),
                                 ("product_code", "different"), ("pdf_sha256", "b" * 64), ("pdf_size_bytes", len(raw) + 1)):
            metadata = {"document_id": doc, "issuer": "woori", "product_code": "500095",
                        "pdf_sha256": artifact["source"]["sha256"], "pdf_size_bytes": len(raw), field: bad_value}
            routes = {"/resources/documents/" + doc: (200, "application/json", json.dumps(metadata).encode()),
                      "/sources/" + doc + "/pdf": (200, "application/pdf", raw)}
            with self.subTest(field=field), MediaFixture(routes) as fixture:
                with self.assertRaisesRegex(ResearchFetchError, "source_metadata_mismatch"):
                    CardRAGPDFFetcher().fetch(MediaProviderConfig(fixture.base_url, self.token_path), artifact["source"])
                self.assertEqual([request["path"] for request in fixture.requests], ["/resources/documents/" + doc])

    def test_duplicate_metadata_keys_are_not_accepted(self):
        artifact = self.remote(pdf_bytes())
        path = "/resources/documents/" + artifact["source"]["document_id"]
        with MediaFixture({path: (200, "application/json", b'{"issuer":"woori","issuer":"other"}')}) as fixture:
            with self.assertRaisesRegex(ResearchFetchError, "metadata_response_invalid"):
                CardRAGPDFFetcher().fetch(MediaProviderConfig(fixture.base_url, self.token_path), artifact["source"])
            self.assertEqual(len(fixture.requests), 1)

    def test_root_owned_group_readonly_token_reference_is_supported(self):
        from researchops.runners import protected_media_fetch as protected
        original = os.fstat
        def root_readonly(fd):
            values = list(original(fd))
            values[0], values[4] = 0o100440, 0
            return os.stat_result(values)
        # Simulate the approved root:service-group 0440 metadata. Do not create,
        # chown, chmod or read any real system credential in a unit test.
        with patch.object(protected.os, "fstat", side_effect=root_readonly):
            self.assertEqual(protected._token(self.token_path), b"synthetic-dedicated-token")
        self.token_path.chmod(0o640)
        with self.assertRaisesRegex(ResearchFetchError, "credential_unavailable"):
            protected._token(self.token_path)
    def test_redirect_never_forwarded_and_credential_absent_from_error(self):
        doc = "doc_" + "a" * 64
        with MediaFixture({"/sources/" + doc + "/pdf": (302, "text/plain", b"secret")}) as fixture:
            provider = MediaProviderConfig(fixture.base_url, self.token_path)
            with self.assertRaisesRegex(ResearchFetchError, "redirect_denied") as captured:
                CardRAGPDFFetcher().fetch(provider, self.remote(b"secret")["source"])
            self.assertEqual(len(fixture.requests), 2)
            self.assertNotIn("synthetic-dedicated-token", str(captured.exception.audit))

    def test_private_single_link_token_file_required(self):
        provider = MediaProviderConfig("http://127.0.0.1:18015", self.token_path)
        self.token_path.chmod(0o644)
        with self.assertRaisesRegex(ResearchFetchError, "credential_unavailable"):
            CardRAGPDFFetcher().fetch(provider, self.remote(pdf_bytes())["source"])
        self.token_path.chmod(0o600)
        alias = self.token_path.with_name("alias")
        os.link(self.token_path, alias)
        with self.assertRaisesRegex(ResearchFetchError, "credential_unavailable"):
            CardRAGPDFFetcher().fetch(provider, self.remote(pdf_bytes())["source"])

    def test_non_loopback_origins_refused_before_process(self):
        for url in ("http://example.com", "http://169.254.169.254", "http://127.0.0.1@evil.test", "http://127.0.0.1/path",
                    "http://127.0.0.1?redirect=evil", "http://localhost", "http://[::ffff:127.0.0.1]"):
            with self.subTest(url=url), patch("researchops.runners.protected_media_fetch.subprocess.Popen") as spawn:
                with self.assertRaises(ResearchFetchError):
                    CardRAGPDFFetcher().fetch(MediaProviderConfig(url, self.token_path), self.remote(pdf_bytes())["source"])
                spawn.assert_not_called()

    def test_timeout_and_cancellation_reap_child(self):
        doc = "doc_" + "a" * 64
        with MediaFixture({"/sources/" + doc + "/pdf": (200, "application/pdf", pdf_bytes(), 0.4)}) as fixture:
            provider = MediaProviderConfig(fixture.base_url, self.token_path)
            with self.assertRaisesRegex(ResearchFetchError, "timeout"):
                CardRAGPDFFetcher().fetch(provider, self.remote(pdf_bytes())["source"], timeout_seconds=0.1)
            start = time.monotonic()
            def cancellation():
                return time.monotonic() - start > 0.1
            with self.assertRaisesRegex(ResearchFetchError, "cancelled"):
                CardRAGPDFFetcher().fetch(provider, self.remote(pdf_bytes())["source"], cancellation_check=cancellation)
            self.assertLess(time.monotonic() - start, 0.8)

    def test_401_and_body_limit_are_distinct(self):
        doc = "doc_" + "a" * 64
        with MediaFixture({"/sources/" + doc + "/pdf": (200, "application/pdf", pdf_bytes())}) as fixture:
            provider = MediaProviderConfig(fixture.base_url, self.token_path)
            with self.assertRaisesRegex(ResearchFetchError, "body_too_large"):
                CardRAGPDFFetcher().fetch(provider, self.remote(pdf_bytes())["source"], max_bytes=10)
            self.token_path.write_text("wrong-synthetic-token")
            result = CardRAGPDFFetcher().fetch(provider, self.remote(pdf_bytes())["source"])
            self.assertEqual(result.status, 401)
            self.assertEqual(result.body, b"")
