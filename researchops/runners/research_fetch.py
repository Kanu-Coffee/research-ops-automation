"""Bounded public HTTP(S) fetch, for a future protected research broker.

This is not a forward proxy, a browser, or an operating-runner egress boundary.
Only GET/HEAD with fixed headers are supported. Each DNS answer and redirect is
checked before connecting to a pinned numeric address; HTTPS still verifies the
original hostname. No environment proxy, cookie jar, caller headers, request
body, credentials, automatic decompression, or executable content is used.

The public API runs its trusted, standard-library-only transport in a short-lived
process. Its deadline therefore also bounds libc DNS and slow/dripping responses.
``result.text()`` is untrusted source text, NOT safe HTML or instructions.
``result.audit`` / ``error.audit`` are log-safe; URLs in the result can contain
caller-supplied query parameters and must not be blindly copied to normal logs.
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urljoin, urlsplit, urlunsplit


_REDIRECTS = frozenset((301, 302, 303, 307, 308))
_HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_V4_DENY = tuple(ipaddress.ip_network(value) for value in (
    "0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4"))
_V6_DENY = tuple(ipaddress.ip_network(value) for value in (
    "64:ff9b::/96", "64:ff9b:1::/48", "2001::/32", "2002::/16"))
_WORKER_INPUT_LIMIT = 32_768


@dataclass(frozen=True)
class FetchPolicy:
    timeout_seconds: float = 20.0
    connect_timeout_seconds: float = 5.0
    max_body_bytes: int = 1_048_576
    max_redirects: int = 3
    max_url_bytes: int = 8192
    max_request_bytes: int = 16_384
    max_header_bytes: int = 65_536
    max_header_count: int = 64
    max_chunk_count: int = 4096
    allowed_hosts: tuple[str, ...] = ()
    require_https: bool = False

    def __post_init__(self):
        if (type(self.require_https) is not bool or
                not isinstance(self.allowed_hosts, (tuple, list)) or len(self.allowed_hosts) > 128 or
                any(not isinstance(host, str) or len(host) > 253 or "." not in host or
                    host != host.lower() or any(not _LABEL.fullmatch(part) for part in host.split("."))
                    for host in self.allowed_hosts)):
            raise ValueError("Invalid protected destination policy")
        for name, maximum in (("timeout_seconds", 30), ("connect_timeout_seconds", 10)):
            value = getattr(self, name)
            if (type(value) not in (int, float) or not math.isfinite(value) or
                    not 0.05 <= value <= maximum):
                raise ValueError(f"{name} is outside the supported range")
        limits = {"max_body_bytes": (1, 4_194_304), "max_redirects": (0, 5),
                  "max_url_bytes": (64, 8192), "max_request_bytes": (256, 16_384),
                  "max_header_bytes": (256, 65_536), "max_header_count": (1, 64),
                  "max_chunk_count": (1, 4096)}
        for name, (minimum, maximum) in limits.items():
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} is outside the supported range")


class ResearchFetchError(Exception):
    """Stable error code; never includes server messages or submitted URLs."""

    def __init__(self, code: str, audit: dict | None = None):
        self.code = code
        self.audit = dict(audit or {}, error_code=code)
        super().__init__(f"Public research fetch failed: {code}")


@dataclass(frozen=True)
class FetchResult:
    status: int
    final_url: str
    headers: dict[str, str]
    body: bytes
    audit: dict

    def text(self, encoding: str = "utf-8") -> str:
        """Decode source bytes with replacement; does not sanitize or execute."""
        return self.body.decode(encoding, errors="replace")

    def to_dict(self, *, include_body: bool = False) -> dict:
        """JSON-compatible result; only ``audit`` is intended for normal logs."""
        result = {"status": self.status, "final_url": self.final_url,
                  "headers": dict(self.headers), "audit": self.audit}
        if include_body:
            result["body_base64"] = base64.b64encode(self.body).decode("ascii")
        return result


@dataclass(frozen=True)
class _Target:
    url: str
    scheme: str
    hostname: str
    port: int
    authority: str
    path: str

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.authority}"


def _public_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        if "%" in value:
            raise ValueError
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ResearchFetchError("destination_denied") from None
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise ResearchFetchError("destination_denied")
    if address.version == 4:
        denied = any(address in network for network in _V4_DENY)
    else:
        # Refuse transition/translation mechanisms and mapped IPv4 even if a
        # Python release classifies their wrapper IPv6 address as global.
        denied = (address.ipv4_mapped is not None or
                  address not in ipaddress.ip_network("2000::/3") or
                  any(address in network for network in _V6_DENY))
    if denied:
        raise ResearchFetchError("destination_denied")
    return address


def _target(url: str, policy: FetchPolicy) -> _Target:
    if not isinstance(url, str):
        raise ResearchFetchError("invalid_url")
    try:
        if (len(url) > policy.max_url_bytes or len(url.encode("utf-8")) > policy.max_url_bytes or
                any(ord(char) <= 32 or ord(char) == 127 for char in url) or
                "\\" in url):
            raise ValueError
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc or
                "@" in parsed.netloc or "%" in parsed.netloc or parsed.fragment):
            raise ValueError
        original_hostname = parsed.hostname
        if not original_hostname or original_hostname.endswith(".."):
            raise ValueError
        hostname = original_hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if policy.require_https and parsed.scheme != "https":
            raise ResearchFetchError("https_required")
        if policy.allowed_hosts and hostname not in policy.allowed_hosts:
            raise ResearchFetchError("host_denied")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        expected = 443 if parsed.scheme == "https" else 80
        if port != expected:
            raise ResearchFetchError("port_denied")
        # An empty port and noncanonical spellings are not accepted.
        authority = f"[{hostname}]" if ":" in hostname else hostname
        original_authority = (f"[{original_hostname}]" if ":" in original_hostname
                              else original_hostname)
        if parsed.netloc.lower() not in (original_authority, original_authority + f":{expected}"):
            raise ValueError
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            if (len(hostname) > 253 or "." not in hostname or
                    any(not _LABEL.fullmatch(label) for label in hostname.split("."))):
                raise ValueError
            if hostname.endswith((".localhost", ".local", ".internal", ".home", ".lan")):
                raise ResearchFetchError("destination_denied")
        else:
            _public_address(hostname)
        path = parsed.path or "/"
        if not path.startswith("/"):
            raise ValueError
        path += ("?" + parsed.query) if parsed.query else ""
        path.encode("ascii")  # Require explicit percent-encoding for path/query.
        canonical = urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))
        return _Target(canonical, parsed.scheme, hostname, port, authority, path)
    except ResearchFetchError:
        raise
    except (ValueError, UnicodeError):
        raise ResearchFetchError("invalid_url") from None


def _resolve(target: _Target) -> tuple[str, ...]:
    try:
        literal = ipaddress.ip_address(target.hostname)
    except ValueError:
        try:
            # Absolute DNS name avoids environment-dependent search suffixes.
            rows = socket.getaddrinfo(target.hostname + ".", target.port,
                                      type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        except OSError:
            raise ResearchFetchError("dns_failed") from None
        if not rows or len(rows) > 32:
            raise ResearchFetchError("dns_failed")
        addresses = []
        for family, kind, protocol, _, endpoint in rows:
            if (family not in (socket.AF_INET, socket.AF_INET6) or
                    kind != socket.SOCK_STREAM or protocol != socket.IPPROTO_TCP):
                raise ResearchFetchError("dns_failed")
            address = _public_address(endpoint[0])
            if ((family == socket.AF_INET) != (address.version == 4) or
                    endpoint[1] != target.port or
                    (family == socket.AF_INET6 and (endpoint[2] or endpoint[3]))):
                raise ResearchFetchError("dns_failed")
            if str(address) not in addresses:
                addresses.append(str(address))
        # Every answer was checked, not merely the address selected for connect.
        return tuple(addresses)
    return (str(_public_address(str(literal))),)


def _remaining(deadline: float, maximum: float | None = None) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ResearchFetchError("timeout")
    return min(remaining, maximum) if maximum is not None else remaining


def _connect(target: _Target, addresses: tuple[str, ...], policy: FetchPolicy,
             deadline: float):
    for address in addresses:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        connection = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        try:
            connection.settimeout(_remaining(deadline, policy.connect_timeout_seconds))
            endpoint = (address, target.port, 0, 0) if family == socket.AF_INET6 else (address, target.port)
            # Do not use create_connection(hostname): it would resolve again.
            connection.connect(endpoint)
            if _public_address(connection.getpeername()[0]) != ipaddress.ip_address(address):
                raise ResearchFetchError("peer_mismatch")
            if target.scheme == "https":
                context = ssl.create_default_context()
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                connection = context.wrap_socket(connection, server_hostname=target.hostname)
            connection.settimeout(_remaining(deadline))
            return connection, address
        except ssl.SSLCertVerificationError:
            connection.close()
            raise ResearchFetchError("tls_verification_failed") from None
        except ResearchFetchError:
            connection.close()
            raise
        except (OSError, ssl.SSLError):
            connection.close()
    raise ResearchFetchError("connection_failed")


class _Reader:
    def __init__(self, stream, policy: FetchPolicy):
        self.stream = stream
        self.policy = policy
        self.framing_bytes = 0
        self.body_bytes = 0

    def line(self) -> bytes:
        line = self.stream.readline(min(8192, self.policy.max_header_bytes) + 1)
        self.framing_bytes += len(line)
        if len(line) > 8192 or self.framing_bytes > self.policy.max_header_bytes:
            raise ResearchFetchError("headers_too_large")
        if not line.endswith(b"\r\n"):
            raise ResearchFetchError("invalid_response")
        return line[:-2]

    def exact(self, size: int) -> bytes:
        data = self.stream.read(size)
        if len(data) != size:
            raise ResearchFetchError("incomplete_response")
        return data

    def body_exact(self, size: int) -> bytes:
        data = self.stream.read(size)
        self.body_bytes += len(data)
        if len(data) != size:
            raise ResearchFetchError("incomplete_response")
        return data

    def headers(self) -> dict[str, list[str]]:
        headers: dict[str, list[str]] = {}
        for _ in range(self.policy.max_header_count + 1):
            line = self.line()
            if not line:
                return headers
            name, separator, value = line.partition(b":")
            if (not separator or not _HEADER_NAME.fullmatch(name) or
                    any(char < 32 and char != 9 or char == 127 for char in value)):
                raise ResearchFetchError("invalid_response")
            headers.setdefault(name.decode("ascii").lower(), []).append(value.decode("latin-1").strip())
        raise ResearchFetchError("too_many_headers")

    def body(self, headers: dict[str, list[str]]) -> bytes:
        transfer, length = headers.get("transfer-encoding"), headers.get("content-length")
        if transfer and length or (length and (len(length) != 1 or not re.fullmatch(r"[0-9]{1,12}", length[0]))):
            raise ResearchFetchError("invalid_framing")
        if transfer:
            if transfer != ["chunked"]:
                raise ResearchFetchError("unsupported_transfer_encoding")
            chunks, total = [], 0
            for _ in range(self.policy.max_chunk_count):
                line = self.line()
                # Extensions are deliberately unsupported, avoiding divergent parsers.
                if not re.fullmatch(rb"[0-9a-fA-F]{1,12}", line):
                    raise ResearchFetchError("invalid_framing")
                size = int(line, 16)
                if size == 0:
                    trailers = self.headers()
                    if set(trailers) & {"content-length", "transfer-encoding", "content-encoding", "location"}:
                        raise ResearchFetchError("invalid_framing")
                    return b"".join(chunks)
                total += size
                if total > self.policy.max_body_bytes:
                    raise ResearchFetchError("body_too_large")
                chunks.append(self.body_exact(size))
                if self.exact(2) != b"\r\n":
                    raise ResearchFetchError("invalid_framing")
            raise ResearchFetchError("too_many_chunks")
        if length:
            size = int(length[0])
            if size > self.policy.max_body_bytes:
                raise ResearchFetchError("body_too_large")
            return self.body_exact(size)
        body = self.stream.read(self.policy.max_body_bytes + 1)
        self.body_bytes += len(body)
        if len(body) > self.policy.max_body_bytes:
            raise ResearchFetchError("body_too_large")
        return body


def _fetch_direct(url: str, method: str, policy: FetchPolicy) -> FetchResult:
    """Trusted worker implementation; callers use PublicResearchFetcher.fetch."""
    audit = {"method": method if method in ("GET", "HEAD") else "denied",
             "hops": [], "body_bytes": 0}
    reader = None
    try:
        if method not in ("GET", "HEAD"):
            raise ResearchFetchError("method_denied")
        target = _target(url, policy)
        deadline = time.monotonic() + policy.timeout_seconds
        seen = set()
        for hop in range(policy.max_redirects + 1):
            if target.url in seen:
                raise ResearchFetchError("redirect_loop")
            seen.add(target.url)
            entry = {"origin": target.origin,
                     "url_sha256": hashlib.sha256(target.url.encode()).hexdigest(),
                     "resolved_addresses": []}
            audit["hops"].append(entry)
            request = (f"{method} {target.path} HTTP/1.1\r\nHost: {target.authority}\r\n"
                       "User-Agent: ResearchOps-PublicFetch/1\r\nAccept: */*\r\n"
                       "Accept-Encoding: identity\r\nConnection: close\r\n\r\n").encode("ascii")
            if len(request) > policy.max_request_bytes:
                raise ResearchFetchError("request_too_large")
            addresses = _resolve(target)
            entry["resolved_addresses"] = list(addresses)
            connection, address = _connect(target, addresses, policy, deadline)
            entry["connected_address"] = address
            with connection:
                connection.sendall(request)
                with connection.makefile("rb") as stream:
                    reader = _Reader(stream, policy)
                    status_line = reader.line()
                    match = re.fullmatch(rb"HTTP/1\.[01] ([2-5][0-9]{2})(?: [\x20-\x7e]*)?", status_line)
                    if not match:
                        raise ResearchFetchError("invalid_response")
                    status = int(match[1])
                    entry["status"] = status
                    headers = reader.headers()
                    if status in _REDIRECTS:
                        locations = headers.get("location", [])
                        if len(locations) != 1:
                            raise ResearchFetchError("invalid_redirect")
                        if any(ord(char) <= 32 or ord(char) == 127 for char in locations[0]):
                            raise ResearchFetchError("invalid_redirect")
                        if hop == policy.max_redirects:
                            raise ResearchFetchError("too_many_redirects")
                        next_target = _target(urljoin(target.url, locations[0]), policy)
                        if target.scheme == "https" and next_target.scheme != "https":
                            raise ResearchFetchError("https_downgrade_denied")
                        target = next_target
                        continue
                    if headers.get("content-encoding", ["identity"]) != ["identity"]:
                        raise ResearchFetchError("unsupported_content_encoding")
                    body = b"" if method == "HEAD" or status in (204, 304) else reader.body(headers)
                    _remaining(deadline)
                    audit.update(body_bytes=len(body), body_sha256=hashlib.sha256(body).hexdigest(),
                                 redirect_count=hop)
                    selected = {name: values[0] for name, values in headers.items()
                                if name in ("content-type", "last-modified", "etag") and len(values) == 1}
                    return FetchResult(status, target.url, selected, body, audit)
        raise ResearchFetchError("too_many_redirects")
    except ResearchFetchError as exc:
        if reader is not None:
            audit["body_bytes"] = reader.body_bytes
        raise ResearchFetchError(exc.code, audit) from None
    except (TimeoutError, socket.timeout):
        raise ResearchFetchError("timeout", audit) from None
    except (OSError, ssl.SSLError):
        raise ResearchFetchError("transport_failed", audit) from None


class PublicResearchFetcher:
    """Synchronous, per-request isolated transport; no shared cookies/sessions.

    Supply policy from protected application configuration, never from task tool
    arguments. A future service must also bound request count/concurrency and
    enforce that the worker has no alternate network path. Neither is implied by
    this standalone backend.
    """

    def __init__(self, policy: FetchPolicy | None = None):
        self.policy = FetchPolicy() if policy is None else policy
        if not isinstance(self.policy, FetchPolicy):
            raise ValueError("policy must be FetchPolicy")

    def fetch(self, url: str, *, method: str = "GET", cancellation_check=None) -> FetchResult:
        if method not in ("GET", "HEAD"):
            raise ResearchFetchError("method_denied")
        _target(url, self.policy)  # Reject invalid requests before any subprocess.
        request = json.dumps({"url": url, "method": method, "policy": asdict(self.policy)},
                             ensure_ascii=True).encode("ascii")
        if len(request) > _WORKER_INPUT_LIMIT:
            raise ResearchFetchError("request_too_large")
        try:
            process = subprocess.Popen(
                [sys.executable, "-I", str(Path(__file__).resolve()), "--worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd="/", env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "Asia/Seoul"},
                close_fds=True, start_new_session=True)
        except OSError:
            raise ResearchFetchError("worker_start_failed") from None
        try:
            output, _ = _communicate(process, request, self.policy.timeout_seconds, cancellation_check)
        except subprocess.TimeoutExpired:
            _stop_worker(process)
            raise ResearchFetchError("timeout", {"worker_terminated": True}) from None
        except BaseException:
            # Cancellation and broken pipes must not leave a network worker alive.
            _stop_worker(process)
            raise
        if process.returncode != 0 or len(output) > self.policy.max_body_bytes * 2 + 262_144:
            raise ResearchFetchError("worker_failed")
        try:
            response = json.loads(output)
            if "error" in response:
                raise ResearchFetchError(response["error"], response.get("audit"))
            body = base64.b64decode(response["body_base64"], validate=True)
            if len(body) > self.policy.max_body_bytes:
                raise ValueError
            return FetchResult(response["status"], response["final_url"],
                               response["headers"], body, response["audit"])
        except (ValueError, KeyError, TypeError):
            raise ResearchFetchError("worker_failed") from None


def _communicate(process, request: bytes, timeout: float, cancellation_check=None):
    """Bound DNS and slow bodies while allowing the owner to fence/cancel work."""
    if cancellation_check is None:
        return process.communicate(request, timeout=timeout)
    deadline = time.monotonic() + timeout
    initial = request
    while True:
        if cancellation_check():
            raise ResearchFetchError("cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            return process.communicate(initial, timeout=min(0.2, remaining))
        except subprocess.TimeoutExpired:
            initial = None  # communicate already sent (or buffered) the request.


def _stop_worker(process) -> None:
    try:
        process.kill()  # Trusted worker spawns no descendants.
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        raise ResearchFetchError("worker_cleanup_failed", {"worker_terminated": False}) from None


def _worker_main() -> int:
    try:
        raw = sys.stdin.buffer.read(_WORKER_INPUT_LIMIT + 1)
        if len(raw) > _WORKER_INPUT_LIMIT:
            raise ResearchFetchError("request_too_large")
        request = json.loads(raw)
        result = _fetch_direct(request["url"], request["method"], FetchPolicy(**request["policy"]))
        response = result.to_dict(include_body=True)
    except ResearchFetchError as exc:
        response = {"error": exc.code, "audit": exc.audit}
    except Exception:
        # No server text, URL, filesystem path, environment or traceback on stdout.
        response = {"error": "worker_failed", "audit": {"error_code": "worker_failed"}}
    sys.stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit(2)
    raise SystemExit(_worker_main())
