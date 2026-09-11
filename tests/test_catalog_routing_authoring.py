"""Catalog routing authoring keeps user packages and legacy interfaces intact."""

import json
import unittest

import yaml

from researchops.errors import ValidationError
from researchops.package.production_template import build_production_package
from tests import test_task_editing, test_task_editor_web


class CatalogRoutingAuthoringTests(unittest.TestCase):
    # Reuse the isolated application fixtures without copying operational data.
    setUp = test_task_editing.TestTaskEditing.setUp
    create = test_task_editing.TestTaskEditing.create
    edit = test_task_editing.TestTaskEditing.edit
    custom_package = test_task_editing.TestTaskEditing.custom_package

    def test_catalog_create_requires_no_fixed_group_and_keeps_user_documents(self):
        version = self.create(recipient_group_id="", recipient_routing_mode="catalog_name")
        self.assertEqual(version.definition.delivery["recipient_routing_mode"], "catalog_name")
        self.assertNotIn("allowed_recipient_group_ids", version.definition.delivery)
        schema = json.loads(version.package_files[version.definition.output["composition_schema"]])
        self.assertIn("recipient_group_name", schema["required"])
        self.assertIn("recipient_group_reason", schema["required"])
        self.assertNotIn("recipient_group_id", schema["properties"])
        self.assertNotIn("enum", schema["properties"]["recipient_group_name"])
        self.assertEqual(version.package_files["task.md"], "# 조사\ninvocation_stage에 따라 공식 발표를 조사한다.\n")
        self.assertNotIn("recipient@example.test", "".join(version.package_files.values()))

    def test_mode_switch_preserves_custom_package_and_nonrouting_schema_constraints(self):
        old = self.custom_package()
        new = self.edit(recipient_routing_mode="catalog_name", recipient_group_id="")
        schema_path = old.definition.output["composition_schema"]
        before = json.loads(old.package_files[schema_path])
        after = json.loads(new.package_files[schema_path])
        for key in before:
            if key not in {"properties", "required"}:
                self.assertEqual(after[key], before[key])
        for key in before["properties"]:
            if key != "recipient_group_id":
                self.assertEqual(after["properties"][key], before["properties"][key])
        self.assertEqual(new.definition.output, old.definition.output)
        self.assertEqual(new.definition.runner, old.definition.runner)
        for path in old.package_files:
            if path not in {"task.yaml", schema_path}:
                self.assertEqual(new.package_files[path], old.package_files[path], path)
        # Old callers omitting the new argument must preserve an existing mode.
        changed = self.edit(name="same catalog contract")
        self.assertEqual(changed.definition.delivery["recipient_routing_mode"], "catalog_name")
        self.assertEqual(changed.package_files[schema_path], new.package_files[schema_path])
        restored = self.edit(recipient_routing_mode="legacy_ids", recipient_group_id="second-team")
        restored_schema = json.loads(restored.package_files[schema_path])
        self.assertEqual(restored_schema["properties"]["recipient_group_id"]["enum"], ["second-team"])
        self.assertNotIn("recipient_group_name", restored_schema["required"])

    def test_nested_routing_conditions_fail_without_changing_original_rules(self):
        old = self.custom_package()
        path = old.definition.output["composition_schema"]
        schema = json.loads(old.package_files[path])
        schema["allOf"] = [{"if": {"properties": {"recipient_group_id": {"const": "research-team"}}},
                            "then": {"properties": {"subject": {"minLength": 20}}}}]
        old.package_files[path] = json.dumps(schema)
        before = dict(old.package_files)
        with self.assertRaisesRegex(ValidationError, "수신자 스키마"):
            self.app.tasks._edit_package(old, task_id=old.task_id, name=old.definition.name,
                instructions=old.package_files["task.md"], email_spec_md=None,
                runner_type="codex_exec", recipient_group_id="", sender_profile_id="default",
                cron="0 9 * * *", schedule_enabled=False, model=None,
                recipient_routing_mode="catalog_name")
        self.assertEqual(old.package_files, before)

    def test_nonrouting_composite_schema_constraints_survive_mode_switch(self):
        old = self.custom_package()
        path = old.definition.output["composition_schema"]
        schema = json.loads(old.package_files[path])
        schema["allOf"] = [{"properties": {"subject": {"minLength": 20}}}]
        old.package_files[path] = json.dumps(schema)
        files = self.app.tasks._edit_package(old, task_id=old.task_id, name=old.definition.name,
            instructions=old.package_files["task.md"], email_spec_md=None,
            runner_type="codex_exec", recipient_group_id="", sender_profile_id="default",
            cron="0 9 * * *", schedule_enabled=False, model=None,
            recipient_routing_mode="catalog_name")
        self.assertEqual(json.loads(files[path])["allOf"], schema["allOf"])
        self.app.tasks.loader.validate_package(yaml.safe_load(files["task.yaml"]), files)

    def test_missing_mode_single_group_api_stays_legacy_and_no_group_api_uses_catalog(self):
        legacy = self.create()
        self.assertNotIn("recipient_routing_mode", legacy.definition.delivery)
        self.assertEqual(self.app.tasks.get_task_editor(legacy.task_id)["recipient_routing_mode"], "legacy_ids")
        current = self.create(task_id="no-selected-group", recipient_group_id="")
        self.assertEqual(current.definition.delivery["recipient_routing_mode"], "catalog_name")

    def test_catalog_clone_and_advanced_edit_preserve_mode_and_schema(self):
        source = self.create(recipient_group_id="", recipient_routing_mode="catalog_name")
        clone = self.create(task_id="catalog-copy", recipient_group_id="", source_task_id=source.task_id)
        self.assertEqual(clone.definition.delivery, source.definition.delivery)
        form = self.app.tasks.get_task_advanced_editor(clone.task_id)
        updated = self.app.tasks.update_production_task_advanced(clone.task_id, form["expected_version_hash"],
            expected_updated_at=form["expected_updated_at"], config_yaml=form["config_yaml"],
            task_md=form["task_md"] + "\nPreserve routing rules.", email_spec_md=form["email_spec_md"])
        self.assertEqual(updated.definition.delivery, source.definition.delivery)
        for name in form["supplemental_files"]:
            self.assertEqual(updated.package_files[name], clone.package_files[name])

    def test_catalog_publication_rejects_only_unmapped_or_deleted_groups(self):
        self.app.catalog.bootstrap_delivery(self.config)
        for key in self.config.recipient_groups:
            self.app.catalog.delete("recipient_group", key)
        with self.assertRaisesRegex(ValidationError, "active recipient group"):
            self.create(recipient_group_id="", recipient_routing_mode="catalog_name")
        self.app.catalog.restore("recipient_group", "second-team")
        self.create(recipient_group_id="", recipient_routing_mode="catalog_name")

    def test_catalog_schema_rejects_static_enum_and_conflicting_id_contract(self):
        files = build_production_package(task_id="dynamic", name="Dynamic", instructions="Research",
            runner_type="codex_exec", recipient_group_id="", cron="0 9 * * *", schedule_enabled=False,
            recipient_routing_mode="catalog_name")
        config = yaml.safe_load(files["task.yaml"])
        self.app.tasks.loader.validate_package(config, files)
        for field, constraint in [("recipient_group_name", {"type": "string", "enum": ["fixed"]}),
                                  ("recipient_group_id", {"enum": ["research-team"]})]:
            changed = dict(files)
            schema = json.loads(files["composition.schema.json"])
            schema["properties"][field] = constraint
            changed["composition.schema.json"] = json.dumps(schema)
            with self.assertRaises(ValidationError):
                self.app.tasks.loader.validate_package(config, changed)


