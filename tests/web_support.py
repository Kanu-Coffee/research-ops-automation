"""Authenticated synthetic browsers for pre-existing business-flow Web tests.

This helper creates an ordinary account/session through AuthService. Requests
still pass the production cookie, CSRF, proxy and authorization checks. Tests
of authentication itself import the real WebRouter directly.
"""

from urllib.parse import parse_qs

from researchops.web.router import WebRouter


SYNTHETIC_USERNAME = "test-operator"
SYNTHETIC_PASSWORD = "synthetic-browser-password"


def admin_session(app):
    grant = getattr(app, "_synthetic_browser_session", None)
    if grant is None:
        if app.auth.initialized():
            grant = app.auth.login(SYNTHETIC_USERNAME, SYNTHETIC_PASSWORD)
        else:
            grant = app.auth.setup(app.auth.issue_setup_token(), SYNTHETIC_USERNAME, SYNTHETIC_PASSWORD)
        app._synthetic_browser_session = grant
    return grant


def authenticated_headers(app, headers=None, *, csrf=True):
    """Add a real cookie while preserving supplied attack headers verbatim."""
    result = dict(headers or {})
    lowered = {key.lower(): value for key, value in result.items()}
    grant = admin_session(app)
    secure = lowered.get("x-forwarded-proto", "http") == "https"
    if (not secure and app.settings.environment == "production" and
            app.settings.web.bind in {"127.0.0.1", "::1"} and not app.settings.web.allow_remote_proxy):
        # Explicit isolated-runtime opt-in, never a product authentication bypass.
        app.settings.web.allow_insecure_local_auth = True
    if "cookie" not in lowered:
        name = "__Host-researchops_session" if secure else "researchops_local_session"
        result["Cookie"] = name + "=" + grant.token
    if csrf and "x-csrf-token" not in lowered:
        result["X-CSRF-Token"] = grant.session.csrf_token
    return result


class AuthenticatedWebRouter(WebRouter):
    def __init__(self, app_service, *, inject_csrf=True):
        super().__init__(app_service)
        self.test_session = admin_session(app_service)
        self.inject_csrf = inject_csrf

    @property
    def csrf_token(self):
        return self.test_session.session.csrf_token

    def handle_request(self, method, raw_path, body=b"", content_type="", *, headers=None, client_ip="127.0.0.1"):
        supplied = headers if headers is not None else {"Host": "localhost", "Origin": "http://localhost"}
        try:
            form_token = "csrf_token" in parse_qs(body.decode("utf-8"), keep_blank_values=True)
        except (UnicodeError, ValueError):
            form_token = False
        return super().handle_request(method, raw_path, body, content_type,
            headers=authenticated_headers(self.app, supplied, csrf=self.inject_csrf and not form_token), client_ip=client_ip)
