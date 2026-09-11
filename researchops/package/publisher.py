"""Atomically publish a sealed UTF-8 task package under its canonical hash."""

import fcntl
import os
from pathlib import Path
import shutil
import tempfile

from researchops.errors import ValidationError
from researchops.package.loader import TaskPackageLoader, compute_package_hash
from researchops.workspace.security import read_safe_bytes


def publish_version(root: Path, version) -> Path:
    files={name:content.encode("utf-8") for name,content in version.package_files.items()}
    if compute_package_hash(files)!=version.version_hash:
        raise ValidationError("Version hash does not match canonical package bytes")
    TaskPackageLoader.validate_relative_path(version.task_id)
    parent=root/version.task_id
    parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if parent.is_symlink():
        raise ValidationError("Version parent cannot be a symlink")
    destination=parent/version.version_hash
    lock=os.open(parent/".publish.lock",os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise ValidationError("Invalid existing candidate package")
            for name,content in files.items():
                TaskPackageLoader.validate_relative_path(name)
                if read_safe_bytes(destination/name,destination,8*1024*1024)!=content:
                    raise ValidationError("Published task version was modified")
            actual={p.relative_to(destination).as_posix() for p in destination.rglob("*") if not p.is_dir()}
            if actual!=set(files):
                raise ValidationError("Published task version contains unexpected entries")
            return destination
        staging=Path(tempfile.mkdtemp(prefix=".candidate-",dir=parent))
        try:
            for name,content in files.items():
                TaskPackageLoader.validate_relative_path(name)
                target=staging/name
                target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,"wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            os.rename(staging,destination)
            fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return destination
    finally:
        os.close(lock)
