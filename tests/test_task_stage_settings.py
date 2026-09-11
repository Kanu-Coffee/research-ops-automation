"""Task stage choices preserve immutable packages and unrelated operator fields."""
import copy
import unittest
from unittest.mock import Mock
from researchops.errors import ValidationError
from tests import test_task_editing as fixtures


class TaskStageSettingsTests(unittest.TestCase):
    setUp = fixtures.TestTaskEditing.setUp
    create = fixtures.TestTaskEditing.create
    edit = fixtures.TestTaskEditing.edit

    def stages(self):
        return {"research": {"type": "antigravity_exec", "model": "synthetic-agy", "reasoning_effort": None},
                "compose": {"type": "codex_exec", "model": "synthetic-codex", "reasoning_effort": "max"}}

    def catalog(self):
        self.app.tasks.model_catalog = Mock()
        self.app.tasks.model_catalog.validate_stage.side_effect = lambda value, legacy=None: copy.deepcopy(value)

    def test_create_edit_and_clone_keep_independent_stages(self):
        self.catalog()
        stages = self.stages()
        first = self.create(stage_settings=stages)
        self.assertEqual(first.definition.runner["stages"], stages)
        self.assertEqual(self.app.tasks.get_task_editor(first.task_id)["stage_settings"], stages)
        changed = copy.deepcopy(stages)
        changed["compose"]["reasoning_effort"] = "ultra"
        second = self.edit(stage_settings=changed)
        self.assertNotEqual(first.version_hash, second.version_hash)
        self.assertEqual(self.app.task_repo.get_version(first.version_hash).definition.runner["stages"], stages)
        self.assertEqual(second.definition.runner["stages"]["research"], stages["research"])
        for name in ("task.md", "email_spec.md"):
            self.assertEqual(first.package_files[name], second.package_files[name])
        clone = self.create(task_id="stage-clone", source_task_id=second.task_id,
                            source_version_hash=second.version_hash, stage_settings=changed)
        self.assertEqual(clone.definition.runner["stages"], changed)

    def test_legacy_edit_without_new_fields_keeps_stage_config(self):
        self.catalog()
        first = self.create(stage_settings=self.stages())
        second = self.edit(name="Renamed")
        self.assertEqual(first.definition.runner["stages"], second.definition.runner["stages"])

    def test_advanced_document_edit_roundtrips_both_stages(self):
        self.catalog()
        first = self.create(stage_settings=self.stages())
        form = self.app.tasks.get_task_advanced_editor(first.task_id)
        updated = self.app.tasks.update_production_task_advanced(first.task_id,
            form["expected_version_hash"], expected_updated_at=form["expected_updated_at"],
            config_yaml=form["config_yaml"], task_md=form["task_md"] + "\nEdited instructions.",
            email_spec_md=form["email_spec_md"])
        self.assertEqual(updated.definition.runner["stages"], self.stages())

    def test_partial_task_stage_map_cannot_be_saved(self):
        self.catalog()
        first = self.create()
        with self.assertRaises(ValidationError):
            self.edit(stage_settings={"compose": self.stages()["compose"]})
        self.assertEqual(self.app.task_repo.get_active_version(first.task_id).version_hash, first.version_hash)
