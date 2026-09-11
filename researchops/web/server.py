"""Bounded HTTP transport with application authentication behind a trusted proxy."""
import http.server
import ipaddress
import socket
import logging
from researchops.errors import ConfigError
from researchops.config import validate_web_bind
from researchops.web.router import WebRouter
from researchops.web.file_response import StreamingFileBody

logger = logging.getLogger("researchops.web")


class ResearchOpsHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def _handle(self, method, head=False):
        router = self.server.router
        # Use the actual socket peer, never X-Forwarded-For. Reject before reading
        # an untrusted caller's declared body or opening any application route.
        peer = ipaddress.ip_address(self.client_address[0])
        if not any(peer in ipaddress.ip_network(cidr)
                   for cidr in self.server.app_service.settings.web.trusted_proxy_cidrs):
            self.close_connection = True
            self.send_error(403, "Untrusted proxy peer")
            return
        body = b""
        if len(self.headers.get_all("Host", [])) != 1 or any(
            len(self.headers.get_all(name, [])) > 1 for name in
            ("Content-Length", "Origin", "X-Forwarded-Host", "X-Forwarded-Proto", "X-CSRF-Token", "Cookie")
        ):
            self.send_error(400, "Ambiguous request headers")
            return
        if self.headers.get("Transfer-Encoding"):
            self.send_error(400, "Transfer encoding is not supported")
            return
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0:
                    raise ValueError()
            except ValueError:
                self.send_error(400, "Invalid Content-Length")
                return
            if length > self.server.app_service.settings.web.max_request_bytes:
                self.close_connection = True
                self.send_error(413, "Request too large")
                return
            try:
                body = self.rfile.read(length)
            except TimeoutError:
                self.send_error(408, "Request timeout")
                return
            if len(body) != length:
                self.send_error(400, "Incomplete request body")
                return
        try:
            status, headers, content = router.handle_request(method, self.path, body,
                self.headers.get("Content-Type", ""), headers=dict(self.headers),
                client_ip=self.client_address[0])
        except Exception:
            logger.exception("Web request failed")
            status, headers, content = 500, {"Content-Type": "text/plain"}, b"Request failed; see operator logs"
        try:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if not head:
                if isinstance(content, StreamingFileBody):
                    for chunk in content.chunks():
                        self.wfile.write(chunk)
                else:
                    self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            if isinstance(content, StreamingFileBody):
                content.close()

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("GET", head=True)

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self.send_error(405, "Method not allowed")

    do_DELETE = do_PUT
    do_PATCH = do_PUT
    do_OPTIONS = do_PUT

    def log_message(self, fmt, *args):
        # Avoid query/body/credential-bearing URLs in access logs.
        logger.info("HTTP request from %s", self.client_address[0])


class ResearchOpsServer(http.server.ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, server_address, RequestHandlerClass, app_service):
        self.app_service = app_service
        self.router = WebRouter(app_service)
        if ":" in server_address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(server_address, RequestHandlerClass)


def create_web_server(app_service, host=None, port=None):
    config = app_service.settings.web
    if not config.enabled:
        raise ConfigError("Web console is disabled; enable web.enabled explicitly")
    bind = host or config.bind
    if bind == "localhost":
        bind = "127.0.0.1"
    validate_web_bind(config, bind)
    return ResearchOpsServer((bind, config.port if port is None else port),
                             ResearchOpsHTTPRequestHandler, app_service)
