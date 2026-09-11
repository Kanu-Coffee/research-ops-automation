"""Unit tests for TechnicalGateValidator and ContentDeduplicator."""

import json
import shutil
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from tests.support import SOURCE_ROOT
from researchops.domain.models import ReportedItem, TaskDefinition
from researchops.engine.dedupe import ContentDeduplicator, derive_entity_key, derive_content_fingerprint
from researchops.engine.technical_gate import TechnicalGateValidator
from researchops.errors import HardGateError
from researchops.storage.db import Database
from researchops.storage.repositories import StateRepository


class TestGateAndDedupe(unittest.TestCase):
    def setUp(self):
        self.gate = TechnicalGateValidator(SOURCE_ROOT / "schemas")
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test.db"
        self.db = Database(self.db_path)
        self.db.init_schema()
        self.state_repo = StateRepository(self.db)
        self.deduplicator = ContentDeduplicator(self.state_repo)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _sample_task_def(self, dedupe_enabled=False):
        return TaskDefinition(
            id="test-task",
            name="Test Task",
            enabled=True,
            workspace={"mode": "persistent_task"},
            runner={"type": "fake"},
            instructions={"research_files": ["task.md"], "compose_files": ["task.md"]},
            output={"research_schema": "output.schema.json"},
            delivery={"mode": "dry_run"},
            state={
                "dedupe": {
                    "enabled": dedupe_enabled,
                    "key_fields": ["publisher", "product_name"],
                    "content_fields": ["release_version", "change_summary"]
                }
            }
        )

    def test_technical_gate_valid_and_normalization(self):
        task_def = self._sample_task_def()
        valid_res = {
            "status": "success",
            "summary": "1 synthetic release found",
            "records": [
                {
                    "publisher": "Atlas Studio",
                    "product_name": "Atlas Notes",
                    "release_version": "10000",
                    "change_summary": "Text export"
                }
            ],
            "coverage": {
                "complete": True,
                "expected_target_count": 1,
                "completed_target_count": 1,
                "issues": []
            }
        }
        res, warns = self.gate.validate_research_output(json.dumps(valid_res), task_def)
        self.assertEqual(res.status, "success")
        self.assertEqual(len(res.records), 1)
        # Record ID should be derived deterministically without dropping
        self.assertTrue(res.records[0]["record_id"].startswith("record-1-"))
        again, _ = self.gate.validate_research_output(json.dumps(valid_res), task_def)
        self.assertEqual(res.records[0]["record_id"], again.records[0]["record_id"])

    def test_technical_gate_invalid_json_and_schema_failure(self):
        task_def = self._sample_task_def()
        with self.assertRaises(HardGateError):
            self.gate.validate_research_output("not valid json {", task_def)

        # Missing required status field
        invalid_schema = {
            "summary": "Missing status",
            "records": []
        }
        with self.assertRaises(HardGateError):
            self.gate.validate_research_output(json.dumps(invalid_schema), task_def)

    def test_dedupe_disabled_preserves_all_records(self):
        task_def = self._sample_task_def(dedupe_enabled=False)
        records = [
            {"publisher": "PublisherA", "product_name": "Prod1", "release_version": "1000"},
            {"publisher": "PublisherB", "product_name": "Prod2", "release_version": "2000"}
        ]
        reportable, excluded, warns = self.deduplicator.process_records(task_def, records)
        self.assertEqual(len(reportable), 2)
        self.assertEqual(len(excluded), 0)

    def test_exact_unchanged_deduplication(self):
        task_def = self._sample_task_def(dedupe_enabled=True)
        rec = {
            "record_id": "publishera::prod1",
            "publisher": "PublisherA",
            "product_name": "Prod1",
            "release_version": "1000",
            "change_summary": "Offline search"
        }
        key = derive_entity_key(rec, ["publisher", "product_name"])
        fp = derive_content_fingerprint(rec, ["release_version", "change_summary"])

        # Record as verified-sent item in state DB
        reported = patch.object(self.state_repo, "is_reported_item_unchanged",
            side_effect=lambda **values: values == {"task_id": "test-task", "entity_key": key, "content_fingerprint": fp})
        reported.start()
        self.addCleanup(reported.stop)

        # 1. Exact unchanged record should be excluded
        reportable, excluded, warns = self.deduplicator.process_records(task_def, [rec])
        self.assertEqual(len(reportable), 0)
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["_dedupe_status"], "already_reported_exact")

        # 2. Record with updated content should be preserved as reportable
        updated_rec = dict(rec)
        updated_rec["release_version"] = "15000"  # Changed content!
        reportable2, excluded2, warns2 = self.deduplicator.process_records(task_def, [updated_rec])
        self.assertEqual(len(reportable2), 1)
        self.assertEqual(len(excluded2), 0)
        self.assertEqual(reportable2[0], updated_rec)


if __name__ == "__main__":
    unittest.main()
