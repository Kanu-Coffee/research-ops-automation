"""Read-only preflight, offline rehearsal, and bounded user-owned installation.

This module deliberately does not create accounts, install/start system units,
read an existing configuration, migrate existing data, or enable delivery.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tomllib


class DeploymentError(ValueError):
    """An unsafe target or unmet installation prerequisite."""


UNIT_NAMES = (
    "researchops-worker.service", "researchops-scheduler.service",
    "researchops-scheduler.timer", "researchops-smtp.service",
    "researchops-web.service",
)
SYSTEM_TARGETS = (Path("/opt/researchops"), Path("/etc/researchops"),
                  Path("/var/lib/researchops")) + tuple(
    Path("/etc/systemd/system") / name for name in UNIT_NAMES
)
SOURCE_FILES = ("pyproject.toml", "uv.lock", "README.md")
SOURCE_TREES = ("researchops", "schemas", "examples")


def _safe_absolute(path: Path) -> Path:
    path = Path(path)
    if (not path.is_absolute() or ".." in path.parts or path == Path("/")
            or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path))):
        raise DeploymentError("Use a dedicated absolute path without whitespace or traversal")
    for component in (path, *path.parents):
        if component.is_symlink():
            raise DeploymentError("Symlink path components are not permitted")
    return path


def _metadata(path: Path) -> dict:
    """Never open a target: even existing secret configurations remain unread."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"path": str(path), "exists": False}
    return {"path": str(path), "exists": True, "uid": info.st_uid,
            "mode": oct(stat.S_IMODE(info.st_mode)),
            "symlink": stat.S_ISLNK(info.st_mode)}


def host_preflight() -> dict:
    targets = [_metadata(path) for path in SYSTEM_TARGETS]
    try:
        account = pwd.getpwnam("researchops")
        account_info = {"exists": True, "uid": account.pw_uid, "gid": account.pw_gid}
    except KeyError:
        account_info = {"exists": False}
    blockers = []
    if any(item["exists"] for item in targets):
        blockers.append("EXISTING_DEPLOYMENT_REQUIRES_REVIEW_NO_OVERWRITE")
    if os.geteuid() != 0:
        blockers.append("SYSTEM_INSTALL_REQUIRES_OPERATOR_ROOT_PRIVILEGE")
    if not account_info["exists"]:
        blockers.append("DEDICATED_SERVICE_ACCOUNT_NOT_PROVISIONED")
    return {"operation": "read_only_preflight", "targets": targets,
            "service_account": account_info, "effective_uid": os.geteuid(),
            "systemctl_available": shutil.which("systemctl") is not None,
            "systemd_verify_available": shutil.which("systemd-analyze") is not None,
            "blockers": blockers, "system_install_performed": False,
            "services_started": False, "smtp_enabled": False,
            "production_runner_ready": False, "timezone": "Asia/Seoul"}


def _source_entries(source: Path) -> list[tuple[Path, Path]]:
    entries = []
    for name in SOURCE_FILES:
        entries.append((source / name, Path(name)))
    for tree in SOURCE_TREES:
        base = source / tree
        if not base.is_dir() or base.is_symlink():
            raise DeploymentError("Required source directory is absent or a symlink")
        for current, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = [name for name in dirs if name != "__pycache__"]
            if any((Path(current) / name).is_symlink() for name in dirs):
                raise DeploymentError("Source directory symlinks are not permitted")
            for name in files:
                if not name.endswith((".pyc", ".pyo")):
                    item = Path(current) / name
                    entries.append((item, item.relative_to(source)))
    for name in UNIT_NAMES:
        entries.append((source / "deploy/systemd" / name,
                        Path("deploy/systemd") / name))
    for item, _ in entries:
        _safe_absolute(item)
        if item.is_symlink() or not item.is_file() or item.stat().st_nlink != 1:
            raise DeploymentError("Only regular non-symlink, non-hardlink source files are allowed")
    return entries


