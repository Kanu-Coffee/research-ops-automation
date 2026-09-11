"""Fail-closed Linux namespace/cgroup execution for uncredentialed subprocesses.

AI adapters remain blocked separately until a credential broker and filesystem
quotas exist. This launcher never treats a sanitized environment as isolation.
"""
import os
from dataclasses import asdict
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional
from uuid import uuid4

from researchops.errors import WorkspaceError
from researchops.runners.base import RunnerExecutionResult
from researchops.runners.workspace_quota import DedicatedFilesystemQuota
from researchops.workspace.security import assert_path_contained


class IsolatedProcessLauncher:
    def __init__(self, default_timeout_seconds: int = 3600, *,
                 cgroup_root: Optional[Path] = None, memory_bytes: int = 536870912,
                 max_pids: int = 64, max_output_bytes: int = 1048576,
                 workspace_quota: Optional[DedicatedFilesystemQuota] = None):
        self.default_timeout_seconds = default_timeout_seconds
        self.cgroup_root = Path(cgroup_root) if cgroup_root else None
        self.memory_bytes = memory_bytes
        self.max_pids = max_pids
        self.max_output_bytes = max_output_bytes
        self.workspace_quota = workspace_quota
        for value in (default_timeout_seconds, memory_bytes, max_pids, max_output_bytes):
            if type(value) is not int or value <= 0:
                raise ValueError("Launcher limits must be positive integers")

    def readiness(self) -> Dict:
        bwrap = shutil.which("bwrap")
        reasons = []
        if not bwrap:
            reasons.append("bubblewrap is not installed")
        if not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
            reasons.append("cgroup v2 is unavailable")
        if not self.cgroup_root:
            reasons.append("a delegated cgroup v2 root is not configured")
        else:
            try:
                group = assert_path_contained(self.cgroup_root, Path("/sys/fs/cgroup"))
                if group == Path("/sys/fs/cgroup"):
                    raise WorkspaceError("the cgroup hierarchy root cannot be delegated to a task launcher")
                if group.stat().st_uid != os.getuid() or not os.access(group, os.W_OK):
                    raise WorkspaceError("cgroup root is not owned and writable by launcher account")
                controllers = (group / "cgroup.subtree_control").read_text().split()
                if not {"memory", "pids"}.issubset(controllers):
                    raise WorkspaceError("memory and pids controllers must already be delegated")
                if not (group / "cgroup.kill").exists():
                    raise WorkspaceError("cgroup.kill is required for descendant termination")
            except (OSError, WorkspaceError) as exc:
                reasons.append(str(exc))
        return {"ready": not reasons, "blockers": reasons, "bwrap_path": bwrap,
                "tier": "namespace-cgroup" if not reasons else "blocked",
                "credential_broker_ready": False, "filesystem_quota_ready": False,
                "kernel_execution_verified": False,
                "internal_missing": ["general-task protected provider control and native-tool prevention",
                                     "operating egress integration of public fetch backend",
                                     "persistent and auxiliary-mount byte/inode quota acceptance"],
                "operator_prerequisites": ["delegated cgroup v2 memory and pids controllers",
                                           "quota-capable workspace storage provisioning",
                                           "approved scoped provider/account contract outside workspace"]}

    def build_isolated_env(self, project_dir: Path, tmp_dir: Path,
                           task_id: Optional[str] = None, run_id: Optional[str] = None,
                           stage: Optional[str] = None,
                           extra_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "Asia/Seoul",
               "HOME": str(project_dir), "TMPDIR": str(tmp_dir), "TEMP": str(tmp_dir),
               "TMP": str(tmp_dir), "XDG_CONFIG_HOME": str(tmp_dir / ".config"),
               "XDG_CACHE_HOME": str(tmp_dir / ".cache"),
               "XDG_DATA_HOME": str(tmp_dir / ".local/share")}
        for key, value in (("TASK_ID", task_id), ("RUN_ID", run_id), ("STAGE", stage)):
            if value:
                env["RESEARCHOPS_" + key] = value
        # Never inherit host PATH, LD_PRELOAD, Python startup hooks or arbitrary secrets.
        for key in ("LANG", "LC_ALL", "TERM"):
            if extra_env and key in extra_env:
                env[key] = str(extra_env[key])
        return env

    def build_namespace_command(self, cmd: List[str], cwd: Path, project_dir: Path,
                                tmp_dir: Path, input_dir: Optional[Path] = None) -> List[str]:
        binary = shutil.which("bwrap")
        if not binary:
            raise WorkspaceError("bubblewrap is not installed")
        directories = [project_dir, tmp_dir, cwd]
        if input_dir is not None:
            directories.append(input_dir)
        for directory in directories:
            assert_path_contained(directory, directory)
            if not directory.is_absolute() or directory == Path("/") or not directory.is_dir():
                raise WorkspaceError("Sandbox mount must be an existing absolute directory")
            assert_path_contained(directory, project_dir.parent)
            if directory == project_dir.parent:
                raise WorkspaceError("Workspace audit/lock root must not be mounted")
            if any(directory == Path(runtime) or directory in Path(runtime).parents
                   for runtime in ("/usr", "/bin", "/lib", "/lib64", "/etc", "/sys", "/proc", "/dev")):
                raise WorkspaceError("Host runtime roots cannot be writable task mounts")
        if len(set(directories)) != len(directories):
            raise WorkspaceError("Sandbox mount roots must be distinct")
        for index, directory in enumerate(directories):
            if any(directory in other.parents or other in directory.parents
                   for other in directories[index + 1:]):
                raise WorkspaceError("Sandbox mount roots must not overlap")
        for credential in (".codex", ".gemini", ".config/antigravity"):
            target = project_dir / credential
            if target.exists() or target.is_symlink():
                raise WorkspaceError("Legacy worker credential artifact requires operator quarantine")
        command = [binary, "--unshare-all", "--new-session", "--die-with-parent",
                   "--cap-drop", "ALL"]
        for runtime in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(runtime).exists():
                command.extend(["--ro-bind", runtime, runtime])
        command.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"])
        for directory in directories:
            command.extend(["--ro-bind" if directory == input_dir else "--bind",
                            str(directory), str(directory)])
        return command + ["--chdir", str(cwd), "--", *cmd]

    def _create_group(self) -> Path:
        group = self.cgroup_root / ("researchops-" + uuid4().hex)
        group.mkdir(mode=0o700)
        try:
            (group / "memory.max").write_text(str(self.memory_bytes))
            (group / "memory.swap.max").write_text("0")
            (group / "pids.max").write_text(str(self.max_pids))
        except Exception:
            group.rmdir()
            raise
        return group

    @staticmethod
    def _group_empty(group: Path) -> bool:
        return "populated 0" in (group / "cgroup.events").read_text().splitlines()

    @classmethod
    def _kill_group(cls, group: Path, proc: subprocess.Popen) -> bool:
        try:
            (group / "cgroup.kill").write_text("1")
            try:
                proc.kill()  # Also covers failure before the stopped helper joined its cgroup.
            except ProcessLookupError:
                pass
            proc.wait(timeout=2)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if cls._group_empty(group):
                    return True
                time.sleep(0.02)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return False

    def run(self, cmd: List[str], cwd: Path, project_dir: Path, tmp_dir: Path,
            timeout_seconds: Optional[int] = None, task_id: Optional[str] = None,
            run_id: Optional[str] = None, stage: Optional[str] = None,
            extra_env: Optional[Dict[str, str]] = None, *, input_dir: Optional[Path] = None,
            network_profile: str = "none", cancellation_check: Optional[Callable[[], bool]] = None,
            fencing_token: Optional[str] = None) -> RunnerExecutionResult:
        ready = self.readiness()
        if not ready["ready"] or network_profile != "none":
            reason = "; ".join(ready["blockers"]) or "Only the no-network subprocess profile is implemented"
            return RunnerExecutionResult(False, -1, "", reason, error_message=reason,
                                         isolation={**ready, "spawned": False}, cleanup_verified=True)
        timeout = timeout_seconds if timeout_seconds is not None else self.default_timeout_seconds
        if type(timeout) is not int or timeout <= 0:
            raise ValueError("timeout_seconds must be a positive integer")
        proc, group = None, None
        buffers = [bytearray(), bytearray()]
        output_overflow = threading.Event()
        readers = []
        reader_pipes = []
        reason, code, cleanup = None, -1, True
        quota_snapshot, quota_rechecked = None, False
        try:
            if self.workspace_quota is not None:
                quota_snapshot = self.workspace_quota.inspect([project_dir, tmp_dir, cwd])
            namespace_cmd = self.build_namespace_command(cmd, cwd, project_dir, tmp_dir, input_dir)
            env = self.build_isolated_env(project_dir, tmp_dir, task_id, run_id, stage, extra_env)
            env.update({"RESEARCHOPS_ISOLATED": "1", "RESEARCHOPS_ISOLATION_TIER": "namespace-cgroup"})
            group = self._create_group()
            # A trusted helper stops before executing untrusted code. Parent assigns the
            # stopped PID to its cgroup, then resumes it; no preexec_fn/thread hazards.
            helper = "import os,signal,sys;os.kill(os.getpid(),signal.SIGSTOP);os.execv(sys.argv[1],sys.argv[1:])"
            proc = subprocess.Popen([sys.executable, "-I", "-c", helper, *namespace_cmd],
                                    cwd="/", env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True)
            stopped = False
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                pid, status = os.waitpid(proc.pid, os.WNOHANG | os.WUNTRACED)
                if pid:
                    stopped = os.WIFSTOPPED(status)
                    if not stopped:
                        proc.returncode = os.waitstatus_to_exitcode(status)
                    break
                time.sleep(0.01)
            if not stopped:
                raise WorkspaceError("Launcher helper did not stop before cgroup assignment")
            (group / "cgroup.procs").write_text(str(proc.pid))
            def drain(pipe, buffer):
                try:
                    while chunk := pipe.read(65536):
                        available = self.max_output_bytes - len(buffer)
                        buffer.extend(chunk[:max(0, available)])
                        if len(chunk) > available:
                            output_overflow.set()
                finally:
                    pipe.close()
            for pipe, buffer in zip((proc.stdout, proc.stderr), buffers):
                reader = threading.Thread(target=drain, args=(pipe, buffer), daemon=True)
                reader.start()
                readers.append(reader)
                reader_pipes.append(pipe)
            if self.workspace_quota is not None:
                # The helper is still stopped: changed storage must never run task code.
                self.workspace_quota.inspect([project_dir, tmp_dir, cwd], expected=quota_snapshot)
                quota_rechecked = True
            os.kill(proc.pid, signal.SIGCONT)
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if cancellation_check and cancellation_check():
                    reason = "Cancelled or lease ownership lost"
                    break
                if output_overflow.is_set():
                    reason = "Runner output exceeded capture limit"
                    break
                if time.monotonic() >= deadline:
                    reason = f"Timeout after {timeout}s"
                    break
                time.sleep(0.05)
            code = proc.returncode if proc.returncode is not None else -1
        except Exception as exc:
            reason = f"Isolated launch failed: {exc}"
        finally:
            if proc is not None and group is not None:
                cleanup = self._kill_group(group, proc)
            for reader in readers:
                reader.join(timeout=2)
                if reader.is_alive():
                    cleanup = False
            # A helper/setup error can occur before the drain threads take ownership
            # of the subprocess pipes. Close those descriptors explicitly instead
            # of relying on Popen garbage collection. Do not close a pipe owned by a
            # live drain: BufferedReader.close() may block behind its pending read.
            if proc is not None:
                for pipe in (proc.stdout, proc.stderr):
                    if pipe is not None and pipe not in reader_pipes:
                        try:
                            pipe.close()
                        except OSError:
                            cleanup = False
            if group is not None and cleanup:
                try:
                    group.rmdir()
                except OSError:
                    cleanup = False
        if not cleanup:
            reason = "Descendant cleanup could not be verified; workspace must remain locked"
        if output_overflow.is_set():
            reason = "Runner output exceeded capture limit"
        stdout, stderr = [bytes(b).decode("utf-8", errors="replace") for b in buffers]
        return RunnerExecutionResult(
            success=code == 0 and reason is None and cleanup, exit_code=code,
            stdout=stdout, stderr=stderr, error_message=reason or (stderr if code != 0 else None),
            cleanup_verified=cleanup,
            isolation={"tier": "namespace-cgroup", "spawned": proc is not None,
                       "cgroup": str(group) if group else None, "cleanup_verified": cleanup,
                       "filesystem_quota_ready": quota_rechecked,
                       "filesystem_quota": asdict(quota_snapshot) if quota_snapshot is not None else None,
                       "filesystem_quota_scope": "project,tmp,cwd only; excludes /tmp and /dev/shm"
                       if quota_rechecked else "unverified",
                       "whole_runner_boundary_ready": False},
            events=[{"type": "runner_exit", "fencing_token": fencing_token,
                     "cleanup_verified": cleanup, "reason": reason}],
        )
