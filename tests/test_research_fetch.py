"""Hermetic public-fetch transport/policy tests; no ordinary test uses DNS."""

import base64
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

from researchops.runners import research_fetch as fetch


PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"


def response(body=b"hello", *, status=200, headers=b"", declared=True):
    return (f"HTTP/1.1 {status} OK\r\n".encode() + headers +
            (f"Content-Length: {len(body)}\r\n".encode() if declared else b"") + b"\r\n" + body)


class FakeSocket:
    def __init__(self, wire, peer=PUBLIC_V4):
        self.wire = wire
        self.peer = peer
        self.request = b""
        self.closed = False
        self.connected = None
        self.timeouts = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.closed = True

    def connect(self, address):
        self.connected = address

    def getpeername(self):
        return self.peer, 443

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def sendall(self, request):
        self.request = request

    def makefile(self, mode):
        assert mode == "rb"
        return io.BytesIO(self.wire)


def dns_row(address, port=443):
    if ":" in address:
        return socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port, 0, 0)
    return socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port)


class TestPublicFetchPolicy(unittest.TestCase):
    def test_official_host_and_https_policy_applies_before_spawn(self):
        policy = fetch.FetchPolicy(allowed_hosts=("official.example.org",), require_https=True)
        for url, reason in (("https://other.example.org/a.png", "host_denied"),
                            ("http://official.example.org/a.png", "https_required")):
            with self.subTest(url=url), patch.object(fetch.subprocess, "Popen") as spawn:
                with self.assertRaisesRegex(fetch.ResearchFetchError, reason):
                    fetch.PublicResearchFetcher(policy).fetch(url)
                spawn.assert_not_called()

    def test_official_policy_rejects_wildcards_and_malformed_types(self):
        for values in ({"allowed_hosts": ["*.example.org"]}, {"allowed_hosts": "example.org"},
                       {"require_https": "true"}, {"allowed_hosts": ["Example.org"]}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                fetch.FetchPolicy(**values)

    def test_redirect_destination_must_stay_in_official_catalog(self):
        wire = response(status=302, headers=b"Location: https://other.example.org/image.png\r\n")
        fake = FakeSocket(wire)
        policy = fetch.FetchPolicy(allowed_hosts=("official.example.org",), require_https=True)
        with patch.object(fetch, "_resolve", return_value=(PUBLIC_V4,)) as resolve, patch.object(
                fetch, "_connect", return_value=(fake, PUBLIC_V4)) as connect:
            with self.assertRaisesRegex(fetch.ResearchFetchError, "host_denied"):
                fetch._fetch_direct("https://official.example.org/start", "GET", policy)
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(connect.call_count, 1)

    def test_incomplete_response_audit_counts_received_body_bytes(self):
        wire = b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\npartial"
        with patch.object(fetch, "_resolve", return_value=(PUBLIC_V4,)), patch.object(
                fetch, "_connect", return_value=(FakeSocket(wire), PUBLIC_V4)):
            with self.assertRaisesRegex(fetch.ResearchFetchError, "incomplete_response") as captured:
                fetch._fetch_direct("https://example.org/file", "GET", fetch.FetchPolicy())
        self.assertEqual(captured.exception.audit["body_bytes"], 7)

    def test_policy_bounds_and_types(self):
        invalid = ({"timeout_seconds": float("inf")}, {"timeout_seconds": float("nan")},
                   {"timeout_seconds": True}, {"timeout_seconds": 31}, {"timeout_seconds": 0},
                   {"max_body_bytes": True}, {"max_body_bytes": 4_194_305},
                   {"max_redirects": -1}, {"max_redirects": 6},
                   {"max_header_bytes": 65_537}, {"max_request_bytes": 128})
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                fetch.FetchPolicy(**values)

    def test_disallowed_methods_rejected_before_spawn(self):
        for method in ("POST", "PUT", "DELETE", "OPTIONS", "CONNECT", "get", "GET\r\nX: x", None):
            with self.subTest(method=method), patch.object(fetch.subprocess, "Popen") as spawn:
                with self.assertRaisesRegex(fetch.ResearchFetchError, "method_denied"):
                    fetch.PublicResearchFetcher().fetch("https://example.org", method=method)
                spawn.assert_not_called()

    def test_malicious_urls_rejected_before_spawn(self):
        urls = ("file:///etc/passwd", "ftp://example.org/file", "gopher://example.org",
                "https://user:password@example.org", "https://user@example.org", "//example.org",
                "https://example.org:8443", "http://example.org:443", "https://example.org:80",
                "https://example.org:0443", "https://example.org:", "https://example.org:0",
                "https://example.org/%x\r\nAuthorization:secret", " https://example.org/",
                "https://example.org/\tpath", "https://example.org\\@127.0.0.1/",
                "https://example%2eorg", "https://example.org/#fragment",
                "https://127.0.0.1", "http://169.254.169.254", "http://[::1]",
                "http://[fe80::1%25eth0]", "http://[::ffff:127.0.0.1]", "http://localhost",
                "http://metadata.google.internal", "http://a.local", "http://a..org",
                "https://-a.example.org", "https://example.org../", "https://example.org/한글")
        for url in urls:
            with self.subTest(url=url), patch.object(fetch.subprocess, "Popen") as spawn:
                with self.assertRaises(fetch.ResearchFetchError):
                    fetch.PublicResearchFetcher().fetch(url)
                spawn.assert_not_called()

    def test_normalization_and_percent_encoded_path(self):
        for url, expected in (
                ("HTTPS://Example.ORG.:443/a?q=%ED%95%9C", "https://example.org/a?q=%ED%95%9C"),
                ("http://example.org:80", "http://example.org/"),
                ("https://bücher.example/", "https://xn--bcher-kva.example/"),
                ("https://[2606:4700:4700::1111]:443/", "https://[2606:4700:4700::1111]/")):
            with self.subTest(url=url):
                self.assertEqual(fetch._target(url, fetch.FetchPolicy()).url, expected)

    def test_nonpublic_and_transition_addresses(self):
        addresses = ("0.1.2.3", "10.0.0.1", "100.64.1.1", "127.2.3.4", "169.254.1.1",
                     "172.16.0.1", "192.168.1.1", "192.0.0.9", "192.0.2.1", "192.88.99.1",
                     "198.18.1.1", "198.51.100.1", "203.0.113.1", "224.0.0.1", "255.255.255.255",
                     "::", "::1", "fc00::1", "fe80::1", "ff0e::1", "2001:db8::1",
                     "64:ff9b::0808:0808", "64:ff9b:1::1", "2002:0808:0808::1",
                     "2001::1", "::ffff:8.8.8.8", "2606:4700::1%eth0")
        for address in addresses:
            with self.subTest(address=address), self.assertRaisesRegex(fetch.ResearchFetchError, "destination_denied"):
                fetch._public_address(address)
        for address in (PUBLIC_V4, PUBLIC_V6, "8.8.8.8"):
            self.assertEqual(str(fetch._public_address(address)), address)

    def test_dns_checks_all_answers_and_absolute_name(self):
        target = fetch._target("https://example.org", fetch.FetchPolicy())
        with patch.object(fetch.socket, "getaddrinfo", return_value=[dns_row(PUBLIC_V4), dns_row(PUBLIC_V6)]) as resolve:
            self.assertEqual(fetch._resolve(target), (PUBLIC_V4, PUBLIC_V6))
            self.assertEqual(resolve.call_args.args, ("example.org.", 443))
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::ffff:127.0.0.1"):
            with self.subTest(address=address), patch.object(fetch.socket, "getaddrinfo", return_value=[dns_row(PUBLIC_V4), dns_row(address)]):
                with self.assertRaisesRegex(fetch.ResearchFetchError, "destination_denied"):
                    fetch._resolve(target)

    def test_legacy_numeric_hostname_dns_cannot_bypass_filter(self):
        for hostname in ("0177.0.0.1", "0x7f.0.0.1"):
            with self.subTest(hostname=hostname), patch.object(fetch.socket, "getaddrinfo", return_value=[dns_row("127.0.0.1")]):
                with self.assertRaisesRegex(fetch.ResearchFetchError, "destination_denied"):
                    fetch._resolve(fetch._target("https://" + hostname, fetch.FetchPolicy()))

    def test_dns_failure_empty_excess_and_bad_family(self):
        target = fetch._target("https://example.org", fetch.FetchPolicy())
        for rows in ([], [dns_row(PUBLIC_V4)] * 33,
                     [(socket.AF_UNIX, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_V4, 443))],
                     [dns_row(PUBLIC_V4, port=80)]):
            with self.subTest(rows=rows), patch.object(fetch.socket, "getaddrinfo", return_value=rows):
                with self.assertRaisesRegex(fetch.ResearchFetchError, "dns_failed"):
                    fetch._resolve(target)
        with patch.object(fetch.socket, "getaddrinfo", side_effect=OSError("secret data")):
            with self.assertRaisesRegex(fetch.ResearchFetchError, "dns_failed") as caught:
                fetch._resolve(target)
            self.assertNotIn("secret", str(caught.exception))

    def test_public_ip_literal_skips_dns(self):
        with patch.object(fetch.socket, "getaddrinfo") as resolve:
            self.assertEqual(fetch._resolve(fetch._target("https://" + PUBLIC_V4, fetch.FetchPolicy())), (PUBLIC_V4,))
            resolve.assert_not_called()


