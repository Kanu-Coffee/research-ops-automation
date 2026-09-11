"""Fail-closed capacity backend for an operator-provisioned task filesystem.

This backend uses the kernel's *whole-filesystem* byte and inode ceilings, not
free-space monitoring, per-file limits, or an unimplemented project quota. Each
task needs its own mounted filesystem containing every writable project/phase
directory. Provisioning, exclusive task assignment, and durable backup belong
to deployment; this module never mounts, formats, resizes, or deletes storage.

Ext4 is supported for persistent workspaces. Tmpfs requires an explicit volatile
opt-in and is never reported as persistent. Passing this check does not cover a
launcher's other writable mounts (notably /tmp and /dev/shm), nor complete the
credential/network/cgroup boundary. Recheck the same snapshot immediately before
launch; workers must not have mount privileges or access to alternative storage.
"""

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
import stat
from typing import Iterable, Optional

from researchops.errors import WorkspaceError
from researchops.workspace.security import assert_path_contained


@dataclass(frozen=True)
class FilesystemQuotaSnapshot:
    mount_path: str
    mount_id: int
    device: str
    filesystem: str
    total_bytes: int
    total_inodes: int
    root_inode: int
    persistent: bool


@dataclass(frozen=True)
class _Mount:
    mount_id: int
    device: str
    root: str
    path: Path
    filesystem: str
    options: frozenset


def _decode_mount_path(value: str) -> str:
    return re.sub(r"\\(040|011|012|134)", lambda match: chr(int(match[1], 8)), value)


def _mounts() -> list[_Mount]:
    # Source/device names are deliberately not returned in user-facing evidence.
    with open("/proc/self/mountinfo", encoding="utf-8") as stream:
        contents = stream.read(4_000_001)
    if len(contents) > 4_000_000:
        raise WorkspaceError("Mount table exceeds inspection limit")
    mounts = []
    for line in contents.splitlines():
        try:
            before, after = line.split(" - ", 1)
            fields, tail = before.split(), after.split()
            if len(fields) < 6 or len(tail) < 3:
                raise ValueError("incomplete mount record")
            mounts.append(_Mount(int(fields[0]), fields[2], _decode_mount_path(fields[3]),
                                 Path(_decode_mount_path(fields[4])), tail[0],
                                 frozenset(fields[5].split(","))))
        except (ValueError, IndexError) as exc:
            raise WorkspaceError("Cannot safely parse kernel mount table") from exc
    return mounts


