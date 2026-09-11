"""Publication boundary regression: local runtime material never enters the pack."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release_check.py"


@unittest.skipUnless(SCRIPT.is_file(), "source release tooling is not installed in the wheel")
class ReleaseInventoryTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("release_check", SCRIPT)
        self.checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.checker)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(self.checker, "ROOT", self.root).start()
        for name in self.checker.TOP_FILES - self.checker.GENERATED:
            (self.root / name).write_text("synthetic source")
        for name in self.checker.TREES:
            (self.root / name).mkdir()

    def test_operational_settings_and_database_sidecars_are_rejected(self):
        for name in (".env", ".env.production", "settings.yaml", "auth.json",
                     "credentials.json", "delivery_config.yaml", "secret.KEY",
                     "state.sqlite3-wal", "state.db-shm"):
            with self.subTest(name=name):
                path = self.root / "examples" / name
                path.write_text("synthetic private content")
                with self.assertRaises(ValueError):
                    self.checker.inventory()
                path.unlink()

    def test_top_level_source_symlink_cannot_export_another_directory(self):
        external = Path(self.temp.name) / "external"
        external.mkdir()
        (external / "note.md").write_text("synthetic outside content")
        (self.root / "docs").rmdir()
        (self.root / "docs").symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.checker.inventory()

    def test_generated_output_symlink_cannot_overwrite_an_external_file(self):
        external = Path(self.temp.name) / "outside"
        external.write_text("synthetic outside content")
        for name in self.checker.GENERATED:
            path = self.root / name
            path.symlink_to(external)
            with self.assertRaises(ValueError):
                self.checker.inventory()
            self.assertEqual(external.read_text(), "synthetic outside content")
            path.unlink()

    def test_hardlink_cannot_export_an_external_file(self):
        external = Path(self.temp.name) / "outside"
        external.write_text("synthetic outside content")
        os.link(external, self.root / "docs" / "note.md")
        with self.assertRaises(ValueError):
            self.checker.inventory()

    def test_explicit_placeholder_settings_remain_distributable(self):
        path = self.root / "examples" / "settings.example.yaml"
        path.write_text("example: true")
        self.assertIn("examples/settings.example.yaml", self.checker.inventory())
