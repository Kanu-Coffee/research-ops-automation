"""The native helper gets copies; only the app's scoped receipts authorize reuse."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from researchops.config import Settings, MediaProviderConfig
from researchops.engine.research_acquisition import ResearchAcquisitionSession
from researchops.errors import HardGateError
from researchops.runners.file_acquisition import acquire_file, _publish
from researchops.runners.research_fetch import FetchResult, ResearchFetchError
from tests.test_artifact_acquirer import pdf_bytes, png_bytes, MediaFixture


class FetchDouble:
    def __init__(self, raw, error=None):
        self.raw, self.error, self.calls = raw, error, []

    def fetch(self, provider, source, **kwargs):
        self.calls.append((copy.deepcopy(source), kwargs))
        if self.error:
            raise self.error
        return FetchResult(200, "http://127.0.0.1/protected", {"content-type": "application/pdf"},
                           self.raw, {"body_bytes": len(self.raw)})


class ResearchAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / "worker"
        self.work.mkdir()
        self.settings = Settings()
        self.settings.media.providers["synthetic"] = MediaProviderConfig(
            "http://127.0.0.1:9", self.root / "unused-token-file")
        self.raw = pdf_bytes()
        self.source = {"kind": "cardrag_pdf", "connection_id": "synthetic", "document_id": "doc_" + "a" * 64,
                       "issuer": "synthetic", "product_code": "code-1",
                       "sha256": hashlib.sha256(self.raw).hexdigest(), "size_bytes": len(self.raw)}
        self.session = self.new_session()
        self.fetcher = FetchDouble(self.raw)
        self.session.pdf_fetcher = self.fetcher

    def new_session(self, suffix=""):
        return ResearchAcquisitionSession(self.settings, run_id="run-synthetic" + suffix,
            task_id="task-synthetic", attempt=1, fencing_token="fence-synthetic" + suffix,
            storage_root=self.root / ("app" + suffix))

    def tearDown(self):
        self.session.close()
        self.temp.cleanup()

    def start(self):
        return self.session.start(self.work)

    def acquire(self, source=None, **kwargs):
        return acquire_file(self.source if source is None else source, artifact_root=self.work,
                            timeout_seconds=3, **kwargs)

    def request(self, payload=None, raw=None):
        request_id = uuid.uuid4().hex
        document = {"schema_version": 1, "session_id": self.session.session_id,
                    "request_id": request_id, "source": self.source}
        if payload:
            document.update(payload)
        _publish(self.work, f".researchops-files/requests/{request_id}.json",
                 raw if raw is not None else json.dumps(document).encode())
        target = self.work / f".researchops-files/responses/{request_id}.json"
        deadline = time.monotonic() + 3
        while not target.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        return json.loads(target.read_text())

    def test_staged_helper_works_without_app_imports_or_configuration(self):
        self.start()
        code = ("import json,sys;sys.path.insert(0,sys.argv[1]);"
                "from researchops.runners.file_acquisition import acquire_file;"
                "print(json.dumps({'response':acquire_file(json.loads(sys.argv[2])),"
                "'module_file':acquire_file.__code__.co_filename}))")
        process = subprocess.run([sys.executable, "-I", "-c", code,
            str(self.work / ".researchops-submit"), json.dumps(self.source)],
            cwd=self.work, capture_output=True, text=True, timeout=5, check=True)
        output = json.loads(process.stdout)
        self.assertEqual(output["module_file"],
                         str(self.work / ".researchops-submit/researchops/runners/file_acquisition.py"))
        response = output["response"]
        self.assertEqual(response["status"], "available")
        self.assertEqual(Path(response["path"]).read_bytes(), self.raw)
        self.assertNotIn("fence-synthetic", process.stdout)
        self.assertNotIn("unused-token-file", process.stdout)
        self.assertNotIn("http", process.stdout)

    def test_staged_package_wins_over_a_regular_package_elsewhere_on_sys_path(self):
        installed = self.root / "other-installed-package"
        package = installed / "researchops"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("raise RuntimeError('Wrong package imported')\n")
        self.start()
        code = ("import json,sys;sys.path[:0]=sys.argv[1:3];"
                "from researchops.runners.file_acquisition import acquire_file;"
                "print(json.dumps(acquire_file(json.loads(sys.argv[3]))))")
        process = subprocess.run([sys.executable, "-I", "-c", code,
            str(self.work / ".researchops-submit"), str(installed), json.dumps(self.source)],
            cwd=self.work, capture_output=True, text=True, timeout=5, check=True)
        self.assertEqual(json.loads(process.stdout)["status"], "available")

    def test_success_reused_once_with_original_and_canonical_provenance(self):
        self.start()
        first, second = self.acquire(), self.acquire()
        self.assertEqual(first, second)
        self.assertEqual(len(self.fetcher.calls), 1)
        self.assertEqual(self.session.consumed_count, 1)
        self.assertEqual(self.session.consumed_bytes, len(self.raw))
        self.assertTrue(self.session.close())
        self.session.raise_if_failed()
        receipt = self.session.lookup(self.source)
        self.assertEqual(self.session.read_original(receipt), self.raw)
        self.assertEqual(self.session.derived_from([first["acquisition_id"]]),
                         [{"acquisition_id": first["acquisition_id"], "source_sha256": self.source["sha256"]}])
        ledger = json.loads((self.session.storage_root / "ledger.json").read_text())
        self.assertEqual(ledger["fencing_token"], "fence-synthetic")
        self.assertEqual(ledger["records"][0]["source"], self.source)
        self.assertTrue(self.session.close())

    def test_failure_reused_without_a_second_download_and_partial_bytes_counted(self):
        self.fetcher.error = ResearchFetchError("source_unauthorized", {"body_bytes": 17})
        self.start()
        first, second = self.acquire(), self.acquire()
        self.assertEqual(first, second)
        self.assertEqual(first["reason_code"], "source_unauthorized")
        self.assertNotIn("path", first)
        self.assertEqual(len(self.fetcher.calls), 1)
        self.assertEqual(self.session.consumed_bytes, 17)
        self.session.close()
        self.assertEqual(self.session.lookup(self.source)["status"], "failed")
        with self.assertRaises(HardGateError):
            self.session.derived_from([first["acquisition_id"]])

    def test_unknown_receipt_or_another_session_cannot_authorize_derivation(self):
        self.start()
        response = self.acquire()
        self.session.close()
        other = self.new_session("-other")
        other.close()
        with self.assertRaises(HardGateError):
            other.derived_from([response["acquisition_id"]])
        with self.assertRaises(HardGateError):
            self.session.derived_from(["acq-" + "0" * 32])
        with self.assertRaises(HardGateError):
            self.session.derived_from([response["acquisition_id"]] * 2)

    def test_changed_descriptor_cannot_bypass_receipt_lookup(self):
        self.start()
        self.acquire()
        self.session.close()
        for field, value in (("sha256", "0" * 64), ("size_bytes", len(self.raw) + 1),
                             ("issuer", "changed"), ("product_code", "changed")):
            with self.subTest(field=field), self.assertRaises(HardGateError):
                self.session.lookup(dict(self.source, **{field: value}))
        unseen = dict(self.source, document_id="doc_" + "b" * 64)
        self.assertIsNone(self.session.lookup(unseen))

    def test_forged_worker_response_is_not_an_authoritative_receipt(self):
        self.start()
        response = self.acquire()
        response_path = next((self.work / ".researchops-files/responses").glob("*.json"))
        fake = json.loads(response_path.read_text())
        fake.update(sha256="0" * 64, acquisition_id="acq-" + "0" * 32)
        response_path.write_text(json.dumps(fake))
        self.session.close()
        receipt = self.session.lookup(self.source)
        self.assertEqual(receipt["acquisition_id"], response["acquisition_id"])
        with self.assertRaises(HardGateError):
            self.session.read_original(dict(receipt, sha256="0" * 64))
        with self.assertRaises(HardGateError):
            self.session.derived_from([fake["acquisition_id"]])

    def test_worker_copy_mutation_fails_close_verification(self):
        self.start()
        response = self.acquire()
        Path(response["path"]).write_bytes(b"changed")
        self.assertTrue(self.session.close())
        self.assertEqual(self.session.failure_reason, "acquisition_file_changed")
        with self.assertRaises(HardGateError):
            self.session.lookup(self.source)

    def test_original_mutation_after_close_cannot_be_reused(self):
        self.start()
        self.acquire()
        self.session.close()
        receipt = self.session.lookup(self.source)
        (self.session.storage_root / receipt["original_path"]).write_bytes(b"changed")
        with self.assertRaises(HardGateError):
            self.session.read_original(receipt)
        with self.assertRaises(HardGateError):
            self.session.derived_from([receipt["acquisition_id"]])

    def test_working_copy_hardlink_fails_verification(self):
        self.start()
        response = self.acquire()
        os.link(response["path"], self.root / "another-name")
        self.session.close()
        self.assertEqual(self.session.failure_reason, "acquisition_file_changed")

    def test_symlink_request_cannot_read_another_file(self):
        self.start()
        secret = self.root / "secret"
        secret.write_text("not-an-acquisition-request")
        (self.work / ".researchops-files/requests" / ("0" * 32 + ".json")).symlink_to(secret)
        deadline = time.monotonic() + 2
        while self.session.failure_reason is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.session.close()
        self.assertEqual(self.session.failure_reason, "acquisition_channel_unsafe")
        self.assertEqual(self.fetcher.calls, [])
        self.assertNotIn("not-an-acquisition-request", (self.session.storage_root / "ledger.json").read_text())

    def test_stale_session_and_duplicate_fields_are_rejected_before_fetch(self):
        self.start()
        response = self.request({"session_id": "0" * 32})
        self.assertEqual(response["reason_code"], "invalid_acquisition_request")
        response = self.request(raw=b'{"source":{},"source":{}}')
        self.assertEqual(response["reason_code"], "invalid_acquisition_request")
        self.assertEqual(self.fetcher.calls, [])
        self.session.close()
        self.assertEqual(self.session.ledger()["request_count"], 2)
        self.assertEqual(len(self.session.ledger()["request_failures"]), 2)

    def test_invalid_source_can_be_corrected_without_exposing_the_submitted_data(self):
        self.start()
        response = self.acquire({"kind": "unsupported", "secret": "must-not-be-retained"})
        self.assertEqual(response["reason_code"], "invalid_acquisition_request")
        self.assertEqual(self.acquire()["status"], "available")
        self.session.close()
        self.assertNotIn("must-not-be-retained", json.dumps(self.session.ledger()))

    def test_request_count_is_bounded_even_if_worker_removes_handled_requests(self):
        self.settings.media.max_files = 1
        self.start()
        for _ in range(4):
            self.assertEqual(self.acquire()["status"], "available")
            for request in (self.work / ".researchops-files/requests").glob("*.json"):
                request.unlink()
        self.assertEqual(self.acquire()["status"], "failed")
        self.session.close()
        self.assertEqual(self.session.ledger()["request_count"], 4)
        self.assertEqual(self.session.failure_reason, "acquisition_channel_unsafe")

    def test_cancellation_or_failed_owner_check_prevents_remote_requests(self):
        self.session.cancellation_check = lambda: (_ for _ in ()).throw(RuntimeError("no lease"))
        self.start()
        self.assertEqual(self.acquire()["status"], "failed")
        self.session.close()
        self.assertEqual(self.fetcher.calls, [])
        self.assertEqual(self.session.failure_reason, "acquisition_authorization_lost")

    def test_never_started_close_has_no_files_and_is_idempotent(self):
        self.assertTrue(self.session.close())
        self.assertTrue(self.session.close())
        self.assertFalse(self.session.storage_root.exists())

    def test_all_intermediate_acquisitions_consume_count_and_byte_budget(self):
        self.settings.media.max_total_bytes = len(self.raw)
        self.start()
        first = self.acquire()
        second = self.acquire(dict(self.source, document_id="doc_" + "b" * 64))
        self.assertEqual(first["status"], "available")
        self.assertEqual(second["reason_code"], "total_bytes_limit")
        self.assertEqual(self.session.consumed_count, 2)
        self.assertEqual(self.session.consumed_bytes, len(self.raw))
        self.assertEqual(len(self.fetcher.calls), 1)

    def test_file_count_cap_includes_unreported_originals(self):
        self.settings.media.max_files = 1
        self.start()
        self.acquire()
        response = self.acquire(dict(self.source, document_id="doc_" + "b" * 64))
        self.assertEqual(response["reason_code"], "artifact_limit_exceeded")
        self.assertEqual(len(self.fetcher.calls), 1)

    def test_model_idle_time_does_not_consume_acquisition_phase_budget(self):
        self.start()
        time.sleep(.1)
        self.assertEqual(self.session.elapsed_seconds, 0)
        self.acquire()
        self.assertLess(self.session.elapsed_seconds, .1)

    def test_active_download_time_is_shared_by_later_requests(self):
        self.settings.media.phase_timeout_seconds = .1
        original = self.fetcher.fetch
        def delayed(*args, **kwargs):
            time.sleep(.06)
            return original(*args, **kwargs)
        self.fetcher.fetch = delayed
        self.start()
        self.assertEqual(self.acquire()["status"], "available")
        response = self.acquire(dict(self.source, document_id="doc_" + "b" * 64))
        self.assertEqual(response["reason_code"], "phase_timeout")
        self.assertEqual(len(self.fetcher.calls), 1)

    def test_failed_media_validation_still_counts_downloaded_bytes(self):
        self.fetcher.raw = b"wrong-pdf"
        self.start()
        response = self.acquire()
        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["reason_code"], "source_hash_mismatch")
        self.assertEqual(self.session.consumed_bytes, len(b"wrong-pdf"))
        self.assertEqual(self.acquire(), response)
        self.assertEqual(len(self.fetcher.calls), 1)

    def test_client_rejects_an_out_of_scope_response_path(self):
        original = self.session._worker_response
        def forged(record):
            response = original(record)
            response["path"] = str(self.root / "outside.pdf")
            return response
        self.session._worker_response = forged
        self.start()
        self.assertEqual(self.acquire()["reason_code"], "file_acquisition_unavailable")

    def test_client_rejects_malformed_or_unbounded_session_metadata(self):
        self.start()
        path = self.work / ".researchops-files/session.json"
        valid = json.loads(path.read_text())
        for document in ([], dict(valid, max_file_bytes=2 ** 100), dict(valid, max_file_bytes=True)):
            with self.subTest(document_type=type(document).__name__):
                path.write_text(json.dumps(document))
                self.assertEqual(self.acquire(), {"status": "failed", "reason_code": "file_acquisition_unavailable"})
        path.write_text(json.dumps(valid))
        self.assertEqual(self.fetcher.calls, [])

    def test_active_session_cannot_authorize_post_research_reuse(self):
        self.start()
        self.acquire()
        with self.assertRaises(HardGateError):
            self.session.lookup(self.source)

    def test_timeout_failure_is_terminal_for_same_source(self):
        self.fetcher.error = ResearchFetchError("timeout")
        self.start()
        first = self.acquire()
        self.fetcher.error = None
        second = self.acquire()
        self.assertEqual(first, second)
        self.assertEqual(first["reason_code"], "timeout")
        self.assertEqual(len(self.fetcher.calls), 1)
        self.assertEqual(self.session.consumed_bytes, len(self.raw))
        self.assertEqual(self.fetcher.calls[0][1]["max_bytes"], len(self.raw))

    def test_unknown_transfer_failure_reserves_budget_even_if_audit_reports_zero(self):
        self.settings.media.max_total_bytes = len(self.raw)
        self.fetcher.error = ResearchFetchError("timeout", {"body_bytes": 0})
        self.start()
        response = self.acquire()
        self.assertEqual(response["reason_code"], "timeout")
        self.assertEqual(self.session.consumed_bytes, len(self.raw))
        response = self.acquire(dict(self.source, document_id="doc_" + "b" * 64))
        self.assertEqual(response["reason_code"], "total_bytes_limit")
        self.assertEqual(len(self.fetcher.calls), 1)
        self.session.close()
        self.assertEqual(self.session.lookup(self.source)["bytes_accounting"], "reserved")

    def test_preflight_failure_does_not_charge_unstarted_download(self):
        self.settings.media.providers.clear()
        self.start()
        self.assertEqual(self.acquire()["reason_code"], "provider_unavailable")
        self.assertEqual(self.session.consumed_bytes, 0)
        self.assertEqual(self.fetcher.calls, [])

    def test_public_image_denied_before_transport_has_no_byte_charge(self):
        self.start()
        response = self.acquire({"kind": "official_image", "url": "https://not-allowed.example/image.png"})
        self.assertEqual(response["reason_code"], "host_denied")
        self.assertEqual(self.session.consumed_bytes, 0)

    def test_actual_protected_timeout_without_complete_byte_audit_reserves_size(self):
        descriptor = dict(self.source, issuer="woori", product_code="500095")
        with MediaFixture({"/sources/" + descriptor["document_id"] + "/pdf":
                           (200, "application/pdf", self.raw, .4)}) as fixture:
            token = self.root / "synthetic-secret"
            token.write_text(fixture.token)
            token.chmod(0o600)
            self.settings.media.file_timeout_seconds = .15
            self.settings.media.providers["synthetic"] = MediaProviderConfig(fixture.base_url, token)
            from researchops.runners.protected_media_fetch import CardRAGPDFFetcher
            self.session.pdf_fetcher = CardRAGPDFFetcher()
            self.start()
            response = self.acquire(descriptor)
            self.assertEqual(response["reason_code"], "timeout")
            self.session.close()
            self.assertEqual(self.session.consumed_bytes, len(self.raw))
            self.assertEqual(self.session.lookup(descriptor)["bytes_accounting"], "reserved")
            self.assertTrue(self.session.cleanup_verified)

    def test_cancellation_joins_inflight_fetch_before_closing(self):
        entered, cancelled = threading.Event(), threading.Event()
        def fetch(*args, **kwargs):
            entered.set()
            while not kwargs["cancellation_check"]():
                time.sleep(.01)
            cancelled.set()
            raise ResearchFetchError("cancelled")
        self.fetcher.fetch = fetch
        self.start()
        results = []
        client = threading.Thread(target=lambda: results.append(self.acquire()))
        client.start()
        self.assertTrue(entered.wait(2))
        self.assertTrue(self.session.close())
        client.join(2)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(client.is_alive())
        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(list((self.session.storage_root / "originals").iterdir()), [])

    def test_uncooperative_fetch_cannot_publish_after_unverified_close(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(*args, **kwargs):
            entered.set()
            release.wait(3)
            return FetchResult(200, "unused", {"content-type": "application/pdf"}, self.raw, {})
        self.fetcher.fetch = fetch
        self.start()
        client = threading.Thread(target=self.acquire)
        client.start()
        self.assertTrue(entered.wait(2))
        try:
            with patch("researchops.engine.research_acquisition._CLOSE_TIMEOUT", .01):
                self.assertFalse(self.session.close())
            self.assertEqual(self.session.failure_reason, "acquisition_cleanup_failed")
        finally:
            release.set()
            client.join(2)
            self.session._thread.join(2)
        self.assertEqual(list((self.session.storage_root / "originals").iterdir()), [])
        with self.assertRaises(HardGateError):
            self.session.lookup(self.source)

    def test_transport_cleanup_failure_remains_unverified_after_thread_exit(self):
        self.fetcher.error = ResearchFetchError("worker_cleanup_failed")
        self.start()
        response = self.acquire()
        self.assertEqual(response["status"], "failed")
        self.assertFalse(self.session.close())
        self.assertFalse(self.session.cleanup_verified)

    def test_originals_must_not_live_within_worker_root(self):
        invalid = ResearchAcquisitionSession(self.settings, run_id="run-test", task_id="task-test",
            attempt=1, fencing_token="fence", storage_root=self.work / "app")
        with self.assertRaises(HardGateError):
            invalid.start(self.work)

    def test_actual_protected_transport_uses_loopback_and_no_credentials_in_receipt(self):
        descriptor = dict(self.source, issuer="woori", product_code="500095")
        with MediaFixture({"/sources/" + descriptor["document_id"] + "/pdf":
                           (200, "application/pdf", self.raw)}) as fixture:
            token = self.root / "synthetic-secret"
            token.write_text(fixture.token)
            token.chmod(0o600)
            self.settings.media.providers["synthetic"] = MediaProviderConfig(fixture.base_url, token)
            from researchops.runners.protected_media_fetch import CardRAGPDFFetcher
            self.session.pdf_fetcher = CardRAGPDFFetcher()
            self.start()
            response = self.acquire(descriptor)
            self.assertEqual(response["status"], "available")
            self.session.close()
            self.assertEqual(len(fixture.requests), 2)
            self.assertTrue(all(item["authorized"] for item in fixture.requests))
            self.assertNotIn(fixture.token, json.dumps(self.session.ledger()))
            self.assertNotIn(str(token), json.dumps(response))

    def test_official_image_query_is_not_preserved_in_public_receipt(self):
        source = {"kind": "official_image", "url": "https://www.shinhancard.com/plate.png?private=hidden"}
        image = png_bytes()
        with patch("researchops.engine.research_acquisition.PublicResearchFetcher") as fetch:
            fetch.return_value.fetch.return_value = FetchResult(200, source["url"],
                {"content-type": "image/png"}, image, {"body_bytes": len(image)})
            self.start()
            response = self.acquire(source)
            self.session.close()
        self.assertEqual(response["status"], "available")
        receipt = self.session.lookup(source)
        self.assertEqual(receipt["source"]["url"], "https://www.shinhancard.com/plate.png")
        self.assertNotIn("hidden", json.dumps(self.session.ledger()))


if __name__ == "__main__":
    unittest.main()
