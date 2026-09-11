"""Standard-library-only client for an invocation's application-owned file broker.

The staged copy knows only its current artifact root. It never loads application
configuration, credentials, an MCP configuration, or a network transport.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid


DIRECTORY = ".researchops-files"
MAX_REQUEST_BYTES = 16_384
MAX_RESPONSE_BYTES = 16_384


def _directory(root, relative=""):
    """Open every directory without following links, including root parents."""
    path = Path(os.path.abspath(root))
    if relative:
        path /= relative
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read(root, relative, maximum=MAX_RESPONSE_BYTES):
    parts = Path(relative)
    if parts.is_absolute() or ".." in parts.parts:
        raise ValueError("Unsafe file acquisition path")
    directory = _directory(root, str(parts.parent))
    try:
        fd = os.open(parts.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
                raise ValueError("Invalid file acquisition response")
            raw = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
            if (len(raw) > maximum or
                    (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink) !=
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink)):
                raise ValueError("File acquisition response changed")
            return raw
    finally:
        os.close(directory)


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate file acquisition field")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON number")))


def _publish(root, relative, raw):
    """Publish complete JSON within an opened, link-free directory."""
    path = Path(relative)
    directory = _directory(root, str(path.parent))
    temporary = ".pending-" + uuid.uuid4().hex
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def acquire_file(source, *, artifact_root=None, timeout_seconds=None):
    """Request a verified file; return available/path/hash or failed/reason_code.

    A returned path is a read-only-by-contract copy. Create derivatives at new
    paths under artifact_root, never by altering this copy. Only the application
    owns the receipt used to accept final artifacts.
    """
    try:
        root = Path(artifact_root) if artifact_root is not None else Path(__file__).resolve().parents[3]
        if not isinstance(source, dict):
            raise ValueError("source must be an object")
        session = _json(_read(root, DIRECTORY + "/session.json"))
        if not isinstance(session, dict):
            raise ValueError("Invalid acquisition session")
        if session.get("state") != "accepting":
            return {"status": "failed", "reason_code": "session_closed"}
        session_id = session.get("session_id")
        if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise ValueError("Invalid acquisition session")
        maximum = session.get("max_file_bytes")
        if type(maximum) is not int or not 0 < maximum <= 20_000_000:
            raise ValueError("Invalid acquisition file limit")
        limit = session.get("request_timeout_seconds", 125)
        if timeout_seconds is not None:
            limit = min(limit, timeout_seconds)
        if type(limit) not in (int, float) or not math.isfinite(limit) or not 0 < limit <= 605:
            raise ValueError("Invalid acquisition timeout")
        request_id = uuid.uuid4().hex
        raw = json.dumps({"schema_version": 1, "session_id": session_id, "request_id": request_id,
                          "source": source}, ensure_ascii=True, allow_nan=False).encode("ascii")
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("File acquisition request exceeds its limit")
        _publish(root, f"{DIRECTORY}/requests/{request_id}.json", raw)
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            try:
                response = _json(_read(root, f"{DIRECTORY}/responses/{request_id}.json"))
            except FileNotFoundError:
                response = None
            if response is not None:
                if (not isinstance(response, dict) or response.get("request_id") != request_id or
                        response.get("session_id") != session_id or
                        response.get("status") not in {"available", "failed"}):
                    raise ValueError("Invalid acquisition response")
                if response["status"] == "available":
                    acquisition_id = response.get("acquisition_id")
                    extensions = {"application/pdf": ".pdf", "image/png": ".png",
                                  "image/jpeg": ".jpg", "image/gif": ".gif"}
                    mime = response.get("mime_type")
                    digest, size = response.get("sha256"), response.get("size_bytes")
                    if (not isinstance(acquisition_id, str) or not re.fullmatch(r"acq-[a-f0-9]{32}", acquisition_id) or
                            not isinstance(mime, str) or mime not in extensions or
                            not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest) or
                            type(size) is not int or not 0 < size <= session["max_file_bytes"]):
                        raise ValueError("Invalid acquired file metadata")
                    relative = f"{DIRECTORY}/files/{acquisition_id}{extensions[mime]}"
                    if response.get("path") != str(root / relative):
                        raise ValueError("Acquisition response points outside its copy directory")
                    body = _read(root, relative, session["max_file_bytes"])
                    if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
                        raise ValueError("Acquisition copy differs from response")
                return {key: value for key, value in response.items() if key not in {"request_id", "session_id"}}
            current = _json(_read(root, DIRECTORY + "/session.json"))
            if not isinstance(current, dict):
                raise ValueError("Invalid acquisition session")
            if current.get("session_id") != session_id or current.get("state") != "accepting":
                return {"status": "failed", "reason_code": current.get("reason_code") or "session_closed"}
            time.sleep(0.025)
        return {"status": "failed", "reason_code": "request_timeout"}
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        # No submitted URL, protected path, or arbitrary server error is echoed.
        return {"status": "failed", "reason_code": "file_acquisition_unavailable"}
