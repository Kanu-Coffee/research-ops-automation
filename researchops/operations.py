"""Offline recovery and conservative retention; all entry points are explicit.

No service is stopped here. Processes hold a shared maintenance guard for their
lifetime; restore must acquire it exclusively before touching database files.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from uuid import uuid4

from researchops.errors import ResearchOpsError
from researchops.workspace.security import assert_path_contained


SERVICE_UNITS = ("researchops-web.service", "researchops-worker.service",
                 "researchops-scheduler.service", "researchops-scheduler.timer",
                 "researchops-smtp.service")


def _marker(database: Path) -> Path:
    return database.parent / f".{database.name}.restore-in-progress.json"


@contextmanager
def runtime_guard(database, *, exclusive: bool = False):
    """Fail immediately if recovery conflicts with a service or DB connection."""
    database = Path(database).absolute()
    assert_path_contained(database, database.parent)
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = database.parent / f".{database.name}.maintenance.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ResearchOpsError("Unsafe database maintenance lock")
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ResearchOpsError("Database maintenance guard is busy; stop ResearchOps services before recovery") from exc
        if not exclusive and (_marker(database).exists() or _marker(database).is_symlink()):
            raise ResearchOpsError("Incomplete restore requires operator recovery before starting services")
        yield
    finally:
        os.close(fd)


def _regular(path: Path) -> Path:
    path = Path(path).absolute()
    assert_path_contained(path, path.parent)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ResearchOpsError(f"Expected singly linked regular file: {path}")
    return path


def _private_directory(path: Path) -> Path:
    path = Path(path).absolute()
    if path in (Path("/"), Path.home(), Path(__file__).resolve().parents[1]) or len(path.parts) < 3:
        raise ResearchOpsError("Refusing a broad directory as an operations target")
    assert_path_contained(path, path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise ResearchOpsError(f"Operations directory must be owned by current account: {path}")
    os.chmod(path, 0o700)
    return path


def _connect_readonly(path: Path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)


def _check_integrity(conn):
    results = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    if results != ["ok"]:
        raise ResearchOpsError("Database integrity check failed")


def _fsync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _consistent_copy(source: Path, destination: Path):
    """SQLite backup creates one self-contained, verified database including WAL."""
    src = _connect_readonly(source)
    dst = None
    try:
        _check_integrity(src)
        dst = sqlite3.connect(destination)
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        _check_integrity(dst)
        dst.close()
        dst = None
        os.chmod(destination, 0o600)
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
    finally:
        if dst is not None:
            dst.close()
        src.close()


def backup_database(database: Path, backup_dir: Path) -> dict:
    database = _regular(database)
    backup_dir = _private_directory(backup_dir)
    name = "researchops_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid4().hex[:8] + ".db"
    destination = backup_dir / name
    fd, staging_name = tempfile.mkstemp(prefix=".backup-", suffix=".db", dir=backup_dir)
    os.close(fd)
    staging = Path(staging_name)
    try:
        with runtime_guard(database):
            _consistent_copy(database, staging)
        # link is exclusive publication; final link count is one after staging unlink.
        os.link(staging, destination)
        staging.unlink()
        _fsync_directory(backup_dir)
        return {"status": "verified", "backup": str(destination), "size_bytes": destination.stat().st_size}
    finally:
        staging.unlink(missing_ok=True)


def _user_manager_environment() -> tuple[dict, bool]:
    """Use only this account's local user bus, never an arbitrary bus address."""
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    configured = bool(os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("DBUS_SESSION_BUS_ADDRESS"))
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.geteuid()}")
    try:
        if not runtime.is_absolute() or ".." in runtime.parts:
            raise ValueError()
        assert_path_contained(runtime, runtime)
        info = runtime.stat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError()
    except (OSError, ValueError, ResearchOpsError) as exc:
        if configured:
            raise ResearchOpsError("Cannot safely inspect the configured systemd user manager before restore") from exc
        return env, False
    env["XDG_RUNTIME_DIR"] = str(runtime)
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if address:
        expected = re.escape(f"unix:path={runtime}/bus")
        if not re.fullmatch(expected + r"(?:,guid=[0-9a-fA-F]{32})?", address):
            raise ResearchOpsError("Restore requires this account's local systemd user bus")
        env["DBUS_SESSION_BUS_ADDRESS"] = address
    return env, True


