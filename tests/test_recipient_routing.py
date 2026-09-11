"""Catalog name routing binds immutable evidence and never sends real mail."""

from dataclasses import replace
from email import policy
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops.delivery.policy import require_approval
from researchops.delivery.recipient_routing import (
    catalog_recipient_snapshot, require_stored_selection, resolve_recipient)
from researchops.delivery.smtp_config import save_delivery_config
from researchops.domain.models import CompositionInput, ResearchResult
from researchops.engine.archive import canonical_json
from researchops.engine.composition_input import CompositionInputBuilder
from researchops.engine.message_validator import MessageValidator
from researchops.errors import DeliveryError, ValidationError
from researchops.services.catalog_service import CatalogService
from tests.delivery_fixtures import DeliveryFixture, smtp_server, smtp_message_bytes


class TestCatalogRouting(DeliveryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.settings.environment = "production"
        self.catalog = CatalogService(self.settings, self.db)
        self.catalog.ensure("recipient_group", "test-team", "상품 담당")
        self.catalog.ensure("recipient_group", "researchops-admins", "운영 담당")
        self.task = replace(self.task, delivery={"mode": "handoff", "recipient_routing_mode": "catalog_name"})
        self.version = replace(self.version, version_hash="c" * 64, definition=self.task)
        self.task_repo.save_version(self.version)
        self.task_repo.set_active_version(self.task.id, self.version.version_hash, delivery_mode="handoff")
        old_archive = self.settings.paths.run_archive_dir / self.task.id / self.run.run_id
        manifest = json.loads((old_archive / "run-manifest.json").read_text())
        self.run = self.make_run("catalog-run")
        self.builder = CompositionInputBuilder(self.settings.paths.schemas_dir)
        self.research = ResearchResult("success", "합성 상품 조사", [{"record_id": "c1"}],
            {"complete": True, "expected_target_count": 1, "completed_target_count": 1, "issues": []}, [], "{}")
        self.comp_input = self.build()
        self.raw = {"recipient_group_name": "  상품 담당  ", "recipient_group_reason": "상품이 있는 경우",
            "subject": "서울 합성 조사", "html_path": "email.html", "text_path": "email.txt",
            "included_record_ids": ["c1"]}
        self.html = '<html><body data-local-date="2026-09-05"><p data-record-id="c1">서울\r\n상품</p></body></html>'.encode()
        (self.stage / "email.html").write_bytes(self.html)
        (self.stage / "composition-result.json").write_bytes(canonical_json(self.raw))
        self.validator = MessageValidator(self.settings.paths.schemas_dir)
        self.result, _, _, self.hashes = self.validator.validate_composition(self.stage, self.comp_input, self.task)
        self.run_repo.save_composition_input(self.comp_input, self.hashes["recipient_resolution"]["composition_input_sha256"])
        self.run_repo.save_composition_result(self.run.run_id, self.task.id, self.result, revision=1,
            recipient_resolution=self.hashes["recipient_resolution"])
        self.archive = self.settings.paths.run_archive_dir / self.task.id / self.run.run_id
        self.archive.mkdir(parents=True)
        manifest["run_id"] = self.run.run_id
        manifest["composition"].update(self.result.to_dict())
        (self.archive / "run-manifest.json").write_bytes(canonical_json(manifest))
        (self.archive / "composition-input.json").write_bytes(canonical_json(self.comp_input.to_dict()))
        (self.archive / "composition-result.json").write_bytes(canonical_json(self.raw))
        (self.archive / "recipient-resolution.json").write_bytes(canonical_json(self.hashes["recipient_resolution"]))

    def build(self, *, task=None, run=None, research=None):
        research = research or self.research
        return self.builder.build_composition_input(task or self.task, run or self.run,
            research, research.records, recipient_groups=catalog_recipient_snapshot(self.db, self.config))

    def approve(self, revision=1, group="test-team"):
        require_approval(self.settings, self.db, self.config, self.task.id,
            self.version.version_hash, group, run_id=self.run.run_id, message_revision=revision)

    @staticmethod
    def smtp_server():
        server = smtp_server()
        return server

    def test_catalog_snapshot_contains_only_public_identity_and_names(self):
        self.assertEqual(self.comp_input.schema_version, 3)
        self.assertEqual(self.comp_input.recipient_groups, [
            {"recipient_group_id": "test-team", "display_name": "상품 담당"},
            {"recipient_group_id": "researchops-admins", "display_name": "운영 담당"}])
        raw = canonical_json(self.comp_input.to_dict())
        self.assertNotIn(b"example.test", raw)
        self.assertNotIn(b"smtp", raw)

    def test_original_name_and_utf8_bodies_survive_smtp_unchanged(self):
        handoff = self.publish()
        server = self.smtp_server()
        with patch("smtplib.SMTP", return_value=server):
            ok, _, message = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok, message)
        self.assertEqual([call.args[0] for call in server.rcpt.call_args_list],
                         ["first@example.test", "second@example.test"])
        mime = BytesParser(policy=policy.default).parsebytes(smtp_message_bytes(server))
        self.assertEqual(mime.get_body(("html",)).get_payload(decode=True), self.html)
        self.assertEqual(mime.get_body(("plain",)).get_payload(decode=True), self.plain)
        self.assertEqual((self.stage / "composition-result.json").read_bytes(), canonical_json(self.raw))
        resolution = self.hashes["recipient_resolution"]
        self.assertEqual(resolution["recipient_group_name"], "  상품 담당  ")
        self.assertEqual(resolution["recipient_group_id"], "test-team")
        self.assertEqual(resolution["composition_result_sha256"], hashlib.sha256(canonical_json(self.raw)).hexdigest())

    def test_two_tasks_can_select_different_names_from_the_same_catalog(self):
        for task_id, selected, expected in (("products-task", "상품 담당", "test-team"),
                                            ("empty-task", "운영 담당", "researchops-admins")):
            task = replace(self.task, id=task_id)
            run = replace(self.run, task_id=task_id, run_id=task_id + "-run")
            comp_input = self.build(task=task, run=run)
            self.assertEqual(resolve_recipient({"recipient_group_name": selected}, comp_input, task), expected)

    def test_new_group_appears_only_in_the_next_snapshot(self):
        self.catalog.ensure("recipient_group", "new-team", "신규 담당")
        self.config.recipient_groups["new-team"] = ["new@example.test"]
        new_input = self.build()
        self.assertEqual(len(self.comp_input.recipient_groups), 2)
        self.assertEqual(len(new_input.recipient_groups), 3)
        with self.assertRaises(ValidationError):
            resolve_recipient({"recipient_group_name": "신규 담당"}, self.comp_input, self.task)
        self.assertEqual(resolve_recipient({"recipient_group_name": "신규 담당"}, new_input, self.task), "new-team")

    def test_unmapped_deleted_and_invalid_mapping_groups_are_excluded(self):
        for key in ("unmapped-team", "deleted-team", "invalid-team"):
            self.catalog.ensure("recipient_group", key)
        self.config.recipient_groups.update({"unmapped-team": [], "deleted-team": ["deleted@example.test"],
                                             "invalid-team": ["not a mailbox"]})
        with self.db.transaction() as conn:
            conn.execute("UPDATE entity_catalog SET deleted_at='2026-09-09' WHERE legacy_key='deleted-team'")
        self.assertEqual(self.build().recipient_groups, self.comp_input.recipient_groups)

    def test_unknown_name_id_fallback_and_multiple_selections_are_rejected(self):
        for data in ({"recipient_group_name": "unknown"}, {"recipient_group_name": "test-team"},
                     {"recipient_group_name": ["상품 담당", "운영 담당"]},
                     {"recipient_group_name": "상품 담당", "recipient_group_id": "test-team"},
                     {"recipient_group_name": ""}):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                resolve_recipient(data, self.comp_input, self.task)

    def test_duplicate_display_name_is_an_explicit_error(self):
        duplicate = replace(self.comp_input, recipient_groups=[
            {"recipient_group_id": "test-team", "display_name": "상품 담당"},
            {"recipient_group_id": "researchops-admins", "display_name": "상품 담당"}])
        with self.assertRaisesRegex(ValidationError, "ambiguous"):
            resolve_recipient({"recipient_group_name": "상품 담당"}, duplicate, self.task)

    def test_renamed_group_keeps_original_fixed_id_and_next_snapshot_changes(self):
        self.catalog.rename("recipient_group", "test-team", "수정한 표시 이름")
        self.approve()
        self.assertEqual(self.publish().recipient_group_id, "test-team")
        self.assertEqual(self.build().recipient_groups[0]["display_name"], "수정한 표시 이름")

    def test_deleted_selected_group_blocks_publication_and_dispatch(self):
        handoff = self.publish()
        with self.db.transaction() as conn:
            conn.execute("UPDATE entity_catalog SET deleted_at='2026-09-09' WHERE legacy_key='test-team'")
        with self.assertRaisesRegex(DeliveryError, "deleted"):
            self.approve()
        with self.assertRaises(DeliveryError):
            self.dispatcher.enqueue_handoff(handoff.handoff_id)

    def test_mapping_removal_blocks_handoff(self):
        self.config.recipient_groups["test-team"] = []
        save_delivery_config(self.config, self.settings.paths.delivery_config_file)
        with self.assertRaisesRegex(DeliveryError, "mapping"):
            self.publish()

    def test_mapping_change_after_queue_blocks_data(self):
        handoff = self.publish()
        self.dispatcher.enqueue_handoff(handoff.handoff_id)
        self.config.recipient_groups["test-team"] = ["replacement@example.test"]
        save_delivery_config(self.config, self.settings.paths.delivery_config_file)
        with patch("smtplib.SMTP") as smtp:
            ok, _, message = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertFalse(ok)
        self.assertIn("configuration changed", message)
        smtp.assert_not_called()

    def test_missing_or_wrong_revision_never_uses_latest_result(self):
        self.approve()
        for revision in (None, 2):
            with self.subTest(revision=revision), self.assertRaises(DeliveryError):
                self.approve(revision=revision)
        second = replace(self.result, recipient_group_id="researchops-admins")
        self.run_repo.save_composition_result(self.run.run_id, self.task.id, second, revision=2)
        self.assertEqual(self.run_repo.get_composition_result(self.run.run_id).recipient_group_id, "researchops-admins")
        self.assertEqual(self.run_repo.get_composition_result(self.run.run_id, 1).recipient_group_id, "test-team")
        self.approve(revision=1)
        with self.assertRaises(DeliveryError):
            self.approve(revision=1, group="researchops-admins")

    def test_other_run_task_version_and_resolution_substitution_are_rejected(self):
        stored = self.run_repo.get_composition_result_record(self.run.run_id, 1)
        for key, value in (("run_id", "another-run"), ("task_version_hash", "a" * 64),
                           ("composition_revision", 9), ("recipient_group_id", "researchops-admins")):
            record = {**stored, "recipient_resolution": {**stored["recipient_resolution"], key: value}}
            with self.subTest(field=key), patch(
                    "researchops.storage.repositories.RunRepository.get_composition_result_record", return_value=record):
                with self.assertRaises(DeliveryError):
                    self.approve()

    def test_stored_input_hash_and_mutated_worker_result_are_rejected(self):
        record = self.run_repo.get_composition_input_record(self.run.run_id, 1)
        with patch("researchops.storage.repositories.RunRepository.get_composition_input_record",
                   return_value={**record, "input_sha256": "0" * 64}):
            with self.assertRaisesRegex(DeliveryError, "hash"):
                self.approve()
        (self.stage / "composition-result.json").write_bytes(canonical_json({**self.raw, "subject": "replaced"}))
        with self.assertRaisesRegex(DeliveryError, "changed"):
            self.publish()

    def test_malformed_stored_input_schema_is_rejected(self):
        record = self.run_repo.get_composition_input_record(self.run.run_id, 1)
        for changed in ({"schema_version": 8}, {"unknown_additional_property": True}):
            payload = {**record["input"], **changed}
            malformed = {**record, "input": payload,
                         "input_sha256": hashlib.sha256(canonical_json(payload)).hexdigest()}
            with self.subTest(changed=changed), patch(
                    "researchops.storage.repositories.RunRepository.get_composition_input_record", return_value=malformed):
                with self.assertRaisesRegex(DeliveryError, "schema"):
                    self.approve()

    def test_archive_resolution_tampering_blocks_smtp(self):
        handoff = self.publish()
        (self.archive / "recipient-resolution.json").write_bytes(canonical_json({}))
        with patch("smtplib.SMTP") as smtp:
            ok, _, message = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertFalse(ok)
        self.assertIn("resolution", message)
        smtp.assert_not_called()

    def test_archive_original_result_tampering_blocks_smtp(self):
        handoff = self.publish()
        (self.archive / "composition-result.json").write_bytes(canonical_json({**self.raw, "recipient_group_name": "운영 담당"}))
        with patch("smtplib.SMTP") as smtp:
            ok, _, _ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertFalse(ok)
        smtp.assert_not_called()

    def test_task_specific_nonrouting_schema_constraints_remain_hard(self):
        schema = {"type": "object", "properties": {"subject": {"const": "required task subject"}}}
        with self.assertRaisesRegex(ValidationError, "schema"):
            self.validator.validate_composition(self.stage, self.comp_input, self.task, schema)

    def test_input_v2_round_trip_preserves_legacy_wire_format(self):
        legacy = replace(self.task, delivery={"allowed_recipient_group_ids": ["test-team"]})
        comp_input = self.build(task=legacy)
        payload = comp_input.to_dict()
        self.assertEqual(payload["schema_version"], 2)
        self.assertNotIn("recipient_groups", payload)
        self.assertNotIn("recipient_routing_mode", payload)
        self.assertEqual(CompositionInput(**payload).to_dict(), payload)
        self.assertEqual(resolve_recipient({"recipient_group_id": "test-team"}, comp_input, legacy), "test-team")

    def test_compose_only_v3_roundtrip_reuses_frozen_names(self):
        payload = self.run_repo.get_composition_input(self.run.run_id, 1)
        payload.update(run_id="compose-only-run", composition_revision=2)
        self.catalog.rename("recipient_group", "test-team", "new label")
        copied = CompositionInput(**payload)
        self.assertEqual(resolve_recipient(self.raw, copied, self.task), "test-team")
        self.assertEqual(copied.recipient_groups, self.comp_input.recipient_groups)


