"""Artifact delivery regression tests use isolated storage and SMTP doubles only."""

from copy import deepcopy
from dataclasses import replace
from email import policy
from email.parser import BytesParser
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch
from jsonschema import Draft202012Validator

from researchops.delivery.artifact_integrity import require_stored_composition
from researchops.delivery.recipient_routing import catalog_recipient_snapshot
from researchops.domain.models import CompositionInput, ResearchResult
from researchops.engine.archive import canonical_json
from researchops.engine.composition_input import CompositionInputBuilder
from researchops.engine.message_validator import MessageValidator
from researchops.errors import DeliveryError, ValidationError
from researchops.services.catalog_service import CatalogService
from tests.delivery_fixtures import DeliveryFixture, smtp_server, smtp_message_bytes
from tests import test_message_validator as media_fixtures


class TestArtifactComposition(DeliveryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.settings.environment = "production"
        self.builder = CompositionInputBuilder(self.settings.paths.schemas_dir)
        self.validator = MessageValidator(self.settings.paths.schemas_dir)
        self.pdf = b"%PDF-1.7\n" + b"Synthetic document\n" * 2000 + b"%%EOF\n"
        self.base_manifest = json.loads((self.settings.paths.run_archive_dir / self.task.id /
            self.run.run_id / "run-manifest.json").read_text())
        self.counter = 0
        self.configure()

    def configure(self, mode="legacy_ids", *, records=None, inline=False, provenance=False):
        self.counter += 1
        delivery = {"mode": "handoff", "recipient_routing_mode": mode}
        if mode == "legacy_ids":
            delivery["allowed_recipient_group_ids"] = ["test-team"]
        self.task = replace(self.task, delivery=delivery)
        self.version = replace(self.version, definition=self.task, version_hash=str(self.counter) * 64)
        self.task_repo.save_version(self.version)
        self.task_repo.set_active_version(self.task.id, self.version.version_hash, delivery_mode="handoff")
        self.run = self.make_run(f"artifact-run-{self.counter}")
        groups = None
        if mode == "catalog_name":
            CatalogService(self.settings, self.db).ensure("recipient_group", "test-team", "Product Team")
            groups = catalog_recipient_snapshot(self.db, self.config)
        records = records or [{"record_id": "c1"}]
        raw = media_fixtures.TestMessageValidator._png() if inline else self.pdf
        path = "card.png" if inline else "terms.pdf"
        sha = hashlib.sha256(raw).hexdigest()
        self.media = {"path": path, "sha256": sha, "size_bytes": len(raw),
            "mime_type": "image/png" if inline else "application/pdf", "artifact_id": "terms-1",
            "record_ids": ["c1"], **({"cid": "card-1"} if inline else {"filename": "상품 약관.pdf"})}
        self.report = {"schema_version": 1, "task_id": self.task.id, "run_id": self.run.run_id,
            "task_version_hash": self.run.task_version_hash, "composition_revision": 1, "entries": [{
            "artifact_id": "terms-1", "path": path, "role": "inline_image" if inline else "attachment",
            "scope": "record", "requested_record_ids": ["c1"], "record_ids": ["c1"],
            "declared_status": "ready", "status": "available", "reason_code": "verified",
            "source": None, "sha256": sha, "size_bytes": len(raw), "include_in_compose": True,
            "announce_missing": False}]}
        if provenance:
            references = [{"acquisition_id": "acq-original", "source_sha256": "a" * 64}]
            self.media["derived_from"] = deepcopy(references)
            self.report["entries"][0]["derived_from"] = deepcopy(references)
        research = ResearchResult("success", "Synthetic research", records,
            {"complete": True, "expected_target_count": len(records), "completed_target_count": len(records), "issues": []}, [], "{}")
        self.comp_input = self.builder.build_composition_input(self.task, self.run, research, records,
            inline_artifacts=[self.media] if inline else [], attachments=[] if inline else [self.media],
            recipient_groups=groups, artifact_report=self.report)
        self.raw = {"recipient_group_reason": "Synthetic products", "subject": "서울 상품",
            "html_path": "email.html", "text_path": "email.txt", "included_record_ids": [r["record_id"] for r in records],
            **({"recipient_group_name": " Product Team "} if mode == "catalog_name" else {"recipient_group_id": "test-team"})}
        rows = ''.join(f'<section data-record-id="{r["record_id"]}">Product'
            + ('<img src="cid:card-1">' if inline and r["record_id"] == "c1" else '')
            + '</section>' for r in records)
        self.html = f'<html><body data-local-date="2026-09-05">{rows}</body></html>'.encode()
        (self.stage / path).write_bytes(raw)
        (self.stage / "email.html").write_bytes(self.html)
        (self.stage / "composition-result.json").write_bytes(canonical_json(self.raw))
        self.result, _, _, self.hashes = self.validator.validate_composition(self.stage, self.comp_input, self.task)
        self.run_repo.save_composition_input(self.comp_input, self.hashes["composition_binding"]["composition_input_sha256"])
        self.run_repo.save_composition_result(self.run.run_id, self.task.id, self.result, revision=1,
            recipient_resolution=self.hashes.get("recipient_resolution"), composition_binding=self.hashes["composition_binding"])
        self.archive = self.settings.paths.run_archive_dir / self.task.id / self.run.run_id
        self.archive.mkdir(parents=True)
        manifest = deepcopy(self.base_manifest)
        manifest["run_id"] = self.run.run_id
        manifest["composition"].update(self.result.to_dict())
        (self.archive / "run-manifest.json").write_bytes(canonical_json(manifest))
        for name, value in (("composition-input.json", self.comp_input.to_dict()),
                            ("composition-result.json", self.raw), ("artifact-report.json", self.report),
                            ("composition-binding.json", self.hashes["composition_binding"])):
            (self.archive / name).write_bytes(canonical_json(value))
        if self.hashes.get("recipient_resolution"):
            (self.archive / "recipient-resolution.json").write_bytes(canonical_json(self.hashes["recipient_resolution"]))
        for name in ("email.html", "email.txt", path):
            (self.archive / name).write_bytes((self.stage / name).read_bytes())

    def verify(self, **kwargs):
        return require_stored_composition(self.db, self.task, self.run.run_id, self.run.task_version_hash,
            kwargs.pop("revision", 1), "test-team", schemas_dir=self.settings.paths.schemas_dir,
            archive_root=self.archive, **kwargs)

    @staticmethod
    def smtp_server():
        server = smtp_server()
        return server

    def test_legacy_v4_smtp_preserves_original_pdf_filename_and_body_bytes(self):
        self.assertEqual(self.comp_input.schema_version, 4)
        handoff = self.publish()
        self.assertEqual(handoff.delivery_request["composition_binding_sha256"],
            hashlib.sha256(canonical_json(self.hashes["composition_binding"])).hexdigest())
        server = self.smtp_server()
        with patch("smtplib.SMTP", return_value=server):
            ok, _, message = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok, message)
        mime = BytesParser(policy=policy.default).parsebytes(smtp_message_bytes(server))
        pdfs = [part for part in mime.walk() if part.get_content_type() == "application/pdf"]
        self.assertEqual(len(pdfs), 1)
        self.assertEqual(pdfs[0].get_payload(decode=True), self.pdf)
        self.assertEqual(pdfs[0].get_filename(), "상품 약관.pdf")
        self.assertEqual(mime.get_body(("html",)).get_payload(decode=True), self.html)
        self.assertEqual(mime.get_body(("plain",)).get_payload(decode=True), self.plain)
        self.assertEqual((self.archive / "composition-result.json").read_bytes(), canonical_json(self.raw))

    def test_catalog_v4_resolves_name_and_preserves_same_pdf(self):
        self.configure("catalog_name")
        self.assertEqual(self.result.recipient_group_id, "test-team")
        handoff = self.publish()
        server = self.smtp_server()
        with patch("smtplib.SMTP", return_value=server):
            self.assertTrue(self.dispatcher.dispatch_handoff(handoff.handoff_id)[0])
        mime = BytesParser(policy=policy.default).parsebytes(smtp_message_bytes(server))
        self.assertEqual(next(p for p in mime.walk() if p.get_content_type() == "application/pdf").get_payload(decode=True), self.pdf)

    def test_derivative_provenance_is_sealed_through_smtp_without_changing_content(self):
        self.configure(provenance=True)
        self.assertEqual(self.verify(), self.hashes["composition_binding"])
        self.assertEqual(self.comp_input.attachments[0]["derived_from"],
            self.comp_input.artifact_report["entries"][0]["derived_from"])
        handoff = self.publish()
        server = self.smtp_server()
        with patch("smtplib.SMTP", return_value=server):
            self.assertTrue(self.dispatcher.dispatch_handoff(handoff.handoff_id)[0])
        mime = BytesParser(policy=policy.default).parsebytes(smtp_message_bytes(server))
        self.assertEqual(next(p for p in mime.walk() if p.get_content_type() == "application/pdf").get_payload(decode=True), self.pdf)
        self.assertEqual(mime.get_body(("html",)).get_payload(decode=True), self.html)
        self.assertEqual((self.archive / "composition-result.json").read_bytes(), canonical_json(self.raw))

    def test_derivative_reference_schema_and_inventory_cannot_be_changed_independently(self):
        self.configure(provenance=True)
        for references in ([], ["acq-original"], [{"acquisition_id": "acq-original", "source_sha256": "invalid"}],
                [{"acquisition_id": "acq-original", "source_sha256": "a" * 64}] * 2,
                [{"acquisition_id": "acq-original", "source_sha256": "a" * 64, "path": "worker.pdf"}]):
            data = deepcopy(self.comp_input.to_dict())
            data["attachments"][0]["derived_from"] = references
            data["artifact_report"]["entries"][0]["derived_from"] = references
            with self.subTest(references=references), self.assertRaises(ValidationError):
                self.validator.validate_composition(self.stage, CompositionInput(**data), self.task)
        for location in ("attachments", "artifact_report"):
            data = deepcopy(self.comp_input.to_dict())
            entry = data["attachments"][0] if location == "attachments" else data["artifact_report"]["entries"][0]
            entry["derived_from"][0]["source_sha256"] = "b" * 64
            with self.subTest(location=location), self.assertRaisesRegex(ValidationError, "metadata or associations"):
                self.validator.validate_composition(self.stage, CompositionInput(**data), self.task)

    def test_optional_acquisition_ledger_binding_is_sealed_and_rejects_malformed_identity(self):
        binding = {"run_id": self.run.run_id, "attempt": 1, "ledger_sha256": "a" * 64}
        data = deepcopy(self.comp_input.to_dict())
        data["artifact_report"]["acquisition_evidence"] = binding
        _, _, _, hashes = self.validator.validate_composition(self.stage, CompositionInput(**data), self.task)
        self.assertEqual(hashes["composition_binding"]["artifact_report_sha256"],
            hashlib.sha256(canonical_json(data["artifact_report"])).hexdigest())
        for changed in ({"run_id": "../foreign"}, {"attempt": True}, {"attempt": 0},
                        {"ledger_sha256": "invalid"}, {"source": "worker supplied"}):
            data["artifact_report"]["acquisition_evidence"] = {**binding, **changed}
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                self.validator.validate_composition(self.stage, CompositionInput(**data), self.task)

    def test_report_run_revision_path_and_source_schema_fail_closed(self):
        for mutation in (lambda d: d["artifact_report"].update(run_id="another-run"),
                         lambda d: d["artifact_report"].update(composition_revision=2),
                         lambda d: d["artifact_report"]["entries"][0].update(path="../escape.pdf"),
                         lambda d: d["artifact_report"]["entries"][0].update(source={"kind": "cardrag_pdf", "authorization": "forbidden"})):
            data = deepcopy(self.comp_input.to_dict())
            mutation(data)
            with self.subTest(data=data["artifact_report"]["entries"][0]["path"]), self.assertRaises(ValidationError):
                self.validator.validate_composition(self.stage, CompositionInput(**data), self.task)

    def test_record_links_inventory_and_missing_availability_fail_closed(self):
        for mutate in (lambda d: d["attachments"][0].update(record_ids=["different-product"]),
                       lambda d: d.update(attachments=[]),
                       lambda d: d["artifact_report"]["entries"][0].update(status="failed"),
                       lambda d: d["artifact_report"]["entries"][0].update(requested_record_ids=["unknown-record"])):
            value = deepcopy(self.comp_input.to_dict())
            mutate(value)
            with self.assertRaises(ValidationError):
                self.validator.validate_composition(self.stage, CompositionInput(**value), self.task)

    def test_missing_file_diagnostic_without_attachment_can_still_compose(self):
        value = deepcopy(self.comp_input.to_dict())
        value["attachments"] = []
        value["artifact_report"]["entries"][0].update(status="failed", reason_code="source_unavailable",
            sha256=None, size_bytes=None, include_in_compose=False)
        result, _, _, _ = self.validator.validate_composition(self.stage, CompositionInput(**value), self.task)
        self.assertEqual(result.included_record_ids, ["c1"])

    def run_evidence_input(self):
        report = deepcopy(self.report)
        report["entries"] = []
        for index in (1, 2):
            content = canonical_json({"query": index, "records_found": 0})
            path = f"query-history-{index}.json"
            (self.stage / path).write_bytes(content)
            report["entries"].append({"artifact_id": f"query-history-{index}", "path": path,
                "role": "evidence", "scope": "run", "requested_scope": "run",
                "requested_record_ids": [], "record_ids": [], "declared_status": "ready",
                "status": "available", "reason_code": "evidence_only", "source": None,
                "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content),
                "include_in_compose": False, "announce_missing": False})
        coverage = {"complete": True, "expected_target_count": 2, "completed_target_count": 2, "issues": []}
        research = ResearchResult("no_updates", "No new products", [], coverage, [], "{}")
        comp_input = self.builder.build_composition_input(self.task, self.run, research, [], artifact_report=report)
        (self.stage / "composition-result.json").write_bytes(canonical_json({**self.raw, "included_record_ids": []}))
        (self.stage / "email.html").write_bytes(b'<html><body data-local-date="2026-09-05">No new products</body></html>')
        (self.stage / "email.txt").write_bytes(b"2026.09.05 No new products")
        return comp_input

    def test_no_updates_with_two_run_json_evidence_files_has_no_compose_media(self):
        comp_input = self.run_evidence_input()
        result, _, _, hashes = self.validator.validate_composition(self.stage, comp_input, self.task)
        self.assertEqual(comp_input.schema_version, 4)
        self.assertEqual(comp_input.result["status"], "no_updates")
        self.assertEqual(comp_input.reportable_records, [])
        self.assertEqual(comp_input.attachments, [])
        self.assertEqual(comp_input.inline_artifacts, [])
        self.assertEqual(result.included_record_ids, [])
        self.assertEqual(len(comp_input.artifact_report["entries"]), 2)
        self.assertTrue(all(e["status"] == "available" and not e["include_in_compose"]
                            for e in comp_input.artifact_report["entries"]))
        self.assertEqual(set(hashes["composition_binding"]["files"]), {"email.html", "email.txt"})
        self.assertEqual(hashes["composition_binding"]["artifact_report_sha256"],
            hashlib.sha256(canonical_json(comp_input.artifact_report)).hexdigest())

    def test_run_evidence_does_not_allow_inline_source_record_links_or_email_inclusion(self):
        comp_input = self.run_evidence_input()
        changes = [
            {"role": "inline_image"},
            {"record_ids": ["product-1"], "requested_record_ids": ["product-1"]},
            {"source": {"kind": "official_image", "url": "https://example.test/image.png", "url_sha256": "a" * 64}},
            {"include_in_compose": True}]
        for change in changes:
            value = deepcopy(comp_input.to_dict())
            value["artifact_report"]["entries"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.validator.validate_composition(self.stage, CompositionInput(**value), self.task)

    def test_requested_scope_diagnostic_is_optional_and_preserves_unknown_safe_scope(self):
        comp_input = self.run_evidence_input()
        for scope in (None, "run", "unsupported-scope"):
            value = deepcopy(comp_input.to_dict())
            value["artifact_report"]["entries"][0].pop("requested_scope")
            value["artifact_report"]["entries"][1]["requested_scope"] = scope
            with self.subTest(scope=scope):
                self.validator.validate_composition(self.stage, CompositionInput(**value), self.task)
        for scope in ("x" * 65, "run\ncredential"):
            value = deepcopy(comp_input.to_dict())
            value["artifact_report"]["entries"][0]["requested_scope"] = scope
            with self.subTest(scope=scope), self.assertRaises(ValidationError):
                self.validator.validate_composition(self.stage, CompositionInput(**value), self.task)

    def test_optional_authoring_schema_accepts_only_local_run_attachment_or_evidence(self):
        schema = json.loads((self.settings.paths.schemas_dir / "generic-result.schema.json").read_text())
        validator = Draft202012Validator({"$ref": "#/$defs/artifactRequests", "$defs": schema["$defs"]})
        for role in (None, "attachment", "evidence"):
            artifact = {"path": "query-history.json", "scope": "run", "mime_type": "application/json"}
            if role:
                artifact["role"] = role
            with self.subTest(role=role):
                self.assertEqual(list(validator.iter_errors([artifact])), [])
                self.assertEqual(list(validator.iter_errors([{**artifact, "source": None, "record_ids": []}])), [])
        for change in ({"role": "inline_image"}, {"record_ids": ["product-1"]},
                       {"source": {"kind": "official_image", "url": "https://example.test/image.png"}}):
            artifact = {"path": "query-history.json", "scope": "run", "role": "evidence", **change}
            self.assertTrue(list(validator.iter_errors([artifact])))
            self.assertEqual(list(Draft202012Validator(schema).iter_errors(
                {"status": "no_updates", "summary": "No new products", "records": [], "artifacts": [artifact]})), [])

    def test_optional_request_failure_policy_is_sealed_without_changing_old_reports(self):
        for failure_policy in (None, "continue", "hold", "retry"):
            value = deepcopy(self.comp_input.to_dict())
            if failure_policy is not None:
                value["artifact_report"]["entries"][0]["on_failure"] = failure_policy
            comp_input = CompositionInput(**value)
            if failure_policy == "retry":
                with self.assertRaisesRegex(ValidationError, "input schema validation failed"):
                    self.validator.validate_composition(self.stage, comp_input, self.task)
            else:
                _, _, _, hashes = self.validator.validate_composition(self.stage, comp_input, self.task)
                self.assertEqual(hashes["composition_binding"]["artifact_report_sha256"],
                    hashlib.sha256(canonical_json(value["artifact_report"])).hexdigest())

    def test_optional_authoring_schema_accepts_receipt_ids_only(self):
        schema = json.loads((self.settings.paths.schemas_dir / "generic-result.schema.json").read_text())
        validator = Draft202012Validator({"$ref": "#/$defs/artifactRequests", "$defs": schema["$defs"]})
        artifact = {"path": "files/extract.txt", "role": "attachment", "derived_from": ["acq-source"]}
        self.assertEqual(list(validator.iter_errors([artifact])), [])
        for references in ([], ["acq-source"] * 2, ["../other"], [{"acquisition_id": "acq-source", "source_sha256": "a" * 64}]):
            with self.subTest(references=references):
                self.assertTrue(list(validator.iter_errors([{**artifact, "derived_from": references}])))

    def test_inline_image_requires_its_own_product_region(self):
        self.configure(records=[{"record_id": "c1"}, {"record_id": "c2"}], inline=True)
        self.assertEqual(self.verify(), self.hashes["composition_binding"])
        wrong = self.html.replace(b'Product<img src="cid:card-1">', b'Product').replace(
            b'data-record-id="c2">Product', b'data-record-id="c2">Product<img src="cid:card-1">')
        (self.stage / "email.html").write_bytes(wrong)
        with self.assertRaisesRegex(ValidationError, "Message validation failed") as failure:
            self.validator.validate_composition(self.stage, self.comp_input, self.task)
        self.assertIn("Inline artifact CID must occur inside its associated record region", failure.exception.errors)

    def test_handoff_rejects_changed_raw_result_or_media_bytes(self):
        for name in ("composition-result.json", "terms.pdf", "email.html"):
            original = (self.stage / name).read_bytes()
            (self.stage / name).write_bytes(original + b" ")
            with self.subTest(name=name), self.assertRaises(DeliveryError):
                self.publish()
            (self.stage / name).write_bytes(original)

    def test_archive_report_binding_and_input_tampering_rejected_before_network(self):
        handoff = self.publish()
        for name in ("artifact-report.json", "composition-binding.json", "composition-input.json", "terms.pdf"):
            original = (self.archive / name).read_bytes()
            (self.archive / name).write_bytes(original + b" ")
            with patch("smtplib.SMTP") as smtp, self.subTest(name=name):
                self.assertFalse(self.dispatcher.dispatch_handoff(handoff.handoff_id)[0])
                smtp.assert_not_called()
            (self.archive / name).write_bytes(original)

    def test_exact_revision_and_input_downgrade_are_not_reinterpreted(self):
        handoff = self.publish()
        with self.assertRaises(DeliveryError):
            self.verify(revision=2, request=handoff.delivery_request)
        original = self.comp_input
        self.comp_input = replace(original, schema_version=2, artifact_report=None,
            attachments=[{k: v for k, v in self.media.items() if k not in ("artifact_id", "record_ids", "source")}])
        with self.assertRaisesRegex(DeliveryError, "legacy input"):
            self.publish()
        self.comp_input = original
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM composition_inputs WHERE run_id=?", (self.run.run_id,))
        with patch("smtplib.SMTP") as smtp:
            self.assertFalse(self.dispatcher.dispatch_handoff(handoff.handoff_id)[0])
            smtp.assert_not_called()

    def test_changed_stored_body_metadata_cannot_replace_original_result(self):
        with self.assertRaises(sqlite3.IntegrityError), self.db.transaction() as conn:
            conn.execute("UPDATE composition_results SET subject='Substitution' WHERE run_id=?", (self.run.run_id,))
        with self.assertRaises(DeliveryError):
            self.verify(comp_result=replace(self.result, subject="Substitution"))

    def test_republish_checks_the_same_archived_artifact_inventory(self):
        handoff = self.publish()
        self.assertEqual(self.publisher.republish_handoff(handoff.handoff_id).handoff_id, handoff.handoff_id)
        (self.archive / "terms.pdf").write_bytes(self.pdf + b"changed")
        with self.assertRaises(DeliveryError):
            self.publisher.republish_handoff(handoff.handoff_id)

    def test_compose_only_reuses_verified_parent_report_and_rejects_wrong_hash(self):
        self.configure(provenance=True)
        for correct in (True, False):
            child = self.make_run("compose-only-" + str(correct))
            report = deepcopy(self.report)
            report["run_id"] = child.run_id
            report["derived_from"] = {"run_id": self.run.run_id, "composition_revision": 1,
                "artifact_report_sha256": hashlib.sha256(canonical_json(self.report)).hexdigest() if correct else "0" * 64}
            comp_input = replace(self.comp_input, run_id=child.run_id, artifact_report=report)
            result, _, _, hashes = self.validator.validate_composition(self.stage, comp_input, self.task)
            self.run_repo.save_composition_input(comp_input, hashes["composition_binding"]["composition_input_sha256"])
            self.run_repo.save_composition_result(child.run_id, self.task.id, result, revision=1,
                composition_binding=hashes["composition_binding"])
            def verify_child():
                return require_stored_composition(self.db, self.task, child.run_id, child.task_version_hash,
                    1, "test-team", schemas_dir=self.settings.paths.schemas_dir, comp_input=comp_input,
                    file_root=self.stage, raw_result=canonical_json(self.raw))
            if correct:
                self.assertEqual(verify_child(), hashes["composition_binding"])
            else:
                with self.assertRaisesRegex(DeliveryError, "parent revision"):
                    verify_child()

    def test_v2_v3_wire_shape_is_unchanged_and_rejects_new_fields(self):
        for version, mode in ((2, "legacy_ids"), (3, "catalog_name")):
            old = replace(self.comp_input, schema_version=version, recipient_routing_mode=mode,
                recipient_groups=[] if version == 2 else [{"recipient_group_id": "test-team", "display_name": "Team"}],
                attachments=[], artifact_report=None)
            value = old.to_dict()
            self.assertNotIn("artifact_report", value)
            self.assertEqual("recipient_routing_mode" in value, version == 3)
            self.assertEqual(CompositionInput(**value).to_dict(), value)

    def test_real_base64_expansion_exact_limit_and_one_byte_over_before_connect(self):
        handoff = self.publish()
        request, files = self.dispatcher._check_handoff(handoff, self.config)
        message_id = "<ro-" + hashlib.sha256(handoff.idempotency_key.encode()).hexdigest() + "@researchops.local>"
        mime = self.dispatcher._mime(request, files, self.config.smtp, self.config.recipient_groups["test-team"], message_id)
        raw_total = sum(map(len, files.values()))
        self.assertGreater(len(mime), raw_total)
        self.settings.delivery.max_message_bytes = len(mime)
        with patch("smtplib.SMTP") as smtp:
            self.dispatcher.enqueue_handoff(handoff.handoff_id)
            smtp.assert_not_called()
        self.settings.delivery.max_message_bytes = len(mime) - 1
        self.assertLess(raw_total, self.settings.delivery.max_message_bytes)
        with patch("smtplib.SMTP") as smtp, self.assertRaisesRegex(DeliveryError, "Encoded SMTP MIME"):
            self.dispatcher.enqueue_handoff(handoff.handoff_id)
            smtp.assert_not_called()
        with patch("smtplib.SMTP") as smtp:
            self.assertFalse(self.dispatcher.dispatch_handoff(handoff.handoff_id)[0])
            smtp.assert_not_called()