def _inactive_services() -> dict:
    """Inspect both managers: an idle user timer can race an offline restore.

    Missing managers on non-systemd hosts remain operator-attested. If this
    account has a configured user manager, an uncertain check must fail closed.
    Shared maintenance guards continue to protect every running app process.
    """
    unavailable = {"status": "unavailable", "basis": "operator asserted --offline and exclusive guard"}
    binary = shutil.which("systemctl")
    if not binary:
        _, configured = _user_manager_environment()
        if configured:
            raise ResearchOpsError("Cannot verify systemd user services without systemctl before restore")
        return {"system": dict(unavailable), "user": dict(unavailable)}
    result = subprocess.run([binary, "is-active", *SERVICE_UNITS], capture_output=True,
                            text=True, timeout=10, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    states = result.stdout.splitlines()
    if any(state in {"active", "activating", "reloading", "deactivating"} for state in states):
        raise ResearchOpsError("ResearchOps systemd services/timer must all be stopped before restore")
    system = ({"status": "checked", "units": dict(zip(SERVICE_UNITS, states))}
              if len(states) == len(SERVICE_UNITS) else dict(unavailable))
    env, configured = _user_manager_environment()
    try:
        result = subprocess.run([binary, "--user", "is-active", *SERVICE_UNITS], capture_output=True,
                                text=True, timeout=10, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if configured:
            raise ResearchOpsError("Cannot verify systemd user services are stopped before restore") from exc
        return {"system": system, "user": dict(unavailable)}
    states = result.stdout.splitlines()
    if any(state in {"active", "activating", "reloading", "deactivating"} for state in states):
        raise ResearchOpsError("ResearchOps systemd user services/timer must all be stopped before restore")
    if (len(states) != len(SERVICE_UNITS) or result.returncode not in (0, 3, 4)
            or any(state not in {"inactive", "failed", "unknown"} for state in states)):
        if configured:
            raise ResearchOpsError("Cannot verify systemd user services are stopped before restore")
        return {"system": system, "user": dict(unavailable)}
    return {"system": system,
            "user": {"status": "checked", "units": dict(zip(SERVICE_UNITS, states))}}


def _assert_no_database_users(database: Path):
    """Reject observable raw SQLite clients; offline is still operator-attested.

    Linux may hide FDs of unrelated same-UID services. Those are recorded, not
    treated as proof of inactivity; the mandatory guard protects all current
    ResearchOps processes, and --offline covers legacy/uncoordinated clients.
    """
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        raise ResearchOpsError("Offline restore requires Linux /proc open-file inspection")
    targets = {str(database), str(database) + "-wal", str(database) + "-shm"}
    inaccessible = 0
    processes = sorted(proc_root.iterdir(), key=lambda entry: entry.name != str(os.getpid()))
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            for fd in (process / "fd").iterdir():
                try:
                    if os.readlink(fd).removesuffix(" (deleted)") in targets:
                        raise ResearchOpsError(f"Database is still open by PID {process.name}; stop it before restore")
                except FileNotFoundError:
                    continue
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            inaccessible += 1
    return {"uninspectable_processes": inaccessible, "offline_attestation_required": True}


def restore_database(backup: Path, database: Path, *, offline: bool = False) -> dict:
    if not offline:
        raise ResearchOpsError("Restore requires --offline after stopping all ResearchOps services and timer")
    backup = _regular(backup)
    database = Path(database).absolute()
    if backup == database:
        raise ResearchOpsError("Backup and restore target must differ")
    assert_path_contained(database, database.parent)
    _private_directory(database.parent)
    for suffix in ("-wal", "-shm"):
        if Path(str(backup) + suffix).exists():
            raise ResearchOpsError("Restore source must be a self-contained backup without WAL/SHM sidecars")
    with runtime_guard(database, exclusive=True):
        if _marker(database).exists() or _marker(database).is_symlink():
            raise ResearchOpsError("Prior incomplete restore must be recovered using its recorded safety directory")
        services = _inactive_services()
        process_check = _assert_no_database_users(database)
        if database.exists():
            _regular(database)
        fd, staging_name = tempfile.mkstemp(prefix=".restore-", suffix=".db", dir=database.parent)
        os.close(fd)
        staging = Path(staging_name)
        safety = None
        try:
            _consistent_copy(backup, staging)
            safety = _private_directory(database.parent / "backups" / ("pre-restore-" + uuid4().hex))
            # Preserve the exact old DB plus WAL/SHM before altering anything.
            for source in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
                if source.exists() or source.is_symlink():
                    _regular(source)
                    target = safety / source.name
                    with source.open("rb") as src, target.open("xb") as dst:
                        os.chmod(target, 0o600)
                        shutil.copyfileobj(src, dst)
                        dst.flush()
                        os.fsync(dst.fileno())
            _fsync_directory(safety)
            with _marker(database).open("x", encoding="utf-8") as stream:
                os.chmod(_marker(database), 0o600)
                json.dump({"database": str(database), "source_backup": str(backup),
                           "safety_directory": str(safety), "replacement": str(staging)}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            _fsync_directory(database.parent)
            # Sidecars move recoverably; shared guards remain blocked if interrupted.
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(database) + suffix)
                if sidecar.exists():
                    os.replace(sidecar, safety / ("retired-" + sidecar.name))
            os.replace(staging, database)
            _fsync_directory(database.parent)
            connection = _connect_readonly(database)
            try:
                _check_integrity(connection)
            finally:
                connection.close()
            _marker(database).unlink()
            _fsync_directory(database.parent)
            return {"status": "restored", "database": str(database), "safety_directory": str(safety),
                    "services": services, "process_check": process_check,
                    "restart_services": "Only after doctor and dry-run verification"}
        finally:
            if not _marker(database).exists():
                staging.unlink(missing_ok=True)


def cleanup_archives(database: Path, archive_root: Path, retention_days: int = 90,
                     *, apply: bool = False, now: datetime | None = None) -> dict:
    if type(retention_days) is not int or retention_days < 1:
        raise ResearchOpsError("Retention days must be a positive integer")
    database = _regular(database)
    archive_root = Path(archive_root).absolute()
    assert_path_contained(archive_root, archive_root)
    if archive_root != database.parent / "run-archive":
        raise ResearchOpsError("Retention target must be the database sibling run-archive directory")
    if archive_root == database.parent or archive_root in database.parents:
        raise ResearchOpsError("Retention target must not contain the database")
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    result = {"dry_run": not apply, "eligible": [], "protected": [], "retired": []}
    if not archive_root.exists():
        return result
    with runtime_guard(database):
        connection = sqlite3.connect(database) if apply else _connect_readonly(database)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"scheduled_runs", "delivery_handoffs", "smtp_attempts"}.issubset(tables):
                raise ResearchOpsError("Retention requires migrated run, handoff and SMTP attempt tables")
            for task_dir in sorted(archive_root.iterdir()):
                if task_dir.is_symlink() or not task_dir.is_dir():
                    continue
                for run_dir in sorted(task_dir.iterdir()):
                    if run_dir.is_symlink() or not run_dir.is_dir():
                        continue
                    assert_path_contained(run_dir, archive_root)
                    run = connection.execute("SELECT * FROM scheduled_runs WHERE run_id=? AND task_id=?",
                                             (run_dir.name, task_dir.name)).fetchone()
                    reason = None
                    if not run or run["status"] not in {"succeeded", "failed", "timed_out", "cancelled"}:
                        reason = "unknown or nonterminal run"
                    elif not run["finished_at"]:
                        reason = "missing completion timestamp"
                    else:
                        finished = datetime.fromisoformat(run["finished_at"])
                        if finished.tzinfo is None:
                            reason = "ambiguous completion timezone"
                        elif finished >= cutoff:
                            reason = "within retention period"
                    if reason is None:
                        active = connection.execute(
                            "SELECT 1 FROM scheduled_runs WHERE task_id=? AND status IN "
                            "('queued','running','awaiting_receipt','needs_attention') LIMIT 1", (task_dir.name,)).fetchone()
                        pending = connection.execute(
                            "SELECT 1 FROM delivery_handoffs WHERE run_id=? AND mode != 'dry_run' "
                            "AND status NOT IN ('smtp_accepted','failed') LIMIT 1", (run_dir.name,)).fetchone()
                        attempts = connection.execute(
                            "SELECT 1 FROM smtp_attempts s JOIN delivery_handoffs h ON s.handoff_id=h.handoff_id "
                            "WHERE h.run_id=? AND s.status NOT IN ('smtp_accepted','failed') LIMIT 1", (run_dir.name,)).fetchone()
                        if active or pending or attempts:
                            reason = "active task or pending/uncertain delivery evidence"
                    if reason:
                        result["protected"].append({"path": str(run_dir), "reason": reason})
                        continue
                    result["eligible"].append(str(run_dir))
                    if apply:
                        retired_root = _private_directory(archive_root.parent / "retired-archives")
                        destination = retired_root / (task_dir.name + "_" + run_dir.name + "_" + uuid4().hex)
                        os.rename(run_dir, destination)
                        _fsync_directory(task_dir)
                        _fsync_directory(retired_root)
                        result["retired"].append({"original": str(run_dir), "recoverable_at": str(destination)})
            connection.commit()
        finally:
            connection.close()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("database", nargs="?")
    backup.add_argument("backup_dir", nargs="?")
    restore = commands.add_parser("restore")
    restore.add_argument("backup")
    restore.add_argument("database", nargs="?")
    restore.add_argument("--offline", action="store_true")
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("days", type=int, nargs="?", default=90)
    cleanup.add_argument("--database")
    cleanup.add_argument("--archive-dir")
    cleanup.add_argument("--apply", action="store_true", help="Move eligible archives to recoverable retired-archives")
    args = parser.parse_args(argv)
    try:
        database = getattr(args, "database", None)
        settings = None
        if not database:
            if os.environ.get("RESEARCHOPS_DATA_DIR"):
                database = Path(os.environ["RESEARCHOPS_DATA_DIR"]) / "researchops.db"
            else:
                from researchops.config import load_settings
                settings = load_settings()
                database = settings.paths.database
        database = Path(database)
        if args.command == "backup":
            result = backup_database(database, Path(args.backup_dir) if args.backup_dir else database.parent / "backups")
        elif args.command == "restore":
            result = restore_database(Path(args.backup), database, offline=args.offline)
        else:
            archive = Path(args.archive_dir) if args.archive_dir else (
                settings.paths.run_archive_dir if settings else database.parent / "run-archive")
            result = cleanup_archives(database, archive, args.days, apply=args.apply)
        print(json.dumps(result, indent=2))
        return 0
    except (ResearchOpsError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"Operations refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
