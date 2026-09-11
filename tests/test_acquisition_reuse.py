"""Authoritative Research receipt reuse shares phase budgets without a second fetch."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from researchops.engine.artifact_acquirer import _source
from researchops.errors import HardGateError
from researchops.runners.research_fetch import FetchResult, ResearchFetchError
from tests.test_artifact_acquirer import ArtifactCase, pdf_bytes, png_bytes


class ReceiptSession:
    """A protected-session double, never the worker's submitted declaration."""
    def __init__(self, artifact, raw, *, status="available", consumed_bytes=None, consumed_count=1, elapsed=0):
        self.record = {"acquisition_id": "acq-test", "source": _source(artifact["source"]), "status": status,
            "reason_code": "source_unavailable" if status == "failed" else "available", "mime_type": "application/pdf",
            "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw), "original_path": "originals/acq-test.pdf"}
        self.raw = raw
        self.consumed_bytes = len(raw) if consumed_bytes is None else consumed_bytes
        self.consumed_count = consumed_count
        self.elapsed_seconds = elapsed
        self.run_id = "run-synthetic"
        self.attempt = 1
        self.temporary = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.temporary.name)
        self.work_dir = self.storage_root / "synthetic-worker"
        (self.storage_root / "ledger.json").write_text(json.dumps(self.ledger(), sort_keys=True))

    def ledger(self):
        return {"run_id": self.run_id, "task_id": "task-synthetic", "attempt": self.attempt,
            "cleanup_verified": True, "records": [deepcopy(self.record)]}

    def __del__(self):
        self.temporary.cleanup()

    def raise_if_failed(self):
        pass

    def lookup(self, source):
        value = _source(source)
        if value.get("document_id") == self.record["source"].get("document_id"):
            if value != self.record["source"]:
                raise HardGateError("Descriptor changed")
            return deepcopy(self.record)
        return None

    def read_original(self, record):
        if record != self.record:
            raise HardGateError("Not authoritative")
        return self.raw

    def derived_from(self, ids):
        if ids != [self.record["acquisition_id"]] or self.record["status"] != "available":
            raise HardGateError("Unknown or failed acquisition")
        return [{"acquisition_id": self.record["acquisition_id"], "source_sha256": self.record["sha256"]}]