class CatalogRoutingWebTests(unittest.TestCase):
    setUp = test_task_editor_web.TaskEditorWebTests.setUp
    request = test_task_editor_web.TaskEditorWebTests.request
    get_form = test_task_editor_web.TaskEditorWebTests.get_form
    create = test_task_editor_web.TaskEditorWebTests.create

    def test_new_form_names_mode_has_no_required_single_group(self):
        self.app.catalog.rename("recipient_group", "research-team", "연구팀 이름")
        fields = self.get_form("/tasks/new", "/tasks/production/create")
        self.assertEqual(fields["recipient_routing_mode"], "catalog_name")
        body = self.request("GET", "/tasks/new")[2].decode()
        self.assertRegex(body, r'<ul[^>]*id="recipient-catalog-names"[^>]*>\s*<li>연구팀 이름</li>')
        self.assertNotIn('name="recipient_group_id" class="form-select" required', body)
        fields.update(task_id="dynamic-ui", name="그룹명 선택", task_md="상품 0건이면 연구팀 이름을 선택한다.",
                      action="create", recipient_group_id="", schedule_enabled="false")
        self.assertEqual(self.request("POST", "/tasks/production/create", fields)[0], 303)
        active = self.app.task_repo.get_active_version("dynamic-ui")
        self.assertEqual(active.definition.delivery["recipient_routing_mode"], "catalog_name")
        self.assertIn("등록 그룹명으로 수신자 선택".encode(), self.request("GET", "/tasks/dynamic-ui")[2])

    def test_legacy_form_round_trip_and_mode_switch_to_catalog(self):
        self.create(recipient_routing_mode="legacy_ids")
        fields = self.get_form("/tasks/operator-task/edit", "/tasks/operator-task/edit")
        self.assertEqual(fields["recipient_routing_mode"], "legacy_ids")
        fields.update(recipient_routing_mode="catalog_name", recipient_group_id="")
        self.assertEqual(self.request("POST", "/tasks/operator-task/edit", fields)[0], 303)
        editor = self.app.tasks.get_task_editor("operator-task")
        self.assertEqual(editor["recipient_routing_mode"], "catalog_name")
        self.assertEqual(editor["task_md"], fields["task_md"])
        clone = self.get_form("/tasks/new?clone=operator-task", "/tasks/production/create")
        self.assertEqual(clone["recipient_routing_mode"], "catalog_name")
        self.assertNotIn("schedule_enabled", clone)


if __name__ == "__main__":
    unittest.main()
