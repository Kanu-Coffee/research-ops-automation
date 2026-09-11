"""Authenticate preserved acquisition receipts against sealed composition inputs."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from researchops.engine.acquisition_evidence import retain_acquisition_evidence
from researchops.errors import HardGateError, WorkspaceError
from tests.test_artifact_acquirer import pdf_bytes


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class RecordingArchive:
    def __init__(self):
        self.files = {}

    def write(self, path, raw):
        if path in self.files:
            raise AssertionError("Immutable archive file was overwritten")
        self.files[path] = raw


class AcquisitionEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.parent = self.root / "parent-compose-run"
        self.parent.mkdir()
        self.task_id = "synthetic-task"
        self.origin = "original-research-run"
        self.acquisition_id = "acq-" + "1" * 32
        self.original_name = "research-acquisition/originals/" + self.acquisition_id + ".pdf"
        self.ledger_name = "research-acquisition/ledger.json"
        self.raw = pdf_bytes("Synthetic inherited source")
        source = {"kind": "cardrag_pdf", "connection_id": "synthetic", "document_id": "doc_" + "2" * 64,
                  "issuer": "synthetic", "product_code": "figure", "sha256": digest(self.raw),
                  "size_bytes": len(self.raw)}
        self.ledger = {"schema_version": 1, "task_id": self.task_id, "run_id": self.origin,
            "attempt": 1, "fencing_token": "original-synthetic-fence", "cleanup_verified": True,
            "failure_reason": None, "consumed_count": 1, "consumed_bytes": len(self.raw),
            "elapsed_seconds": 0.1, "records": [{"acquisition_id": self.acquisition_id,
                "source": source, "status": "available", "reason_code": "available", "mime_type": "application/pdf",
                "sha256": digest(self.raw), "size_bytes": len(self.raw),
                "original_path": "originals/" + self.acquisition_id + ".pdf"}]}
        self.ledger_raw = encoded(self.ledger)
        self.composition = {"task_id": self.task_id, "run_id": self.parent.name,
            "artifact_report": {"entries": [{"path": "images/figure.png", "derived_from": [
                {"acquisition_id": self.acquisition_id, "source_sha256": digest(self.raw)}]}],
                "acquisition_evidence": {"run_id": self.origin, "attempt": 1,
                                         "ledger_sha256": digest(self.ledger_raw)}}}
        self.write(self.parent, self.ledger_name, self.ledger_raw)
        self.write(self.parent, self.original_name, self.raw)
        self.index(self.parent)

    @staticmethod
    def write(root, name, raw):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    def index(self, root):
        files = [{"relative_path": path.relative_to(root).as_posix(), "sha256": digest(path.read_bytes()),
                  "size_bytes": path.stat().st_size, "role": "evidence", "mime_type": "application/octet-stream"}
                 for path in sorted(root.rglob("*")) if path.is_file() and path.name != "artifact-manifest.json"]
        (root / "artifact-manifest.json").write_bytes(encoded({"task_id": self.task_id,
            "run_id": root.name, "artifacts": files}))

    def copy(self, *, parent=None, composition=None):
        archive = RecordingArchive()
        retain_acquisition_evidence(archive, parent or self.parent, composition or self.composition)
        return archive

    def assert_rejected(self, *, composition=None):
        archive = RecordingArchive()
        with self.assertRaises((HardGateError, WorkspaceError)):
            retain_acquisition_evidence(archive, self.parent, composition or self.composition)
        self.assertEqual(archive.files, {}, "Reject before writing inherited receipt files")

    def test_bound_parent_receipt_and_original_are_copied_exactly(self):
        archive = self.copy()
        self.assertEqual(archive.files, {self.ledger_name: self.ledger_raw, self.original_name: self.raw})

    def test_legacy_input_without_acquisition_remains_a_noop(self):
        parent = self.root / "legacy"
        parent.mkdir()
        archive = self.copy(parent=parent, composition={"task_id": self.task_id,
            "run_id": parent.name, "artifact_report": {"entries": []}})
        self.assertEqual(archive.files, {})

    def test_repeated_recompose_retains_the_original_producer_and_bytes(self):
        first = self.copy()
        parent = self.root / "second-compose-run"
        parent.mkdir()
        for name, raw in first.files.items():
            self.write(parent, name, raw)
        self.index(parent)
        composition = copy.deepcopy(self.composition)
        composition["run_id"] = parent.name
        second = self.copy(parent=parent, composition=composition)
        self.assertEqual(second.files, first.files)
        self.assertEqual(json.loads(second.files[self.ledger_name])["run_id"], self.origin)

    def test_mutated_ledger_is_rejected_even_if_local_archive_index_is_updated(self):
        for field in ("run_id", "source", "sha256"):
            with self.subTest(field=field):
                altered = copy.deepcopy(self.ledger)
                if field == "run_id":
                    altered["run_id"] = "unrelated-research-run"
                elif field == "source":
                    altered["records"][0]["source"]["document_id"] = "doc_" + "3" * 64
                else:
                    altered["records"][0]["sha256"] = "4" * 64
                self.write(self.parent, self.ledger_name, encoded(altered))
                self.index(self.parent)
                self.assert_rejected()

    def test_binding_producer_and_attempt_must_match_the_hashed_ledger(self):
        for field, value in (("run_id", "unrelated-research-run"), ("attempt", 2)):
            with self.subTest(field=field):
                composition = copy.deepcopy(self.composition)
                composition["artifact_report"]["acquisition_evidence"][field] = value
                self.assert_rejected(composition=composition)

    def test_derivative_requires_the_sealed_ledger_binding(self):
        composition = copy.deepcopy(self.composition)
        del composition["artifact_report"]["acquisition_evidence"]
        self.assert_rejected(composition=composition)

    def test_bound_ledger_is_required_even_without_final_derivatives(self):
        composition = copy.deepcopy(self.composition)
        composition["artifact_report"]["entries"] = []
        (self.parent / self.ledger_name).unlink()
        self.assert_rejected(composition=composition)

    def test_missing_ledger_with_derivative_is_rejected(self):
        (self.parent / self.ledger_name).unlink()
        self.assert_rejected()

    def test_missing_index_or_ledger_index_entry_is_rejected(self):
        index = self.parent / "artifact-manifest.json"
        index.unlink()
        self.assert_rejected()
        self.index(self.parent)
        document = json.loads(index.read_bytes())
        document["artifacts"] = [item for item in document["artifacts"] if item["relative_path"] != self.ledger_name]
        index.write_bytes(encoded(document))
        self.assert_rejected()

    def test_index_hash_and_size_must_match_the_bound_ledger(self):
        index = self.parent / "artifact-manifest.json"
        for field, value in (("sha256", "9" * 64), ("size_bytes", len(self.ledger_raw) + 1)):
            with self.subTest(field=field):
                self.index(self.parent)
                document = json.loads(index.read_bytes())
                next(item for item in document["artifacts"] if item["relative_path"] == self.ledger_name)[field] = value
                index.write_bytes(encoded(document))
                self.assert_rejected()

    def test_changed_original_is_rejected_even_with_a_fresh_archive_index(self):
        self.write(self.parent, self.original_name, pdf_bytes("A different source"))
        self.index(self.parent)
        self.assert_rejected()

    def test_unavailable_or_different_source_provenance_is_rejected(self):
        for field, value in (("acquisition_id", "acq-" + "f" * 32), ("source_sha256", "f" * 64)):
            with self.subTest(field=field):
                composition = copy.deepcopy(self.composition)
                composition["artifact_report"]["entries"][0]["derived_from"][0][field] = value
                self.assert_rejected(composition=composition)


if __name__ == "__main__":
    unittest.main()