class AcquisitionReuseTests(ArtifactCase):
    def test_closed_ledger_exact_bytes_are_bound_even_without_final_references(self):
        raw = pdf_bytes()
        session = ReceiptSession(self.remote(raw), raw)
        ledger = (session.storage_root / "ledger.json").read_bytes()
        result = self.acquire([], acquisition_session=session)
        self.assertEqual(result.report["acquisition_evidence"], {"run_id": "run-synthetic", "attempt": 1,
            "ledger_sha256": hashlib.sha256(ledger).hexdigest()})
        self.assertEqual(result.report["entries"], [])
        session.consumed_count = session.consumed_bytes = 0
        self.assertEqual(self.acquire([], acquisition_session=session).report["acquisition_evidence"],
            result.report["acquisition_evidence"])

    def test_mutated_ledger_source_or_attempt_is_rejected_before_import(self):
        raw = pdf_bytes()
        session = ReceiptSession(self.remote(raw), raw)
        path = session.storage_root / "ledger.json"
        for change in (lambda value: value.update(attempt=2),
                       lambda value: value["records"][0]["source"].update(issuer="changed")):
            value = session.ledger()
            change(value)
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(HardGateError, "ledger"):
                self.acquire([], acquisition_session=session)

    def test_confirmed_original_reuses_one_budget_slot_without_network_after_deadline(self):
        raw = pdf_bytes()
        artifact = self.remote(raw)
        session = ReceiptSession(artifact, raw, elapsed=240)
        self.settings.media.max_total_bytes = len(raw)
        self.settings.media.max_files = 1
        with patch.object(self.acquirer.pdf_fetcher, "fetch") as fetch:
            result = self.acquire([artifact], acquisition_session=session)
        fetch.assert_not_called()
        self.assertEqual(result.attachments[0]["source"], artifact["source"])
        self.assertEqual(result.file_paths[artifact["path"]].read_bytes(), raw)

    def test_failed_receipt_is_not_retried_and_retains_hold_policy(self):
        raw = pdf_bytes()
        artifact = self.remote(raw, on_failure="hold")
        session = ReceiptSession(artifact, raw, status="failed", elapsed=240)
        with patch.object(self.acquirer.pdf_fetcher, "fetch") as fetch:
            result = self.acquire([artifact], acquisition_session=session, network_allowed=True)
        fetch.assert_not_called()
        self.assertEqual(result.report["entries"][0]["reason_code"], "source_unavailable")
        self.assertTrue(result.hold)
        self.assertFalse((self.root / artifact["path"]).exists())

    def test_original_official_image_keeps_sanitized_source_and_full_url_binding(self):
        raw = png_bytes()
        artifact = {"path": "files/image.png", "role": "inline_image", "mime_type": "image/png",
            "record_ids": ["r1"], "source": {"kind": "official_image", "url": "https://example.test/card.png?revision=1"}}
        session = ReceiptSession(artifact, raw)
        session.record["mime_type"] = "image/png"
        (session.storage_root / "ledger.json").write_text(json.dumps(session.ledger(), sort_keys=True))
        with patch("researchops.engine.artifact_acquirer.PublicResearchFetcher.fetch") as fetch:
            result = self.acquire([artifact], acquisition_session=session)
        fetch.assert_not_called()
        source = result.inline_artifacts[0]["source"]
        self.assertEqual(source["url"], "https://example.test/card.png")
        self.assertEqual(source["url_sha256"], hashlib.sha256(artifact["source"]["url"].encode()).hexdigest())
        self.assertEqual(result.file_paths[artifact["path"]].read_bytes(), raw)

    def test_over_budget_acquisition_evidence_has_a_limit_diagnostic(self):
        raw = pdf_bytes()
        local = self.local()
        for limits in ({"consumed_bytes": self.settings.media.max_total_bytes + 1},
                       {"consumed_count": self.settings.media.max_files + 1}):
            session = ReceiptSession(self.remote(raw), raw, **limits)
            with self.subTest(limits=limits), self.assertRaisesRegex(HardGateError, "combined"):
                self.acquire([local], acquisition_session=session)
            expected = "total_bytes_limit" if "consumed_bytes" in limits else "artifact_limit_exceeded"
            self.assertEqual(self.acquirer.last_report["entries"][0]["reason_code"], expected)

    def test_undeclared_acquisition_and_local_derivative_share_byte_and_file_limits(self):
        raw = pdf_bytes()
        session = ReceiptSession(self.remote(raw), raw)
        local = self.local(b"derived", derived_from=["acq-test"])
        self.settings.media.max_files = 1
        with self.assertRaisesRegex(HardGateError, "combined file limit"):
            self.acquire([local], acquisition_session=session)
        self.settings.media.max_files = 2
        self.settings.media.max_total_bytes = len(raw) + len(b"derived") - 1
        with self.assertRaisesRegex(HardGateError, "combined raw byte limit"):
            self.acquire([local], acquisition_session=session)
        self.settings.media.max_total_bytes += 1
        result = self.acquire([local], acquisition_session=session)
        refs = [{"acquisition_id": "acq-test", "source_sha256": hashlib.sha256(raw).hexdigest()}]
        self.assertEqual(result.attachments[0]["derived_from"], refs)
        self.assertEqual(result.report["entries"][0]["derived_from"], refs)

    def test_failed_intermediate_download_still_reserves_bytes_and_count(self):
        raw = pdf_bytes()
        session = ReceiptSession(self.remote(raw), raw, status="failed", consumed_bytes=30)
        later = self.remote(raw, "b")
        self.settings.media.max_files = 1
        with patch.object(self.acquirer.pdf_fetcher, "fetch") as fetch:
            result = self.acquire([later], acquisition_session=session, network_allowed=True)
        fetch.assert_not_called()
        self.assertEqual(result.report["entries"][0]["reason_code"], "artifact_limit_exceeded")
        self.settings.media.max_files = 2
        self.settings.media.max_total_bytes = len(raw) + 29
        with patch.object(self.acquirer.pdf_fetcher, "fetch") as fetch:
            result = self.acquire([later], acquisition_session=session, network_allowed=True)
        fetch.assert_not_called()
        self.assertEqual(result.report["entries"][0]["reason_code"], "total_bytes_limit")

    def test_new_download_gets_only_remaining_byte_and_time_budget(self):
        raw = pdf_bytes()
        session = ReceiptSession(self.remote(raw), raw, status="failed", consumed_bytes=30, elapsed=115)
        self.settings.media.max_total_bytes = len(raw) + 30
        self.provider("http://127.0.0.1:18015")
        with patch("researchops.engine.artifact_acquirer.time.monotonic", side_effect=[100, 101]), \
             patch.object(self.acquirer.pdf_fetcher, "fetch", return_value=FetchResult(200, "", {"content-type": "application/pdf"}, raw, {})) as fetch:
            result = self.acquire([self.remote(raw, "b")], acquisition_session=session, network_allowed=True)
        self.assertEqual(len(result.attachments), 1)
        self.assertEqual(fetch.call_args.kwargs["max_bytes"], len(raw))
        self.assertEqual(fetch.call_args.kwargs["timeout_seconds"], 4)

    def test_postphase_unknown_partial_failure_reserves_the_pdf_transfer_bound(self):
        raw = pdf_bytes()
        self.provider("http://127.0.0.1:18015")
        self.settings.media.max_total_bytes = 2 * len(raw) - 1
        for code in ("timeout", "transport_failed", "worker_failed"):
            with self.subTest(code=code), patch.object(self.acquirer.pdf_fetcher, "fetch",
                    side_effect=ResearchFetchError(code, {"body_bytes": 0})) as fetch:
                result = self.acquire([self.remote(raw), self.remote(raw, "b")], network_allowed=True)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.kwargs["max_bytes"], len(raw))
            self.assertEqual([entry["reason_code"] for entry in result.report["entries"]], [code, "total_bytes_limit"])

    def test_postphase_pretransport_denial_does_not_charge_an_unstarted_download(self):
        raw = pdf_bytes()
        self.provider("http://127.0.0.1:18015")
        self.settings.media.max_total_bytes = len(raw)
        with patch.object(self.acquirer.pdf_fetcher, "fetch", side_effect=[ResearchFetchError("worker_start_failed"),
                FetchResult(200, "", {"content-type": "application/pdf"}, raw, {})]) as fetch:
            result = self.acquire([self.remote(raw), self.remote(raw, "b")], network_allowed=True)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(result.attachments), 1)

    def test_unknown_failed_and_foreign_derivation_references_fail_before_network(self):
        raw = pdf_bytes()
        remote = self.remote(raw)
        session = ReceiptSession(remote, raw)
        local = self.local(derived_from=["forged-id"])
        for ids in (["forged-id"], ["acq-test", "acq-test"], [], [{"acquisition_id": "acq-test"}]):
            with self.subTest(ids=ids), patch.object(self.acquirer.pdf_fetcher, "fetch") as fetch:
                with self.assertRaises(HardGateError):
                    self.acquire([{**local, "derived_from": ids}], acquisition_session=session)
                fetch.assert_not_called()
        session.record["status"] = "failed"
        with self.assertRaises(HardGateError):
            self.acquire([{**local, "derived_from": ["acq-test"]}], acquisition_session=session)
        session.run_id = "foreign-run"
        with self.assertRaisesRegex(HardGateError, "another Run"):
            self.acquire([], acquisition_session=session)
        with self.assertRaises(HardGateError):
            self.acquire([{**local, "derived_from": ["acq-test"]}])

    def test_changed_descriptor_or_protected_bytes_never_falls_back_to_fetch(self):
        raw = pdf_bytes()
        artifact = self.remote(raw)
        session = ReceiptSession(artifact, raw)
        modified = deepcopy(artifact)
        modified["source"]["sha256"] = "0" * 64
        with patch.object(self.acquirer.pdf_fetcher, "fetch") as fetch:
            with self.assertRaises(HardGateError):
                self.acquire([modified], acquisition_session=session, network_allowed=True)
            session.raw = b"tampered"
            with self.assertRaisesRegex(HardGateError, "receipt"):
                self.acquire([artifact], acquisition_session=session, network_allowed=True)
        fetch.assert_not_called()

    def test_worker_owned_remote_path_stays_forbidden_even_with_valid_receipt(self):
        raw = pdf_bytes()
        artifact = self.remote(raw)
        session = ReceiptSession(artifact, raw)
        path = self.root / artifact["path"]
        path.parent.mkdir()
        path.write_bytes(raw)
        with self.assertRaisesRegex(HardGateError, "already contains"):
            self.acquire([artifact], acquisition_session=session)
        path.unlink()
        path.symlink_to(self.root / "nonexistent")
        with self.assertRaises(HardGateError):
            self.acquire([artifact], acquisition_session=session)
