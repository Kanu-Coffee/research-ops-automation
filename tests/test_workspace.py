"""Unit tests for researchops workspace management and security."""

import os
import shutil
import tempfile
import unittest
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from researchops.config import Settings, PathsConfig
from researchops.errors import ConcurrencyError, WorkspaceError
from researchops.workspace.manager import WorkspaceManager
from researchops.workspace.security import assert_path_contained, safe_file_info, read_safe_bytes


class TestWorkspace(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.ws_root = Path(self.temp_dir) / "workspaces"
        self.ws_root.mkdir(parents=True)
        paths = PathsConfig(
            repo_root=Path(self.temp_dir),
            tasks_dir=Path(self.temp_dir) / "tasks",
            data_dir=Path(self.temp_dir) / "var",
            task_workspaces_dir=self.ws_root,
            run_archive_dir=Path(self.temp_dir) / "var/run-archive",
            delivery_outbox_dir=Path(self.temp_dir) / "var/delivery-outbox",
            receipts_dir=Path(self.temp_dir) / "var/receipts",
            task_drafts_dir=Path(self.temp_dir) / "var/task-drafts",
            task_versions_dir=Path(self.temp_dir) / "var/task-versions",
            database=Path(self.temp_dir) / "var/db.sqlite",
            schemas_dir=Path(self.temp_dir) / "schemas"
        )
        self.settings = Settings(paths=paths)
        self.mgr = WorkspaceManager(self.settings)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_path_containment_and_traversal_prevention(self):
        root = Path(self.temp_dir) / "safe_root"
        root.mkdir()
        safe_file = root / "inside.txt"
        safe_file.write_text("safe")

        # Contained path should pass
        resolved = assert_path_contained(safe_file, root)
        self.assertEqual(resolved, safe_file.resolve())

        # Path traversal should be rejected
        escape_file = root / "../outside.txt"
        with self.assertRaises(WorkspaceError):
            assert_path_contained(escape_file, root)

        # Invalid task_id with slash or dot-dot should fail
        with self.assertRaises(WorkspaceError):
            self.mgr.get_task_workspace_dir("../etc")
        with self.assertRaises(WorkspaceError):
            self.mgr.get_task_workspace_dir("sub/dir")

    def test_symlink_escape_prevention(self):
        root = Path(self.temp_dir) / "safe_root_sym"
        root.mkdir()
        outside = Path(self.temp_dir) / "secret.txt"
        outside.write_text("secret")

        symlink = root / "escape_link.txt"
        os.symlink(outside, symlink)

        with self.assertRaises(WorkspaceError):
            assert_path_contained(symlink, root)

        with self.assertRaises(WorkspaceError):
            safe_file_info(symlink, root)

    def test_task_workspace_and_attempt_staging(self):
        ws_dir = self.mgr.init_task_workspace("task-1")
        self.assertTrue((ws_dir / "project").exists())

        in_dir, tmp_dir, out_dir = self.mgr.prepare_attempt_staging(
            task_id="task-1",
            run_id="run-001",
            attempt=1,
            phase="research"
        )
        self.assertTrue(in_dir.exists())
        self.assertTrue(tmp_dir.exists())
        self.assertTrue(out_dir.exists())
        self.assertIn("attempt-1/research/input", str(in_dir))

        # Cleanup staging
        self.mgr.cleanup_attempt_staging("task-1", "run-001", 1)
        self.assertFalse(in_dir.exists())
        # But persistent project/ must remain untouched!
        self.assertTrue((ws_dir / "project").exists())

    def test_workspace_concurrency_lock(self):
        # Acquire lock for run-1
        self.assertTrue(self.mgr.acquire_workspace_lock("task-1", "run-1", "token-1"))

        # Second attempt from run-2 must raise ConcurrencyError
        with self.assertRaises(ConcurrencyError):
            self.mgr.acquire_workspace_lock("task-1", "run-2", "token-2")

        # Release lock
        self.mgr.release_workspace_lock("task-1", "run-1", "token-1", child_cleanup_verified=True)

        # Now run-2 can acquire
        self.assertTrue(self.mgr.acquire_workspace_lock("task-1", "run-2", "token-2"))

    def test_invalid_run_identity_does_not_escape_workspace(self):
        with self.assertRaises(WorkspaceError):
            self.mgr.prepare_attempt_staging("task-1", "../../outside", 1, "research")
        with self.assertRaises(WorkspaceError):
            self.mgr.prepare_attempt_staging("task-1", "run-1", 0, "research")

    def test_lock_requires_matching_fence_and_cleanup_evidence(self):
        self.mgr.acquire_workspace_lock("task-1", "run-1", "token-1")
        with self.assertRaises(ConcurrencyError):
            self.mgr.acquire_workspace_lock("task-1", "run-1", "token-2")
        with self.assertRaises(ConcurrencyError):
            self.mgr.release_workspace_lock("task-1", "run-1", "token-1")
        with self.assertRaises(ConcurrencyError):
            self.mgr.force_unlock_workspace("task-1")
        self.assertTrue(self.mgr.is_locked("task-1")[0])

    def test_corrupt_lock_stays_locked(self):
        workspace = self.mgr.init_task_workspace("task-1")
        (workspace / ".lock.json").write_text("{")
        self.assertTrue(self.mgr.is_locked("task-1")[0])
        with self.assertRaises(ConcurrencyError):
            self.mgr.acquire_workspace_lock("task-1", "run-1", "token-1")

    def test_concurrent_acquire_has_one_owner(self):
        def acquire(index):
            try:
                self.mgr.acquire_workspace_lock("task-1", f"run-{index}", f"token-{index}")
                return True
            except ConcurrencyError:
                return False
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(acquire, range(16)))
        self.assertEqual(sum(results), 1)

    def test_reject_internal_symlink_hardlink_fifo_and_oversize(self):
        root = self.ws_root
        regular = root / "regular"
        regular.write_bytes(b"valid")
        (root / "symlink").symlink_to(regular)
        os.link(regular, root / "hardlink")
        os.mkfifo(root / "fifo")
        for filename in ("regular", "symlink", "hardlink", "fifo"):
            with self.subTest(filename=filename), self.assertRaises(WorkspaceError):
                read_safe_bytes(root / filename, root)
        standalone = root / "standalone"
        standalone.write_bytes(b"12345")
        with self.assertRaises(WorkspaceError):
            read_safe_bytes(standalone, root, 4)
        self.assertEqual(read_safe_bytes(standalone, root, 5), b"12345")

    def test_snapshot_excludes_credentials_and_links(self):
        workspace = self.mgr.init_task_workspace("task-1")
        project = workspace / "project"
        (project / ".codex").mkdir()
        (project / ".codex/auth.json").write_text("test-only")
        (project / ".gemini").symlink_to("/does-not-exist")
        (project / "safe.txt").write_text("safe")
        snapshot = self.mgr.snapshot_workspace("task-1")
        with tarfile.open(snapshot) as archive:
            names = archive.getnames()
        self.assertIn("task-1/project/safe.txt", names)
        self.assertFalse(any(".codex" in name or ".gemini" in name for name in names))
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
