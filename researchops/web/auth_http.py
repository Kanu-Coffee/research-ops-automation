"""Authentication transport and authorization for the existing Web adapter."""

from dataclasses import asdict
import ipaddress
import json
import re
from urllib.parse import quote, unquote, urlsplit

from researchops.errors import NotFoundError, ValidationError
from researchops.services.auth_service import AuthorizationError
from researchops.web.auth_views import auth_page, password_page, users_page

PUBLIC_PATHS = {"/login", "/setup"}
ACCOUNT_PATHS = {"/account/password", "/logout", "/api/session"}


def safe_next(value):
    value = value or "/dashboard"
    decoded = unquote(value)
    if (not value.startswith("/") or decoded.startswith("//") or "\\" in decoded or
            any(ord(char) < 32 or ord(char) == 127 for char in decoded) or len(value) > 2000 or
            urlsplit(value).netloc or urlsplit(value).scheme or urlsplit(value).path in PUBLIC_PATHS | ACCOUNT_PATHS):
        return "/dashboard"
    return value


def cookie_policy(settings, headers, client_ip):
    local = (ipaddress.ip_address(client_ip).is_loopback and
             urlsplit("//" + headers.get("host", "")).hostname in {"localhost", "127.0.0.1", "::1"} and
             not settings.web.allow_remote_proxy and settings.web.bind in {"localhost", "127.0.0.1", "::1"})
    insecure = local and (settings.environment != "production" or settings.web.allow_insecure_local_auth)
    secure = headers.get("x-forwarded-proto", "http") == "https"
    if not secure and not insecure:
        raise AuthorizationError("HTTPS 연결로 로그인하세요.")
    return ("__Host-researchops_session", True) if secure else ("researchops_local_session", False)


def read_cookie(headers, name):
    text = headers.get("cookie", "")
    if len(text) > 8192:
        raise ValidationError("쿠키를 확인하세요.")
    values = {}
    for part in text.split(";"):
        if not part.strip():
            continue
        key, separator, value = part.strip().partition("=")
        if not separator or key in values:
            raise ValidationError("중복되거나 잘못된 쿠키입니다.")
        values[key] = value
    return values.get(name, "")


def session_cookie(token, name, secure, *, expire=False):
    return (f"{name}={token}; Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "") +
            ("; Max-Age=0" if expire else "; Max-Age=43200"))


def principal_dict(principal):
    return asdict(principal) if principal else None


def authorize(app, principal, method, path):
    if path in ACCOUNT_PATHS:
        return
    if principal.role == "admin":
        app.auth.require_admin(principal)
        return
    if principal.role == "user":
        if method == "GET" and path in {"/", "", "/dashboard", "/tasks", "/runs", "/tasks/new", "/delivery",
                "/api/task-options", "/api/schedule/preview", "/api/model-catalog"}:
            return
        task = re.fullmatch(r"/tasks/([a-zA-Z0-9_-]+)(?:/(edit|advanced|rename|delete|restore|enable|disable|approve-delivery|activate|run))?", path)
        if task:
            app.auth.require_task(principal, task[1], manage=method != "GET")
            return
        run = re.match(r"^/(?:api/)?runs/([a-zA-Z0-9_-]+)(?:/|$)", path)
        if run:
            app.auth.require_run(principal, run[1], manage=method != "GET")
            return
        handoff = re.fullmatch(r"/handoffs/([a-zA-Z0-9_-]+)/(retry-email|republish)", path)
        if handoff and method == "POST":
            app.auth.require_handoff(principal, handoff[1])
            return
        catalog = re.fullmatch(r"/delivery/catalog/(sender|recipient_group)/([a-zA-Z0-9_-]+)/(rename|delete|restore)", path)
        if catalog and method == "POST":
            (app.auth.require_sender if catalog[1] == "sender" else app.auth.require_recipient_group)(principal, catalog[2])
            return
        if method == "POST" and path in {"/tasks/production/create", "/delivery/save", "/delivery/test-connection", "/delivery/send-test",
                "/delivery/groups/create", "/delivery/groups/add", "/delivery/groups/remove"}:
            return  # Controller verifies every form reference before reading it.
        job = re.fullmatch(r"/api/smtp-jobs/([a-zA-Z0-9_-]+)", path)
        if job and method == "GET":
            app.auth.require_smtp_job(principal, job[1])
            return
        raise AuthorizationError("관리자만 사용할 수 있습니다.")
    if method != "GET":
        raise AuthorizationError("조회자는 이 작업을 수행할 수 없습니다.")
    if path in {"/", "", "/dashboard", "/tasks", "/runs"}:
        return
    if path in {"/tasks/new", "/tasks/drafts"} or path.startswith("/tasks/drafts/"):
        raise AuthorizationError("관리자만 사용할 수 있습니다.")
    match = re.fullmatch(r"/tasks/([a-zA-Z0-9_-]+)", path)
    if match:
        app.auth.require_task(principal, match[1])
        return
    match = re.match(r"^/(?:api/)?runs/([a-zA-Z0-9_-]+)(?:/|$)", path)
    if match:
        app.auth.require_run(principal, match[1])
        return
    match = re.fullmatch(r"/api/smtp-jobs/([a-zA-Z0-9_-]+)", path)
    if match:
        app.auth.require_smtp_job(principal, match[1])
        return
    raise AuthorizationError("관리자만 사용할 수 있습니다.")