def user_preflight(root: Path) -> dict:
    """Inspect only directory metadata, never existing application/configuration contents."""
    root = _safe_absolute(root)
    info = _metadata(root)
    blockers = []
    if os.geteuid() == 0:
        blockers.append("USER_INSTALL_MUST_RUN_UNPRIVILEGED")
    if not info["exists"]:
        blockers.append("USER_ROOT_NOT_PROVISIONED")
    else:
        current = root.lstat()
        if not stat.S_ISDIR(current.st_mode):
            blockers.append("USER_ROOT_NOT_DIRECTORY")
        elif current.st_uid != os.geteuid() or stat.S_IMODE(current.st_mode) != 0o700:
            blockers.append("USER_ROOT_REQUIRES_CURRENT_OWNER_AND_MODE_0700")
        elif any(root.iterdir()):
            blockers.append("EXISTING_USER_DEPLOYMENT_REQUIRES_REVIEW_NO_OVERWRITE")
    if shutil.which("uv") is None:
        blockers.append("EXISTING_UV_INSTALLATION_REQUIRED")
    return {"operation": "read_only_user_preflight", "root": str(root),
            "target": info, "effective_uid": os.geteuid(), "blockers": blockers,
            "systemctl_available": shutil.which("systemctl") is not None,
            "systemd_verify_available": shutil.which("systemd-analyze") is not None,
            "services_registered": False, "services_started": False,
            "smtp_enabled": False, "production_runner_ready": False,
            "timezone": "Asia/Seoul"}


def _source_bytes(path: Path) -> bytes:
    """Read a bounded regular source file without following a replacement symlink."""
    _safe_absolute(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 8_000_000:
        raise DeploymentError("Source must be a bounded regular non-hardlink file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns,
                                 item.st_ctime_ns, item.st_nlink, item.st_mode)
        if identity(before) != identity(opened):
            raise DeploymentError("Source changed while opening snapshot")
        payload = stream.read(8_000_001)
        after = os.fstat(stream.fileno())
        if len(payload) != before.st_size or identity(before) != identity(after):
            raise DeploymentError("Source changed while reading snapshot")
    return payload


def user_install_plan(source: Path, root: Path, *, web_port: int = 8765) -> tuple[dict, dict[Path, bytes]]:
    source, root = _safe_absolute(source), _safe_absolute(root)
    if source == root or source in root.parents or root in source.parents:
        raise DeploymentError("User installation must be outside the source checkout")
    if type(web_port) is not int or not 1024 <= web_port <= 65535:
        raise DeploymentError("User web port must be an integer from 1024 through 65535")
    preflight = user_preflight(root)
    if preflight["blockers"]:
        raise DeploymentError("User installation preflight blocked: " + ", ".join(preflight["blockers"]))
    entries = _source_entries(source)
    entries.extend((source / "deploy/systemd-user" / name, Path("deploy/systemd-user") / name)
                   for name in UNIT_NAMES)
    payloads = {relative: _source_bytes(original) for original, relative in entries}
    if sum(map(len, payloads.values())) > 64_000_000:
        raise DeploymentError("Source snapshot exceeds the installation size limit")
    try:
        version = tomllib.loads(payloads[Path("pyproject.toml")].decode())["project"]["version"]
    except (KeyError, TypeError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise DeploymentError("Cannot identify source project version") from exc
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", version):
        raise DeploymentError("Unsupported project version")
    hashes = {str(relative): hashlib.sha256(payload).hexdigest()
              for relative, payload in sorted(payloads.items())}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    release_id = version + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + digest[:12]
    release = root / "releases" / release_id
    return {"operation": "user_owned_installation", "applied": False,
            "root": str(root), "source": str(source), "release_id": release_id,
            "application": str(release), "current": str(root / "current"),
            "config": str(root / "config/settings.yaml"), "runtime": str(root / "data"),
            "executable": str(root / "current/.venv/bin/researchctl"),
            "units": str(root / "units"), "evidence": str(root / "evidence"),
            "source_version": version, "source_sha256": hashes, "snapshot_sha256": digest,
            "source_file_count": len(payloads), "preflight": preflight,
            "timezone": "Asia/Seoul", "runner_default": "codex_exec",
            "smtp_enabled": False, "schedules_enabled": False,
            "web_enabled": True, "web_bind": "127.0.0.1", "web_port": web_port,
            "services_registered": False, "services_started": False,
            "system_install_performed": False, "database_initialized": False,
            "production_runner_ready": False, "commands": []}, payloads


def stage_plan(source: Path, prefix: Path) -> tuple[dict, list[tuple[Path, Path]]]:
    source, prefix = _safe_absolute(source), _safe_absolute(prefix)
    if source == prefix or source in prefix.parents or prefix in source.parents:
        raise DeploymentError("Installation rehearsal must be outside the source checkout")
    if prefix.exists():
        raise DeploymentError("Installation target already exists; nothing will be overwritten")
    parent = prefix.parent.stat()
    if (parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077
            or not stat.S_ISDIR(parent.st_mode)):
        raise DeploymentError("Target parent must be an existing private directory owned by this user")
    if os.geteuid() == 0:
        raise DeploymentError("Run the isolated installation rehearsal unprivileged, not as root")
    entries = _source_entries(source)
    app = prefix / "opt/researchops"
    config = prefix / "etc/researchops/settings.yaml"
    runtime = prefix / "var/lib/researchops"
    return {"operation": "isolated_installation_rehearsal", "applied": False,
            "prefix": str(prefix), "application": str(app), "config": str(config),
            "runtime": str(runtime), "executable": str(app / ".venv/bin/researchctl"),
            "source_file_count": len(entries), "timezone": "Asia/Seoul",
            "smtp_enabled": False, "schedules_enabled": False, "web_enabled": False,
            "services_started": False, "system_install_performed": False,
            "production_runner_ready": False}, entries


def _exclusive_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode), "wb") as stream:
        stream.write(payload)


