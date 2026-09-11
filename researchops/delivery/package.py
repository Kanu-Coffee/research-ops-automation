"""File-safe immutable delivery package operations."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile

from jsonschema import Draft202012Validator, FormatChecker

from researchops.errors import DeliveryError

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_PACKAGE_BYTES = 50 * 1024 * 1024


def read_file(root: Path, relative: str, limit: int = MAX_FILE_BYTES) -> bytes:
    """Walk using directory descriptors; never follow a link, including races."""
    path = PurePosixPath(relative)
    if (not relative or path.is_absolute() or "\\" in relative or
            any(p in ("", ".", "..") for p in relative.split("/"))):
        raise DeliveryError("Unsafe delivery package path")
    descriptors = []
    try:
        current = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(current)
        for part in path.parts[:-1]:
            current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            descriptors.append(current)
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        descriptors.append(file_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise DeliveryError("Delivery file must be a single-link regular file within the size limit")
        with os.fdopen(os.dup(file_fd), "rb") as stream:
            content = stream.read(limit + 1)
        after = os.fstat(file_fd)
        if len(content) > limit or (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise DeliveryError("Delivery file changed during validation or exceeds its size limit")
        return content
    except OSError as exc:
        raise DeliveryError("Cannot safely read delivery package file") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def validated_files(root: Path, request: dict, schema_dir: Path) -> dict[str, bytes]:
    schema = json.loads((schema_dir / "delivery-request.schema.json").read_text())
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(request))
    if errors:
        raise DeliveryError("Invalid delivery request: " + "; ".join(e.json_path for e in errors))
    files = {}
    total = 0
    cids = set()
    for info in [request["body"]["html"], request["body"]["text"], *request["attachments"]]:
        path = info["path"]
        if path in files or path == "delivery-request.json":
            raise DeliveryError("Duplicate or reserved delivery path")
        content = read_file(root, path)
        if hashlib.sha256(content).hexdigest() != info["sha256"] or len(content) != info["size_bytes"]:
            raise DeliveryError("Delivery file hash or size mismatch")
        if info.get("content_id"):
            cid = info["content_id"]
            if cid in cids or any(c in cid for c in "\r\n<>"):
                raise DeliveryError("Invalid or duplicate Content-ID")
            cids.add(cid)
        if info["media_type"] in ("text/html", "text/plain"):
            try:
                content.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise DeliveryError("Delivery text must be valid UTF-8") from exc
        total += len(content)
        if total > MAX_PACKAGE_BYTES:
            raise DeliveryError("Delivery package exceeds total size limit")
        files[path] = content
    return files


def validate_outbox(settings, handoff):
    if not handoff.handoff_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in handoff.handoff_id):
        raise DeliveryError("Invalid handoff ID")
    root = settings.paths.delivery_outbox_dir / handoff.handoff_id
    raw = read_file(root, "delivery-request.json", 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != handoff.delivery_request_sha256:
        raise DeliveryError("Delivery request hash mismatch")
    request = json.loads(raw)
    if request != handoff.delivery_request:
        raise DeliveryError("Delivery request does not match durable handoff")
    for name in ("handoff_id", "idempotency_key", "task_id", "run_id", "task_version_hash",
                 "message_revision", "message_type", "recipient_group_id"):
        if request[name] != getattr(handoff, name):
            raise DeliveryError("Delivery request identity mismatch")
    return request, validated_files(root, request, settings.paths.schemas_dir)


def publish_package(root: Path, handoff_id: str, request_bytes: bytes, files: dict[str, bytes], *, request_name="delivery-request.json") -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    destination = root / handoff_id
    if destination.exists():
        if read_file(destination, request_name) != request_bytes:
            raise DeliveryError("Immutable delivery package already exists with different contents")
        for path, content in files.items():
            if read_file(destination, path) != content:
                raise DeliveryError("Immutable delivery package contents changed")
        return
    staging = Path(tempfile.mkdtemp(prefix=".publish-", dir=root))
    try:
        for relative, content in {**files, request_name: request_bytes}.items():
            output = staging / relative
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with output.open("xb") as stream:
                os.chmod(output, 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        for directory in sorted((p for p in staging.rglob("*") if p.is_dir()), reverse=True):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(staging, destination)
        parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
