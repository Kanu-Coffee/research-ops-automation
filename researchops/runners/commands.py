"""Pure, non-executing CLI command contracts from inspected installed help.

These builders are intentionally not connected to the real adapters. A command
line is not a security boundary: credential, network and quota backends remain
required before any returned command may be executed by a real adapter.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    cwd: Path
    stdin: bytes | None


def _text(value: str, label: str, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be nonempty text without NUL")
    if not multiline and (any(ord(char) < 32 for char in value) or value.startswith("-")):
        raise ValueError(f"{label} cannot contain control characters or start with an option prefix")
    return value


def _path(value: Path, label: str) -> str:
    value = Path(value)
    if not value.is_absolute() or value == Path("/") or ".." in value.parts:
        raise ValueError(f"{label} must be an absolute non-root path without traversal")
    return _text(str(value), label)


def build_codex_command(binary: str, *, prompt: str, cwd: Path,
                        writable_dirs: Sequence[Path] = (), model: str | None = None,
                        reasoning_effort: str | None = None,
                        output_schema: Path | None = None) -> CommandSpec:
    """Prepare a fresh JSONL invocation with stdin, not a shell command.

    The installed help advertises generic ``-c`` but does not establish a
    reasoning-effort key/value contract. Reject that optional setting instead
    of silently dropping it or assuming a config key.
    """
    if reasoning_effort is not None:
        raise ValueError("Codex reasoning-effort configuration has not been capability-verified")
    argv = [_text(binary, "binary"), "exec", "--json", "--ephemeral", "--ignore-user-config",
            "--ignore-rules", "--sandbox", "workspace-write", "--skip-git-repo-check",
            "--cd", _path(cwd, "cwd")]
    for directory in writable_dirs:
        argv.extend(["--add-dir", _path(directory, "writable directory")])
    if model is not None:
        argv.extend(["--model", _text(model, "model")])
    if output_schema is not None:
        argv.extend(["--output-schema", _path(output_schema, "output schema")])
    argv.append("-")
    return CommandSpec(tuple(argv), Path(cwd), _text(prompt, "prompt", multiline=True).encode("utf-8"))


def build_antigravity_command(binary: str, *, prompt: str, cwd: Path,
                              writable_dirs: Sequence[Path] = (), model: str | None = None,
                              reasoning_effort: str | None = None,
                              output_schema: Path | None = None,
                              timeout_seconds: int = 7200) -> CommandSpec:
    """Prepare fresh print-mode stream JSON; agy uses process cwd, not ``--cd``."""
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive integer")
    if reasoning_effort is not None and reasoning_effort not in {"low", "medium", "high"}:
        raise ValueError("Antigravity reasoning effort must be low, medium or high")
    if _text(prompt, "prompt", multiline=True).startswith("-"):
        raise ValueError("Antigravity print prompt cannot start with an option prefix")
    _path(cwd, "cwd")
    argv = [_text(binary, "binary"), "--print", _text(prompt, "prompt", multiline=True),
            "--output-format", "stream-json", "--sandbox", "--disable-slash-commands",
            "--print-timeout", f"{timeout_seconds}s"]
    for directory in writable_dirs:
        argv.extend(["--add-dir", _path(directory, "writable directory")])
    if model is not None:
        argv.extend(["--model", _text(model, "model")])
    if reasoning_effort is not None:
        argv.extend(["--effort", reasoning_effort])
    if output_schema is not None:
        argv.extend(["--json-schema", _path(output_schema, "output schema")])
    return CommandSpec(tuple(argv), Path(cwd), None)