def _run(command: list[str], *, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=120, check=False)


def stage_installation(source: Path, prefix: Path, *, apply: bool = False) -> dict:
    report, entries = stage_plan(source, prefix)
    if not apply:
        return report
    uv = shutil.which("uv")
    if uv is None:
        raise DeploymentError("Existing uv installation is required; no tool will be installed")
    # A private parent and exclusive mkdir prevent reuse or overwrite of a deployment.
    prefix.mkdir(mode=0o700)
    app, runtime = Path(report["application"]), Path(report["runtime"])
    config = Path(report["config"])
    for directory in (app, runtime, config.parent, config.parent / "tasks", prefix / "units"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for original, relative in entries:
        target = app / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _exclusive_write(target, original.read_bytes())
    report["source_sha256"] = {
        str(relative): hashlib.sha256((app / relative).read_bytes()).hexdigest()
        for _, relative in entries
    }
    report["python_version"] = ".".join(map(str, sys.version_info[:3]))
    # JSON is a YAML subset; no dependency import or operator setting is needed.
    settings = {"version": 2, "environment": "installation-rehearsal",
                "timezone": "Asia/Seoul",
                "paths": {"tasks_dir": str(config.parent / "tasks"),
                          "data_dir": str(runtime), "database": str(runtime / "researchops.db"),
                          "schemas_dir": str(app / "schemas"),
                          "delivery_config_file": str(runtime / "delivery_config.yaml")},
                "runner": {"default_type": "fake", "default_network_profile": "none"},
                "scheduler": {"enabled": False},
                "delivery": {"global_handoff_kill_switch": True, "default_mode": "dry_run",
                             "publisher": "builtin_smtp", "require_verified_receipt_for_success": True},
                "web": {"enabled": False, "bind": "127.0.0.1"}}
    _exclusive_write(config, (json.dumps(settings, indent=2) + "\n").encode())
    _exclusive_write(runtime / "delivery_config.yaml", b'{"version":2,"enabled":false,"auto_dispatch":false,"recipient_groups":{}}\n')
    account = pwd.getpwuid(os.geteuid()).pw_name
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*\$?", account):
        raise DeploymentError("Unsupported service account name")
    for name in UNIT_NAMES:
        text = (app / "deploy/systemd" / name).read_text(encoding="utf-8")
        for original, replacement in (("/opt/researchops", str(app)),
                                      ("/etc/researchops", str(config.parent)),
                                      ("/var/lib/researchops", str(runtime))):
            text = text.replace(original, replacement)
        lines = [line for line in text.splitlines() if not line.startswith(
            ("StateDirectory=", "StateDirectoryMode=", "RuntimeDirectory=", "RuntimeDirectoryMode=", "Group="))]
        text = "\n".join(lines).replace("User=researchops", "User=" + account) + "\n"
        _exclusive_write(prefix / "units" / name, text.encode())
    # Do not inherit SMTP credentials, operator config, PYTHONPATH, or UV_PROJECT_ENVIRONMENT.
    env = {key: value for key, value in os.environ.items() if key in ("PATH", "HOME", "LANG")}
    env.update({"TZ": "Asia/Seoul", "RESEARCHOPS_ROOT": str(app),
                "RESEARCHOPS_CONFIG": str(config), "UV_PYTHON_DOWNLOADS": "never"})
    try:
        install = _run([uv, "sync", "--frozen", "--offline", "--no-dev", "--no-editable", "--link-mode", "copy",
                        "--python", sys.executable, "--project", str(app)], cwd=prefix, env=env)
        if install.returncode != 0:
            raise DeploymentError("Offline locked installation failed; cache/build prerequisites must be prepared")
        executable = Path(report["executable"])
        smoke = _run([str(executable), "--help"], cwd=prefix, env=env)
        if smoke.returncode != 0 or "usage:" not in smoke.stdout.lower():
            raise DeploymentError("Installed executable smoke failed")
        verify = shutil.which("systemd-analyze")
        report["systemd_unit_verify"] = "unavailable"
        if verify:
            checked = _run([verify, "verify", *[str(prefix / "units" / name) for name in UNIT_NAMES]],
                           cwd=prefix, env=env)
            report["systemd_unit_verify"] = "passed" if checked.returncode == 0 else "failed"
            if checked.returncode != 0:
                raise DeploymentError("Staged systemd unit validation failed")
        report.update({"applied": True, "installed_executable_smoke": "passed"})
    except (DeploymentError, OSError, subprocess.TimeoutExpired) as exc:
        report["failure"] = str(exc) if isinstance(exc, DeploymentError) else type(exc).__name__
        _exclusive_write(prefix / "installation-report.json", (json.dumps(report, indent=2) + "\n").encode())
        raise
    _exclusive_write(prefix / "installation-report.json", (json.dumps(report, indent=2) + "\n").encode())
    return report


def user_installation(source: Path, root: Path, *, apply: bool = False, web_port: int = 8765) -> dict:
    """Populate a new user-owned root; do not register units, open DBs, or start services."""
    report, payloads = user_install_plan(source, root, web_port=web_port)
    if not apply:
        return report
    root = Path(report["root"])
    uv = shutil.which("uv")
    if uv is None:
        raise DeploymentError("Existing uv installation disappeared after planning")
    # Repeat the metadata/empty check immediately before an exclusive installation claim.
    if user_preflight(root)["blockers"]:
        raise DeploymentError("User root changed after installation planning; nothing will be overwritten")
    _exclusive_write(root / "installation-claim.json", (json.dumps({
        "release_id": report["release_id"], "pid": os.getpid(),
        "snapshot_sha256": report["snapshot_sha256"]}) + "\n").encode())
    release, runtime = Path(report["application"]), Path(report["runtime"])
    config, evidence = Path(report["config"]), Path(report["evidence"])
    report_path = root / ("installation-" + report["release_id"] + ".json")
    try:
        evidence.mkdir(mode=0o700)
        report_path = evidence / ("installation-" + report["release_id"] + ".json")
        for directory in (root / "releases", release, runtime, config.parent,
                          config.parent / "tasks", root / "units"):
            directory.mkdir(mode=0o700)
        for relative, payload in payloads.items():
            target = release / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _exclusive_write(target, payload)
        settings = {"version": 2, "environment": "production", "timezone": "Asia/Seoul",
                    "paths": {"tasks_dir": str(config.parent / "tasks"),
                              "data_dir": str(runtime), "database": str(runtime / "researchops.db"),
                              "schemas_dir": str(root / "current/schemas"),
                              "delivery_config_file": str(runtime / "delivery_config.yaml")},
                    "runner": {"default_type": "codex_exec", "default_network_profile": "none",
                               "codex_binary": shutil.which("codex") or "codex",
                               "antigravity_binary": shutil.which("agy") or "agy"},
                    "scheduler": {"enabled": False},
                    "delivery": {"global_handoff_kill_switch": True, "default_mode": "dry_run",
                                 "publisher": "builtin_smtp", "require_verified_receipt_for_success": True},
                    "web": {"enabled": True, "bind": "127.0.0.1", "port": web_port,
                            "allowed_hosts": ["localhost", "127.0.0.1", "::1"],
                            "require_origin_check": True, "csrf_protection": True}}
        _exclusive_write(config, (json.dumps(settings, indent=2) + "\n").encode())
        _exclusive_write(runtime / "delivery_config.yaml",
                         b'{"version":2,"enabled":false,"auto_dispatch":false,"recipient_groups":{}}\n')
        for name in UNIT_NAMES:
            unit = payloads[Path("deploy/systemd-user") / name].decode("utf-8")
            unit = unit.replace("@ROOT@", str(root)).replace("@PORT@", str(web_port))
            if re.search(r"@[A-Z_]+@", unit):
                raise DeploymentError("Unresolved placeholder in user service template")
            _exclusive_write(root / "units" / name, unit.encode())
        # This link is exclusively controller-owned release selection, never an auth bridge.
        (root / "current").symlink_to(release.relative_to(root), target_is_directory=True)
        env = {key: value for key, value in os.environ.items()
               if key in ("PATH", "HOME", "LANG", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")}
        env.update({"TZ": "Asia/Seoul", "RESEARCHOPS_ROOT": str(root / "current"),
                    "RESEARCHOPS_CONFIG": str(config), "UV_PYTHON_DOWNLOADS": "never",
                    "UV_LINK_MODE": "copy"})
        report["python_version"] = ".".join(map(str, sys.version_info[:3]))
        report["python_executable"] = sys.executable

        def command_check(label: str, command: list[str]) -> subprocess.CompletedProcess:
            command_report = {"step": label, "argv": command, "cwd": str(root)}
            report["commands"].append(command_report)
            try:
                completed = _run(command, cwd=root, env=env)
            except subprocess.TimeoutExpired as exc:
                command_report["timed_out"] = True
                completed = subprocess.CompletedProcess(command, None, exc.stdout, exc.stderr)
                raise
            finally:
                if "completed" in locals():
                    command_report["returncode"] = completed.returncode
                    for stream_name in ("stdout", "stderr"):
                        output = getattr(completed, stream_name) or ""
                        if isinstance(output, bytes):
                            output = output.decode("utf-8", errors="replace")
                        path = evidence / (label + "." + stream_name + ".log")
                        _exclusive_write(path, output[:1_000_000].encode("utf-8", errors="replace"))
                        command_report[stream_name + "_log"] = str(path)
                        command_report[stream_name + "_truncated"] = len(output) > 1_000_000
            return completed

        installed = command_check("offline-install", [uv, "sync", "--frozen", "--offline",
                                    "--no-dev", "--no-editable", "--link-mode", "copy", "--python", sys.executable,
                                    "--project", str(release)])
        if installed.returncode != 0:
            raise DeploymentError("Offline locked user installation failed; inspect preserved install evidence")
        smoke = command_check("executable-help", [report["executable"], "--help"])
        if smoke.returncode != 0 or "usage:" not in (smoke.stdout or "").lower():
            raise DeploymentError("Installed user executable smoke failed")
        version = command_check("installed-version", [str(root / "current/.venv/bin/python"), "-c",
                                "from importlib.metadata import version; print(version('researchops'))"])
        if version.returncode != 0 or (version.stdout or "").strip() != report["source_version"]:
            raise DeploymentError("Installed package version differs from source snapshot")
        report["systemd_user_unit_verify"] = "unavailable"
        verify = shutil.which("systemd-analyze")
        if verify:
            checked = command_check("user-unit-verify", [verify, "--user", "verify",
                                    *[str(root / "units" / name) for name in UNIT_NAMES]])
            report["systemd_user_unit_verify"] = "passed" if checked.returncode == 0 else "failed"
            if checked.returncode != 0:
                raise DeploymentError("User service template verification failed; nothing was started")
        report.update({"applied": True, "installed_executable_smoke": "passed",
                       "installed_version_smoke": "passed"})
    except (Exception, KeyboardInterrupt) as exc:
        report["failure"] = str(exc) if isinstance(exc, DeploymentError) else type(exc).__name__
        report["report_path"] = str(report_path)
        _exclusive_write(report_path, (json.dumps(report, indent=2) + "\n").encode())
        raise
    report["report_path"] = str(report_path)
    _exclusive_write(report_path, (json.dumps(report, indent=2) + "\n").encode())
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("preflight", help="Inspect metadata only; never initialize the application")
    stage = commands.add_parser("stage", help="Plan an isolated offline installation; never install system services")
    stage.add_argument("--source", type=Path, required=True)
    stage.add_argument("--prefix", type=Path, required=True)
    stage.add_argument("--apply", action="store_true", help="Create the new private rehearsal target")
    user_check = commands.add_parser("user-preflight", help="Inspect a user-owned initial install root without reading files")
    user_check.add_argument("--root", type=Path, required=True)
    user = commands.add_parser("user-install", help="Plan an offline install below an empty user-owned root")
    user.add_argument("--source", type=Path, required=True)
    user.add_argument("--root", type=Path, required=True)
    user.add_argument("--web-port", type=int, default=8765)
    user.add_argument("--apply", action="store_true", help="Install only inside the provided root; never start services")
    args = parser.parse_args(argv)
    try:
        if args.command == "stage":
            report = stage_installation(args.source, args.prefix, apply=args.apply)
        elif args.command == "user-install":
            report = user_installation(args.source, args.root, apply=args.apply, web_port=args.web_port)
        elif args.command == "user-preflight":
            report = user_preflight(args.root)
        else:
            report = host_preflight()
        print(json.dumps(report, indent=2))
        return 78 if report.get("blockers") else 0
    except (DeploymentError, OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, DeploymentError) else type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