class DedicatedFilesystemQuota:
    """Verify a separately provisioned task-volume capacity, without mutation.

    max_bytes/max_inodes are policy ceilings on *total* kernel capacity, not
    current availability. A full small disk cannot make a larger shared disk
    pass. Exact-mount matching also prevents ordinary directories or bind mounts
    of subdirectories from masquerading as separately bounded filesystems.
    """

    def __init__(self, mount_path: Path, *, max_bytes: int, max_inodes: int,
                 allow_volatile: bool = False):
        for value in (max_bytes, max_inodes):
            if type(value) is not int or value <= 0:
                raise ValueError("Filesystem byte and inode limits must be positive integers")
        if type(allow_volatile) is not bool:
            raise ValueError("allow_volatile must be a boolean")
        self.mount_path = Path(mount_path)
        self.max_bytes = max_bytes
        self.max_inodes = max_inodes
        self.allow_volatile = allow_volatile

    def inspect(self, writable_paths: Iterable[Path], *,
                expected: Optional[FilesystemQuotaSnapshot] = None) -> FilesystemQuotaSnapshot:
        """Return kernel-backed capacity evidence or raise before spawning code."""
        root = self.mount_path
        if not root.is_absolute() or ".." in root.parts:
            raise WorkspaceError("Task filesystem must use an absolute canonical mount path")
        protected = ("/", "/home", "/root", "/tmp", "/var", "/var/tmp", "/dev",
                     "/proc", "/sys", "/usr", "/etc", "/run", "/opt")
        if root in map(Path, protected):
            raise WorkspaceError("Host/shared filesystem roots cannot be task quota volumes")
        assert_path_contained(root, root)
        paths = list(writable_paths)
        if not paths:
            raise WorkspaceError("All writable task paths must be supplied to quota validation")
        descriptors = []
        try:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(root_fd)
            root_info = os.fstat(root_fd)
            if root_info.st_uid != os.getuid() or stat.S_IMODE(root_info.st_mode) & 0o022:
                raise WorkspaceError("Task volume root must be owned by the launcher and not group/world writable")
            table = _mounts()
            matching = [mount for mount in table if mount.path == root]
            if len(matching) != 1:
                raise WorkspaceError("Task volume must be one exact dedicated filesystem mount")
            mount = matching[0]
            if mount.root != "/":
                raise WorkspaceError("A bind-mounted subdirectory is not a dedicated filesystem")
            if mount.filesystem not in {"ext4", "tmpfs"}:
                raise WorkspaceError("Only separately provisioned ext4 and explicit volatile tmpfs are supported")
            if mount.filesystem == "tmpfs" and not self.allow_volatile:
                raise WorkspaceError("Tmpfs is volatile; persistent task storage is required")
            if not {"rw", "nodev", "nosuid"}.issubset(mount.options):
                raise WorkspaceError("Task filesystem requires rw,nodev,nosuid mount options")
            if any(entry.device == mount.device and entry.path != root for entry in table):
                raise WorkspaceError("Shared/aliased host filesystems cannot be dedicated task volumes")
            if any(root in entry.path.parents for entry in table):
                raise WorkspaceError("Nested mounts could escape task filesystem capacity limits")
            device = f"{os.major(root_info.st_dev)}:{os.minor(root_info.st_dev)}"
            if mount.device != device:
                raise WorkspaceError("Task filesystem device changed during quota inspection")
            capacity = os.fstatvfs(root_fd)
            total_bytes = capacity.f_blocks * capacity.f_frsize
            if capacity.f_frsize <= 0 or not 0 < total_bytes <= self.max_bytes:
                raise WorkspaceError("Total filesystem byte capacity is missing or exceeds task policy")
            if not 0 < capacity.f_files <= self.max_inodes:
                raise WorkspaceError("Total filesystem inode capacity is missing or exceeds task policy")
            if capacity.f_flag & os.ST_RDONLY:
                raise WorkspaceError("Task filesystem is read-only")
            for path in paths:
                path = Path(path)
                if not path.is_absolute() or ".." in path.parts:
                    raise WorkspaceError("Writable task paths must be absolute and canonical")
                path = assert_path_contained(path, root)
                if path == root:
                    raise WorkspaceError("Mount root is controller-owned; expose only task subdirectories")
                parent_fd = root_fd
                for part in path.relative_to(root).parts:
                    parent_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                        dir_fd=parent_fd)
                    descriptors.append(parent_fd)
                    if os.fstat(parent_fd).st_dev != root_info.st_dev:
                        raise WorkspaceError("Writable task path is on a different filesystem")
            result = FilesystemQuotaSnapshot(str(root), mount.mount_id, device, mount.filesystem,
                                             total_bytes, capacity.f_files, root_info.st_ino,
                                             mount.filesystem == "ext4")
            if expected is not None and result != expected:
                raise WorkspaceError("Task filesystem identity or capacity changed since preparation")
            # A mount replacement during traversal must not inherit earlier evidence.
            if _mounts() != table or os.stat(root, follow_symlinks=False) != root_info:
                raise WorkspaceError("Task filesystem changed during quota inspection")
            return result
        except OSError as exc:
            raise WorkspaceError(f"Task filesystem quota inspection failed: {exc.strerror}") from exc
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    def readiness(self, writable_paths: Iterable[Path]) -> dict:
        try:
            evidence = self.inspect(writable_paths)
        except (WorkspaceError, ValueError) as exc:
            return {"ready": False, "backend": "dedicated-filesystem-capacity",
                    "persistent": False, "kernel_exhaustion_verified": False,
                    "whole_runner_boundary_ready": False, "blockers": [str(exc)]}
        return {"ready": True, "backend": "dedicated-filesystem-capacity",
                "persistent": evidence.persistent, "limits": asdict(evidence),
                "kernel_exhaustion_verified": False, "whole_runner_boundary_ready": False,
                "blockers": [], "scope": "supplied task-volume paths only; not other writable mounts"}
