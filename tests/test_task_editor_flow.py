"""Task editor defaults and preservation across the new step/tab presentation."""

import re
import shutil
import subprocess
import unittest

from researchops.web.task_editor import task_editor_body
from tests.test_task_editor_web import OperatorForms


class TaskEditorFlowTests(unittest.TestCase):
    def render(self, values=None, mode="create", groups=None):
        return task_editor_body(values, mode=mode,
            recipient_groups=groups if groups is not None else {"group-opaque": ["hidden@example.test"]},
            recipient_names={"group-opaque": "조사팀"},
            sender_profiles={"sender-opaque": {}}, sender_names={"sender-opaque": "보고서 계정"})

    def test_new_task_defaults_to_save_only_with_no_internal_id_input(self):
        body = self.render()
        fields = OperatorForms(body).find("/tasks/production/create")
        self.assertEqual(fields["launch_mode"], "save")
        self.assertNotIn("schedule_enabled", fields)
        self.assertEqual(fields["task_id"], "")
        self.assertNotIn('id="task-id"', body)
        visible = re.findall(r'<input\b[^>]*\bname="([^\"]+)"[^>]*>', body)
        self.assertIn("name", visible)
        self.assertEqual(re.findall(r'data-editor-panel="(.*?)"', body),
                         ["research", "email", "execution", "review"])
        self.assertNotIn("hidden@example.test", body)

    def test_edit_keeps_schedule_and_independent_documents_and_revision_guards(self):
        value = {"task_id": "same-id", "name": "같은 Task", "schedule_enabled": True,
            "task_md": "# 원본\n<태그> 조사 규칙\n", "email_spec_md": "메일 규격\n",
            "expected_version_hash": "version-before", "expected_updated_at": "updated-before",
            "cron": "*/15 8-18 * * 1,3,5", "recipient_routing_mode": "legacy_ids",
            "recipient_group_id": "group-opaque", "sender_profile_id": "sender-opaque",
            "stage_settings": {"research": {"type": "codex_exec", "model": "preserved-research"},
                               "compose": {"type": "antigravity_exec", "model": "preserved-compose"}}}
        body = self.render(value, mode="edit")
        fields = OperatorForms(body).find("/tasks/same-id/edit")
        for name in ("task_id", "name", "task_md", "email_spec_md", "expected_version_hash", "expected_updated_at", "cron", "recipient_routing_mode"):
            self.assertEqual(fields[name], value[name], name)
        self.assertEqual(fields["schedule_enabled"], "true")
        self.assertEqual(fields["research_model"], "preserved-research")
        self.assertEqual(fields["compose_model"], "preserved-compose")
        self.assertNotIn("launch_mode", fields)
        self.assertEqual(re.findall(r'data-editor-panel="(.*?)"', body), ["research", "email", "execution"])

    def test_clone_starts_save_only_and_retains_source_guard_and_old_routing(self):
        values = {"task_id": "", "source_task_id": "source", "source_version_hash": "immutable-source",
                  "task_md": "그룹의 기존 ID를 그대로 사용", "email_spec_md": "기존 규격",
                  "recipient_group_id": "group-opaque", "schedule_enabled": False}
        fields = OperatorForms(self.render(values, "clone")).find("/tasks/production/create")
        self.assertEqual(fields["launch_mode"], "save")
        self.assertNotIn("schedule_enabled", fields)
        self.assertEqual(fields["source_task_id"], "source")
        self.assertEqual(fields["source_version_hash"], "immutable-source")
        self.assertEqual(fields["recipient_routing_mode"], "legacy_ids")
        self.assertEqual(fields["task_md"], values["task_md"])

    def test_error_rerender_keeps_explicit_launch_choice_and_nonregistered_sender(self):
        values = {"name": "보존할 이름", "task_md": "보존할 지시", "launch_mode": "run", "sender_profile_id": "missing"}
        body = self.render(values)
        fields = OperatorForms(body).find("/tasks/production/create")
        self.assertEqual(fields["launch_mode"], "run")
        self.assertEqual(fields["sender_profile_id"], "missing")
        self.assertEqual(fields["name"], values["name"])
        self.assertEqual(fields["task_md"], values["task_md"])

    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_inline_javascript_syntax(self):
        scripts = re.findall(r"<script>(.*?)</script>", self.render(), re.S)
        self.assertGreaterEqual(len(scripts), 3)
        for script in scripts:
            result = subprocess.run(["node", "--check"], input=script, text=True,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_existing_task_links_to_direct_advanced_settings(self):
        body = self.render({"task_id": "same-id", "expected_version_hash": "active",
                            "expected_updated_at": "before"}, "edit")
        self.assertIn('href="/tasks/same-id/advanced"', body)
        self.assertNotIn("/tasks/drafts", body)
        self.assertNotIn("임시 저장", body)


if __name__ == "__main__":
    unittest.main()