class TestPublicFetchTransport(unittest.TestCase):
    def run_fetch(self, wires, *, url="https://example.org/private?token=secret", method="GET", policy=None):
        sockets = [FakeSocket(wire) for wire in wires]
        context = MagicMock()
        context.wrap_socket.side_effect = lambda connection, **kwargs: connection
        with patch.object(fetch.socket, "getaddrinfo", return_value=[dns_row(PUBLIC_V4)]) as resolve, \
                patch.object(fetch.socket, "socket", side_effect=sockets) as create, \
                patch.object(fetch.ssl, "create_default_context", return_value=context):
            result = fetch._fetch_direct(url, method, policy or fetch.FetchPolicy())
        return result, sockets, context, resolve, create

    def test_numeric_connection_pinned_and_tls_hostname_verified(self):
        result, sockets, context, resolve, _ = self.run_fetch([response(headers=b"Content-Type: text/plain\r\nSet-Cookie: session=secret\r\n")])
        self.assertEqual(sockets[0].connected, (PUBLIC_V4, 443))
        resolve.assert_called_once()
        context.wrap_socket.assert_called_once_with(sockets[0], server_hostname="example.org")
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(result.body, b"hello")
        self.assertEqual(result.text(), "hello")
        self.assertEqual(result.headers, {"content-type": "text/plain"})
        self.assertTrue(sockets[0].closed)
        self.assertNotIn("secret", json.dumps(result.audit))
        self.assertNotIn("private", json.dumps(result.audit))
        self.assertEqual(result.audit["body_bytes"], 5)
        self.assertEqual(len(result.audit["body_sha256"]), 64)
        self.assertEqual(base64.b64decode(result.to_dict(include_body=True)["body_base64"]), b"hello")

    def test_fixed_headers_never_copy_proxy_auth_cookies_or_environment(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://secret:password@127.0.0.1:8888",
                                     "HTTP_COOKIE": "session=secret", "OPENAI_API_KEY": "secret"}):
            _, sockets, _, _, _ = self.run_fetch([response()])
        request = sockets[0].request
        for forbidden in (b"Cookie:", b"Authorization:", b"Proxy-Authorization:", b"password"):
            self.assertNotIn(forbidden, request)
        self.assertIn(b"Accept-Encoding: identity\r\n", request)
        self.assertIn(b"Host: example.org\r\n", request)

    def test_peer_address_is_checked(self):
        connection = FakeSocket(response(), peer="8.8.8.8")
        with patch.object(fetch.socket, "socket", return_value=connection):
            with self.assertRaisesRegex(fetch.ResearchFetchError, "peer_mismatch"):
                fetch._connect(fetch._target("http://example.org", fetch.FetchPolicy()),
                               (PUBLIC_V4,), fetch.FetchPolicy(), time.monotonic() + 2)
        self.assertTrue(connection.closed)

    def test_http_uses_port_80_and_no_tls(self):
        connection = FakeSocket(response())
        with patch.object(fetch.socket, "getaddrinfo", return_value=[dns_row(PUBLIC_V4, port=80)]), \
                patch.object(fetch.socket, "socket", return_value=connection), \
                patch.object(fetch.ssl, "create_default_context") as tls:
            result = fetch._fetch_direct("http://example.org/", "GET", fetch.FetchPolicy())
        self.assertEqual(result.status, 200)
        self.assertEqual(connection.connected, (PUBLIC_V4, 80))
        tls.assert_not_called()

    def test_connect_failover_uses_only_prevalidated_addresses(self):
        first, second = FakeSocket(response()), FakeSocket(response(), peer="8.8.8.8")
        first.connect = MagicMock(side_effect=OSError("refused"))
        with patch.object(fetch.socket, "socket", side_effect=[first, second]), \
                patch.object(fetch.socket, "getaddrinfo") as resolve:
            connection, address = fetch._connect(
                fetch._target("http://example.org/", fetch.FetchPolicy()),
                (PUBLIC_V4, "8.8.8.8"), fetch.FetchPolicy(), time.monotonic() + 2)
        self.assertTrue(first.closed)
        self.assertIs(connection, second)
        self.assertEqual(address, "8.8.8.8")
        resolve.assert_not_called()

    def test_transport_timeout_and_errors_are_sanitized(self):
        for error, code in ((socket.timeout("secret"), "timeout"), (OSError("secret"), "transport_failed")):
            connection = FakeSocket(response())
            connection.sendall = MagicMock(side_effect=error)
            with self.subTest(code=code), patch.object(fetch, "_resolve", return_value=(PUBLIC_V4,)), \
                    patch.object(fetch, "_connect", return_value=(connection, PUBLIC_V4)):
                with self.assertRaisesRegex(fetch.ResearchFetchError, code) as caught:
                    fetch._fetch_direct("https://example.org/?secret", "GET", fetch.FetchPolicy())
            self.assertTrue(connection.closed)
            self.assertNotIn("secret", str(caught.exception))

    def test_tls_failure_is_not_fallback_to_cleartext_or_next_ip(self):
        connection = FakeSocket(response())
        context = MagicMock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError("secret hostname")
        with patch.object(fetch.socket, "socket", return_value=connection) as create, \
                patch.object(fetch.ssl, "create_default_context", return_value=context):
            with self.assertRaisesRegex(fetch.ResearchFetchError, "tls_verification_failed") as caught:
                fetch._connect(fetch._target("https://example.org", fetch.FetchPolicy()),
                               (PUBLIC_V4, "8.8.8.8"), fetch.FetchPolicy(), time.monotonic() + 2)
        self.assertNotIn("secret", str(caught.exception))
        create.assert_called_once()
        self.assertTrue(connection.closed)

    def test_same_host_redirect_resolves_again_and_rejects_rebinding(self):
        connection = FakeSocket(response(status=302, headers=b"Location: /next\r\n"))
        context = MagicMock()
        context.wrap_socket.side_effect = lambda connection, **kwargs: connection
        with patch.object(fetch.socket, "getaddrinfo", side_effect=[[dns_row(PUBLIC_V4)], [dns_row("127.0.0.1")]]), \
                patch.object(fetch.socket, "socket", return_value=connection) as create, \
                patch.object(fetch.ssl, "create_default_context", return_value=context):
            with self.assertRaisesRegex(fetch.ResearchFetchError, "destination_denied"):
                fetch._fetch_direct("https://example.org/", "GET", fetch.FetchPolicy())
            create.assert_called_once()

    def test_safe_redirect_has_fresh_fixed_request_without_cookies(self):
        result, sockets, _, resolve, _ = self.run_fetch([
            response(status=302, headers=b"Location: https://other.example.org/next\r\nSet-Cookie: session=secret\r\n"),
            response(body=b"done")])
        self.assertEqual(result.final_url, "https://other.example.org/next")
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(result.audit["redirect_count"], 1)
        self.assertIn(b"Host: other.example.org\r\n", sockets[1].request)
        self.assertNotIn(b"Cookie:", sockets[1].request)
        self.assertTrue(all(sock.closed for sock in sockets))

    def test_redirect_destinations_and_loops(self):
        cases = ((b"http://127.0.0.1/", "destination_denied"),
                 (b"//169.254.169.254/", "destination_denied"),
                 (b"https://user:secret@example.org/", "invalid_url"),
                 (b"https://example.org:8443/", "port_denied"),
                 (b"file:///etc/passwd", "invalid_url"),
                 (b"http://example.org/", "https_downgrade_denied"),
                 (b"/a\tb", "invalid_redirect"),
                 (b"/private?token=secret", "redirect_loop"))
        for location, error in cases:
            with self.subTest(location=location), self.assertRaisesRegex(fetch.ResearchFetchError, error):
                self.run_fetch([response(status=302, headers=b"Location: " + location + b"\r\n")])

    def test_redirect_limit_and_invalid_location(self):
        with self.assertRaisesRegex(fetch.ResearchFetchError, "too_many_redirects"):
            self.run_fetch([response(status=302, headers=b"Location: /next\r\n")], policy=fetch.FetchPolicy(max_redirects=0))
        for headers in (b"", b"Location: /one\r\nLocation: /two\r\n"):
            with self.subTest(headers=headers), self.assertRaisesRegex(fetch.ResearchFetchError, "invalid_redirect"):
                self.run_fetch([response(status=302, headers=headers)])

    def test_request_size_checked_before_dns_or_connection(self):
        with patch.object(fetch.socket, "getaddrinfo") as resolve:
            with self.assertRaisesRegex(fetch.ResearchFetchError, "request_too_large"):
                fetch._fetch_direct("https://example.org/" + "a" * 300, "GET", fetch.FetchPolicy(max_request_bytes=256))
            resolve.assert_not_called()

    def test_body_limits_declared_unknown_and_chunked(self):
        wires = (response(body=b"123456"), response(body=b"123456", declared=False),
                 response(body=b"6\r\n123456\r\n0\r\n\r\n", headers=b"Transfer-Encoding: chunked\r\n", declared=False))
        for wire in wires:
            with self.subTest(wire=wire), self.assertRaisesRegex(fetch.ResearchFetchError, "body_too_large"):
                self.run_fetch([wire], policy=fetch.FetchPolicy(max_body_bytes=5))

    def test_supported_bodies_head_and_status_only(self):
        for wire in (response(), response(declared=False), response(
                body=b"2\r\nhe\r\n3\r\nllo\r\n0\r\nX-Metadata: ok\r\n\r\n",
                headers=b"Transfer-Encoding: chunked\r\n", declared=False)):
            self.assertEqual(self.run_fetch([wire])[0].body, b"hello")
        result, sockets, _, _, _ = self.run_fetch([response()], method="HEAD")
        self.assertEqual(result.body, b"")
        self.assertTrue(sockets[0].request.startswith(b"HEAD "))
        for status in (204, 304):
            self.assertEqual(self.run_fetch([response(status=status)])[0].body, b"")
        self.assertEqual(self.run_fetch([response(status=404)])[0].status, 404)

    def test_malformed_response_and_framing(self):
        cases = ((b"HTTP/1.1 200 OK\n\n", "invalid_response"),
                 (b"HTTP/1.1 101 Switching Protocols\r\n\r\n", "invalid_response"),
                 (response(headers=b"X: value\x00bad\r\n"), "invalid_response"),
                 (response(headers=b" Folded: no\r\n"), "invalid_response"),
                 (response(headers=b"Content-Length: 5\r\n"), "invalid_framing"),
                 (response(headers=b"Transfer-Encoding: chunked\r\n"), "invalid_framing"),
                 (response(headers=b"Content-Encoding: gzip\r\n"), "unsupported_content_encoding"),
                 (response(headers=b"Transfer-Encoding: gzip\r\n", declared=False), "unsupported_transfer_encoding"),
                 (response(body=b"xyz\r\n", headers=b"Transfer-Encoding: chunked\r\n", declared=False), "invalid_framing"),
                 (response(body=b"1;x=a\r\nx\r\n", headers=b"Transfer-Encoding: chunked\r\n", declared=False), "invalid_framing"),
                 (response(body=b"0\r\nContent-Length: 1\r\n\r\n", headers=b"Transfer-Encoding: chunked\r\n", declared=False), "invalid_framing"),
                 (response()[:-1], "incomplete_response"))
        for wire, error in cases:
            with self.subTest(wire=wire), self.assertRaisesRegex(fetch.ResearchFetchError, error):
                self.run_fetch([wire])

    def test_response_header_count_bytes_and_chunk_count_limits(self):
        cases = ((response(headers=b"X: " + b"a" * 300 + b"\r\n"), fetch.FetchPolicy(max_header_bytes=256), "headers_too_large"),
                 (response(headers=b"A: 1\r\nB: 2\r\n"), fetch.FetchPolicy(max_header_count=2), "too_many_headers"),
                 (response(body=b"1\r\na\r\n1\r\nb\r\n0\r\n\r\n", headers=b"Transfer-Encoding: chunked\r\n", declared=False),
                  fetch.FetchPolicy(max_chunk_count=2), "too_many_chunks"))
        for wire, policy, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(fetch.ResearchFetchError, error):
                self.run_fetch([wire], policy=policy)


