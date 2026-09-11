"""Recovery reuses exact authenticated mail and never invokes a model."""

from dataclasses import replace
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops.engine.execution_plan import build_execution_plan, encode_execution_plan, resolve_task_stages
from researchops.errors import DeliveryError, ValidationError
from researchops.services.application import ApplicationService
from researchops.services.prepared_delivery import load_prepared_message
from tests.support import fixture_runner, isolated_settings, register_fixture_task


class PreparedDeliveryCoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = isolated_settings(Path(self.temporary.name))
        self.runner = fixture_runner(self.settings)
        self.app = ApplicationService(self.settings, custom_runner=self.runner)
        self.version = register_fixture_task(self.app)

    def source(self):
        source = self.app.runs.enqueue_run(self.version.task_id)
        with patch.object(self.app.orchestrator.handoff_publisher, "create_and_publish_handoff",
                          side_effect=DeliveryError("synthetic delivery preparation failure")):
            failed = self.app.runs.execute_run(source.run_id)
        self.assertEqual(failed.status, "failed", failed.error_message)
        self.assertIsNone(self.app.delivery_repo.get_handoff_for_run(source.run_id))
        return failed

    def prepared(self, source, **kwargs):
        return load_prepared_message(self.settings, self.app.task_repo, self.app.run_repo,
                                     self.app.delivery_repo, source.run_id, **kwargs)

    def child(self, source, prepared, suffix="child"):
        run = replace(source, run_id=source.run_id + "-" + suffix, status="queued", phase="queued",
                      trigger_type="delivery_only", parent_run_id=source.run_id, started_at=None,
                      finished_at=None, error_message=None)
        plan = build_execution_plan(resolve_task_stages(self.version.definition), scope="delivery_only",
            source_composition=prepared.source_composition, source_message=prepared.source_message,
            selection_source={"kind": "parent_run", "run_id": source.run_id})
        revision = self.app.run_repo.get_execution_controls(source.run_id)["composition_revision"] + 1
        self.app.run_repo.create_run(run, composition_revision=revision, execution_plan=plan)
        return run

    def test_validated_source_has_authentic_original_body_and_complete_pins(self):
        source = self.source()
        prepared = self.prepared(source)
        self.assertEqual(prepared.source_run.run_id, source.run_id)
        self.assertEqual(prepared.source_message["html_sha256"],
                         hashlib.sha256(prepared.files[prepared.composition_result.html_path]).hexdigest())
        self.assertEqual(prepared.composition_input.run["local_date"], source.local_date)
        self.assertEqual(self.prepared(source, expected_source=prepared.source_message).source_message,
                         prepared.source_message)

    def test_changed_result_body_input_and_archive_index_are_refused(self):
        for name in ("composition-result.json", "email.html", "composition-input.json", "artifact-manifest.json"):
            source = self.source()
            prepared = self.prepared(source)
            root = self.settings.paths.run_archive_dir / source.task_id / source.run_id
            path = root / name
            path.write_bytes(path.read_bytes() + b" ")
            with self.subTest(name=name), self.assertRaises((ValidationError, DeliveryError)):
                self.prepared(source, expected_source=prepared.source_message)

    def test_dry_run_cancelled_or_unverified_cleanup_cannot_be_recovered(self):
        for column in ("force_dry_run", "cancel_requested", "child_cleanup_verified"):
            source = self.source()
            with self.app.run_repo.db.transaction() as conn:
                conn.execute(f"UPDATE execution_controls SET {column}=? WHERE run_id=?",
                             (0 if column == "child_cleanup_verified" else 1, source.run_id))
            with self.subTest(column=column), self.assertRaises(ValidationError):
                self.prepared(source)

    def test_existing_handoff_blocks_prepared_message_path(self):
        source = self.source()
        with patch.object(self.app.delivery_repo, "get_handoff_for_run", return_value=object()):
            with self.assertRaisesRegex(ValidationError, "HANDOFF_ALREADY_EXISTS"):
                self.prepared(source)

    def test_delivery_only_copies_exact_bytes_without_research_or_compose(self):
        source = self.source()
        prepared = self.prepared(source)
        source_root = self.settings.paths.run_archive_dir / source.task_id / source.run_id
        before = {p.relative_to(source_root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in source_root.rglob("*") if p.is_file()}
        child = self.child(source, prepared)
        with patch.object(self.runner, "execute_research", side_effect=AssertionError("no research")), \
                patch.object(self.runner, "execute_compose", side_effect=AssertionError("no compose")):
            completed = self.app.runs.execute_run(child.run_id)
        self.assertEqual(completed.status, "succeeded", completed.error_message)
        child_root = self.settings.paths.run_archive_dir / child.task_id / child.run_id
        for name in ("composition-result.json", prepared.composition_result.html_path,
                     prepared.composition_result.text_path, *[a["path"] for a in
                         prepared.composition_input.attachments + prepared.composition_input.inline_artifacts]):
            self.assertEqual((child_root / name).read_bytes(), prepared.files[name])
        self.assertEqual(before, {p.relative_to(source_root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in source_root.rglob("*") if p.is_file()})
        manifest = json.loads((child_root / "validation-report.json").read_text())
        self.assertEqual(manifest["executions"], [])
        self.assertEqual(self.app.run_repo.get_run(source.run_id).status, "failed")
        self.assertEqual(completed.local_date, source.local_date)
        self.assertEqual(completed.scheduled_for, source.scheduled_for)

    def test_delivery_retry_before_input_save_retains_original_message_source(self):
        source = self.source()
        prepared = self.prepared(source)
        child = self.child(source, prepared)
        with patch.object(self.app.run_repo, "save_composition_input", side_effect=ValidationError("prepare failed")):
            failed = self.app.runs.execute_run(child.run_id)
        self.assertEqual(failed.status, "failed", failed.error_message)
        self.assertIsNone(self.app.run_repo.get_composition_input_record(child.run_id))
        recovered = self.prepared(failed)
        self.assertEqual(recovered.source_message, prepared.source_message)
        grandchild = self.child(failed, recovered, "retry")
        with patch.object(self.runner, "execute_research", side_effect=AssertionError("no research")), \
                patch.object(self.runner, "execute_compose", side_effect=AssertionError("no compose")):
            completed = self.app.runs.execute_run(grandchild.run_id)
        self.assertEqual(completed.status, "succeeded", completed.error_message)

    def test_tampering_after_enqueue_blocks_delivery_without_any_model_call(self):
        source = self.source()
        prepared = self.prepared(source)
        child = self.child(source, prepared)
        root = self.settings.paths.run_archive_dir / source.task_id / source.run_id
        (root / prepared.composition_result.text_path).write_text("changed")
        with patch.object(self.runner, "execute_research", side_effect=AssertionError("no research")), \
                patch.object(self.runner, "execute_compose", side_effect=AssertionError("no compose")), \
                patch.object(self.app.orchestrator.handoff_publisher, "create_and_publish_handoff") as handoff:
            failed = self.app.runs.execute_run(child.run_id)
        self.assertEqual(failed.status, "failed", failed.error_message)
        handoff.assert_not_called()

    def test_old_execution_plan_bytes_unchanged_and_delivery_pin_required(self):
        stages = resolve_task_stages(self.version.definition)
        plan = build_execution_plan(stages)
        self.assertNotIn("source_message", plan)
        raw, digest = encode_execution_plan(plan)
        self.assertEqual(json.loads(raw), plan)
        self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(), digest)
        with self.assertRaises(ValidationError):
            build_execution_plan(stages, scope="delivery_only")

    def test_catalog_name_result_is_preserved_and_child_resolution_is_rebound(self):
        from researchops.delivery.smtp_config import BuiltinDeliveryConfig, SmtpSettings, save_delivery_config
        definition = copy.deepcopy(self.version.definition)
        definition.delivery["recipient_routing_mode"] = "catalog_name"
        files = dict(self.version.package_files)
        files[definition.output["composition_schema"]] = (self.settings.paths.schemas_dir / "composition-result.schema.json").read_text()
        self.version = replace(self.version, definition=definition, package_files=files,
                               version_hash="a" * 64)
        self.app.task_repo.save_version(self.version)
        self.app.task_repo.set_active_version(self.version.task_id, self.version.version_hash)
        group = definition.delivery["allowed_recipient_group_ids"][0]
        self.app.catalog.ensure("recipient_group", group, "상품 담당")
        save_delivery_config(BuiltinDeliveryConfig(enabled=False,
            smtp=SmtpSettings(host="smtp.example.test", sender_email="sender@example.test"),
            recipient_groups={group: ["recipient@example.test"]}), self.settings.paths.delivery_config_file)
        source = self.source()
        prepared = self.prepared(source)
        self.assertIn("recipient_group_name", json.loads(prepared.files["composition-result.json"]))
        child = self.child(source, prepared)
        with patch.object(self.runner, "execute_research", side_effect=AssertionError("no research")), \
                patch.object(self.runner, "execute_compose", side_effect=AssertionError("no compose")):
            completed = self.app.runs.execute_run(child.run_id)
        self.assertEqual(completed.status, "succeeded", completed.error_message)
        child_record = self.app.run_repo.get_composition_result_record(child.run_id)
        source_record = self.app.run_repo.get_composition_result_record(source.run_id)
        self.assertEqual(child_record["recipient_resolution"]["recipient_group_id"], group)
        self.assertNotEqual(child_record["recipient_resolution"], source_record["recipient_resolution"])
        child_root = self.settings.paths.run_archive_dir / child.task_id / child.run_id
        self.assertEqual((child_root / "composition-result.json").read_bytes(), prepared.files["composition-result.json"])


if __name__ == "__main__":
    unittest.main()
