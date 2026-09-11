"""Acquisition through immutable Compose and SMTP, without external delivery."""

from copy import deepcopy
from dataclasses import replace
from email import policy
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.delivery_fixtures import smtp_server, smtp_message_bytes

from researchops.config import MediaProviderConfig
from researchops.delivery.smtp_config import BuiltinDeliveryConfig, SmtpSettings, save_delivery_config
from researchops.engine.archive import RunArchive, canonical_json
from researchops.runners.fake import FakeRunner
from researchops.runners.research_fetch import FetchResult, ResearchFetchError
from researchops.services.application import ApplicationService
from tests.media_fixture import PDFS, SYNTHETIC_TOKEN, authenticated_cardrag, requested_pdfs
from tests.support import isolated_settings
from tests import test_message_validator as message_fixtures


class MediaPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = isolated_settings(self.root / "runtime")
        self.settings.environment = "production"
        self.settings.delivery.global_handoff_kill_switch = False
        save_delivery_config(BuiltinDeliveryConfig(enabled=True, auto_dispatch=False,
            smtp=SmtpSettings(host="smtp.example.test", sender_email="sender@example.test"),
            recipient_groups={"synthetic-team": ["recipient@example.test"]}),
            self.settings.paths.delivery_config_file)
        self.research = {"status": "success", "summary": "Synthetic media verification",
            "records": [{"record_id": f"product-{i}", "title": f"Product {i}",
                         "issuer": "synthetic", "product_code": str(i)} for i in (1, 2)],
            "coverage": {"complete": True, "expected_target_count": 2, "completed_target_count": 2, "issues": []},
            "warnings": [], "artifacts": requested_pdfs()}
        self.runner = FakeRunner(custom_research_result=self.research)
        self.app = ApplicationService(self.settings, custom_runner=self.runner)
        self.app.tasks.create_production_task(task_id="synthetic-media", name="Synthetic media",
            instructions="Attach only explicitly requested files. Select synthetic-team. Never send from the worker.",
            runner_type="codex_exec", recipient_group_id="synthetic-team", schedule_enabled=False)

    def execute(self, *, dry=False):
        run = self.app.runs.enqueue_run("synthetic-media", force_dry_run=dry)
        result = self.app.runs.execute_run(run.run_id)
        archive = self.settings.paths.run_archive_dir / run.task_id / run.run_id
        return result, archive

    def provider(self, state):
        self.settings.media.providers = {"cardrag": MediaProviderConfig(state["base_url"], state["token"])}

    def deliver(self, run):
        smtp = smtp_server()
        handoff = self.app.delivery_repo.get_handoff_for_run(run.run_id)
        with patch("smtplib.SMTP", return_value=smtp), patch("smtplib.SMTP_SSL") as ssl:
            ok, _, message = self.app.smtp_dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok, message)
        ssl.assert_not_called()
        return smtp_message_bytes(smtp)

    def test_two_authenticated_pdfs_and_png_preserve_bytes_and_product_links_in_smtp(self):
        png = message_fixtures.TestMessageValidator._png()
        self.research["artifacts"].append({"artifact_id": "product-image", "path": "inline/상품 이미지.png",
            "role": "inline_image", "mime_type": "image/png", "record_ids": ["product-2"],
            "source": {"kind": "official_image", "url": "https://www.shinhancard.com/synthetic.png"}})
        image_result = FetchResult(200, "https://www.shinhancard.com/synthetic.png", {"content-type": "image/png"}, png, {})
        with authenticated_cardrag(self.root) as state:
            self.provider(state)
            with patch("researchops.engine.artifact_acquirer.PublicResearchFetcher.fetch", return_value=image_result) as fetch:
                run, archive = self.execute()
            self.assertEqual(run.status, "awaiting_receipt", run.error_message)
            fetch.assert_called_once()
            self.assertEqual(len(state["requests"]), 4)
            self.assertTrue(all(item["authenticated"] for item in state["requests"]))
            self.assertEqual([r["path"] for r in state["requests"]], [
                "/resources/documents/doc_" + "1" * 64, "/sources/doc_" + "1" * 64 + "/pdf",
                "/resources/documents/doc_" + "2" * 64, "/sources/doc_" + "2" * 64 + "/pdf"])
            wire = self.deliver(run)
            message = BytesParser(policy=policy.default).parsebytes(wire)
            pdfs = [part for part in message.walk() if part.get_content_type() == "application/pdf"]
            self.assertEqual([part.get_payload(decode=True) for part in pdfs], list(PDFS.values()))
            self.assertEqual([part.get_filename() for part in pdfs], ["상품 1 약관.pdf", "상품 2 약관.pdf"])
            image = next(part for part in message.walk() if part.get_content_type() == "image/png")
            self.assertEqual(image.get_payload(decode=True), png)
            comp = self.app.run_repo.get_composition_input(run.run_id)
            self.assertEqual(comp["schema_version"], 4)
            self.assertEqual(comp["inline_artifacts"][0]["record_ids"], ["product-2"])
            self.assertEqual(str(image["Content-ID"]), "<" + comp["inline_artifacts"][0]["cid"] + ">")
            for kind, filename in (("html", "email.html"), ("plain", "email.txt")):
                self.assertEqual(message.get_body((kind,)).get_payload(decode=True), (archive / filename).read_bytes())
            for path in archive.rglob("*"):
                if path.is_file():
                    self.assertNotIn(SYNTHETIC_TOKEN.encode(), path.read_bytes(), str(path))
            self.assertNotIn(SYNTHETIC_TOKEN.encode(), wire)
            report = json.loads((archive / "artifact-report.json").read_bytes())
            self.assertEqual([e["status"] for e in report["entries"]], ["available"] * 3)

    def test_failed_request_continues_without_injecting_missing_notice_into_mail(self):
        with authenticated_cardrag(self.root) as state:
            self.provider(state)
            state["statuses"]["/sources/doc_" + "2" * 64 + "/pdf"] = 401
            run, archive = self.execute()
            self.assertEqual(run.status, "awaiting_receipt", run.error_message)
            comp = self.app.run_repo.get_composition_input(run.run_id)
            self.assertEqual(len(comp["attachments"]), 1)
            self.assertEqual(comp["result"]["warnings"], [])
            self.assertEqual(comp["artifact_report"]["entries"][1]["reason_code"], "source_unauthorized")
            self.assertFalse(comp["artifact_report"]["entries"][1]["announce_missing"])
            self.assertNotIn(b"source_unauthorized", (archive / "email.html").read_bytes())
            self.deliver(run)

    def test_explicit_hold_retains_completed_pdf_and_never_composes(self):
        self.research["artifacts"][1].update(on_failure="hold", announce_missing=True)
        with authenticated_cardrag(self.root) as state:
            self.provider(state)
            state["statuses"]["/sources/doc_" + "2" * 64 + "/pdf"] = 403
            with patch.object(self.runner, "execute_compose", wraps=self.runner.execute_compose) as compose:
                run, archive = self.execute()
            self.assertEqual(run.status, "needs_attention", run.error_message)
            compose.assert_not_called()
            self.assertEqual((archive / "attachments/doc_first.pdf").read_bytes(), PDFS["doc_first"])
            report = json.loads((archive / "artifact-report.json").read_bytes())
            self.assertTrue(report["entries"][1]["announce_missing"])
            self.assertIsNone(self.app.delivery_repo.get_handoff_for_run(run.run_id))

    def test_explicit_notice_is_authored_by_compose_and_preserved_in_both_bodies(self):
        self.research["artifacts"][1]["announce_missing"] = True
        with authenticated_cardrag(self.root) as state:
            self.provider(state)
            state["statuses"]["/sources/doc_" + "2" * 64 + "/pdf"] = 404
            run, archive = self.execute()
            self.assertEqual(run.status, "awaiting_receipt", run.error_message)
            wire = self.deliver(run)
            mime = BytesParser(policy=policy.default).parsebytes(wire)
            notice = "요청한 파일을 확보하지 못했습니다: attachments/doc_second.pdf".encode()
            for kind, name in (("html", "email.html"), ("plain", "email.txt")):
                body = mime.get_body((kind,)).get_payload(decode=True)
                self.assertIn(notice, body)
                self.assertEqual(body, (archive / name).read_bytes())

    def test_no_request_makes_no_fetch_and_no_file_report(self):
        self.research["artifacts"] = []
        with patch("researchops.engine.artifact_acquirer.CardRAGPDFFetcher.fetch") as pdf:
            run, archive = self.execute(dry=True)
        self.assertEqual(run.status, "succeeded", run.error_message)
        pdf.assert_not_called()
        self.assertFalse((archive / "artifact-report.json").exists())
        self.assertEqual(self.app.run_repo.get_composition_input(run.run_id)["schema_version"], 2)

    def test_compose_only_reuses_archived_files_and_rebinds_exact_revision(self):
        with authenticated_cardrag(self.root) as state:
            self.provider(state)
            parent, archive = self.execute(dry=True)
            self.assertEqual(parent.status, "succeeded", parent.error_message)
            with patch("researchops.engine.artifact_acquirer.ArtifactAcquirer.acquire") as acquire:
                child = self.app.runs.compose_only(parent.run_id)
                child = self.app.runs.execute_run(child.run_id)
            acquire.assert_not_called()
            self.assertEqual(child.status, "succeeded", child.error_message)
            comp = self.app.run_repo.get_composition_input(child.run_id, 2)
            self.assertEqual(comp["artifact_report"]["run_id"], child.run_id)
            self.assertEqual(comp["artifact_report"]["derived_from"]["run_id"], parent.run_id)
            child_archive = self.settings.paths.run_archive_dir / child.task_id / child.run_id
            for artifact in comp["attachments"]:
                self.assertEqual((child_archive / artifact["path"]).read_bytes(), (archive / artifact["path"]).read_bytes())

    def test_tampered_parent_artifact_refuses_compose_only_before_worker(self):
        with authenticated_cardrag(self.root) as state:
            self.provider(state)
            parent, archive = self.execute(dry=True)
            self.assertEqual(parent.status, "succeeded", parent.error_message)
            (archive / "attachments/doc_first.pdf").write_bytes(b"replaced")
            with patch.object(self.runner, "execute_compose") as compose:
                child = self.app.runs.compose_only(parent.run_id)
                result = self.app.runs.execute_run(child.run_id)
            compose.assert_not_called()
            self.assertEqual(result.status, "failed")
            self.assertIn("artifact changed", result.error_message)

    def test_report_disk_failure_still_releases_workspace_and_blocks_handoff(self):
        original = RunArchive.json
        def reject_report(archive, name, value):
            if name == "artifact-report.json":
                raise OSError("synthetic report fsync failure")
            return original(archive, name, value)
        with authenticated_cardrag(self.root) as state:
            self.provider(state)
            run = self.app.runs.enqueue_run("synthetic-media")
            with patch.object(RunArchive, "json", new=reject_report), self.assertRaises(OSError):
                self.app.runs.execute_run(run.run_id)
            self.assertEqual(self.app.run_repo.get_run(run.run_id).status, "needs_attention")
            self.assertFalse(self.app.workspace_mgr.is_locked(run.task_id)[0])
            self.assertIsNone(self.app.run_repo.get_lease(run.run_id))
            self.assertEqual(self.app.delivery_repo.get_handoff_for_run(run.run_id).status, "failed")

    def test_unconfirmed_acquisition_cleanup_keeps_claim_for_operator_attention(self):
        with patch("researchops.engine.artifact_acquirer.ArtifactAcquirer.acquire",
                   side_effect=ResearchFetchError("worker_cleanup_failed")):
            run, archive = self.execute(dry=True)
        self.assertEqual(run.status, "needs_attention", run.error_message)
        self.assertTrue(self.app.workspace_mgr.is_locked(run.task_id)[0])
        self.assertIsNotNone(self.app.run_repo.get_lease(run.run_id))
        self.assertIsNone(self.app.delivery_repo.get_handoff_for_run(run.run_id))
        validation = json.loads((archive / "validation-report.json").read_bytes())
        self.assertFalse(validation["cleanup_verified"])
