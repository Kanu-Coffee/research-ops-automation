"""Bounded CLI execution for explicitly opted-in, trusted development inputs.

The caller supplies the environment allowlist and a dedicated working directory.
This is not a hostile-code sandbox: process groups cannot contain descendants
which deliberately escape with setsid(), and this module does not provide
credential, filesystem, network, memory, PID, or filesystem-quota isolation.
``cleanup_verified`` means the direct child was reaped, captured pipes reached
EOF, and its process group has no executing members. It is not cgroup evidence.
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from typing import Callable


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    spawned: bool
    cleanup_verified: bool
    error: str | None = None
    timed_out: bool = False
    cancelled: bool = False
    cleanup_evidence: dict = field(default_factory=dict)
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0


def _group_quiescent(process_group: int) -> bool:
    """Check live members without reading command lines or process environments.

    Linux can retain orphan zombies until its init process reaps them. Those
    have no executing code or open file descriptors, so they are quiescent.
    Where this cannot be established, fail closed instead of claiming cleanup.
    """
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    if not Path("/proc/self/stat").exists():
        return False
    found = False
    try:
        for process in Path("/proc").iterdir():
            if not process.name.isdecimal():
                continue
            try:
                # comm can contain spaces and parentheses; fields after its
                # last closing parenthesis start with state, ppid, and pgrp.
                stat = (process / "stat").read_text()
                fields = stat[stat.rfind(")") + 2:].split()
                if int(fields[2]) != process_group:
                    continue
                found = True
                if fields[0] not in {"Z", "X"}:
                    return False
            except FileNotFoundError:
                continue
            except (OSError, ValueError, IndexError):
                return False
    except OSError:
        return False
    if found:
        return True
    # A group may have vanished while /proc was being inspected.
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return True
    except OSError:
        pass
    return False


def run_bounded(argv: list[str], *, cwd: Path, env: dict[str, str],
                stdin: bytes | None = None, timeout_seconds: int,
                max_output_bytes: int = 2_000_000,
                cancellation_check: Callable[[], bool] | None = None,
                stdout_path: Path | None = None, stderr_path: Path | None = None,
                max_preview_bytes: int = 65_536,
                stdout_consumer: Callable[[bytes], None] | None = None) -> ProcessResult:
    """Run one trusted CLI with raw, combined-size-bounded stdout and stderr.

    No shell, environment inheritance, auth-file reads, or persistent/global
    configuration changes are performed. Timeouts, cancellation, output
    overflow, and normal leader exit all trigger cleanup of the newly created
    process group. Cancellation callback failure also terminates the child.
    Raw bytes are preserved even when a size limit bisects a UTF-8 character;
    decoding and provider/event validation belong to the caller.
    Optional exclusive, no-follow log files receive original bytes; the returned
    byte strings then contain only bounded previews. A failed writer or parser
    is never called again during cleanup; pipes are still drained to EOF.
    """
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive integer")
    if type(max_output_bytes) is not int or max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be a positive integer")
    if type(max_preview_bytes) is not int or max_preview_bytes <= 0:
        raise ValueError("max_preview_bytes must be a positive integer")
    log_paths = {"stdout": Path(stdout_path) if stdout_path is not None else None,
                 "stderr": Path(stderr_path) if stderr_path is not None else None}
    if any(path is not None and not path.is_absolute() for path in log_paths.values()):
        raise ValueError("Log paths must be absolute")
    if stdout_path is not None and stderr_path is not None and log_paths["stdout"] == log_paths["stderr"]:
        raise ValueError("Log paths must be distinct")
    if not isinstance(argv, list) or not argv or any(
            not isinstance(argument, str) or "\x00" in argument for argument in argv):
        raise ValueError("argv must be a nonempty list of strings without NUL")
    if not isinstance(env, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            or not key or "=" in key or "\x00" in key or "\x00" in value
            for key, value in env.items()):
        raise ValueError("env must be an explicit string mapping")
    if stdin is not None and not isinstance(stdin, bytes):
        raise ValueError("stdin must be bytes or None")
    if os.name != "posix":
        return ProcessResult(-1, b"", b"", False, True, "POSIX process groups are required")
    if cancellation_check is not None:
        try:
            if cancellation_check():
                return ProcessResult(130, b"", b"", False, True, "cancelled", cancelled=True)
        except Exception as exc:
            return ProcessResult(-1, b"", b"", False, True,
                                 "cancellation_check_failed: " + type(exc).__name__)

    proc = None
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    log_files = {}
    stream_bytes = {"stdout": 0, "stderr": 0}
    capture_failed = False
    pipes = []
    pending_input = memoryview(stdin or b"")
    captured_bytes = 0
    error = None
    timed_out = cancelled = False
    cleanup_verified = True
    returncode = -1
    cleanup_evidence = {}

    def close_pipe(pipe):
        try:
            selector.unregister(pipe)
        except (KeyError, ValueError):
            pass
        pipe.close()

    def collect_output(pipe, name):
        nonlocal captured_bytes, error, capture_failed
        try:
            chunk = os.read(pipe.fileno(), 65536)
        except BlockingIOError:
            return
        if not chunk:
            close_pipe(pipe)
            return
        if capture_failed:
            return
        remaining = max_output_bytes - captured_bytes
        accepted = chunk[:remaining]
        captured_bytes += len(accepted)
        try:
            if name in log_files:
                pending = memoryview(accepted)
                while pending:
                    written = log_files[name].write(pending)
                    if not written:
                        raise OSError("Log write made no progress")
                    stream_bytes[name] += written
                    pending = pending[written:]
            else:
                stream_bytes[name] += len(accepted)
        except Exception as exc:
            capture_failed = True
            error = error or "log_write_failed: " + type(exc).__name__
            return
        preview_remaining = max_preview_bytes - len(buffers[name]) if name in log_files else len(accepted)
        buffers[name].extend(accepted[:preview_remaining])
        if name == "stdout" and accepted and stdout_consumer is not None:
            try:
                stdout_consumer(accepted)
            except Exception as exc:
                # Only fixed transport codes can enter public diagnostics.
                code = getattr(exc, "trace_error_code", None)
                allowed_codes = {"trace_total_limit_exceeded", "trace_event_limit_exceeded",
                    "trace_event_count_exceeded", "trace_invalid_event", "trace_invalid_lifecycle",
                    "trace_incomplete_event", "trace_missing_terminal", "trace_closed"}
                error = error or (code if isinstance(code, str) and code in allowed_codes else
                                  "stdout_consumer_failed: " + type(exc).__name__)
                capture_failed = True
        if len(chunk) > remaining and error is None:
            error = "output_limit_exceeded"
            capture_failed = True

    try:
        for name, path in log_paths.items():
            if path is not None:
                try:
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    try:
                        log_files[name] = os.fdopen(descriptor, "wb", buffering=0)
                    except BaseException:
                        os.close(descriptor)
                        raise
                except OSError as exc:
                    error = "log_open_failed: " + type(exc).__name__
                    raise
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, shell=False,
                                    stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True, close_fds=True, bufsize=0)
        except (OSError, ValueError) as exc:
            error = "spawn_failed: " + type(exc).__name__
        if proc is not None:
            pipes.extend((proc.stdout, proc.stderr))
            if proc.stdin is not None:
                pipes.append(proc.stdin)
            for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            if proc.stdin is not None:
                if pending_input:
                    os.set_blocking(proc.stdin.fileno(), False)
                    selector.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    proc.stdin.close()
            deadline = time.monotonic() + timeout_seconds
            while True:
                if error is not None:
                    break
                if cancellation_check is not None:
                    try:
                        cancelled = bool(cancellation_check())
                    except Exception as exc:
                        error = error or "cancellation_check_failed: " + type(exc).__name__
                        break
                    if cancelled:
                        error = "cancelled"
                        break
                if time.monotonic() >= deadline:
                    timed_out = True
                    error = "timeout"
                    break
                if error is not None or proc.poll() is not None:
                    break
                for key, _ in selector.select(min(0.05, max(0, deadline - time.monotonic()))):
                    if key.data != "stdin":
                        collect_output(key.fileobj, key.data)
                        continue
                    try:
                        written = os.write(key.fileobj.fileno(), pending_input[:8192])
                        pending_input = pending_input[written:]
                    except BrokenPipeError:
                        pending_input = memoryview(b"")
                    except BlockingIOError:
                        continue
                    if not pending_input:
                        close_pipe(key.fileobj)
    except Exception as exc:
        error = error or "process_io_failed: " + type(exc).__name__
        capture_failed = True
    finally:
        if proc is not None:
            cleanup_verified = False
            cleanup_evidence = {"direct_child_pid": proc.pid, "process_group_id": proc.pid,
                "group_signal_succeeded": False, "direct_child_reaped": False,
                "captured_pipes_eof": False, "process_group_quiescent": False}
            # This PGID was created by start_new_session for this direct child.
            # Never signal the caller's group or an arbitrary configured PID.
            group_signalled = proc.pid > 1 and proc.pid != os.getpgrp()
            if group_signalled:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    group_signalled = False
            try:
                proc.kill()  # Covers an unexpected direct-child group change.
            except OSError:
                pass
            if proc.stdin is not None and not proc.stdin.closed:
                close_pipe(proc.stdin)
            try:
                returncode = proc.wait(timeout=2)
                child_reaped = True
            except (OSError, subprocess.TimeoutExpired):
                child_reaped = False
            cleanup_evidence.update(group_signal_succeeded=group_signalled, direct_child_reaped=child_reaped)
            # Bounded drain also handles data already buffered when the leader
            # exited. An escaped process retaining a pipe cannot hang cleanup.
            drain_deadline = time.monotonic() + 2
            try:
                while selector.get_map() and time.monotonic() < drain_deadline:
                    for key, _ in selector.select(0.02):
                        collect_output(key.fileobj, key.data)
                pipes_drained = not selector.get_map()
                cleanup_evidence["captured_pipes_eof"] = pipes_drained
                group_empty = _group_quiescent(proc.pid)
                while not group_empty and time.monotonic() < drain_deadline:
                    time.sleep(0.02)
                    group_empty = _group_quiescent(proc.pid)
                cleanup_verified = group_signalled and child_reaped and pipes_drained and group_empty
                cleanup_evidence["process_group_quiescent"] = group_empty
            except Exception:
                cleanup_verified = False
            if not cleanup_verified and error is None:
                error = "process_group_cleanup_unverified"
        for pipe in pipes:
            if not pipe.closed:
                pipe.close()
        selector.close()
        pending_input.release()
        for stream in log_files.values():
            # Closing is attempted once even when fsync fails; never let a
            # secondary close error hide the first transport/cleanup result.
            try:
                if not capture_failed:
                    os.fsync(stream.fileno())
            except OSError as exc:
                error = error or "log_write_failed: " + type(exc).__name__
            try:
                stream.close()
            except OSError as exc:
                error = error or "log_write_failed: " + type(exc).__name__

    return ProcessResult(returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"]),
                         proc is not None, cleanup_verified, error, timed_out, cancelled, cleanup_evidence,
                         log_paths["stdout"] if "stdout" in log_files else None,
                         log_paths["stderr"] if "stderr" in log_files else None,
                         stream_bytes["stdout"], stream_bytes["stderr"])
