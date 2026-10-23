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

    def _alias_task(self):
        task = self._sample_task_def(dedupe_enabled=True)
        task.state['dedupe'] = {
            'enabled': True, 'key_fields': ['issuer', 'product_code'],
            'content_fields': ['issuer', 'product_code'],
            'entity_aliases': [{'identities': [
                {'issuer': 'issuer-a', 'product_code': 'catalog-123'},
                {'issuer': 'issuer-a', 'product_code': 'type-xyz'}],
                'evidence': 'Official catalog links both identifiers to one product'}]}
        return task


    def test_alias_matches_both_directions_against_unmodified_history(self):
        task = self._alias_task()
        cfg = task.state['dedupe']
        for previous, incoming in [('catalog-123', 'type-xyz'), ('type-xyz', 'catalog-123')]:
            old = {'issuer': 'issuer-a', 'product_code': previous}
            expected = dict(task_id=task.id, entity_key=derive_entity_key(old, cfg['key_fields']),
                content_fingerprint=derive_content_fingerprint(old, cfg['content_fields']))
            with patch.object(self.state_repo, 'is_reported_item_unchanged', side_effect=lambda **v: v == expected):
                reported, excluded, warnings = self.deduplicator.process_records(task, [
                    dict(old, product_code=incoming, record_id='new-record')])
            self.assertFalse(reported)
            self.assertFalse(warnings)
            self.assertEqual(excluded[0]['_dedupe_match_basis'], 'configured_identity_alias')
            self.assertEqual(excluded[0]['_matched_entity_key'], expected['entity_key'])


    def test_alias_does_not_merge_other_issuer_or_unknown_code_or_similar_name(self):
        task = self._alias_task()
        records = [{'issuer': 'issuer-b', 'product_code': 'type-xyz', 'product_name': 'same name'},
                   {'issuer': 'issuer-a', 'product_code': 'unknown', 'product_name': 'same name'}]
        old = {'issuer': 'issuer-a', 'product_code': 'catalog-123'}
        cfg = task.state['dedupe']
        expected = dict(task_id=task.id, entity_key=derive_entity_key(old, cfg['key_fields']),
            content_fingerprint=derive_content_fingerprint(old, cfg['content_fields']))
        with patch.object(self.state_repo, 'is_reported_item_unchanged', side_effect=lambda **v: v == expected):
            reported, excluded, _ = self.deduplicator.process_records(task, records)
        self.assertEqual(reported, records)
        self.assertFalse(excluded)


    def test_alias_preserves_changed_content_and_requires_verified_history(self):
        task = self._alias_task()
        task.state['dedupe']['content_fields'].append('benefit')
        cfg = task.state['dedupe']
        old = {'issuer': 'issuer-a', 'product_code': 'catalog-123', 'benefit': 'old'}
        incoming = dict(old, product_code='type-xyz', benefit='new')
        expected = dict(task_id=task.id, entity_key=derive_entity_key(old, cfg['key_fields']),
            content_fingerprint=derive_content_fingerprint(old, cfg['content_fields']))
        with patch.object(self.state_repo, 'is_reported_item_unchanged', side_effect=lambda **v: v == expected):
            reported, excluded, _ = self.deduplicator.process_records(task, [incoming])
        self.assertEqual(reported, [incoming]); self.assertFalse(excluded)
        # No verified SMTP history: aliases cannot suppress either identity.
        with patch.object(self.state_repo, 'is_reported_item_unchanged', return_value=False):
            reported, excluded, _ = self.deduplicator.process_records(task, [dict(old, product_code='type-xyz')])
        self.assertEqual(len(reported), 1); self.assertFalse(excluded)


    def test_alias_rejects_overlapping_partial_and_unbounded_declarations(self):
        from copy import deepcopy
        from researchops.engine.dedupe import validate_entity_aliases
        from researchops.errors import ValidationError
        good = self._alias_task().state['dedupe']
        for kind in ('overlap', 'partial', 'no-evidence', 'too-many'):
            cfg = deepcopy(good)
            if kind == 'overlap': cfg['entity_aliases'] *= 2
            if kind == 'partial': cfg['entity_aliases'][0]['identities'][0].pop('issuer')
            if kind == 'no-evidence': cfg['entity_aliases'][0]['evidence'] = ' '
            if kind == 'too-many': cfg['entity_aliases'] *= 129
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                validate_entity_aliases(cfg)



if __name__ == "__main__":
    unittest.main()
