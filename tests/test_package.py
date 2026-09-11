"""Unit tests for researchops immutable package validation."""

import shutil
import tempfile
import unittest
from pathlib import Path
import yaml
import os

from researchops.config import load_settings
from researchops.package.loader import TaskPackageLoader, compute_package_hash
from researchops.package.templates import list_templates, get_template_by_id
from researchops.services.application import ApplicationService
from tests.package_support import template_package, register_package
from researchops.storage.db import Database
from researchops.storage.repositories import TaskRepository, StateRepository
from tests.support import isolated_settings
from researchops.errors import ValidationError


class TestPackage(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.settings = isolated_settings(Path(self.temp_dir))
        self.db_path = Path(self.temp_dir) / "test.db"
        self.db = Database(self.db_path)
        self.db.init_schema()
        self.task_repo = TaskRepository(self.db)
        self.state_repo = StateRepository(self.db)
        self.app = ApplicationService(self.settings)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_canonical_package_hasher(self):
        files_a = {
            "task.yaml": b"version: 2\nid: test",
            "task.md": b"# Test Instruction"
        }
        hash_a1 = compute_package_hash(files_a)
        hash_a2 = compute_package_hash(files_a)
        self.assertEqual(hash_a1, hash_a2)

        files_a_reversed = {
            "task.md": b"# Test Instruction",
            "task.yaml": b"version: 2\nid: test"
        }
        self.assertEqual(hash_a1, compute_package_hash(files_a_reversed))

        files_b = {
            "task.yaml": b"version: 2\nid: test-modified",
            "task.md": b"# Test Instruction"
        }
        self.assertNotEqual(hash_a1, compute_package_hash(files_b))

    def test_template_discovery(self):
        templates = list_templates(self.settings)
        self.assertGreater(len(templates), 0)
        t_ids = [t.template_id for t in templates]
        self.assertIn("software-releases", t_ids)

        release_tpl = get_template_by_id(self.settings, "software-releases")
        self.assertIsNotNone(release_tpl)
        self.assertIn("instructions", release_tpl.config_yaml)

    def test_template_registers_immutable_candidate_without_activation(self):
        files = template_package(self.settings, "new-release-task")
        version = register_package(self.app, files)
        self.assertFalse(version.definition.enabled)
        self.assertEqual(version.definition.delivery["mode"], "dry_run")
        self.assertEqual(len(version.version_hash), 64)
        self.assertIsNone(self.app.task_repo.get_active_version(version.task_id))
        self.assertEqual(self.app.task_repo.get_version(version.version_hash).package_files, files)

    def test_new_bytes_create_new_candidate_without_changing_old_version(self):
        files = template_package(self.settings, "base-task")
        original = register_package(self.app, files, active=True)
        files["task.md"] += "\nChanged fixture instructions.\n"
        candidate = register_package(self.app, files)
        self.assertNotEqual(candidate.version_hash, original.version_hash)
        self.assertEqual(self.app.task_repo.get_active_version(original.task_id).version_hash, original.version_hash)
        self.assertEqual(self.app.task_repo.get_version(original.version_hash).package_files, original.package_files)

    def test_package_rejects_traversal_and_missing_instruction_files(self):
        loader = TaskPackageLoader(self.settings.paths.schemas_dir)
        files = template_package(self.settings, "path-task")
        config = yaml.safe_load(files["task.yaml"])
        for name in ("../outside.md", "missing.md"):
            config["instructions"]["research_files"].append(name)
            with self.assertRaises(ValidationError):
                loader.validate_package(config, files)
            config["instructions"]["research_files"].pop()

    def test_non_seoul_schedule_is_rejected(self):
        files = template_package(self.settings, "seoul-task")
        config = yaml.safe_load(files["task.yaml"])
        config["schedule"]["timezone"] = "UTC"
        files["task.yaml"] = yaml.safe_dump(config)
        with self.assertRaises(ValidationError) as error:
            register_package(self.app, files)
        self.assertTrue(any("Asia/Seoul" in item for item in error.exception.errors))

    def test_package_rejects_symlinks_and_hardlinks(self):
        loader=TaskPackageLoader(self.settings.paths.schemas_dir)
        package=self.settings.paths.tasks_dir/"software-releases"
        outside=Path(self.temp_dir)/"outside.txt"
        outside.write_text("fixture only")
        link=package/"link.txt"
        link.symlink_to(outside)
        with self.assertRaises(ValidationError):
            loader.load_from_dir(package)
        link.unlink()
        os.link(outside,link)
        with self.assertRaises(ValidationError):
            loader.load_from_dir(package)


if __name__ == "__main__":
    unittest.main()