class TestPublicFetchWorker(unittest.TestCase):
    def test_fencing_callback_preserves_exception_and_reaps_worker(self):
        process = MagicMock()
        process.communicate.return_value = (b"", None)
        def fenced():
            raise RuntimeError("lease lost")
        with patch.object(fetch.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "lease lost"):
                fetch.PublicResearchFetcher().fetch("https://example.org/", cancellation_check=fenced)
        process.kill.assert_called_once()
        process.communicate.assert_called_once_with(timeout=1)

    def test_explicit_invalid_policy_never_falls_back_to_defaults(self):
        for policy in (False, 0, {}, [], "", True, object()):
            with self.subTest(policy=repr(policy)), self.assertRaises(ValueError):
                fetch.PublicResearchFetcher(policy)

    def test_parent_starts_isolated_worker_and_decodes_safe_response(self):
        result = fetch.FetchResult(200, "https://example.org/", {}, b"hello", {"body_bytes": 5})
        process = MagicMock(returncode=0)
        process.communicate.return_value = (json.dumps(result.to_dict(include_body=True)).encode(), None)
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:8888", "SSL_CERT_FILE": "/bad", "PYTHONPATH": "/bad"}), \
                patch.object(fetch.subprocess, "Popen", return_value=process) as spawn:
            actual = fetch.PublicResearchFetcher().fetch("https://example.org/")
        self.assertEqual(actual, result)
        command = spawn.call_args.args[0]
        self.assertEqual(command[:2], [sys.executable, "-I"])
        self.assertTrue(Path(command[2]).is_absolute())
        self.assertEqual(command[3], "--worker")
        options = spawn.call_args.kwargs
        self.assertEqual(options["cwd"], "/")
        self.assertEqual(options["env"], {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "Asia/Seoul"})
        self.assertTrue(options["close_fds"])
        self.assertTrue(options["start_new_session"])
        self.assertEqual(process.communicate.call_args.kwargs["timeout"], 20)

    def test_parent_deadline_kills_and_reaps_worker(self):
        process = MagicMock()
        process.communicate.side_effect = [subprocess.TimeoutExpired(["worker"], 0.1), (b"", None)]
        with patch.object(fetch.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(fetch.ResearchFetchError, "timeout") as caught:
                fetch.PublicResearchFetcher(fetch.FetchPolicy(timeout_seconds=0.1)).fetch("https://example.org/")
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.call_count, 2)
        self.assertTrue(caught.exception.audit["worker_terminated"])

    def test_parent_cancellation_kills_worker_and_preserves_interrupt(self):
        process = MagicMock()
        process.communicate.side_effect = [KeyboardInterrupt(), (b"", None)]
        with patch.object(fetch.subprocess, "Popen", return_value=process):
            with self.assertRaises(KeyboardInterrupt):
                fetch.PublicResearchFetcher().fetch("https://example.org/")
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.call_args.kwargs, {"timeout": 1})

    def test_failed_cleanup_is_not_claimed_as_terminated(self):
        process = MagicMock()
        process.communicate.side_effect = subprocess.TimeoutExpired(["worker"], 0.1)
        with patch.object(fetch.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(fetch.ResearchFetchError, "worker_cleanup_failed") as caught:
                fetch.PublicResearchFetcher().fetch("https://example.org/")
        self.assertFalse(caught.exception.audit["worker_terminated"])

    def test_real_subprocess_deadline_contains_blocking_dns(self):
        original_popen = subprocess.Popen
        processes = []

        def blocked_worker(command, **kwargs):
            # Simulate libc resolver never returning inside the trusted child.
            child = original_popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"], **kwargs)
            processes.append(child)
            return child

        start = time.monotonic()
        with patch.object(fetch.subprocess, "Popen", side_effect=blocked_worker):
            with self.assertRaisesRegex(fetch.ResearchFetchError, "timeout"):
                fetch.PublicResearchFetcher(fetch.FetchPolicy(timeout_seconds=0.1)).fetch("https://example.org/")
        self.assertLess(time.monotonic() - start, 3)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stdin.closed)

    def test_worker_error_is_preserved_without_traceback(self):
        process = MagicMock(returncode=0)
        process.communicate.return_value = (b'{"error":"dns_failed","audit":{"hops":[]}}', None)
        with patch.object(fetch.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(fetch.ResearchFetchError, "dns_failed") as caught:
                fetch.PublicResearchFetcher().fetch("https://example.org/?secret=1")
        self.assertNotIn("secret", str(caught.exception))

    def test_bad_worker_output_and_start_failure(self):
        for output in (b"not json", b"{}", b'{"body_base64":"!"}'):
            process = MagicMock(returncode=0)
            process.communicate.return_value = (output, None)
            with self.subTest(output=output), patch.object(fetch.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(fetch.ResearchFetchError, "worker_failed"):
                    fetch.PublicResearchFetcher().fetch("https://example.org/")
        with patch.object(fetch.subprocess, "Popen", side_effect=OSError("secret")):
            with self.assertRaisesRegex(fetch.ResearchFetchError, "worker_start_failed"):
                fetch.PublicResearchFetcher().fetch("https://example.org/")

    def test_actual_worker_script_works_outside_checkout_without_network(self):
        request = json.dumps({"url": "http://169.254.169.254/latest", "method": "GET",
                              "policy": fetch.asdict(fetch.FetchPolicy())}).encode()
        result = subprocess.run([sys.executable, "-I", str(Path(fetch.__file__).resolve()), "--worker"],
                                input=request, capture_output=True, cwd="/", env={}, timeout=3, check=True)
        self.assertEqual(json.loads(result.stdout)["error"], "destination_denied")
        self.assertEqual(result.stderr, b"")


if __name__ == "__main__":
    unittest.main()
