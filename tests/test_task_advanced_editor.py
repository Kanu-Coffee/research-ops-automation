"""Direct advanced edits preserve immutable history and publish under CAS guards."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from researchops.delivery.smtp_config import BuiltinDeliveryConfig, save_delivery_config
from researchops.errors import ValidationError
from researchops.services.application import ApplicationService
from researchops.web.task_advanced_editor import render_task_advanced_editor
from tests.package_support import register_package
from tests.support import isolated_settings


class TestTaskAdvancedEditor(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = isolated_settings(Path(temp.name))
        self.settings.environment = "production"
        self.app = ApplicationService(self.settings)
        save_delivery_config(BuiltinDeliveryConfig(recipient_groups={"team": ["fixture@example.test"]}),
                             self.settings.paths.delivery_config_file)
        self.source = self.app.tasks.create_production_task(task_id="advanced-task", name="고급 설정 시험",
            task_md="# 조사\n원본 조사 지시입니다.\n", instructions="원본 조사 지시입니다.",
            email_spec_md="# 이메일\n원본 메일 규격입니다.\n", recipient_group_id="team")

    def form(self):
        return self.app.tasks.get_task_advanced_editor(self.source.task_id)

    def save(self, form=None, **changes):
        values = dict(form or self.form())
        values.update(changes)
        return self.app.tasks.update_production_task_advanced(self.source.task_id, values["expected_version_hash"],
            **{key: values[key] for key in ("config_yaml", "task_md", "email_spec_md", "expected_updated_at")})

    def assert_no_execution(self):
        with self.app.db.get_connection() as conn:
            for table in ("task_drafts", "scheduled_runs", "smtp_attempts", "delivery_handoffs"):
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0, table)

    def test_get_is_nonmutating_and_reads_exact_active_documents(self):
        before = self.app.task_repo.get_task_status(self.source.task_id)
        values = self.form()
        self.assertEqual(values["config_yaml"], self.source.package_files["task.yaml"])
        self.assertEqual(values["task_md"], self.source.package_files["task.md"])
        self.assertEqual(values["email_spec_md"], self.source.package_files["email_spec.md"])
        self.assertEqual(before, self.app.task_repo.get_task_status(self.source.task_id))
        self.assertEqual(len(self.app.task_repo.list_versions(self.source.task_id)), 1)
        self.assert_no_execution()

    def test_save_creates_version_and_preserves_old_bytes_without_running(self):
        form = self.form()
        text = form["config_yaml"] + "\n# Preserve exact operator YAML formatting.\n"
        saved = self.save(form, config_yaml=text, task_md="# 수정\n조사 지시 수정.")
        self.assertEqual(saved.task_id, self.source.task_id)
        self.assertEqual(saved.package_files["task.yaml"], text)
        self.assertNotEqual(saved.version_hash, self.source.version_hash)
        self.assertEqual(self.app.task_repo.get_version(self.source.version_hash).package_files, self.source.package_files)
        self.assertEqual(self.app.task_repo.get_active_version(saved.task_id).version_hash, saved.version_hash)
        self.assert_no_execution()

    def test_supplemental_files_and_dedupe_policy_survive_document_edit(self):
        files = dict(self.source.package_files)
        config = yaml.safe_load(files["task.yaml"])
        config["state"]["dedupe"] = {"enabled": True, "key_fields": ["title"], "content_fields": ["summary"]}
        config["instructions"]["research_files"].append("rules/extra.md")
        files["rules/extra.md"] = "Exact supplemental instructions.\n"
        files["assets/layout.html"] = "<table>Custom layout</table>\n"
        files["task.yaml"] = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
        register_package(self.app, files, active=True)
        form = self.form()
        saved = self.save(form, email_spec_md="# 이메일\n수정된 규격.")
        self.assertEqual(saved.definition.state, config["state"])
        for name in form["supplemental_files"]:
            self.assertEqual(saved.package_files[name], files[name])
        self.assert_no_execution()

    def test_preserves_runtime_schedule_even_if_package_flag_is_older(self):
        self.app.tasks.set_task_enabled(self.source.task_id, True)
        saved = self.save(task_md="# 조사\n시간 설정을 바꾸지 않는 수정.")
        self.assertTrue(self.app.task_repo.get_task_status(saved.task_id)["enabled"])
        self.assertFalse(saved.definition.enabled)
        self.assert_no_execution()

    def test_yaml_cannot_reassign_identity_owner_or_execution_controls(self):
        form = self.form()
        original = yaml.safe_load(form["config_yaml"])
        for change in ({"id": "other-task"}, {"owner_user_id": "other-user"}, {"enabled": True},
                       {"delivery": {**original["delivery"], "mode": "dry_run"}}):
            config = {**original, **change}
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.save(form, config_yaml=yaml.safe_dump(config))
        self.assertEqual(self.app.task_repo.get_active_version(self.source.task_id).version_hash, self.source.version_hash)
        self.assert_no_execution()

    def test_invalid_yaml_schema_paths_and_references_do_not_publish(self):
        form = self.form()
        for mutate in (lambda c: c["schedule"].update(timezone="UTC"),
                       lambda c: c["instructions"]["research_files"].append("../outside.md"),
                       lambda c: c["delivery"].update(sender_profile_id="missing"),
                       lambda c: c["delivery"].update(allowed_recipient_group_ids=["missing"])):
            config = yaml.safe_load(form["config_yaml"])
            mutate(config)
            with self.assertRaises(ValidationError):
                self.save(form, config_yaml=yaml.safe_dump(config))
        with self.assertRaises(ValidationError):
            self.save(form, config_yaml="invalid: [yaml")
        self.assertEqual(len(self.app.task_repo.list_versions(self.source.task_id)), 1)
        self.assert_no_execution()

    def test_empty_research_and_missing_revision_are_rejected(self):
        for changes in ({"task_md": "  "}, {"expected_updated_at": ""}):
            with self.assertRaises(ValidationError):
                self.save(**changes)
        self.assert_no_execution()

    def test_exact_duplicate_submission_does_not_create_another_version(self):
        form = self.form()
        first = self.save(form, task_md="# 동일 요청\n중복 저장.")
        second = self.save(form, task_md="# 동일 요청\n중복 저장.")
        self.assertEqual(first.version_hash, second.version_hash)
        self.assertEqual(len(self.app.task_repo.list_versions(self.source.task_id)), 2)
        self.assert_no_execution()

    def test_stale_edit_cannot_overwrite_newer_version(self):
        form = self.form()
        first = self.save(form, task_md="# 먼저 저장한 내용")
        with self.assertRaises(ValidationError):
            self.save(form, task_md="# 오래된 탭의 다른 내용")
        self.assertEqual(self.app.task_repo.get_active_version(self.source.task_id).version_hash, first.version_hash)
        self.assert_no_execution()

    def test_concurrent_saves_only_publish_one_version(self):
        form = self.form()
        def save(text):
            try:
                return self.save(form, task_md=text)
            except ValidationError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ["# 첫 번째 변경", "# 두 번째 변경"]))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.app.task_repo.list_versions(self.source.task_id)), 2)
        self.assert_no_execution()

    def test_schedule_change_between_render_and_save_causes_conflict(self):
        form = self.form()
        self.app.tasks.set_task_enabled(self.source.task_id, True)
        with self.assertRaises(ValidationError):
            self.save(form, task_md="# 오래된 화면")
        self.assertTrue(self.app.task_repo.get_task_status(self.source.task_id)["enabled"])
        self.assert_no_execution()

    def test_run_enqueued_between_validation_and_commit_blocks_publication(self):
        original = self.app.task_repo.publish_production_update
        def concurrent_run(*args, **kwargs):
            self.app.runs.enqueue_run(self.source.task_id, request_key="race-fixture")
            return original(*args, **kwargs)
        with patch.object(self.app.task_repo, "publish_production_update", side_effect=concurrent_run):
            with self.assertRaises(ValidationError):
                self.save(task_md="# 실행과 경합하는 수정")
        self.assertEqual(self.app.task_repo.get_active_version(self.source.task_id).version_hash, self.source.version_hash)
        self.assertEqual(len(self.app.task_repo.list_versions(self.source.task_id)), 1)

    def test_renderer_escapes_posted_values_and_keeps_guard_and_save_bar(self):
        values = {**self.form(), "task_md": "</textarea><script>danger()</script>", "request_key": "same-request"}
        body = render_task_advanced_editor(values, error="YAML 형식을 확인하세요.")
        self.assertIn('&lt;/textarea&gt;&lt;script&gt;', body)
        self.assertNotIn('<script>danger()', body)
        self.assertIn('name="expected_version_hash"', body)
        self.assertIn('name="expected_updated_at"', body)
        self.assertIn('name="request_key" value="same-request"', body)
        self.assertIn('class="save-bar"', body)
        self.assertIn('role="tablist"', body)
        self.assertNotIn('/tasks/drafts', body)


if __name__ == "__main__":
    unittest.main()