def dispatch_account(app, method, path, form, query, session, raw_token):
    """Return response and optional replacement session, without shared state."""
    get = lambda name, default="": form.get(name, [default])[0]
    principal = session.principal if session else None
    if path in PUBLIC_PATHS:
        if method == "GET":
            if principal:
                destination = "/account/password" if principal.must_change_password else safe_next(query.get("next", "/dashboard"))
                return (302, {"Location": destination}, b""), None
            setup = not app.auth.initialized()
            if (path == "/setup") != setup:
                destination = "/setup" if setup else "/login"
                return (302, {"Location": destination + "?next=" + quote(safe_next(query.get("next", "/dashboard")), safe="")}, b""), None
            return _html(auth_page("setup" if setup else "login", next_path=safe_next(query.get("next", "/dashboard")))), None
        if method == "POST":
            try:
                if path == "/setup":
                    grant = app.auth.setup(get("setup_token"), get("username"), get("password"))
                else:
                    grant = app.auth.login(get("username"), get("password"))
                app.auth.logout(raw_token)
                target = "/account/password" if grant.session.principal.must_change_password else safe_next(get("next"))
                return (303, {"Location": target}, b""), grant
            except (ValidationError, NotFoundError, AuthorizationError) as error:
                return _html(auth_page("setup" if path == "/setup" else "login", error=str(error), next_path=safe_next(get("next")), username=get("username")), 400), None
            except Exception as error:
                if hasattr(error, "status_code"):
                    response = _html(auth_page("setup" if path == "/setup" else "login", error=str(error), next_path=safe_next(get("next")), username=get("username")), error.status_code)
                    if error.status_code == 429:
                        response[1]["Retry-After"] = str(error.retry_after)
                    return response, None
                raise
    if path == "/logout" and method == "POST":
        app.auth.logout(raw_token)
        return (303, {"Location": "/login"}, b""), "logout"
    if path == "/api/session" and method == "GET":
        return (200, {"Content-Type":"application/json"}, json.dumps({"user": principal_dict(principal), "csrf_token": session.csrf_token}).encode()), None
    if path == "/account/password":
        if method == "GET":
            return _html(password_page(must_change=principal.must_change_password)), None
        try:
            if get("new_password") != get("confirm_password"):
                raise ValidationError("새 비밀번호가 일치하지 않습니다.")
            grant = app.auth.change_password(principal, get("current_password"), get("new_password"))
            return (303, {"Location":"/dashboard?success=" + quote("비밀번호를 변경했습니다.")}, b""), grant
        except Exception as error:
            if isinstance(error, ValidationError) or hasattr(error, "status_code"):
                return _html(password_page(must_change=principal.must_change_password, error=str(error)), getattr(error,"status_code",400)), None
            raise
    if path == "/settings/users" or path.startswith("/settings/users/"):
        app.auth.require_admin(principal)
        users = app.auth.list_users(principal)
        user_id = query.get("user") if method == "GET" else (path.split("/")[3] if path.count("/") >= 3 else None)
        selected = next((user for user in users if user["user_id"] == user_id), None)
        if user_id and selected is None:
            raise NotFoundError("사용자를 찾을 수 없습니다.")
        error_text, success = "", query.get("success", "")
        if method == "POST":
            try:
                if path == "/settings/users":
                    app.auth.create_user(principal, get("username"), get("display_name"), get("role"), get("temporary_password"), form.get("task_ids", []))
                elif path.endswith("/reset-password") and path.count("/") == 4:
                    app.auth.reset_password(principal, user_id, get("temporary_password"))
                elif path.count("/") == 3:
                    app.auth.update_user(principal, user_id, display_name=get("display_name"), role=get("role"), active=get("active") == "true", task_ids=form.get("task_ids", []))
                else:
                    raise NotFoundError("요청을 찾을 수 없습니다.")
                return (303, {"Location":"/settings/users?success=" + quote("계정 설정을 저장했습니다.")}, b""), None
            except (ValidationError, AuthorizationError) as error:
                error_text = str(error)
                # Preserve nonsecret entries and task selections after errors.
                if not path.endswith("/reset-password"):
                    selected = {**(selected or {}), "username":get("username"),"display_name":get("display_name"),"role":get("role","viewer"),"task_ids":form.get("task_ids",[]),"active":get("active")=="true"}
        tasks = []
        from researchops.services.web_access_service import WebAccessService
        access = WebAccessService(app)
        for deleted in (False, True):
            page = 1
            while True:
                result = access.tasks(principal, page=page, page_size=100, deleted=deleted)
                tasks.extend(result["items"])
                if page * 100 >= result["total"]:
                    break
                page += 1
        return _html(users_page(users,tasks,selected=selected,error=error_text,success=success),400 if error_text else 200), None
    return None, None


def _html(content, status=200):
    return status, {"Content-Type":"text/html; charset=utf-8"}, content.encode("utf-8")
