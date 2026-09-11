"""Local run evidence survives an empty Research result without entering mail."""

from email import policy
from email.parser import BytesParser
import hashlib
import json
import unittest
from unittest.mock import patch

from researchops.delivery.artifact_integrity import validate_artifact_contract
from researchops.domain.models import CompositionInput
from tests import test_media_pipeline as pipeline_fixtures


class RunEvidencePipelineTests(unittest.TestCase):
    execute = pipeline_fixtures.MediaPipelineTests.execute
    deliver = pipeline_fixtures.MediaPipelineTests.deliver

    def setUp(self):
        pipeline_fixtures.MediaPipelineTests.setUp(self)
        self.files = {
            "state/sent-products.snapshot.json": b'{"sent": [], "kind": "snapshot"}\n',
            "state/sent-products.delta.json": '{"sent": [], "kind": "변경 없음"}\n'.encode(),
        }
        self.research.update(status="no_updates", records=[],
            coverage={"complete": True, "expected_target_count": 0,
                      "completed_target_count": 0, "issues": []},
            artifacts=[{"artifact_id": f"history-{i}", "path": name,
                "scope": "run", "role": "evidence", "mime_type": "application/json",
                "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
                for i, (name, raw) in enumerate(self.files.items())])
        original = self.runner.execute_research

        def research_with_local_history(*args, **kwargs):
            result = original(*args, **kwargs)
            root = result.output_files["result"].parent
            for name, raw in self.files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            return result

        self.runner.execute_research = research_with_local_history

    def assert_archived_evidence(self, run, archive, revision=1):
        value = self.app.run_repo.get_composition_input(run.run_id, revision)
        self.assertEqual(value["schema_version"], 4)
        self.assertEqual(value["result"]["status"], "no_updates")
        self.assertEqual(value["reportable_records"], [])
        self.assertEqual(value["attachments"], [])
        self.assertEqual(value["inline_artifacts"], [])
        validate_artifact_contract(CompositionInput(**value))
        report = json.loads((archive / "artifact-report.json").read_bytes())
        self.assertEqual(value["artifact_report"], report)
        self.assertEqual(len(report["entries"]), 2)
        index = json.loads((archive / "artifact-manifest.json").read_bytes())
        archived = {item["relative_path"]: item for item in index["artifacts"]}
        for entry in report["entries"]:
            self.assertEqual((entry["scope"], entry["requested_scope"], entry["role"]),
                             ("run", "run", "evidence"))
            self.assertEqual((entry["status"], entry["reason_code"]), ("available", "evidence_only"))
            self.assertEqual(entry["requested_record_ids"], [])
            self.assertEqual(entry["record_ids"], [])
            self.assertIsNone(entry["source"])
            self.assertFalse(entry["include_in_compose"])
            name = "research-artifacts/" + entry["path"]
            raw = self.files[entry["path"]]
            self.assertEqual((archive / name).read_bytes(), raw)
            self.assertEqual(archived[name]["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(entry["sha256"], archived[name]["sha256"])
            self.assertEqual(entry["size_bytes"], len(raw))
        return value

    def test_no_updates_two_json_evidence_reach_v4_archive_and_zero_mime_attachments(self):
        with patch("researchops.engine.artifact_acquirer.CardRAGPDFFetcher.fetch") as pdf, \
                patch("researchops.engine.artifact_acquirer.PublicResearchFetcher.fetch") as image:
            run, archive = self.execute()
        self.assertEqual(run.status, "awaiting_receipt", run.error_message)
        pdf.assert_not_called()
        image.assert_not_called()
        self.assert_archived_evidence(run, archive)
        request = self.app.delivery_repo.get_handoff_for_run(run.run_id).delivery_request
        self.assertEqual(request["attachments"], [])
        wire = self.deliver(run)
        mime = BytesParser(policy=policy.default).parsebytes(wire)
        self.assertEqual(list(mime.iter_attachments()), [])
        self.assertFalse(any(part.get_content_type() == "application/json" for part in mime.walk()))
        for name, raw in self.files.items():
            self.assertNotIn(name.encode(), wire)
            self.assertNotIn(raw, wire)

    def test_compose_only_preserves_local_evidence_and_rejects_later_tampering(self):
        parent, archive = self.execute(dry=True)
        self.assertEqual(parent.status, "succeeded", parent.error_message)
        self.assert_archived_evidence(parent, archive)
        with patch("researchops.engine.artifact_acquirer.ArtifactAcquirer.acquire") as acquire:
            child = self.app.runs.compose_only(parent.run_id)
            child = self.app.runs.execute_run(child.run_id)
        acquire.assert_not_called()
        self.assertEqual(child.status, "succeeded", child.error_message)
        child_archive = self.settings.paths.run_archive_dir / child.task_id / child.run_id
        value = self.assert_archived_evidence(child, child_archive, revision=2)
        self.assertEqual(value["artifact_report"]["derived_from"]["run_id"], parent.run_id)
        (child_archive / "research-artifacts/state/sent-products.delta.json").write_bytes(b"{}")
        with patch.object(self.runner, "execute_compose") as compose:
            rejected = self.app.runs.compose_only(child.run_id)
            rejected = self.app.runs.execute_run(rejected.run_id)
        self.assertEqual(rejected.status, "failed")
        self.assertIn("Confirmed artifact changed", rejected.error_message)
        compose.assert_not_called()
        self.assertIsNone(self.app.delivery_repo.get_handoff_for_run(rejected.run_id))
