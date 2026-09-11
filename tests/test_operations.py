"""Backup, offline restore and retention use temporary SQLite/runtime only."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from researchops.errors import ResearchOpsError
from researchops.operations import (SERVICE_UNITS, _inactive_services, _user_manager_environment,
                                    backup_database, cleanup_archives, restore_database, runtime_guard)


SOURCE_ROOT = Path(__file__).resolve().parents[1]


class TestOperations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "researchops.db"
        with closing(sqlite3.connect(self.database)) as conn:
            conn.executescript("""
                CREATE TABLE records(value TEXT);
                INSERT INTO records VALUES('original');
                CREATE TABLE scheduled_runs(run_id TEXT PRIMARY KEY,task_id TEXT,status TEXT,finished_at TEXT);
                CREATE TABLE delivery_handoffs(handoff_id TEXT PRIMARY KEY,run_id TEXT,mode TEXT,status TEXT);
                CREATE TABLE smtp_attempts(handoff_id TEXT,status TEXT);
            """)
        self.backups = self.root / "backups"

    def tearDown(self):
        self.temp.cleanup()

    def test_backup_is_verified_private_and_includes_committed_wal(self):
        with closing(sqlite3.connect(self.database)) as live:
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("INSERT INTO records VALUES('wal')")
            live.commit()
            result = backup_database(self.database, self.backups)
            backup = Path(result["backup"])
            with closing(sqlite3.connect(backup)) as copy:
                self.assertEqual(copy.execute("SELECT count(*) FROM records").fetchone()[0], 2)
                self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.backups.stat().st_mode & 0o777, 0o700)
        self.assertFalse(Path(str(backup) + "-wal").exists())

    def test_corrupt_source_never_publishes_backup(self):
        self.database.write_bytes(b"not sqlite")
        with self.assertRaises(sqlite3.DatabaseError):
            backup_database(self.database, self.backups)
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_restore_requires_offline_and_refuses_live_guard(self):
        backup = Path(backup_database(self.database, self.backups)["backup"])
        before = self.database.read_bytes()
        with self.assertRaisesRegex(ResearchOpsError, "--offline"):
            restore_database(backup, self.database)
        with runtime_guard(self.database):
            with self.assertRaisesRegex(ResearchOpsError, "guard is busy"):
                restore_database(backup, self.database, offline=True)
        self.assertEqual(self.database.read_bytes(), before)

    def test_restore_refuses_uncoordinated_open_sqlite_connection(self):
        backup = Path(backup_database(self.database, self.backups)["backup"])
        with closing(sqlite3.connect(self.database)) as live:
            live.execute("SELECT * FROM records").fetchall()
            with patch("researchops.operations._inactive_services", return_value={}):
                with self.assertRaisesRegex(ResearchOpsError, "still open"):
                    restore_database(backup, self.database, offline=True)

    def test_restore_refuses_active_systemd_service(self):
        backup = Path(backup_database(self.database, self.backups)["backup"])
        active = subprocess.CompletedProcess([], 0, "active\n", "")
        with patch("researchops.operations.shutil.which", return_value="/usr/bin/systemctl"), \
                patch("researchops.operations.subprocess.run", return_value=active):
            with self.assertRaisesRegex(ResearchOpsError, "must all be stopped"):
                restore_database(backup, self.database, offline=True)

    def test_restore_refuses_active_user_timer_before_touching_database(self):
        backup = Path(backup_database(self.database, self.backups)["backup"])
        before = self.database.read_bytes()
        inactive = subprocess.CompletedProcess([], 3, "inactive\n" * len(SERVICE_UNITS), "")
        for state in ("active", "activating", "reloading", "deactivating"):
            states = ["inactive"] * len(SERVICE_UNITS)
            states[SERVICE_UNITS.index("researchops-scheduler.timer")] = state
            active = subprocess.CompletedProcess([], 0, "\n".join(states), "")
            with self.subTest(state=state), \
                    patch("researchops.operations.shutil.which", return_value="/usr/bin/systemctl"), \
                    patch("researchops.operations._user_manager_environment", return_value=({}, True)), \
                    patch("researchops.operations.subprocess.run", side_effect=[inactive, active]) as run:
                with self.assertRaisesRegex(ResearchOpsError, "user services/timer must all be stopped"):
                    restore_database(backup, self.database, offline=True)
                self.assertEqual(run.call_args_list[1].args[0][1:3], ["--user", "is-active"])
                self.assertEqual(self.database.read_bytes(), before)
                self.assertFalse((self.root / ".researchops.db.restore-in-progress.json").exists())

    def test_service_inspection_records_both_scopes(self):
        inactive = subprocess.CompletedProcess([], 3, "inactive\n" * len(SERVICE_UNITS), "")
        with patch("researchops.operations.shutil.which", return_value="/usr/bin/systemctl"), \
                patch("researchops.operations._user_manager_environment", return_value=({}, True)), \
                patch("researchops.operations.subprocess.run", return_value=inactive):
            result = _inactive_services()
        self.assertEqual(set(result), {"system", "user"})
        for scope in result.values():
            self.assertEqual(scope["status"], "checked")
            self.assertEqual(scope["units"], dict.fromkeys(SERVICE_UNITS, "inactive"))

    def test_configured_user_manager_uncertainty_fails_closed(self):
        inactive = subprocess.CompletedProcess([], 3, "inactive\n" * len(SERVICE_UNITS), "")
        failures = [subprocess.CompletedProcess([], 1, "", "unavailable"),
                    subprocess.CompletedProcess([], 3, "inactive\n", ""),
                    subprocess.CompletedProcess([], 1, "inactive\n" * len(SERVICE_UNITS), ""),
                    subprocess.CompletedProcess([], 3, "unrecognized\n" * len(SERVICE_UNITS), ""),
                    subprocess.TimeoutExpired("systemctl", 10), OSError("unavailable")]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), \
                    patch("researchops.operations.shutil.which", return_value="/usr/bin/systemctl"), \
                    patch("researchops.operations._user_manager_environment", return_value=({}, True)), \
                    patch("researchops.operations.subprocess.run", side_effect=[inactive, failure]):
                with self.assertRaisesRegex(ResearchOpsError, "Cannot verify systemd user"):
                    _inactive_services()

    def test_missing_user_manager_is_explicit_on_non_user_service_host(self):
        inactive = subprocess.CompletedProcess([], 3, "inactive\n" * len(SERVICE_UNITS), "")
        for failure in (subprocess.CompletedProcess([], 1, "", "unavailable"),
                        subprocess.TimeoutExpired("systemctl", 10), OSError("unavailable")):
            with self.subTest(failure=type(failure).__name__), \
                    patch("researchops.operations.shutil.which", return_value="/usr/bin/systemctl"), \
                    patch("researchops.operations._user_manager_environment", return_value=({}, False)), \
                    patch("researchops.operations.subprocess.run", side_effect=[inactive, failure]):
                result = _inactive_services()
            self.assertEqual(result["system"]["status"], "checked")
            self.assertEqual(result["user"]["status"], "unavailable")

    def test_missing_systemctl_only_allows_unconfigured_user_manager(self):
        with patch("researchops.operations.shutil.which", return_value=None):
            with patch("researchops.operations._user_manager_environment", return_value=({}, False)):
                result = _inactive_services()
                self.assertEqual(result["system"]["status"], "unavailable")
                self.assertEqual(result["user"]["status"], "unavailable")
            with patch("researchops.operations._user_manager_environment", return_value=({}, True)):
                with self.assertRaisesRegex(ResearchOpsError, "without systemctl"):
                    _inactive_services()

    def test_unknown_absent_units_are_checked_in_both_managers(self):
        unknown = subprocess.CompletedProcess([], 4, "unknown\n" * len(SERVICE_UNITS), "")
        with patch("researchops.operations.shutil.which", return_value="/usr/bin/systemctl"), \
                patch("researchops.operations._user_manager_environment", return_value=({}, True)), \
                patch("researchops.operations.subprocess.run", return_value=unknown):
            result = _inactive_services()
        self.assertEqual(result["user"]["units"], dict.fromkeys(SERVICE_UNITS, "unknown"))

    def test_user_manager_environment_is_minimal_and_local(self):
        runtime = self.root / "user-runtime"
        runtime.mkdir(mode=0o700)
        address = f"unix:path={runtime}/bus,guid=" + "a" * 32
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime),
                                    "DBUS_SESSION_BUS_ADDRESS": address,
                                    "SMTP_PASSWORD": "synthetic-never-forwarded"}, clear=True):
            env, configured = _user_manager_environment()
        self.assertTrue(configured)
        self.assertEqual(env, {"PATH": "/usr/bin:/bin", "LC_ALL": "C",
                               "XDG_RUNTIME_DIR": str(runtime), "DBUS_SESSION_BUS_ADDRESS": address})

    def test_user_manager_environment_refuses_nonlocal_or_other_account_bus(self):
        runtime = self.root / "user-runtime"
        runtime.mkdir(mode=0o700)
        for address in ("tcp:host=example.invalid,port=1234", "unix:path=/run/user/99999/bus",
                        f"unix:path={runtime}/bus;tcp:host=example.invalid,port=1234"):
            with self.subTest(address=address), patch.dict(os.environ, {
                    "XDG_RUNTIME_DIR": str(runtime), "DBUS_SESSION_BUS_ADDRESS": address}, clear=True):
                with self.assertRaisesRegex(ResearchOpsError, "local systemd user bus"):
                    _user_manager_environment()

    def test_user_manager_environment_refuses_unsafe_or_missing_runtime(self):
        public = self.root / "public"
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        linked = self.root / "linked"
        linked.symlink_to(self.root, target_is_directory=True)
        for runtime in (public, linked, self.root / "missing", Path("relative")):
            with self.subTest(runtime=str(runtime)), patch.dict(os.environ, {
                    "XDG_RUNTIME_DIR": str(runtime)}, clear=True):
                with self.assertRaisesRegex(ResearchOpsError, "Cannot safely inspect"):
                    _user_manager_environment()

    def test_restore_replaces_atomically_and_preserves_previous_database(self):
        backup = Path(backup_database(self.database, self.backups)["backup"])
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("INSERT INTO records VALUES('later')")
            conn.commit()
        with patch("researchops.operations._inactive_services", return_value={}):
            result = restore_database(backup, self.database, offline=True)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM records").fetchone()[0], 1)
        prior = Path(result["safety_directory"]) / self.database.name
        with closing(sqlite3.connect(prior)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM records").fetchone()[0], 2)
        self.assertFalse((self.root / ".researchops.db.restore-in-progress.json").exists())

    def test_interrupted_restore_blocks_restart_and_keeps_recovery_evidence(self):
        backup = Path(backup_database(self.database, self.backups)["backup"])
        with patch("researchops.operations._inactive_services", return_value={}), \
                patch("researchops.operations.os.replace", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                restore_database(backup, self.database, offline=True)
        marker = self.root / ".researchops.db.restore-in-progress.json"
        self.assertTrue(marker.exists())
        metadata = json.loads(marker.read_text())
        self.assertTrue(Path(metadata["safety_directory"]).is_dir())
        self.assertTrue(Path(metadata["replacement"]).is_file())
        with self.assertRaisesRegex(ResearchOpsError, "Incomplete restore"):
            with runtime_guard(self.database):
                pass

    def _archive(self, task_id, run_id, status="succeeded", *, days_old=100,
                 handoff_status=None, attempt_status=None):
        archive = self.root / "run-archive" / task_id / run_id
        archive.mkdir(parents=True)
        (archive / "result.json").write_text("evidence")
        finished = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("INSERT INTO scheduled_runs VALUES(?,?,?,?)", (run_id, task_id, status, finished))
            if handoff_status:
                conn.execute("INSERT INTO delivery_handoffs VALUES(?,?,?,?)",
                             ("handoff-" + run_id, run_id, "handoff", handoff_status))
            if attempt_status:
                conn.execute("INSERT INTO smtp_attempts VALUES(?,?)", ("handoff-" + run_id, attempt_status))
            conn.commit()
        return archive

    def test_retention_defaults_dryrun_and_protects_pending_evidence(self):
        eligible = self._archive("finished-task", "old")
        running = self._archive("active-task", "active", status="running")
        uncertain = self._archive("uncertain-task", "uncertain", handoff_status="uncertain")
        smtp_pending = self._archive("pending-task", "pending", handoff_status="smtp_accepted", attempt_status="sending")
        recent = self._archive("recent-task", "recent", days_old=1)
        result = cleanup_archives(self.database, self.root / "run-archive")
        self.assertEqual(result["eligible"], [str(eligible)])
        self.assertTrue(result["dry_run"])
        for path in (eligible, running, uncertain, smtp_pending, recent):
            self.assertTrue(path.exists())
        self.assertFalse((self.root / "retired-archives").exists())

    def test_retention_apply_is_recoverable_and_never_touches_workspaces(self):
        eligible = self._archive("task", "old")
        workspace = self.root / "task-workspaces" / "task" / "project"
        workspace.mkdir(parents=True)
        (workspace / "tool.txt").write_text("keep")
        result = cleanup_archives(self.database, self.root / "run-archive", apply=True)
        self.assertFalse(eligible.exists())
        retired = Path(result["retired"][0]["recoverable_at"])
        self.assertEqual((retired / "result.json").read_text(), "evidence")
        self.assertEqual((workspace / "tool.txt").read_text(), "keep")
        with self.assertRaises(ResearchOpsError):
            cleanup_archives(self.database, self.root / "task-workspaces", apply=True)

    def test_shell_wrappers_backup_and_offline_restore_only_temp_database(self):
        # Never query an actual service manager while restoring a synthetic DB.
        tools_dir = self.root / "tools"
        tools_dir.mkdir(mode=0o700)
        systemctl = tools_dir / "systemctl"
        systemctl.write_text(f"#!{sys.executable}\nprint('inactive\\n' * {len(SERVICE_UNITS)}, end='')\n")
        systemctl.chmod(0o700)
        env = dict(os.environ, PATH=str(tools_dir) + os.pathsep + os.environ.get("PATH", ""))
        backup = subprocess.run(["bash", str(SOURCE_ROOT / "bin/backup-db.sh"), str(self.database), str(self.backups)],
                                capture_output=True, text=True, check=True, env=env)
        backup_path = json.loads(backup.stdout)["backup"]
        restore = subprocess.run(["bash", str(SOURCE_ROOT / "bin/restore-db.sh"), backup_path,
                                  str(self.database), "--offline"], capture_output=True, text=True, env=env)
        self.assertEqual(restore.returncode, 0, restore.stderr)
        self.assertEqual(json.loads(restore.stdout)["status"], "restored")

    def test_shellcheck(self):
        binary = shutil.which("shellcheck")
        if not binary:
            self.skipTest("shellcheck not installed")
        result = subprocess.run([binary, *(str(SOURCE_ROOT / "bin" / name) for name in (
            "backup-db.sh", "restore-db.sh", "cleanup-archive.sh"))], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
