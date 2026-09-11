"""Containment checks and descriptor-based reads of untrusted worker files."""

import hashlib
import os
from pathlib import Path
import stat
from contextlib import contextmanager
from typing import Tuple

from researchops.errors import WorkspaceError


def assert_path_contained(target_path: Path, root_path: Path) -> Path:
    """Reject traversal and every symlink, including links that stay inside root."""
    root = Path(os.path.abspath(root_path))
    target = Path(os.path.abspath(target_path))
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise WorkspaceError(f"Path escape violation: {target_path} is outside {root_path}") from exc
    for component in (root, *reversed(root.parents)):
        if component.is_symlink():
            raise WorkspaceError(f"Symlink in trusted root: {component}")
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise WorkspaceError(f"Symlink forbidden: {current}")
    return target


def read_safe_bytes(file_path: Path, root_path: Path, max_bytes: int = 20_000_000) -> bytes:
    """Bounded read with O_NOFOLLOW at every level and regular-file checks."""
    if type(max_bytes) is not int or max_bytes < 0:
        raise WorkspaceError("max_bytes must be a nonnegative integer")
    target = assert_path_contained(file_path, root_path)
    root = Path(os.path.abspath(root_path))
    parts = target.relative_to(root).parts
    if not parts:
        raise WorkspaceError(f"Expected a file below root: {file_path}")
    descriptors = []
    try:
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory_fd)
        for part in parts[:-1]:
            directory_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=directory_fd)
            descriptors.append(directory_fd)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory_fd)
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise WorkspaceError(f"Expected singly linked regular file: {file_path}")
        if before.st_size > max_bytes:
            raise WorkspaceError(f"File exceeds {max_bytes} bytes: {file_path}")
        chunks, total = [], 0
        while chunk := os.read(fd, min(65536, max_bytes + 1 - total)):
            total += len(chunk)
            if total > max_bytes:
                raise WorkspaceError(f"File exceeds {max_bytes} bytes: {file_path}")
            chunks.append(chunk)
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink):
            raise WorkspaceError(f"File changed while importing: {file_path}")
        return b"".join(chunks)
    except OSError as exc:
        raise WorkspaceError(f"Unsafe or unreadable file: {file_path}: {exc.strerror}") from exc
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


@contextmanager
def open_safe_file(file_path: Path, root_path: Path, max_bytes: int = 20_000_000):
    """Open a bounded regular file without following links; verify on close.

    Callers may read in chunks or seek for a small preview. The descriptor pins
    the opened inode, and metadata checks detect changes during the operation.
    """
    if type(max_bytes) is not int or max_bytes < 0:
        raise WorkspaceError("max_bytes must be a nonnegative integer")
    target = assert_path_contained(file_path, root_path)
    parts = target.relative_to(Path(os.path.abspath(root_path))).parts
    if not parts:
        raise WorkspaceError("Expected a file below root")
    descriptors = []
    try:
        fd = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        for part in parts[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > max_bytes:
                raise WorkspaceError("Expected a bounded singly linked regular file")
            yield stream, before.st_size
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink):
                raise WorkspaceError("File changed during streaming read")
    except OSError as exc:
        raise WorkspaceError("Unsafe or unreadable streaming file") from exc
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def safe_file_info(file_path: Path, root_path: Path, max_bytes: int = 20_000_000) -> Tuple[str, int]:
    digest, total = hashlib.sha256(), 0
    with open_safe_file(file_path, root_path, max_bytes) as (stream, size):
        while chunk := stream.read(min(65536, max_bytes + 1 - total)):
            total += len(chunk)
            if total > max_bytes:
                raise WorkspaceError("File exceeded its streaming byte limit")
            digest.update(chunk)
        if total != size:
            raise WorkspaceError("File size changed during streaming read")
    return digest.hexdigest(), total
