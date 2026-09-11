"""Persistent task workspace and attempt staging manager."""

import json
import fcntl
import os
import re
import stat
from contextlib import contextmanager
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from researchops.config import Settings
from researchops.errors import ConcurrencyError, WorkspaceError
from researchops.storage.db import Database
from researchops.workspace.security import assert_path_contained, read_safe_bytes


class WorkspaceManager:
    def __init__(self, settings: Settings, db: Optional[Database] = None):
        self.settings = settings
        self.workspaces_root = settings.paths.task_workspaces_dir.resolve()
        self.db = db

    def get_task_workspace_dir(self, task_id: str) -> Path:
        """Derive fixed persistent task workspace path strictly from task_id."""
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", task_id) or ".." in task_id:
            raise WorkspaceError(f"Invalid task_id for workspace: {task_id}")
        return assert_path_contained(self.workspaces_root / task_id, self.workspaces_root)

    def init_task_workspace(self, task_id: str) -> Path:
        """Ensure persistent workspace root and project/ directory exist."""
        ws_dir = self.get_task_workspace_dir(task_id)
        project_dir = ws_dir / "project"
        assert_path_contained(project_dir, self.workspaces_root)
        project_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return ws_dir

    def prepare_attempt_staging(
        self,
        task_id: str,
        run_id: str,
        attempt: int,
        phase: str  # research or compose
    ) -> Tuple[Path, Path, Path]:
        """Create and return (input_dir, tmp_dir, output_dir) for an attempt phase."""
        if phase not in ("research", "compose"):
            raise WorkspaceError(f"Invalid staging phase: {phase}")
        self._validate_attempt(run_id, attempt)

        ws_dir = self.init_task_workspace(task_id)
        attempt_dir = ws_dir / ".runs" / run_id / f"attempt-{attempt}" / phase
        input_dir = attempt_dir / "input"
        tmp_dir = attempt_dir / "tmp"
        output_dir = attempt_dir / "output"

        for d in (input_dir, tmp_dir, output_dir):
            assert_path_contained(d, ws_dir)
            d.mkdir(parents=True, exist_ok=True, mode=0o700)

        return input_dir, tmp_dir, output_dir

    def cleanup_attempt_staging(self, task_id: str, run_id: str, attempt: int) -> None:
        """Clean up temporary attempt staging directory after run completes."""
        ws_dir = self.get_task_workspace_dir(task_id)
        self._validate_attempt(run_id, attempt)
        attempt_dir = ws_dir / ".runs" / run_id / f"attempt-{attempt}"
        assert_path_contained(attempt_dir, ws_dir)
        if attempt_dir.exists():
            shutil.rmtree(attempt_dir, ignore_errors=True)

    @staticmethod
    def _validate_attempt(run_id: str, attempt: int) -> None:
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id) or ".." in run_id:
            raise WorkspaceError("Invalid run_id")
        if type(attempt) is not int or attempt < 1:
            raise WorkspaceError("attempt must be a positive integer")

    @contextmanager
    def _lock_guard(self, task_id: str):
        # A stable inode serializes check/update/unlink across threads and processes.
        ws_dir = self.get_task_workspace_dir(task_id)
        guards = assert_path_contained(self.workspaces_root / ".locks", self.workspaces_root)
        guards.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(guards / f"{task_id}.guard", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_nlink != 1:
                raise WorkspaceError("Unsafe workspace lock guard")
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.init_task_workspace(task_id)
            yield ws_dir / ".lock.json"
        finally:
            os.close(fd)

    def _read_lock(self, lock_file: Path) -> Dict[str, Any]:
        try:
            value = json.loads(read_safe_bytes(lock_file, lock_file.parent, 16_384))
            if not isinstance(value, dict) or not value.get("run_id") or not value.get("fencing_token"):
                raise ValueError("missing owner")
            return value
        except Exception as exc:
            raise ConcurrencyError("Workspace lock is corrupt; explicit recovery evidence required") from exc

    def acquire_workspace_lock(self, task_id: str, run_id: str, fencing_token: str) -> bool:
        """Acquire single-concurrency lock for task workspace."""
        self._validate_attempt(run_id, 1)
        if not isinstance(fencing_token, str) or not fencing_token:
            raise WorkspaceError("fencing_token is required")
        with self._lock_guard(task_id) as lock_file:
            if lock_file.exists() or lock_file.is_symlink():
                data = self._read_lock(lock_file)
                if data.get("run_id") == run_id and data.get("fencing_token") == fencing_token:
                    return True
                raise ConcurrencyError(f"Workspace for task '{task_id}' is already locked")
            lock_data = {"task_id": task_id, "run_id": run_id, "fencing_token": fencing_token,
                         "locked_at": datetime.now(timezone.utc).isoformat(), "child_groups": []}
            fd = os.open(lock_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(lock_data, handle)
                handle.flush()
                os.fsync(handle.fileno())
        return True

    def release_workspace_lock(self, task_id: str, run_id: str, fencing_token: str,
                               *, child_cleanup_verified: bool = False) -> None:
        """Release task workspace lock if matching run and token."""
        with self._lock_guard(task_id) as lock_file:
            if not lock_file.exists() and not lock_file.is_symlink():
                return
            data = self._read_lock(lock_file)
            if data.get("run_id") != run_id or data.get("fencing_token") != fencing_token:
                raise ConcurrencyError("Workspace lock owner/fencing token mismatch")
            if not child_cleanup_verified:
                raise ConcurrencyError("Child cleanup evidence is required before releasing workspace")
            lock_file.unlink()

    def force_unlock_workspace(self, task_id: str, *, child_cleanup_verified: bool = False) -> None:
        """Forcefully remove stale workspace lock."""
        if not child_cleanup_verified:
            raise ConcurrencyError("Cannot force unlock without proven descendant termination")
        with self._lock_guard(task_id) as lock_file:
            assert_path_contained(lock_file, lock_file.parent)
            lock_file.unlink(missing_ok=True)


    def is_locked(self, task_id: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
        ws_dir = self.get_task_workspace_dir(task_id)
        lock_file = ws_dir / ".lock.json"
        if not lock_file.exists() and not lock_file.is_symlink():
            return False, None
        try:
            data = self._read_lock(lock_file)
            return True, data
        except Exception:
            return True, {"error": "corrupt lock; explicit recovery required"}

    def inspect_workspace(self, task_id: str) -> Dict[str, Any]:
        ws_dir = self.get_task_workspace_dir(task_id)
        locked, lock_info = self.is_locked(task_id)
        project_dir = ws_dir / "project"

        file_count = 0
        total_bytes = 0
        if ws_dir.exists():
            for f in ws_dir.rglob("*"):
                if not f.is_symlink() and f.is_file():
                    file_count += 1
                    total_bytes += f.stat().st_size

        return {
            "task_id": task_id,
            "workspace_path": str(ws_dir),
            "exists": ws_dir.exists(),
            "locked": locked,
            "lock_info": lock_info,
            "project_exists": project_dir.exists(),
            "total_files": file_count,
            "total_bytes": total_bytes
        }

    def reset_workspace(self, task_id: str) -> None:
        """Reset project/ content inside workspace. Disallowed if active lock exists."""
        with self._lock_guard(task_id):
            locked, lock_info = self.is_locked(task_id)
            if locked:
                raise ConcurrencyError(f"Cannot reset workspace '{task_id}': active lock held by {lock_info}")
            ws_dir = self.get_task_workspace_dir(task_id)
            project_dir = assert_path_contained(ws_dir / "project", ws_dir)
            if project_dir.exists():
                shutil.rmtree(project_dir)
            project_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def purge_workspace(self, task_id: str) -> None:
        """Completely remove task workspace. Disallowed if active lock exists."""
        with self._lock_guard(task_id):
            locked, lock_info = self.is_locked(task_id)
            if locked:
                raise ConcurrencyError(f"Cannot purge workspace '{task_id}': active lock held by {lock_info}")
            ws_dir = self.get_task_workspace_dir(task_id)
            if ws_dir.exists():
                shutil.rmtree(ws_dir)

    def snapshot_workspace(self, task_id: str) -> Path:
        """Create a compressed tar.gz snapshot of the workspace."""
        with self._lock_guard(task_id):
            return self._snapshot_unlocked(task_id)

    def _snapshot_unlocked(self, task_id: str) -> Path:
        locked, lock_info = self.is_locked(task_id)
        if locked:
            raise ConcurrencyError(f"Cannot snapshot workspace '{task_id}': active lock held by {lock_info}")
        ws_dir = self.get_task_workspace_dir(task_id)
        if not ws_dir.exists():
            raise WorkspaceError(f"Workspace for task '{task_id}' does not exist")

        snapshots_dir = self.settings.paths.data_dir / "workspace-snapshots" / task_id
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        now_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        snapshot_path = snapshots_dir / f"snapshot_{task_id}_{now_tag}.tar.gz"

        excluded = {".codex", ".gemini", ".config", ".env", ".lock.json", ".lock.guard"}
        def safe_member(info):
            if any(part in excluded for part in Path(info.name).parts):
                return None
            if info.issym() or info.islnk() or not (info.isfile() or info.isdir()):
                return None
            if info.isfile() and (ws_dir.parent / info.name).lstat().st_nlink != 1:
                return None
            return info
        with snapshot_path.open("xb") as stream:
            os.chmod(snapshot_path, 0o600)
            with tarfile.open(fileobj=stream, mode="w:gz", dereference=False) as tar:
                tar.add(ws_dir, arcname=task_id, filter=safe_member)

        return snapshot_path
