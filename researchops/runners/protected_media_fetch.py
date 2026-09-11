"""Protected, fixed-origin CardRAG PDF GET transport.

The child reads one dedicated application credential reference and never receives
worker headers, a source URL, CLI authentication, proxies or cookies. It connects
directly to the configured numeric loopback address and refuses redirects.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

if __package__:
    from researchops.runners.research_fetch import (
        FetchResult, ResearchFetchError, _Reader, _communicate, _stop_worker,
    )
else:
    # -I does not add the script directory to sys.path. Load the adjacent trusted
    # stdlib-only parser explicitly; never import from cwd or a task workspace.
    import importlib.util
    _spec = importlib.util.spec_from_file_location("_researchops_public_transport", Path(__file__).with_name("research_fetch.py"))
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _module
    _spec.loader.exec_module(_module)
    FetchResult, ResearchFetchError = _module.FetchResult, _module.ResearchFetchError
    _Reader, _communicate, _stop_worker = _module._Reader, _module._communicate, _module._stop_worker


@dataclass(frozen=True)
class _PDFPolicy:
    max_body_bytes: int
    max_header_bytes: int = 65_536
    max_header_count: int = 64
    max_chunk_count: int = 4096


def _origin(value):
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port or 80
        authority = f"[{address}]" if address.version == 6 else str(address)
        if (parsed.scheme != "http" or not address.is_loopback or
                getattr(address, "ipv4_mapped", None) is not None or
                parsed.path not in ("", "/") or parsed.query or parsed.fragment or
                parsed.netloc not in (authority, authority + f":{port}") or not 1 <= port <= 65535):
            raise ValueError
        return address, port, parsed.netloc
    except (ValueError, TypeError, AttributeError):
        raise ResearchFetchError("provider_configuration_invalid") from None


def _token(path):
    """Open every component without links and read only a private owned file."""
    descriptors = []
    try:
        target = Path(path)
        if not target.is_absolute() or ".." in target.parts:
            raise ValueError
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        for part in target.parts[1:-1]:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            descriptors.append(descriptor)
        descriptor = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        owned_private = before.st_uid == os.getuid() and not before.st_mode & 0o077
        root_readonly = before.st_uid == 0 and not before.st_mode & 0o227
        if (not stat.S_ISREG(before.st_mode) or not (owned_private or root_readonly) or
                before.st_nlink != 1 or before.st_mode & 0o7000 or not 1 <= before.st_size <= 4096):
            raise ValueError
        raw = os.read(descriptor, 4097)
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink):
            raise ValueError
        # Allow a conventional final LF, never whitespace or header injection.
        raw = raw.removesuffix(b"\n").removesuffix(b"\r")
        if not re.fullmatch(rb"[A-Za-z0-9._~+/-]+=*", raw):
            raise ValueError
        return raw
    except (OSError, ValueError, TypeError):
        raise ResearchFetchError("credential_unavailable") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _get(base_url, token_file, document_id, max_bytes, timeout, *, metadata=False):
    address, port, authority = _origin(base_url)
    if (not isinstance(document_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}", document_id)):
        raise ResearchFetchError("invalid_document_id")
    if (type(max_bytes) is not int or not 1 <= max_bytes <= 20_000_000 or
            type(timeout) not in (int, float) or not 0.05 <= timeout <= 30):
        raise ResearchFetchError("provider_configuration_invalid")
    secret = _token(token_file)
    path = "/resources/documents/" + document_id if metadata else "/sources/" + document_id + "/pdf"
    audit = {"method": "GET", "body_bytes": 0, "redirect_count": 0}
    reader = None
    deadline = time.monotonic() + timeout
    connection = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET,
                               socket.SOCK_STREAM, socket.IPPROTO_TCP)
    try:
        connection.settimeout(min(timeout, 5))
        connection.connect((str(address), port, 0, 0) if address.version == 6 else (str(address), port))
        if ipaddress.ip_address(connection.getpeername()[0]) != address:
            raise ResearchFetchError("peer_mismatch")
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        request = (f"GET {path} HTTP/1.1\r\nHost: {authority}\r\n"
                   "User-Agent: ResearchOps-Media/1\r\nAccept: */*\r\n"
                   "Accept-Encoding: identity\r\nConnection: close\r\nAuthorization: Bearer ").encode("ascii")
        connection.sendall(request + secret + b"\r\n\r\n")
        del secret
        with connection.makefile("rb") as stream:
            reader = _Reader(stream, _PDFPolicy(max_bytes))
            match = re.fullmatch(rb"HTTP/1\.[01] ([2-5][0-9]{2})(?: [\x20-\x7e]*)?", reader.line())
            if not match:
                raise ResearchFetchError("invalid_response")
            status = int(match[1])
            audit["status"] = status
            headers = reader.headers()
            if 300 <= status <= 399:
                raise ResearchFetchError("redirect_denied")
            if headers.get("content-encoding", ["identity"]) != ["identity"]:
                raise ResearchFetchError("unsupported_content_encoding")
            body = reader.body(headers) if status == 200 else b""
            if time.monotonic() >= deadline:
                raise ResearchFetchError("timeout")
            audit.update(body_bytes=len(body), body_sha256=hashlib.sha256(body).hexdigest())
            # No arbitrary response header/server text escapes the transport.
            selected = {"content-type": headers["content-type"][0]} if len(headers.get("content-type", [])) == 1 else {}
            return FetchResult(status, base_url.rstrip("/") + path, selected, body, audit)
    except ResearchFetchError as exc:
        if reader is not None:
            audit["body_bytes"] = reader.body_bytes
        raise ResearchFetchError(exc.code, audit) from None
    except (TimeoutError, socket.timeout):
        raise ResearchFetchError("timeout", audit) from None
    except OSError:
        raise ResearchFetchError("transport_failed", audit) from None
    finally:
        connection.close()


def _direct(base_url, token_file, descriptor, max_bytes, timeout):
    if (not isinstance(descriptor, dict) or
            any(not isinstance(descriptor.get(key), str) for key in ("document_id", "issuer", "product_code", "sha256")) or
            not re.fullmatch(r"doc_[a-f0-9]{64}", descriptor["document_id"]) or
            not re.fullmatch(r"[a-f0-9]{64}", descriptor["sha256"]) or
            type(descriptor.get("size_bytes")) is not int or descriptor["size_bytes"] < 1):
        raise ResearchFetchError("provider_configuration_invalid")
    deadline = time.monotonic() + timeout
    metadata = _get(base_url, token_file, descriptor["document_id"], 262_144, timeout, metadata=True)
    if metadata.status != 200:
        return metadata
    if metadata.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ResearchFetchError("metadata_response_invalid")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    try:
        document = json.loads(metadata.body, object_pairs_hook=unique,
                              parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        if not isinstance(document, dict):
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ResearchFetchError("metadata_response_invalid") from None
    expected = {"document_id": descriptor["document_id"], "issuer": descriptor["issuer"],
                "product_code": descriptor["product_code"], "pdf_sha256": descriptor["sha256"],
                "pdf_size_bytes": descriptor["size_bytes"]}
    if any(type(document.get(key)) is not type(value) or document[key] != value for key, value in expected.items()):
        raise ResearchFetchError("source_metadata_mismatch")
    remaining = deadline - time.monotonic()
    if remaining < 0.05:
        raise ResearchFetchError("timeout")
    result = _get(base_url, token_file, descriptor["document_id"], max_bytes, remaining)
    result.audit.update(metadata_verified=True, request_count=2)
    return result


class CardRAGPDFFetcher:
    def fetch(self, provider, descriptor, *, max_bytes=20_000_000, timeout_seconds=20, cancellation_check=None):
        _origin(provider.base_url)
        if (not isinstance(descriptor, dict) or not isinstance(descriptor.get("document_id"), str) or
                not re.fullmatch(r"doc_[a-f0-9]{64}", descriptor["document_id"])):
            raise ResearchFetchError("invalid_document_id")
        request = json.dumps({"base_url": provider.base_url, "token_file": str(provider.bearer_token_file),
                              "descriptor": descriptor, "max_bytes": max_bytes,
                              "timeout": timeout_seconds}, ensure_ascii=True).encode("ascii")
        if len(request) > 16_384:
            raise ResearchFetchError("provider_configuration_invalid")
        try:
            process = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "--worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd="/", env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "Asia/Seoul"},
                close_fds=True, start_new_session=True)
        except OSError:
            raise ResearchFetchError("worker_start_failed") from None
        try:
            output, _ = _communicate(process, request, timeout_seconds, cancellation_check)
        except subprocess.TimeoutExpired:
            _stop_worker(process)
            raise ResearchFetchError("timeout", {"worker_terminated": True}) from None
        except BaseException:
            _stop_worker(process)
            raise
        try:
            if process.returncode != 0 or len(output) > max_bytes * 2 + 262_144:
                raise ValueError
            result = json.loads(output)
            if "error" in result:
                raise ResearchFetchError(result["error"], result.get("audit"))
            body = base64.b64decode(result["body_base64"], validate=True)
            if len(body) > max_bytes:
                raise ValueError
            return FetchResult(result["status"], result["final_url"], result["headers"], body, result["audit"])
        except (ValueError, TypeError, KeyError):
            raise ResearchFetchError("worker_failed") from None


def _main():
    try:
        raw = sys.stdin.buffer.read(16_385)
        if len(raw) > 16_384:
            raise ResearchFetchError("provider_configuration_invalid")
        result = _direct(**json.loads(raw))
        response = result.to_dict(include_body=True)
    except ResearchFetchError as exc:
        response = {"error": exc.code, "audit": exc.audit}
    except Exception:
        response = {"error": "worker_failed", "audit": {"error_code": "worker_failed"}}
    sys.stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit(2)
    _main()
