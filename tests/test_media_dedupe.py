"""Real dedupe history and media routing through the isolated application pipeline."""

from copy import deepcopy
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import json
import unittest
from unittest.mock import patch

import yaml

from researchops.domain.models import TaskDefinition, TaskVersion
from researchops.package.loader import compute_package_hash
from researchops.package.publisher import publish_version
from tests import test_media_pipeline as pipeline_fixtures
from tests.media_fixture import PDFS, authenticated_cardrag, requested_pdfs


class MediaDedupeTests(unittest.TestCase):
    # Reuse setup/actions, without inheriting or importing another TestCase into
    # this module's namespace (unittest would execute its tests a second time).
    def setUp(self):
        pipeline_fixtures.MediaPipelineTests.setUp(self)

    execute = pipeline_fixtures.MediaPipelineTests.execute
    provider = pipeline_fixtures.MediaPipelineTests.provider
    deliver = pipeline_fixtures.MediaPipelineTests.deliver

    def seal_dedupe(self, enabled):
        source = self.app.task_repo.get_active_version("synthetic-media")
        config = deepcopy(source.definition.to_dict())
        config["state"]["dedupe"] = {"enabled": enabled,
            "key_fields": ["issuer", "product_code"], "content_fields": ["title"]}
        files = dict(source.package_files)
        files["task.yaml"] = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
        version = TaskVersion(task_id=source.task_id,
            version_hash=compute_package_hash({name: value.encode() for name, value in files.items()}),
            sealed_at=datetime.now(timezone.utc).isoformat(), definition=TaskDefinition(**config), package_files=files)
        self.app.tasks.loader.validate_package(config, files)
        publish_version(self.settings.paths.task_versions_dir, version)
        self.app.task_repo.save_version(version)
        self.app.tasks.activate_version(version.task_id, version.version_hash)

    def seed_sent(self, record_ids):
        """Commit history through SMTP success, never through a synthetic DB insert."""
        records, artifacts = self.research["records"], self.research["artifacts"]
        try:
            self.research["records"] = [deepcopy(record) for record in records if record["record_id"] in record_ids]
            self.research["artifacts"] = []
            run, _ = self.execute()
            self.assertEqual(run.status, "awaiting_receipt", run.error_message)
            self.deliver(run)
            history = self.app.state_repo.get_reported_items("synthetic-media")
            self.assertEqual(len(history), len(record_ids))
            for item in history:
                self.assertEqual(item.run_id, run.run_id)
                handoff = self.app.delivery_repo.get_handoff(item.handoff_id)
                self.assertEqual(handoff.status, "smtp_accepted")
                self.assertEqual(handoff.receipt_trust_status, "verified_local_smtp")
        finally:
            self.research["records"], self.research["artifacts"] = records, artifacts

    def evidence(self, run, archive):
        self.assertEqual(run.status, "awaiting_receipt", run.error_message)
        comp = self.app.run_repo.get_composition_input(run.run_id)
        report = json.loads((archive / "artifact-report.json").read_bytes())
        self.assertEqual(comp["artifact_report"], report)
        return comp, report, json.loads((archive / "dedupe-report.json").read_bytes())

    @staticmethod
    def pdf_parts(wire):
        message = BytesParser(policy=policy.default).parsebytes(wire)
        return [part for part in message.walk() if part.get_content_type() == "application/pdf"]

    def test_dedupe_off_keeps_previously_sent_records_and_downloads_both_pdfs(self):
        self.seal_dedupe(False)
        self.seed_sent({"product-1", "product-2"})
        with authenticated_cardrag(self.root) as state:
            self.provider(state)
            run, archive = self.execute()
            comp, report, dedupe = self.evidence(run, archive)
            self.assertFalse(dedupe["enabled"])
            self.assertEqual(dedupe["excluded_records"], [])
            self.assertEqual([r["record_id"] for r in comp["reportable_records"]], ["product-1", "product-2"])
            self.assertEqual([a["record_ids"] for a in comp["attachments"]], [["product-1"], ["product-2"]])
            self.assertEqual([e["status"] for e in report["entries"]], ["available", "available"])
            self.assertEqual(len(state["requests"]), 4)
            self.assertEqual([p.get_payload(decode=True) for p in self.pdf_parts(self.deliver(run))], list(PDFS.values()))

    def test_dedupe_on_without_verified_history_keeps_both_pdfs(self):
        self.seal_dedupe(True)
        self.assertEqual(self.app.state_repo.get_reported_items("synthetic-media"), [])
        with authenticated_cardrag(self.root) as state:
            self.provider(state)
            run, archive = self.execute()
            comp, _, dedupe = self.evidence(run, archive)
            self.assertTrue(dedupe["enabled"])
            self.assertEqual(dedupe["excluded_records"], [])
            self.assertEqual(len(comp["attachments"]), 2)
            self.assertEqual(len(state["requests"]), 4)

    def test_shared_pdf_kept_once_when_one_of_its_records_remains(self):
        self.seal_dedupe(True)
        self.seed_sent({"product-1"})
        shared = requested_pdfs()[0]
        shared["record_ids"] = ["product-1", "product-2"]
        self.research["artifacts"] = [shared]
        with authenticated_cardrag(self.root) as state:
            self.provider(state)
            run, archive = self.execute()
            comp, report, dedupe = self.evidence(run, archive)
            self.assertEqual([r["record_id"] for r in dedupe["excluded_records"]], ["product-1"])
            self.assertEqual([r["record_id"] for r in comp["reportable_records"]], ["product-2"])
            self.assertEqual(comp["attachments"][0]["record_ids"], ["product-2"])
            self.assertEqual(report["entries"][0]["requested_record_ids"], ["product-1", "product-2"])
            self.assertEqual(report["entries"][0]["record_ids"], ["product-2"])
            self.assertEqual(len(state["requests"]), 2)
            parts = self.pdf_parts(self.deliver(run))
            self.assertEqual(len(parts), 1)
            self.assertEqual(parts[0].get_payload(decode=True), PDFS["doc_first"])

    def test_all_records_excluded_make_no_source_requests_or_attachments(self):
        self.seal_dedupe(True)
        self.seed_sent({"product-1", "product-2"})
        with authenticated_cardrag(self.root) as state:
            self.provider(state)
            run, archive = self.execute()
            comp, report, dedupe = self.evidence(run, archive)
            self.assertEqual(len(dedupe["excluded_records"]), 2)
            self.assertEqual(comp["reportable_records"], [])
            self.assertEqual(comp["attachments"], [])
            self.assertEqual(state["requests"], [])
            self.assertEqual([e["status"] for e in report["entries"]], ["excluded", "excluded"])
            self.assertEqual([e["reason_code"] for e in report["entries"]], ["records_excluded", "records_excluded"])
            self.assertTrue(all(not e["include_in_compose"] for e in report["entries"]))
            self.assertEqual(len(self.app.run_repo.get_research_result(run.run_id).records), 2)
            self.assertEqual(self.pdf_parts(self.deliver(run)), [])
            self.assertFalse((archive / "attachments/doc_first.pdf").exists())

    def test_legacy_file_excluded_only_after_record_removal_and_explicit_run_file_stays(self):
        self.seal_dedupe(True)
        files = {"attachments/legacy.pdf": PDFS["doc_first"], "attachments/common.pdf": PDFS["doc_second"]}
        self.research["artifacts"] = [
            {"artifact_id": "legacy-file", "path": "attachments/legacy.pdf", "filename": "Legacy.pdf",
             "role": "attachment", "mime_type": "application/pdf"},
            {"artifact_id": "run-file", "path": "attachments/common.pdf", "filename": "Common.pdf",
             "role": "attachment", "mime_type": "application/pdf", "scope": "run"}]
        original = self.runner.execute_research
        def produce_local_files(*args, **kwargs):
            result = original(*args, **kwargs)
            for name, content in files.items():
                path = kwargs["output_dir"] / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            return result
        with patch.object(self.runner, "execute_research", side_effect=produce_local_files):
            before, archive = self.execute(dry=True)
            self.assertEqual(before.status, "succeeded", before.error_message)
            before_comp = self.app.run_repo.get_composition_input(before.run_id)
            self.assertEqual([a["artifact_id"] for a in before_comp["attachments"]], ["legacy-file", "run-file"])
            before_report = json.loads((archive / "artifact-report.json").read_bytes())
            self.assertEqual([e["status"] for e in before_report["entries"]], ["available", "available"])
            self.seed_sent({"product-1"})
            after, archive = self.execute()
        comp, report, dedupe = self.evidence(after, archive)
        self.assertEqual([r["record_id"] for r in dedupe["excluded_records"]], ["product-1"])
        self.assertEqual([a["artifact_id"] for a in comp["attachments"]], ["run-file"])
        self.assertEqual(report["entries"][0]["status"], "excluded")
        self.assertEqual(report["entries"][0]["reason_code"], "legacy_unscoped_dedupe")
        self.assertEqual(report["entries"][1]["scope"], "run")
        self.assertEqual(report["entries"][1]["status"], "available")
        self.assertEqual((archive / "research-artifacts/attachments/legacy.pdf").read_bytes(), PDFS["doc_first"])
        parts = self.pdf_parts(self.deliver(after))
        self.assertEqual([(p.get_filename(), p.get_payload(decode=True)) for p in parts], [("Common.pdf", PDFS["doc_second"])])

    def test_changed_content_with_same_product_key_keeps_its_pdf(self):
        self.seal_dedupe(True)
        self.seed_sent({"product-1", "product-2"})
        self.research["records"][0]["title"] = "Product 1 updated benefit"
        with authenticated_cardrag(self.root) as state:
            self.provider(state)
            run, archive = self.execute()
            comp, _, dedupe = self.evidence(run, archive)
            self.assertEqual([r["record_id"] for r in comp["reportable_records"]], ["product-1"])
            self.assertEqual([r["record_id"] for r in dedupe["excluded_records"]], ["product-2"])
            self.assertEqual([a["artifact_id"] for a in comp["attachments"]], ["doc_first"])
            self.assertEqual(len(state["requests"]), 2)
