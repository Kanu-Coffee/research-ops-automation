"""Real-task authoring with immutable versions, isolated from operating data."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from researchops.delivery.smtp_config import BuiltinDeliveryConfig, SmtpSettings, save_delivery_config
from researchops.domain.models import DeliveryHandoff, TaskDefinition, TaskVersion
from researchops.errors import ValidationError
from researchops.package.loader import compute_package_hash
from researchops.package.publisher import publish_version
from researchops.services.application import ApplicationService
from tests.support import isolated_settings


class TestTaskEditing(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = isolated_settings(Path(temp.name))
        self.settings.environment = "production"
        self.app = ApplicationService(self.settings)
        self.config = BuiltinDeliveryConfig(recipient_groups={
            "research-team": ["recipient@example.test"], "second-team": ["second@example.test"]},
            sender_profiles={"team-sender": SmtpSettings(username="sender@example.test")})
        save_delivery_config(self.config, self.settings.paths.delivery_config_file)

    def create(self, **changes):
        args = {"task_id": "policy-digest", "name": "실제 업무", "instructions": "공식 발표를 조사한다.",
                "task_md": "# 조사\ninvocation_stage에 따라 공식 발표를 조사한다.\n",
                "email_spec_md": "# 이메일\n내가 지정한 구성과 서식을 정확히 사용한다.\n",
                "runner_type": "codex_exec", "recipient_group_id": "research-team"}
        args.update(changes)
        return self.app.tasks.create_production_task(**args)

    def edit(self, task_id="policy-digest", form=None, **changes):
        form = form or self.app.tasks.get_task_editor(task_id)
        fields = ("name", "instructions", "email_spec_md", "runner_type", "recipient_group_id",
                  "cron", "schedule_enabled", "model", "sender_profile_id", "expected_updated_at")
        args = {name: form[name] for name in fields}
        args["expected_version_hash"] = form["expected_version_hash"]
        args.update(changes)
        return self.app.tasks.update_production_task(task_id, **args)

    def custom_package(self):
        source = self.create()
        files = dict(source.package_files)
        config = source.definition.to_dict()
        config["description"] = "Keep my custom task policy"
        config["runner"]["timeout_seconds"] = 4500
        config["delivery"]["partial_policy"] = "hold"
        config["instructions"]["research_files"].append("instructions/research-extra.md")
        config["instructions"]["compose_files"].append("instructions/email-extra.md")
        files["instructions/research-extra.md"] = "Supplemental source selection rules.\n"
        files["instructions/email-extra.md"] = "Preserve the supplied column ordering.\n"
        files["assets/table.html"] = "<table><caption>Operator layout</caption></table>\n"
        for role in ("research_schema", "composition_record_schema", "composition_schema"):
            old = config["output"][role]
            path = "contracts/" + role + ".json"
            files[path] = files.pop(old)
            config["output"][role] = path
        config["delivery"]["allowed_recipient_group_ids"] = ["research-team", "second-team"]
        schema_path = config["output"]["composition_schema"]
        schema = json.loads(files[schema_path])
        schema["properties"]["recipient_group_id"]["enum"] = ["research-team", "second-team"]
        schema["description"] = "Keep this custom composition schema"
        files[schema_path] = json.dumps(schema, indent=2)
        files["task.yaml"] = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
        version = TaskVersion(task_id=source.task_id,
            version_hash=compute_package_hash({name: text.encode() for name, text in files.items()}),
            sealed_at=datetime.now(timezone.utc).isoformat(), definition=TaskDefinition(**config),
            package_files=files)
        self.app.tasks.loader.validate_package(config, files)
        publish_version(self.settings.paths.task_versions_dir, version)
        self.app.task_repo.save_version(version)
        self.app.tasks.activate_version(version.task_id, version.version_hash)
        return self.app.task_repo.get_active_version(version.task_id)

    def test_separate_markdown_is_stored_exactly_without_template_rewrite(self):
        research = "# 조사\n\n이 지시만 보존.\n"
        email = "# 이메일\n\n배치·서식은 이 문서만 편집.\n"
        version = self.create(task_md=research, email_spec_md=email)
        self.assertEqual(version.package_files["task.md"], research)
        self.assertEqual(version.package_files["email_spec.md"], email)
        self.assertNotIn("email_spec.md", version.definition.instructions["research_files"])
        self.assertIn("email_spec.md", version.definition.instructions["compose_files"])

    def test_editor_reads_actual_contents_and_current_schedule_without_creating_intermediate_state(self):
        version = self.create(schedule_enabled=True)
        self.app.tasks.set_task_enabled(version.task_id, False)
        before = self.app.task_repo.get_task_status(version.task_id)
        editor = self.app.tasks.get_task_editor(version.task_id)
        self.assertEqual(editor["task_md"], version.package_files["task.md"])
        self.assertFalse(editor["schedule_enabled"])
        self.assertEqual(editor["expected_updated_at"], before["updated_at"])
        with self.app.db.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_drafts").fetchone()[0], 0)
        self.assertEqual(self.app.task_repo.get_task_status(version.task_id), before)

    def test_edit_keeps_identity_workspace_history_and_mail_spec(self):
        old = self.create()
        workspace = self.app.workspace_mgr.init_task_workspace(old.task_id)
        marker = workspace / "project" / "existing.txt"
        marker.write_text("persistent operator work")
        sealed = self.settings.paths.task_versions_dir / old.task_id / old.version_hash / "task.md"
        old_bytes = sealed.read_bytes()
        new = self.edit(instructions="# 수정\ninvocation_stage 오타만 수정함.\n")
        self.assertEqual(new.task_id, old.task_id)
        self.assertNotEqual(new.version_hash, old.version_hash)
        self.assertEqual(len(self.app.task_repo.list_versions(old.task_id)), 2)
        self.assertEqual(new.package_files["email_spec.md"], old.package_files["email_spec.md"])
        self.assertEqual(sealed.read_bytes(), old_bytes)
        self.assertFalse(self.app.task_repo.get_version(old.version_hash).is_active)
        self.assertTrue(new.is_active)
        self.assertEqual(self.app.workspace_mgr.get_task_workspace_dir(old.task_id), workspace)
        self.assertEqual(marker.read_text(), "persistent operator work")
        conn = self.app.db.get_connection()
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM scheduled_runs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM smtp_attempts").fetchone()[0], 0)
        finally:
            conn.close()

    def test_stale_edit_rejected_but_exact_replay_does_not_reenable_schedule(self):
        self.create()
        form = self.app.tasks.get_task_editor("policy-digest")
        new = self.edit(form=form, name="Changed once", schedule_enabled=True)
        with self.assertRaisesRegex(ValidationError, "changed"):
            self.edit(form=form, name="Conflicting stale edit")
        self.app.tasks.set_task_enabled(new.task_id, False)
        duplicate = self.edit(form=form, name="Changed once", schedule_enabled=True)
        self.assertEqual(duplicate.version_hash, new.version_hash)
        self.assertFalse(self.app.task_repo.get_task_status(new.task_id)["enabled"])

    def test_concurrent_different_edits_have_exactly_one_winner(self):
        self.create()
        form = self.app.tasks.get_task_editor("policy-digest")
        def update(name):
            try:
                return self.edit(form=form, name=name)
            except ValidationError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, ["First update", "Second update"]))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertEqual(len(self.app.task_repo.list_versions("policy-digest")), 2)

    def test_edit_preserves_disabled_delivery_and_current_schedule(self):
        old = self.create(schedule_enabled=True)
        self.app.tasks.approve_delivery(old.task_id, False)
        self.app.tasks.set_task_enabled(old.task_id, False)
        self.edit(name="Typos corrected")
        status = self.app.task_repo.get_task_status(old.task_id)
        self.assertEqual(status["delivery_mode"], "disabled")
        self.assertFalse(status["enabled"])

    def test_editor_conflicts_with_new_schedule_toggle(self):
        self.create()
        form = self.app.tasks.get_task_editor("policy-digest")
        self.app.tasks.set_task_enabled("policy-digest", True)
        with self.assertRaisesRegex(ValidationError, "changed"):
            self.edit(form=form, name="Must not disable new schedule")
        self.assertTrue(self.app.task_repo.get_task_status("policy-digest")["enabled"])

    def test_same_package_edit_can_explicitly_reenable_schedule(self):
        version = self.create(schedule_enabled=True)
        self.app.tasks.set_task_enabled(version.task_id, False)
        form = self.app.tasks.get_task_editor(version.task_id)
        result = self.edit(form=form, schedule_enabled=True)
        self.assertEqual(result.version_hash, version.version_hash)
        self.assertTrue(self.app.task_repo.get_task_status(version.task_id)["enabled"])
        self.app.tasks.set_task_enabled(version.task_id, False)
        with self.assertRaisesRegex(ValidationError, "changed"):
            self.edit(form=form, schedule_enabled=True)

    def test_pending_run_and_smtp_statuses_block_edit_without_changing_run(self):
        old = self.create()
        run = self.app.runs.enqueue_run(old.task_id, request_key="existing-user-run")
        for status in ("queued", "running", "awaiting_receipt"):
            with self.app.db.transaction() as conn:
                conn.execute("UPDATE scheduled_runs SET status=? WHERE run_id=?", (status, run.run_id))
            with self.subTest(status=status), self.assertRaisesRegex(ValidationError, "Wait for it to finish"):
                self.edit(name="Wait until existing execution completes")
            self.assertEqual(self.app.task_repo.get_active_version(old.task_id).version_hash, old.version_hash)
            self.assertEqual(self.app.run_repo.get_run(run.run_id).task_version_hash, old.version_hash)

    def test_manual_enqueue_rechecks_version_after_concurrent_edit(self):
        self.create()
        create_run = self.app.run_repo.create_run
        def edit_then_enqueue(run, **kwargs):
            self.edit(name="Published between Run click and queue transaction")
            return create_run(run, **kwargs)
        with patch.object(self.app.run_repo, "create_run", side_effect=edit_then_enqueue):
            with self.assertRaisesRegex(ValidationError, "before this run was queued"):
                self.app.runs.enqueue_run("policy-digest", request_key="interleaved-manual-run")
        self.assertEqual(self.app.run_repo.list_runs(), [])
        self.assertEqual(self.app.task_repo.get_active_version("policy-digest").definition.name,
                         "Published between Run click and queue transaction")

    def test_completed_dry_run_prepared_archive_does_not_block_edit(self):
        old = self.create()
        run = self.app.runs.enqueue_run(old.task_id, force_dry_run=True, request_key="previous-dry-run")
        with self.app.db.transaction() as conn:
            conn.execute("UPDATE scheduled_runs SET status='succeeded' WHERE run_id=?", (run.run_id,))
        self.app.delivery_repo.save_handoff(DeliveryHandoff(handoff_id="dry-archive", idempotency_key="dry-key",
            run_id=run.run_id, task_id=old.task_id, task_version_hash=old.version_hash,
            message_revision=1, message_type="research_digest", recipient_group_id="research-team",
            mode="dry_run", status="prepared", delivery_request={}, delivery_request_sha256="0" * 64))
        edited = self.edit(name="Normal edit after a completed dry-run")
        self.assertNotEqual(edited.version_hash, old.version_hash)

    def test_explicit_candidate_keeps_dry_run_and_original_version(self):
        old = self.create()
        self.edit(name="Current version")
        candidate = self.app.runs.enqueue_run(old.task_id, candidate_version_hash=old.version_hash,
            trigger_type="candidate_dry_run", request_key="historical-candidate")
        self.assertEqual(candidate.task_version_hash, old.version_hash)
        self.assertTrue(self.app.run_repo.get_execution_controls(candidate.run_id)["force_dry_run"])

    def test_custom_package_edit_preserves_all_assets_schemas_and_phase_files(self):
        old = self.custom_package()
        new = self.edit(instructions=old.package_files["task.md"] + "Corrected typo.\n")
        for path, text in old.package_files.items():
            if path not in {"task.md", "task.yaml"}:
                self.assertEqual(new.package_files[path], text, path)
        self.assertEqual(new.definition.instructions, old.definition.instructions)
        self.assertEqual(new.definition.output, old.definition.output)
        self.assertEqual(new.definition.description, old.definition.description)
        self.assertEqual(new.definition.runner["timeout_seconds"], 4500)
        self.assertEqual(new.definition.delivery["partial_policy"], "hold")
        self.assertEqual(new.definition.delivery["allowed_recipient_group_ids"], ["research-team", "second-team"])

    def test_changing_recipient_updates_only_custom_routing_enum(self):
        old = self.custom_package()
        new = self.edit(recipient_group_id="second-team")
        path = old.definition.output["composition_schema"]
        old_schema = json.loads(old.package_files[path])
        new_schema = json.loads(new.package_files[path])
        old_schema["properties"]["recipient_group_id"]["enum"] = ["second-team"]
        self.assertEqual(new_schema, old_schema)
        self.assertEqual(new.definition.delivery["allowed_recipient_group_ids"], ["second-team"])
        self.assertEqual(new.package_files["email_spec.md"], old.package_files["email_spec.md"])

    def test_clone_uses_complete_source_package_and_does_not_change_original(self):
        old = self.custom_package()
        clone = self.create(task_id="cloned-policy", name="Copy of real work", source_task_id=old.task_id,
            source_version_hash=old.version_hash, task_md=old.package_files["task.md"],
            email_spec_md=old.package_files["email_spec.md"])
        self.assertFalse(self.app.task_repo.get_task_status(clone.task_id)["enabled"])
        self.assertEqual(clone.definition.output, old.definition.output)
        for path, text in old.package_files.items():
            if path != "task.yaml":
                self.assertEqual(clone.package_files[path], text, path)
        self.assertEqual(self.app.task_repo.get_active_version(old.task_id).version_hash, old.version_hash)
        with self.app.db.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_drafts").fetchone()[0], 0)
        self.assertNotEqual(self.app.workspace_mgr.get_task_workspace_dir(clone.task_id),
                            self.app.workspace_mgr.get_task_workspace_dir(old.task_id))

    def test_clone_rejects_same_id_or_changed_source(self):
        old = self.create()
        with self.assertRaises(ValidationError):
            self.create(source_task_id=old.task_id)
        self.edit(name="Changed since clone opened")
        with self.assertRaisesRegex(ValidationError, "source task changed"):
            self.create(task_id="cloned-policy", source_task_id=old.task_id, source_version_hash=old.version_hash)

    def test_selected_sender_is_saved_as_id_without_account_details(self):
        old = self.create(sender_profile_id="team-sender")
        self.assertEqual(old.definition.delivery["sender_profile_id"], "team-sender")
        self.assertNotIn("sender@example.test", "".join(old.package_files.values()))
        new = self.edit(sender_profile_id="default")
        self.assertEqual(new.definition.delivery["sender_profile_id"], "default")
        with self.assertRaises(ValidationError):
            self.edit(sender_profile_id="unknown-sender")
        with self.assertRaises(ValidationError):
            self.edit(sender_profile_id="mail@example.test")

    def test_empty_email_spec_is_not_silently_replaced_with_default(self):
        self.create()
        with self.assertRaises(ValidationError):
            self.edit(email_spec_md="")


if __name__ == "__main__":
    unittest.main()