class TestCatalogRoutingLifecycle(unittest.TestCase):
    def test_published_catalog_task_research_compose_smtp_and_compose_only(self):
        from researchops.delivery.smtp_config import BuiltinDeliveryConfig, SmtpSettings
        from researchops.runners.fake import FakeRunner
        from researchops.services.application import ApplicationService
        from tests.support import isolated_settings
        with tempfile.TemporaryDirectory() as temporary:
            settings = isolated_settings(Path(temporary))
            settings.environment = "production"
            settings.delivery.global_handoff_kill_switch = False
            app = ApplicationService(settings, custom_runner=FakeRunner())
            config = BuiltinDeliveryConfig(enabled=True,
                smtp=SmtpSettings(host="smtp.example.test", sender_email="sender@example.test"),
                recipient_groups={"product-team": ["recipient@example.test"],
                                  "empty-team": ["empty@example.test"]})
            save_delivery_config(config, settings.paths.delivery_config_file)
            task = app.tasks.create_production_task(task_id="catalog-lifecycle", name="Catalog workflow",
                instructions="상품 조사 결과가 있으면 product-team, 없으면 empty-team에 보낸다.",
                runner_type="codex_exec", recipient_routing_mode="catalog_name")
            run = app.runs.enqueue_run(task.task_id)
            finished = app.runs.execute_run(run.run_id)
            self.assertEqual(finished.status, "awaiting_receipt", finished.error_message)
            archive = settings.paths.run_archive_dir / task.task_id / run.run_id
            original_input = app.run_repo.get_composition_input(run.run_id, 1)
            raw = json.loads((archive / "composition-result.json").read_bytes())
            self.assertEqual(raw["recipient_group_name"], "product-team")
            self.assertNotIn("recipient_group_id", raw)
            resolution = json.loads((archive / "recipient-resolution.json").read_bytes())
            self.assertEqual(resolution["recipient_group_id"], "product-team")
            manifest_before = (archive / "run-manifest.json").read_bytes()
            handoff = app.delivery_repo.get_handoff_for_run(run.run_id)
            server = TestCatalogRouting.smtp_server()
            with patch("smtplib.SMTP", return_value=server):
                ok, _, message = app.delivery.smtp_dispatcher.dispatch_handoff(handoff.handoff_id)
            self.assertTrue(ok, message)
            app.catalog.rename("recipient_group", "product-team", "새 이름")
            child = app.runs.compose_only(run.run_id)
            with patch.object(app.orchestrator.custom_runner, "execute_research",
                              side_effect=AssertionError("Compose-only must reuse research")):
                finished = app.runs.execute_run(child.run_id, force_dry_run=True)
            self.assertEqual(finished.status, "succeeded", finished.error_message)
            copied = app.run_repo.get_composition_input(child.run_id, 2)
            self.assertEqual(copied["recipient_groups"], original_input["recipient_groups"])
            self.assertEqual(copied["reportable_records"], original_input["reportable_records"])
            self.assertEqual(manifest_before, (archive / "run-manifest.json").read_bytes())
