"""System health check and capability doctor."""

import os
import shutil
import subprocess
import stat
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from researchops.config import Settings
from researchops.storage.db import Database
from researchops.workspace.security import read_safe_bytes


class DoctorService:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    def check_all(self) -> Dict[str, Any]:
        results: Dict[str, Any] = {
            "database": self._check_db(),
            "directories": self._check_directories(),
            "schemas": self._check_schemas(),
            "runners": self._check_runners(),
            "mcp": self._check_mcp(),
            "runtime_security": self._check_runtime_security(),
            "timezone": {"ok": self.settings.timezone == "Asia/Seoul", "value": self.settings.timezone},
            "overall_status": "ok"
        }

        # If any essential check fails, overall_status = degraded or error
        if not results["database"]["ok"] or not results["directories"]["ok"]:
            results["overall_status"] = "error"
        elif not results["schemas"]["ok"]:
            results["overall_status"] = "degraded"
        if not results["runtime_security"]["ok"]:
            results["overall_status"] = "error"
        elif not results["runners"]["isolation"]["live_runner_ready"] and results["overall_status"] == "ok":
            results["overall_status"] = "degraded"
        results["internal_fake_ready"] = all(results[key]["ok"] for key in
            ("database", "directories", "schemas", "runtime_security", "timezone"))
        results["live_runner_ready"] = bool(results["internal_fake_ready"] and
                                               results["runners"]["isolation"]["live_runner_ready"])
        results["hostile_task_ready"] = False
        results["provider_authentication"] = "not_checked"

        return results

    run_doctor = check_all

    def _check_db(self) -> Dict[str, Any]:
        try:
            conn = self.db.get_connection()
            row = conn.execute("SELECT sqlite_version();").fetchone()
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
            migrations = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
            conn.close()
            return {"ok": integrity == "ok" and violations == 0, "sqlite_version": row[0],
                    "db_path": str(self.db.db_path), "integrity": integrity,
                    "foreign_key_violations": violations, "migrations": migrations}
        except Exception as e:
            return {"ok": False, "error": str(e), "db_path": str(self.db.db_path)}

    def _check_directories(self) -> Dict[str, Any]:
        p = self.settings.paths
        dirs = [
            ("tasks_dir", p.tasks_dir),
            ("data_dir", p.data_dir),
            ("task_drafts_dir", p.task_drafts_dir),
            ("task_versions_dir", p.task_versions_dir),
            ("task_workspaces_dir", p.task_workspaces_dir),
            ("run_archive_dir", p.run_archive_dir),
            ("delivery_outbox_dir", p.delivery_outbox_dir),
            ("receipts_dir", p.receipts_dir),
        ]
        status = {}
        all_ok = True
        for name, d in dirs:
            exists = d.exists()
            writable = os.access(d, os.W_OK) if exists else False
            if not exists or not writable:
                all_ok = False
            status[name] = {"exists": exists, "writable": writable, "path": str(d)}

        return {"ok": all_ok, "details": status, "directories": status}

    def _check_schemas(self) -> Dict[str, Any]:
        schemas_dir = self.settings.paths.schemas_dir
        expected = [
            "task.schema.json",
            "generic-result.schema.json",
            "composition-input.schema.json",
            "composition-result.schema.json",
            "delivery-request.schema.json",
            "delivery-receipt.schema.json",
            "run-manifest.schema.json",
            "artifact-manifest.schema.json"
        ]
        missing = [s for s in expected if not (schemas_dir / s).exists()]
        return {
            "ok": len(missing) == 0,
            "schemas_dir": str(schemas_dir),
            "missing_schemas": missing
        }

    def _check_runners(self) -> Dict[str, Any]:
        from researchops.runners.probe import RunnerCapabilityProbe
        probe = RunnerCapabilityProbe(
            codex_binary=self.settings.runner.codex_binary,
            antigravity_binary=self.settings.runner.antigravity_binary,
            cgroup_root=self.settings.runner.cgroup_root,
            trusted_operator=self.settings.environment == "production",
        )
        return probe.probe_all()

    def _check_runtime_security(self) -> Dict[str, Any]:
        paths = self.settings.paths
        problems = []
        for name, path in (("runtime root", paths.data_dir), ("database", paths.database),
                           ("SMTP configuration", paths.delivery_config_file)):
            if path is None or not path.exists():
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_mode & 0o077:
                problems.append({"kind": "unsafe_permissions", "target": name,
                                 "path": str(path), "mode": oct(stat.S_IMODE(info.st_mode))})
        if paths.task_workspaces_dir.exists():
            for workspace in paths.task_workspaces_dir.iterdir():
                if workspace.is_symlink() or not workspace.is_dir():
                    continue
                for relative in ("project/.codex", "project/.gemini", "project/.config/antigravity"):
                    target = workspace / relative
                    if target.exists() or target.is_symlink():
                        problems.append({"kind": "legacy_worker_credentials", "path": str(target),
                                         "action": "operator quarantine and credential review required"})
                lock = workspace / ".lock.json"
                if lock.exists() or lock.is_symlink():
                    # A currently owned workspace is ordinary operation, not an
                    # unsafe legacy lock. Unreadable or stale locks still fail closed.
                    owned = False
                    try:
                        value = json.loads(read_safe_bytes(lock, workspace, 4096))
                        conn = self.db.get_connection()
                        try:
                            row = conn.execute("""SELECT l.lease_expires_at FROM run_leases l
                                JOIN task_claims c ON c.run_id=l.run_id AND c.fencing_token=l.fencing_token
                                JOIN scheduled_runs r ON r.run_id=l.run_id
                                WHERE c.task_id=? AND l.run_id=? AND l.fencing_token=? AND r.status='running'""",
                                (workspace.name, value.get("run_id"), value.get("fencing_token"))).fetchone()
                            owned = bool(row and datetime.fromisoformat(row[0]) > datetime.now(timezone.utc))
                        finally:
                            conn.close()
                    except Exception:
                        owned = False
                    if not owned:
                        problems.append({"kind": "workspace_lock_requires_review", "path": str(lock)})
        return {"ok": not problems, "issues": problems,
                "global_send_blocked": self.settings.delivery.global_handoff_kill_switch,
                "credential_contents_inspected": False}

    def _check_mcp(self) -> Dict[str, Any]:
        """Inspect native registration without launching servers or testing auth.

        MCP is optional unless a task explicitly requires it, so these diagnostics
        never change the existing system-wide execution readiness decisions.
        """
        from researchops.runners.native_mcp import inspect_native_mcp

        providers = {}
        for provider, binary in (("codex_exec", self.settings.runner.codex_binary),
                                 ("antigravity_exec", self.settings.runner.antigravity_binary)):
            if not shutil.which(binary):
                providers[provider] = {"provider": provider, "servers": [],
                    "config_sources": [], "issues": [{"code": "cli_unavailable"}],
                    "connections_checked": False, "inventory_complete": False}
                continue
            try:
                providers[provider] = inspect_native_mcp(provider, binary)
            except Exception:
                # Configuration/CLI exceptions may contain secrets. A safe code
                # is sufficient for the normal Doctor/Web diagnostic surface.
                providers[provider] = {"provider": provider, "servers": [],
                    "config_sources": [], "issues": [{"code": "inspection_failed"}],
                    "connections_checked": False, "inventory_complete": False}
        return {"providers": providers, "connections_checked": False,
                "scope": "current_process_user_config", "affects_readiness": False}
