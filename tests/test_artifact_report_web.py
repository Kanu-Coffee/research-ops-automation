"""Artifact declarations versus verified archive files; isolated HTTP fixtures."""

import copy
import hashlib
import http.client
import json
import os
import threading
import unittest
from urllib.parse import quote

from researchops.engine.archive import canonical_json
from researchops.web.artifact_views import render_run_artifact_report
from tests.web_support import AuthenticatedWebRouter as WebRouter, admin_session, authenticated_headers
from researchops.web.server import create_web_server
from tests import test_archive_queries


class ArtifactReportWebTests(unittest.TestCase):
    setUp = test_archive_queries.TestArchiveQueries.setUp

    def request(self, path):
        if path == f"/runs/{self.run.run_id}":
            path += "?tab=files"
        return WebRouter(self.app).handle_request("GET", path, headers={"Host": "localhost"})

    def entry(self, **changes):
        raw = b"%PDF-1.4\nfixture PDF\n%%EOF\n"
        value = {"artifact_id": "terms-pdf", "path": "documents/이용 약관.pdf", "role": "attachment",
            "scope": "record", "requested_record_ids": ["product-1"], "record_ids": ["product-1"],
            "declared_status": "ready", "status": "available", "reason_code": None,
            "source": {"kind": "official_image", "url": "https://private.invalid/file?token=secret-token",
                       "url_sha256": "a" * 64},
            "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw),
            "include_in_compose": True, "announce_missing": False}
        value.update(changes)
        return value, raw

    def report(self, entries):
        return {"schema_version": 1, "task_id": self.run.task_id, "run_id": self.run.run_id,
            "task_version_hash": self.run.task_version_hash, "composition_revision": 1, "entries": entries}

    def write_report(self, report, files=None, raw=None):
        # Deliberately control immutable evidence only within the synthetic fixture.
        for path, payload in (files or {}).items():
            output = self.archive / path
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(payload)
        data = canonical_json(report) if raw is None else raw
        (self.archive / "artifact-report.json").write_bytes(data)
        index_path = self.archive / "artifact-manifest.json"
        index = json.loads(index_path.read_text())
        for path, payload in {"artifact-report.json": data, **(files or {})}.items():
            index["artifacts"] = [item for item in index["artifacts"] if item["relative_path"] != path]
            index["artifacts"].append({"relative_path": path, "role": "evidence",
                "sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload),
                "mime_type": "application/pdf" if path.endswith(".pdf") else "application/json"})
        index_path.write_bytes(canonical_json(index))

    def test_no_request_and_empty_report_do_not_add_missing_file_section(self):
        (self.archive / "artifact-report.json").unlink(missing_ok=True)
        self.assertEqual(self.app.runs.get_run_artifact_report(self.run.run_id)["status"], "not_recorded")
        self.assertNotIn(b'id="requested-artifact-report"', self.request(f"/runs/{self.run.run_id}")[2])
        self.write_report(self.report([]))
        self.assertEqual(self.app.runs.get_run_artifact_report(self.run.run_id), {"status": "recorded", "entries": []})
        self.assertNotIn(b'id="requested-artifact-report"', self.request(f"/runs/{self.run.run_id}")[2])

    def test_declared_ready_verified_file_has_only_safe_diagnostics_and_download(self):
        entry, raw = self.entry()
        self.write_report(self.report([entry]), {entry["path"]: raw})
        result = self.app.runs.show_run(self.run.run_id)["artifact_report"]
        self.assertEqual(result["status"], "recorded")
        item = result["entries"][0]
        self.assertEqual(item["declared_status"], "ready")
        self.assertEqual(item["status"], "available")
        self.assertEqual(item["record_ids"], ["product-1"])
        self.assertEqual(item["download_status"], "verified")
        self.assertEqual(item["download_path"], entry["path"])
        self.assertNotIn("source", item)
        status, _, body = self.request(f"/runs/{self.run.run_id}")
        self.assertEqual(status, 200)
        self.assertIn("확보·검증 완료".encode(), body)
        self.assertIn("product-1".encode(), body)
        self.assertNotIn(b"secret-token", body)
        self.assertNotIn(b"private.invalid", body)
        self.assertIn(quote(entry["path"], safe="/").encode(), body)

    def test_missing_requested_file_is_reported_on_run_without_changing_message(self):
        entry, _ = self.entry(status="failed", reason_code="local_file_missing", sha256=None, size_bytes=None,
                              include_in_compose=False, record_ids=[])
        before = (self.archive / "email.html").read_bytes()
        saved_warnings = self.app.run_repo.get_research_result(self.run.run_id).warnings
        self.write_report(self.report([entry]))
        item = self.app.runs.get_run_artifact_report(self.run.run_id)["entries"][0]
        self.assertEqual(item["declared_status"], "ready")
        self.assertEqual(item["status"], "failed")
        self.assertIsNone(item["download_path"])
        body = self.request(f"/runs/{self.run.run_id}")[2]
        self.assertIn("확보·검증 실패".encode(), body)
        self.assertNotIn("파일 다운로드".encode(), body)
        self.assertEqual((self.archive / "email.html").read_bytes(), before)
        self.assertEqual(self.app.run_repo.get_research_result(self.run.run_id).warnings, saved_warnings)

    def test_excluded_research_evidence_can_be_downloaded_without_compose_inclusion(self):
        entry, raw = self.entry(status="excluded", reason_code="records_excluded",
                              record_ids=[], include_in_compose=False)
        preserved = "research-artifacts/" + entry["path"]
        self.write_report(self.report([entry]), {preserved: raw})
        item = self.app.runs.get_run_artifact_report(self.run.run_id)["entries"][0]
        self.assertEqual(item["status"], "excluded")
        self.assertFalse(item["include_in_compose"])
        self.assertEqual(item["download_path"], preserved)
        status, _, body = self.request(f"/runs/{self.run.run_id}/artifacts/{quote(preserved, safe='/')}")
        self.assertEqual((status, body), (200, raw))

    def test_run_evidence_is_research_only_with_requested_scope_and_verified_download(self):
        entry, raw = self.entry(role="evidence", scope="run", requested_scope="run", source=None,
            requested_record_ids=[], record_ids=[], reason_code="evidence_only", include_in_compose=False)
        preserved = "research-artifacts/" + entry["path"]
        self.write_report(self.report([entry]), {preserved: raw})
        report_bytes = (self.archive / "artifact-report.json").read_bytes()
        summary = self.app.runs.get_run_artifact_report(self.run.run_id)
        item = summary["entries"][0]
        self.assertEqual((item["role"], item["scope"], item["requested_scope"]), ("evidence", "run", "run"))
        self.assertEqual((item["status"], item["reason_code"]), ("available", "evidence_only"))
        self.assertEqual(item["download_path"], preserved)
        self.assertFalse(item["include_in_compose"])
        rendered = render_run_artifact_report(summary, self.run.run_id)
        self.assertIn("용도: 연구 증거", rendered)
        self.assertIn("요청 범위: 실행 전체 (run)", rendered)
        self.assertIn("연구 보관 전용 · 미포함", rendered)
        self.assertNotIn("메일 첨부", rendered)
        self.assertNotIn("파일 안전성 검증을 통과하지 못했습니다", rendered)
        self.assertEqual((self.archive / "artifact-report.json").read_bytes(), report_bytes)

    def test_role_scope_configuration_errors_are_distinct_from_file_safety_failures(self):
        for code in ("invalid_artifact_role", "invalid_artifact_scope", "invalid_artifact_role_scope"):
            with self.subTest(reason_code=code):
                entry, _ = self.entry(role="evidence", scope="run", requested_scope="run", source=None,
                    requested_record_ids=[], record_ids=[], status="failed", reason_code=code,
                    sha256=None, size_bytes=None, include_in_compose=False)
                self.write_report(self.report([entry]))
                summary = self.app.runs.get_run_artifact_report(self.run.run_id)
                self.assertEqual(summary["entries"][0]["reason_code"], code)
                rendered = render_run_artifact_report(summary, self.run.run_id)
                self.assertIn("요청 설정 오류", rendered)
                self.assertIn("요청 범위: 실행 전체 (run)", rendered)
                self.assertNotIn("파일 안전성 검증을 통과하지 못했습니다", rendered)
                self.assertNotIn("파일 다운로드", rendered)

    def test_requested_scope_keeps_unknown_values_out_of_the_safe_summary(self):
        entry, _ = self.entry(role="evidence", scope="legacy", source=None,
            requested_scope='<script>secret-scope-token</script>', requested_record_ids=[], record_ids=[],
            status="failed", reason_code="invalid_artifact_scope", sha256=None, size_bytes=None,
            include_in_compose=False)
        self.write_report(self.report([entry]))
        summary = self.app.runs.get_run_artifact_report(self.run.run_id)
        self.assertEqual(summary["entries"][0]["requested_scope"], "unknown")
        self.assertNotIn("secret-scope-token", json.dumps(summary))
        rendered = render_run_artifact_report(summary, self.run.run_id)
        self.assertIn("요청 범위: 지원하지 않는 범위", rendered)
        self.assertNotIn("secret-scope-token", rendered)
        for value in (None, False, "x" * 65, "run\n"):
            with self.subTest(requested_scope=value):
                self.write_report(self.report([{**entry, "requested_scope": value}]))
                summary = self.app.runs.get_run_artifact_report(self.run.run_id)
                self.assertEqual(summary["status"], "recorded" if value is None else "unavailable")
                if value is None:
                    self.assertIsNone(summary["entries"][0]["requested_scope"])

    def test_old_reports_do_not_invent_requested_scope_or_reclassify_original_errors(self):
        entry, _ = self.entry(role="evidence", scope="legacy", source=None,
            requested_record_ids=[], record_ids=[], status="failed", reason_code="unsafe_artifact",
            sha256=None, size_bytes=None, include_in_compose=False)
        self.write_report(self.report([entry]))
        summary = self.app.runs.get_run_artifact_report(self.run.run_id)
        self.assertNotIn("requested_scope", summary["entries"][0])
        self.assertEqual(summary["entries"][0]["reason_code"], "unsafe_artifact")
        rendered = render_run_artifact_report(summary, self.run.run_id)
        self.assertIn("기록된 범위: 기존 형식 (legacy)", rendered)
        self.assertNotIn("요청 범위:", rendered)
        self.assertNotIn("실행 전체 (run)", rendered)
        self.assertNotIn("요청 설정 오류", rendered)

    def test_current_file_missing_or_changed_does_not_rewrite_recorded_availability(self):
        entry, raw = self.entry()
        self.write_report(self.report([entry]), {entry["path"]: raw})
        path = self.archive / entry["path"]
        path.unlink()
        item = self.app.runs.get_run_artifact_report(self.run.run_id)["entries"][0]
        self.assertEqual(item["status"], "available")
        self.assertEqual(item["download_status"], "missing")
        path.write_bytes(b"changed")
        item = self.app.runs.get_run_artifact_report(self.run.run_id)["entries"][0]
        self.assertEqual(item["download_status"], "changed")
        self.assertIsNone(item["download_path"])

    def test_report_identity_hash_and_malformed_data_are_unavailable_not_missing(self):
        entry, raw = self.entry()
        original = self.report([entry])
        invalid = []
        for key, value in (("run_id", "another-run"), ("task_id", "another-task"),
                           ("task_version_hash", "f" * 64), ("composition_revision", 2), ("schema_version", True)):
            report = copy.deepcopy(original)
            report[key] = value
            invalid.append(report)
        malformed = copy.deepcopy(original)
        malformed["entries"][0]["path"] = "../outside.pdf"
        invalid.append(malformed)
        for report in invalid:
            with self.subTest(report=report.get("run_id")):
                self.write_report(report)
                self.assertEqual(self.app.runs.get_run_artifact_report(self.run.run_id)["status"], "unavailable")
        self.write_report(original, raw=b'{"schema_version":1,"schema_version":1}')
        self.assertEqual(self.app.runs.get_run_artifact_report(self.run.run_id)["status"], "unavailable")
        self.write_report(original, {entry["path"]: raw})
        (self.archive / "artifact-report.json").write_bytes(canonical_json(self.report([])))
        self.assertEqual(self.app.runs.get_run_artifact_report(self.run.run_id)["status"], "unavailable")
        body = self.request(f"/runs/{self.run.run_id}")[2]
        self.assertIn("진단 기록을 확인할 수 없습니다".encode(), body)
        self.assertNotIn("현재 보관 파일을 찾을 수 없습니다".encode(), body)

    def test_arbitrary_source_status_and_reason_values_never_enter_summary(self):
        entry, raw = self.entry(declared_status="secret-status-token", reason_code="secret-error-token")
        self.write_report(self.report([entry]), {entry["path"]: raw})
        summary = self.app.runs.get_run_artifact_report(self.run.run_id)
        self.assertNotIn("secret", json.dumps(summary))
        self.assertEqual(summary["entries"][0]["declared_status"], "unknown")
        self.assertEqual(summary["entries"][0]["reason_code"], "unknown")

    def test_metadata_verification_failures_have_specific_safe_labels(self):
        for code, label in (("source_metadata_mismatch", "원본 문서 정보가 요청한 문서와 다릅니다."),
                            ("metadata_response_invalid", "문서 정보 응답을 검증하지 못했습니다.")):
            with self.subTest(reason_code=code):
                entry, _ = self.entry(status="failed", reason_code=code, sha256=None, size_bytes=None,
                                      include_in_compose=False)
                self.write_report(self.report([entry]))
                summary = self.app.runs.get_run_artifact_report(self.run.run_id)
                self.assertEqual(summary["entries"][0]["reason_code"], code)
                body = self.request(f"/runs/{self.run.run_id}")[2]
                self.assertIn(label.encode(), body)
                self.assertNotIn(b"secret-token", body)
                self.assertNotIn("파일 다운로드".encode(), body)

    def test_symbolic_and_hardlinked_payloads_never_get_verified_downloads(self):
        entry, raw = self.entry()
        self.write_report(self.report([entry]), {entry["path"]: raw})
        path = self.archive / entry["path"]
        target = self.archive / "safe-copy.pdf"
        target.write_bytes(raw)
        for kind in ("symbolic", "hard"):
            path.unlink()
            path.symlink_to(target) if kind == "symbolic" else os.link(target, path)
            item = self.app.runs.get_run_artifact_report(self.run.run_id)["entries"][0]
            self.assertEqual(item["download_status"], "unavailable")
            self.assertIsNone(item["download_path"])
            self.assertEqual(self.request(f"/runs/{self.run.run_id}/artifacts/{quote(entry['path'], safe='/')}")[0], 400)

    def test_encoded_download_names_are_strict_and_headers_are_ascii(self):
        entry, raw = self.entry()
        self.write_report(self.report([entry]), {entry["path"]: raw})
        path = f"/runs/{self.run.run_id}/artifacts/"
        status, headers, body = self.request(path + quote(entry["path"], safe="/"))
        self.assertEqual((status, body), (200, raw))
        disposition = headers["Content-Disposition"]
        self.assertTrue(disposition.isascii())
        self.assertIn("filename*=UTF-8''" + quote("이용 약관.pdf", safe=""), disposition)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        for suffix in ("documents%2Fname.pdf", "documents%5cname.pdf", "%2e%2e/outside.pdf",
                       "documents/%0d%0aHeader.pdf", "%252e%252e/outside.pdf", "%252Foutside.pdf",
                       "documents/%FF.pdf", "documents/%E3%81.pdf", "documents/bad%2.pdf", "documents/bad%GG.pdf"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.request(path + suffix)[0], 400)

    def test_unicode_download_through_real_http_preserves_content_and_header(self):
        entry, raw = self.entry()
        self.write_report(self.report([entry]), {entry["path"]: raw})
        server = create_web_server(self.app, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            client.request("GET", f"/runs/{self.run.run_id}?tab=files", headers=authenticated_headers(self.app, {"Host": "localhost"}))
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("요청한 파일 확인".encode(), response.read())
            client.request("GET", f"/runs/{self.run.run_id}/artifacts/{quote(entry['path'], safe='/')}", headers=authenticated_headers(self.app, {"Host": "localhost"}))
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), raw)
            self.assertIn("filename*=UTF-8''", response.getheader("Content-Disposition"))
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
