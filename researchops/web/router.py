"""Request routing and controller logic for ResearchOps Web UI."""

import json
import mimetypes
from pathlib import Path
import re
import secrets
import hmac
import ipaddress
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, quote, quote_plus, unquote, urlparse

from researchops.errors import NotFoundError, ValidationError, ResearchOpsError, WorkspaceError
from researchops.package.templates import get_available_templates
from researchops.services.application import ApplicationService
from researchops.web.schedule_form import cron_from_form
from researchops.web.views import (
    render_dashboard, render_delivery_view, render_doctor_view,
    render_run_detail, render_runs_list, render_task_create, render_task_detail,
    render_tasks_list
)


class WebRouter:
    def __init__(self, app_service: ApplicationService):
        self.app = app_service
        from researchops.services.web_access_service import WebAccessService
        self.access = WebAccessService(app_service)

    def handle_request(
        self, method, raw_path, body=b"", content_type="", *, headers=None, client_ip="127.0.0.1"
    ):
        from researchops.web.auth_http import (PUBLIC_PATHS, ACCOUNT_PATHS, authorize, cookie_policy,
            dispatch_account, principal_dict, read_cookie, safe_next, session_cookie)
        from researchops.web.context import request_context
        from researchops.services.audit_context import audit_actor
        method = method.upper()
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        web = self.app.settings.web
        session = None
        cookie_header = None
        def finish(response):
            return self._finish_response(response, raw_path, session.csrf_token if session else "", cookie_header)
        if not web.enabled:
            return finish((503, {"Content-Type": "text/plain"}, b"Web console is disabled"))
        if len(body) > web.max_request_bytes:
            return finish((413, {"Content-Type": "text/plain"}, b"Request too large"))
        if method not in {"GET", "POST"}:
            return finish((405, {"Content-Type":"text/plain"}, b"Method not allowed"))
        try:
            peer = ipaddress.ip_address(client_ip)
            if not any(peer in ipaddress.ip_network(cidr) for cidr in web.trusted_proxy_cidrs):
                raise ValueError("Untrusted peer")
            host = headers.get("host", "")
            parsed_host = urlparse("//" + host)
            if not host or any(c in host for c in "/\\@, \t\r\n") or parsed_host.hostname not in web.allowed_hosts:
                raise ValueError("Invalid Host")
            if parsed_host.port is not None and not 1 <= parsed_host.port <= 65535:
                raise ValueError("Invalid port")
            scheme = headers.get("x-forwarded-proto", "http")
            if scheme not in ("http", "https"):
                raise ValueError("Invalid forwarded protocol")
            if headers.get("x-forwarded-host", host) != host:
                raise ValueError("Forwarded Host mismatch")
            parsed_path = urlparse(raw_path)
            if parsed_path.netloc or parsed_path.scheme or raw_path.startswith("//"):
                raise ValueError("Absolute request target forbidden")
            query_lists = parse_qs(parsed_path.query, max_num_fields=100)
            query = {key: value[0] for key,value in query_lists.items()}
        except ValueError:
            return finish((403, {"Content-Type": "text/plain"}, b"Untrusted request origin or host"))
        path = parsed_path.path
        if method == "GET":
            from researchops.web.brand_assets import asset_response
            brand = asset_response(path)
            if brand is not None:
                return finish(brand)
        form = {}
        if method == "POST":
            if headers.get("origin") != f"{scheme}://{host}":
                return finish((403, {"Content-Type": "text/plain"}, b"Origin check failed"))
            try:
                form = parse_qs(body.decode("utf-8"), max_num_fields=1000, keep_blank_values=True)
                if len(form.get("csrf_token", [])) > 1:
                    raise ValueError("Duplicate CSRF")
            except (ValueError, UnicodeDecodeError):
                return finish((400, {"Content-Type": "text/plain"}, b"Invalid form"))
            if form.get("panel", [""])[0] in {"sender", "group"}:
                query["panel"] = form["panel"][0]
        try:
            cookie_name, secure_cookie = cookie_policy(self.app.settings, headers, client_ip)
            raw_token = read_cookie(headers, cookie_name)
            session = self.app.auth.resolve_session(raw_token, touch=False)
            if path in PUBLIC_PATHS and method == "GET" and session is None:
                grant = self.app.auth.new_preauth_session()
                session, raw_token = grant.session, grant.token
                cookie_header = session_cookie(grant.token, cookie_name, secure_cookie)
            if method == "POST":
                if session is None:
                    return finish((401, {"Content-Type":"text/plain; charset=utf-8"}, "로그인이 만료되었습니다. 다시 로그인하세요.".encode()))
                token = headers.get("x-csrf-token") or form.get("csrf_token", [""])[0]
                if not token.isascii() or not hmac.compare_digest(token, session.csrf_token):
                    return finish((403, {"Content-Type":"text/plain"}, b"CSRF check failed"))
            principal = session.principal if session else None
            if path not in PUBLIC_PATHS and principal is None:
                if path.startswith("/api/") or method == "POST" or query.get("partial") == "1":
                    return finish((401,{"Content-Type":"application/json"}, b'{"error":"authentication_required"}'))
                return finish((302,{"Location":"/login?next=" + quote(safe_next(raw_path),safe="")},b""))
            if principal and principal.must_change_password and path not in PUBLIC_PATHS | ACCOUNT_PATHS:
                if path.startswith("/api/") or method == "POST" or query.get("partial") == "1":
                    return finish((403,{"Content-Type":"application/json"},b'{"error":"password_change_required"}'))
                return finish((302,{"Location":"/account/password"},b""))
            if principal and path not in PUBLIC_PATHS:
                if path == "/tasks/drafts" or path.startswith("/tasks/drafts/"):
                    return finish((410,{"Content-Type":"text/plain; charset=utf-8"},"작성 중 사본 기능이 종료되었습니다. Task를 직접 편집하세요.".encode()))
                authorize(self.app, principal, method, path)
            from researchops.services.ownership import creation_owner, entity_owner
            owner = principal.user_id if principal else None
            context_task = form.get("task_id", [query.get("task_id", "")])[0]
            if principal and path.startswith("/delivery") and context_task:
                self.app.auth.require_task(principal, context_task, manage=True)
                owner = entity_owner(self.app.db, "task", context_task)
                query["task_id"] = context_task
            with request_context(principal_dict(principal), query), audit_actor(principal.audit_actor if principal else "web-anonymous"), creation_owner(owner):
                response, replacement = dispatch_account(self.app,method,path,form,query,session,raw_token)
                if response is None:
                    response = self._dispatch(method,raw_path,body,content_type)
                if method == "POST" and (path.startswith("/delivery/groups/") or path.startswith("/delivery/catalog/recipient_group/")):
                    location = response[1].get("Location", "")
                    if location.startswith("/delivery?"):
                        response[1]["Location"] = location.replace("/delivery?", "/delivery?tab=groups&", 1)
                if method == "POST" and query.get("panel") in {"sender", "group"}:
                    location = response[1].get("Location", "")
                    if location.startswith("/delivery"):
                        response[1]["Location"] = location + ("&" if "?" in location else "?") + "panel=" + query["panel"]
                        if context_task:
                            response[1]["Location"] += "&task_id=" + quote(context_task, safe="")
                if replacement == "logout":
                    cookie_header = session_cookie("",cookie_name,secure_cookie,expire=True)
                    session = None
                elif replacement is not None:
                    session = replacement.session
                    cookie_header = session_cookie(replacement.token,cookie_name,secure_cookie)
                elif principal and not path.startswith("/api/") and query.get("partial") != "1":
                    self.app.auth.resolve_session(raw_token,touch=True)
                return finish(response)
        except NotFoundError:
            return finish((404,{"Content-Type":"text/plain"},b"Not found"))
        except ValidationError as error:
            return finish((400,{"Content-Type":"text/plain; charset=utf-8"},str(error).encode()))
        except ResearchOpsError as error:
            status = getattr(error,"status_code",400)
            return finish((status,{"Content-Type":"text/plain; charset=utf-8"},str(error).encode()))

    def _finish_response(self, response, raw_path, csrf_token, cookie_header=None):
        status, response_headers, content = response
        response_headers.setdefault("X-Content-Type-Options", "nosniff")
        response_headers.setdefault("Referrer-Policy", "no-referrer")
        response_headers.setdefault("Cache-Control", "no-store")
        response_headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        if cookie_header:
            response_headers["Set-Cookie"] = cookie_header
        response_path = urlparse(raw_path).path
        if "text/html" in response_headers.get("Content-Type", "") and "/preview/" not in response_path and "/artifacts/" not in response_path:
            response_headers["Referrer-Policy"] = "same-origin"
            html = content.decode("utf-8")
            def secure_form(match):
                opening, contents = match.group(0).split(">", 1)
                fields = f'<input type="hidden" name="csrf_token" value="{csrf_token}">'
                if not re.search(r'<input\b[^>]*\bname\s*=\s*(["\x27])request_key\1', contents, flags=re.I):
                    fields += f'<input type="hidden" name="request_key" value="{secrets.token_urlsafe(24)}">'
                return opening + ">" + fields + contents
            html = re.sub(r'<form\b[^>]*method=["\x27]post["\x27][^>]*>.*?</form\s*>',
                          secure_form, html, flags=re.I | re.S)
            content = html.encode("utf-8")
            response_headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'"
        response_headers["Content-Length"] = str(len(content))
        return status, response_headers, content

    def _dispatch(
        self,
        method: str,
        raw_path: str,
        body: bytes = b"",
        content_type: str = ""
    ) -> Tuple[int, Dict[str, str], bytes]:
        """Dispatch HTTP request and return (status_code, headers_dict, body_bytes)."""
        method = method.upper()
        if method not in ("GET", "POST"):
            return 405, {"Content-Type": "text/plain"}, b"Method Not Allowed"

        parsed = urlparse(raw_path)
        path = parsed.path
        query = parse_qs(parsed.query)
        from researchops.web.context import get_principal
        from researchops.services.auth_service import Principal
        principal = Principal(**get_principal())
        viewer = principal.role == "viewer"

        # Parse flash messages from query params
        flash = None
        if "success" in query:
            flash = {"type": "success", "message": query["success"][0]}
        elif "error" in query:
            flash = {"type": "error", "message": query["error"][0]}
        elif "warning" in query:
            flash = {"type": "warning", "message": query["warning"][0]}

        # Parse form data or json body for POST
        form_data: Dict[str, list] = {}
        json_data: Dict[str, Any] = {}
        if method == "POST":
            ct = content_type.lower()
            if "application/x-www-form-urlencoded" in ct or "multipart/form-data" in ct:
                form_data = parse_qs(body.decode("utf-8", errors="replace"))
            elif "application/json" in ct:
                try:
                    json_data = json.loads(body.decode("utf-8", errors="replace"))
                except Exception:
                    json_data = {}
            else:
                # Default attempt form parse
                form_data = parse_qs(body.decode("utf-8", errors="replace"))

        def get_field(name: str, default: str = "") -> str:
            if name in form_data and form_data[name]:
                return form_data[name][0]
            if name in json_data:
                return str(json_data[name])
            return default

        def task_editor_response(values=None, *, mode="create", error=None, status=200):
            from researchops.services.ownership import entity_owner
            owner = entity_owner(self.app.db, "task", values["task_id"]) if mode == "edit" else principal.user_id
            cfg = self.app.delivery.get_delivery_config()
            recipient_names = {item['legacy_key']: item['display_name']
                               for item in self.app.catalog.list("recipient_group", owner_user_id=owner)}
            sender_names = {item['legacy_key']: f"{item['display_name']} · #{item['entity_id']}"
                            for item in self.app.catalog.list("sender", owner_user_id=owner)}
            values = dict(values or {})
            if mode != "edit" and values.get("sender_profile_id", "default") not in sender_names:
                values["sender_profile_id"] = next(iter(sender_names), "")
                if mode == "clone":
                    values["recipient_group_id"] = ""
                    values["recipient_group_ids"] = []
            try:
                operating_state = self.app.delivery.operating_status(
                    sender_profile_id=values.get("sender_profile_id", next(iter(sender_names), "")))
            except ResearchOpsError as exc:
                operating_state = {"production": self.app.settings.environment == "production", "missing": [str(exc)]}
            html_body = render_task_create([], [], editor_values=values, editor_mode=mode,
                recipient_groups={key: val for key, val in cfg.recipient_groups.items() if key in recipient_names},
                sender_profiles={key: val for key, val in cfg.all_senders().items() if key in sender_names},
                recipient_names=recipient_names, sender_names=sender_names,
                flash={"type": "error", "message": error} if error else flash,
                operating_state=operating_state, model_catalog=self.app.model_catalog.snapshot(refresh=False))
            return status, {"Content-Type": "text/html; charset=utf-8"}, html_body.encode("utf-8")

        def posted_stage_settings(stages=("research", "compose"), *, key="stage_settings"):
            if key in json_data:
                value = json_data[key]
                if not isinstance(value, dict):
                    raise ValidationError("단계별 AI 설정 형식이 올바르지 않습니다.")
                return value
            if not any(stage + "_provider" in form_data or stage + "_provider" in json_data for stage in stages):
                return None
            return {stage: {"type": get_field(stage + "_provider"),
                            "model": get_field(stage + "_model").strip() or None,
                            "reasoning_effort": get_field(stage + "_effort").strip() or None} for stage in stages}

        def run_detail_response(run_id, *, retry_values=None, error=None, status=200,
                                send_email_error=None, send_email_request_key=None):
            run_data = self.app.runs.show_run(run_id)
            task_entity = self.app.catalog.get("task", run_data["run"]["task_id"])
            if task_entity:
                run_data["run"]["task_name"] = task_entity["display_name"]
            artifacts = self.app.runs.get_run_artifacts(run_id)
            handoff = run_data.get("handoff")
            if handoff and handoff.get("handoff_id"):
                run_data["email_retry"] = self.app.delivery.email_retry_status(handoff["handoff_id"])
            else:
                run_data["prepared_email"] = self.app.runs.prepared_email_status(run_id)
            current_task = None
            try:
                if viewer:
                    raise NotFoundError("No editor for viewers")
                editor = self.app.tasks.get_task_editor(run_data["run"]["task_id"])
                from researchops.web.ai_settings import stage_settings
                current_task = {"stage_settings": stage_settings(editor),
                                "expected_version_hash": editor["expected_version_hash"]}
            except ResearchOpsError:
                pass
            html_body = render_run_detail(run_data, artifacts, run_data["audit_events"],
                flash={"type": "error", "message": error} if error else flash,
                model_catalog=self.app.model_catalog.snapshot(refresh=False) if not viewer else None,
                retry_values=retry_values, current_task=current_task,
                send_email_error=send_email_error, send_email_request_key=send_email_request_key,
                tab="email" if send_email_error else query.get("tab", ["overview"])[0], partial=query.get("partial", [""])[0] == "1")
            return status, {"Content-Type": "text/html; charset=utf-8"}, html_body.encode("utf-8")

        def delivery_catalog():
            from researchops.web.context import get_query
            from researchops.services.ownership import entity_owner
            task_id = get_query().get("task_id")
            scope = {"owner_user_id":entity_owner(self.app.db,"task",task_id)} if task_id else (
                {"owner_user_id":principal.user_id} if principal.role == "user" else {})
            return {kind: self.app.catalog.list(kind, include_deleted=True, **scope)
                    for kind in ("recipient_group", "sender")}

        def delivery_config_view():
            """Only expose the selected namespace to a settings renderer."""
            cfg = self.app.delivery.get_delivery_config()
            catalogs = delivery_catalog()
            sender_keys = {item['legacy_key'] for item in catalogs['sender']}
            group_keys = {item['legacy_key'] for item in catalogs['recipient_group']}
            from types import SimpleNamespace
            senders = {key: value for key, value in cfg.all_senders().items() if key in sender_keys}
            def get_sender(key):
                if key not in senders:
                    raise NotFoundError("발신 계정을 찾을 수 없습니다.")
                return senders[key]
            return SimpleNamespace(enabled=cfg.enabled, all_senders=lambda: senders, get_sender=get_sender,
                recipient_groups={key: value for key, value in cfg.recipient_groups.items() if key in group_keys})

        def advanced_response(task_id, *, values=None, error=None, status=200):
            from researchops.web.task_advanced_editor import render_task_advanced_editor
            if values is None:
                values = self.app.tasks.get_task_advanced_editor(task_id)
            return status, {"Content-Type":"text/html; charset=utf-8"}, render_task_advanced_editor(values,error=error).encode()

        def posted_editor_values():
            values = {key: get_field(key) for key in ("task_id", "name", "email_spec_md", "runner_type",
                "recipient_group_id", "recipient_routing_mode", "model", "sender_profile_id", "expected_version_hash", "expected_updated_at",
                "source_task_id", "source_version_hash", "cron", "schedule_preset", "schedule_time",
                "schedule_weekday", "schedule_monthday", "schedule_minute", "launch_mode", "action")}
            values["task_md"] = get_field("task_md", get_field("instructions"))
            values["schedule_enabled"] = get_field("schedule_enabled") == "true"
            values["sender_profile_id"] = values["sender_profile_id"] or "default"
            staged = posted_stage_settings()
            if staged is not None:
                values["stage_settings"] = staged
            return values

        try:
            if method == "POST":
                if path in {"/delivery/save", "/delivery/test-connection", "/delivery/send-test"}:
                    if path != "/delivery/save" or get_field("create_sender") != "true":
                        self.app.auth.require_sender(principal, get_field("sender_profile_id", "default"))
                    elif principal.role == "user" and not get_field("display_name").strip():
                        raise ValidationError("발신 계정 이름을 입력하세요.")
                if path in {"/delivery/groups/add", "/delivery/groups/remove"}:
                    self.app.auth.require_recipient_group(principal,get_field("group_id"))
                if path == "/tasks/production/create" and get_field("source_task_id"):
                    self.app.auth.require_task(principal,get_field("source_task_id"),manage=True)
                if principal.role == "user" and (path == "/tasks/production/create" or re.fullmatch(r"/tasks/[a-zA-Z0-9_-]+/edit",path)):
                    self.app.auth.require_sender(principal,get_field("sender_profile_id", "default"))
                    if get_field("recipient_group_id"):
                        self.app.auth.require_recipient_group(principal,get_field("recipient_group_id"))
                if path == "/delivery/groups/create" and principal.role == "user" and not get_field("display_name").strip():
                    raise ValidationError("수신자 그룹 이름을 입력하세요.")
            # ==========================================
            # GET Routes
            # ==========================================
            if method == "GET":
                log_match = re.fullmatch(r"/api/runs/([a-zA-Z0-9_-]+)/logs", path)
                if log_match:
                    from researchops.web.log_api import read_log_window
                    result = read_log_window(self.app,log_match[1],query.get("file",["research.stdout"])[0],query.get("offset",["0"])[0])
                    return 200,{"Content-Type":"application/json"},json.dumps(result,ensure_ascii=False).encode()
                if path == "/api/schedule/preview":
                    from researchops.services.web_access_service import next_occurrences
                    from researchops.web.schedule_form import schedule_summary
                    cron = cron_from_form(lambda key, default="": query.get(key, [default])[0])
                    values = next_occurrences(cron)
                    result = {"occurrences": values, "summary": schedule_summary(cron)}
                    return 200, {"Content-Type":"application/json"}, json.dumps(result,ensure_ascii=False).encode()
                if path == "/api/task-options":
                    from researchops.services.ownership import entity_owner
                    task_context = query.get("task_id", [""])[0]
                    if task_context:
                        self.app.auth.require_task(principal,task_context,manage=True)
                    owner = entity_owner(self.app.db,"task",task_context) if task_context else principal.user_id
                    cfg = self.app.delivery.get_delivery_config()
                    senders = self.app.catalog.list("sender",owner_user_id=owner)
                    groups = self.app.catalog.list("recipient_group",owner_user_id=owner)
                    result = {"senders":[{"id":item["legacy_key"],"name":item["display_name"],
                                        "missing":self.app.delivery.operating_status(sender_profile_id=item["legacy_key"])["missing"]}
                                       for item in senders if item["legacy_key"] in cfg.all_senders()],
                              "groups":[{"id":item["legacy_key"],"name":item["display_name"],"count":len(cfg.recipient_groups.get(item["legacy_key"],[]))} for item in groups if cfg.recipient_groups.get(item["legacy_key"])]}
                    return 200, {"Content-Type":"application/json"}, json.dumps(result,ensure_ascii=False).encode()
                if path == "/api/model-catalog":
                    return 200, {"Content-Type": "application/json"}, json.dumps(
                        self.app.model_catalog.snapshot(refresh=False), ensure_ascii=False).encode("utf-8")
                # 1. Root redirect
                if path in ("", "/"):
                    return 302, {"Location": "/dashboard"}, b""

                # 2. Dashboard
                if path == "/dashboard":
                    tasks = self.access.tasks(principal, page_size=5)["items"]
                    runs = self.access.runs(principal, page_size=10)["items"]
                    html_body = render_dashboard({}, tasks, runs, flash=flash,
                        dashboard_data=self.access.dashboard(principal))
                    return 200, {"Content-Type": "text/html; charset=utf-8"}, html_body.encode("utf-8")

                # 3. Task Create Page (Templates & Clone)
                if path == "/tasks/new":
                    clone_source = query.get("clone", [None])[0]
                    if clone_source:
                        self.app.auth.require_task(principal, clone_source, manage=True)
                        values = self.app.tasks.get_task_editor(clone_source)
                        values.update(source_task_id=clone_source,
                            source_version_hash=values["expected_version_hash"],
                            task_id="", name=values["name"] + " (복사)", schedule_enabled=False)
                        return task_editor_response(values, mode="clone")
                    return task_editor_response()

                m_advanced = re.fullmatch(r"/tasks/([a-zA-Z0-9_-]+)/advanced", path)
                if m_advanced:
                    return advanced_response(m_advanced[1])

                m_edit = re.fullmatch(r"/tasks/([a-zA-Z0-9_\-]+)/edit", path)
                if m_edit:
                    task_id = m_edit.group(1)
                    return task_editor_response(self.app.tasks.get_task_editor(m_edit.group(1)), mode="edit")

                # 6. Tasks List
                if path == "/tasks":
                    deleted = query.get("view", [""])[0] == "deleted"
                    filters = {"q":query.get("q",[""])[0],"status":query.get("status",[""])[0]}
                    page = self.access.tasks(principal, **filters, deleted=deleted, page=query.get("page",[1])[0])
                    html_body = render_tasks_list(page["items"], flash=flash, deleted=deleted,
                                                  pagination=page, filters=filters)
                    return 200, {"Content-Type": "text/html; charset=utf-8"}, html_body.encode("utf-8")

                # 7. Task Detail
                m_task = re.match(r"^/tasks/([a-zA-Z0-9_\-]+)$", path)
                if m_task:
                    task_id = m_task.group(1)
                    if viewer:
                        from researchops.web.viewer_views import render_viewer_task
                        task = self.app.task_repo.get_task_status(task_id)
                        entity = next((item for item in self.app.catalog.list("task",include_deleted=True) if item["legacy_key"] == task_id),None)
                        runs_page = self.access.runs(principal,task_id=task_id,page=query.get("page",[1])[0])
                        html_body = render_viewer_task(task_id,task,entity,runs_page)
                        return 200,{"Content-Type":"text/html; charset=utf-8"},html_body.encode()
                    try:
                        task_info = self.app.tasks.show_task(task_id)
                    except NotFoundError:
                        return 404, {"Content-Type": "text/plain"}, b"Task Not Found"
                    runs = [r.to_dict() for r in self.app.runs.list_runs(task_id=task_id, limit=20)]
                    try:
                        ws_info = self.app.workspaces.inspect(task_id)
                    except Exception:
                        ws_info = None
                    versions = [
                        {"version_hash": v["hash"], "sealed_at": v["sealed_at"], "is_active": v["is_active"]}
                        for v in task_info.get("versions", [])
                    ]
                    from researchops.services.ownership import entity_owner
                    task_owner = entity_owner(self.app.db,"task",task_id)
                    html_body = render_task_detail(task_id, task_info, runs, ws_info, versions=versions,
                        flash=flash, production=self.app.settings.environment == "production",
                        recipient_names=self.app.catalog.names("recipient_group", owner_user_id=task_owner),
                        sender_names=self.app.catalog.names("sender", owner_user_id=task_owner))
                    return 200, {"Content-Type": "text/html; charset=utf-8"}, html_body.encode("utf-8")

                # 8. Runs List
                if path == "/runs":
                    status_filter = query.get("status", [None])[0]
                    task_filter = query.get("task_id", [None])[0]
                    filters = {"q":query.get("q",[""])[0],"status":status_filter,"task_id":task_filter}
                    page = self.access.runs(principal, **filters, page=query.get("page",[1])[0])
                    html_body = render_runs_list(page["items"], filter_status=status_filter, flash=flash,
                                                pagination=page,filters=filters)
                    return 200, {"Content-Type": "text/html; charset=utf-8"}, html_body.encode("utf-8")

                # 9. Run Detail
                m_run = re.match(r"^/runs/([a-zA-Z0-9_\-]+)$", path)
                if m_run:
                    run_id = m_run.group(1)
                    try:
                        return run_detail_response(run_id)
                    except NotFoundError:
                        return 404, {"Content-Type": "text/plain"}, b"Run Not Found"

                # 10. Run Email HTML Preview (Sandboxed & Strict CSP)
                m_prev_html = re.match(r"^/runs/([a-zA-Z0-9_\-]+)/preview/html$", path)
                if m_prev_html:
                    run_id = m_prev_html.group(1)
                    try:
                        html_content = self.app.runs.read_run_archive_file(run_id, "email.html").decode("utf-8")
                    except NotFoundError:
                        html_content = "<!DOCTYPE html><html><body><p style='color:#666; font-family:sans-serif;'>No composed HTML email available for this run.</p></body></html>"

                    headers = {
                        "Content-Type": "text/html; charset=utf-8",
                        "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data: cid:; script-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none';",
                        "X-Content-Type-Options": "nosniff"
                    }
                    return 200, headers, html_content.encode("utf-8")

                # 11. Run Email Plain Text Preview
                m_prev_txt = re.match(r"^/runs/([a-zA-Z0-9_\-]+)/preview/text$", path)
                if m_prev_txt:
                    run_id = m_prev_txt.group(1)
                    try:
                        txt_content = self.app.runs.read_run_archive_file(run_id, "email.txt").decode("utf-8")
                    except NotFoundError:
                        txt_content = "No composed plain text email available for this run."
                    return 200, {"Content-Type": "text/plain; charset=utf-8"}, txt_content.encode("utf-8")

                # 12. Artifact Download / View
                m_art = re.fullmatch(r"/runs/([a-zA-Z0-9_\-]+)/artifacts/(.+)", path)
                if m_art:
                    run_id = m_art.group(1)
                    encoded_name = m_art.group(2)
                    if (re.search(r"%(?![0-9A-Fa-f]{2})", encoded_name) or
                            re.search(r"%(?:2f|5c)", encoded_name, re.IGNORECASE)):
                        raise ValidationError("Invalid artifact URL escaping")
                    try:
                        filename = unquote(encoded_name, encoding="utf-8", errors="strict")
                    except UnicodeError as exc:
                        raise ValidationError("Artifact URL must use UTF-8") from exc
                    if re.search(r"%[0-9A-Fa-f]{2}", filename):
                        raise ValidationError("Repeated artifact URL encoding is forbidden")
                    try:
                        if re.fullmatch(r"logs/(research|compose)\.(stdout|stderr)", filename):
                            from researchops.web.file_response import StreamingFileBody
                            target = self.app.runs.get_run_archive_file(run_id, filename)
                            if target is None:
                                raise NotFoundError("Archive log not found")
                            if target.stat().st_size > 1_000_000:
                                content = StreamingFileBody(target, target.parent.parent)
                            else:
                                content = self.app.runs.read_run_archive_file(run_id, filename)
                        else:
                            content = self.app.runs.read_run_archive_file(run_id, filename)
                    except NotFoundError:
                        return 404, {"Content-Type": "text/plain"}, b"Artifact Not Found"

                    mime_type, _ = mimetypes.guess_type(filename)
                    if not mime_type:
                        mime_type = "application/octet-stream"
                    if mime_type.startswith("text/") or mime_type == "application/json":
                        mime_type += "; charset=utf-8"

                    headers = {
                        "Content-Type": mime_type,
                        "Content-Length": str(len(content)),
                        "X-Content-Type-Options": "nosniff"
                    }
                    basename = Path(filename).name
                    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", basename)
                    if not ascii_name.strip("._"):
                        ascii_name = "download"
                    headers["Content-Disposition"] = (f'attachment; filename="{ascii_name}"; '
                        + "filename*=UTF-8''" + quote(basename, safe=""))
                    headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
                    return 200, headers, content

                # 13. System Health / Doctor
                if path == "/doctor":
                    doc_data = self.app.doctor.check_all()
                    html_body = render_doctor_view(doc_data, flash=flash)
                    return 200, {"Content-Type": "text/html; charset=utf-8"}, html_body.encode("utf-8")

                # 14. Delivery & SMTP Settings
                if path == "/delivery":
                    cfg = delivery_config_view()
                    active_sender_keys = [item['legacy_key'] for item in delivery_catalog()['sender'] if not item['deleted_at']]
                    sender_profile_id = query.get("sender", [next(iter(active_sender_keys), "")])[0]
                    creating_sender = query.get("new_sender", [""])[0] == "true"
                    if "sender" in query:
                        self.app.auth.require_sender(principal,sender_profile_id)
                        cfg.get_sender(sender_profile_id)
                    if not active_sender_keys:
                        creating_sender = True
                    if not creating_sender:
                        try:
                            cfg.get_sender(sender_profile_id)
                            self.app.catalog.require_active("sender", sender_profile_id)
                        except ResearchOpsError as exc:
                            return 400, {"Content-Type": "text/plain; charset=utf-8"}, (
                                str(exc) + "\n발신 계정 목록으로 돌아가 등록된 계정을 선택하세요: /delivery").encode("utf-8")
                    html_body = render_delivery_view(cfg, flash=flash,
                        operating_state=({"production":self.app.settings.environment == "production","missing":[]}
                            if creating_sender else self.app.delivery.operating_status(sender_profile_id=sender_profile_id)),
                        sender_profile_id=sender_profile_id, creating_sender=creating_sender,
                        catalog_entries=delivery_catalog())
                    return 200, {"Content-Type": "text/html; charset=utf-8"}, html_body.encode("utf-8")

                # 14. Run Status Polling API (JSON)
                m_smtp_job = re.match(r"^/api/smtp-jobs/([a-zA-Z0-9_\-]+)$", path)
                if m_smtp_job:
                    job = self.app.delivery.show_smtp_job(m_smtp_job.group(1))
                    return (200 if job else 404), {"Content-Type": "application/json"}, json.dumps(job or {"error":"SMTP job not found"}).encode("utf-8")
                m_api_run = re.match(r"^/api/runs/([a-zA-Z0-9_\-]+)/status$", path)
                if m_api_run:
                    run_id = m_api_run.group(1)
                    try:
                        run_info = self.app.runs.get_run_status(run_id)
                    except NotFoundError:
                        return 404, {"Content-Type": "application/json"}, b'{"error": "Run not found"}'
                    return 200, {"Content-Type": "application/json"}, json.dumps(run_info, indent=2).encode("utf-8")

                return 404, {"Content-Type": "text/plain"}, b"404 Not Found"

            # ==========================================
            # POST Routes (Mutating Actions)
            # ==========================================
            elif method == "POST":
                m_advanced = re.fullmatch(r"/tasks/([a-zA-Z0-9_-]+)/advanced", path)
                if m_advanced:
                    task_id = m_advanced[1]
                    values = self.app.tasks.get_task_advanced_editor(task_id)
                    values.update({key:get_field(key) for key in ("config_yaml","task_md","email_spec_md",
                        "expected_version_hash","expected_updated_at","request_key")})
                    try:
                        if principal.role == "user":
                            import yaml
                            try:
                                definition = yaml.safe_load(values['config_yaml'])
                            except yaml.YAMLError:
                                definition = None  # The editor service reports syntax errors with retained input.
                            if isinstance(definition,dict):
                                delivery = definition.get('delivery')
                                if isinstance(delivery,dict):
                                    sender = delivery.get('sender_profile_id','default')
                                    if isinstance(sender,str):
                                        self.app.auth.require_sender(principal,sender)
                                # Detailed schema and routing rules are validated by the shared service.
                                if isinstance(delivery,dict):
                                    group_ids = delivery.get('allowed_recipient_group_ids',[])
                                    for group in group_ids if isinstance(group_ids,list) else []:
                                        if isinstance(group,str):
                                            self.app.auth.require_recipient_group(principal,group)
                                alerts = definition.get('alerting')
                                if isinstance(alerts,dict) and alerts.get('events'):
                                    alert_group = alerts.get('recipient_group_id','researchops-admins')
                                    if isinstance(alert_group,str):
                                        self.app.auth.require_recipient_group(principal,alert_group)
                        self.app.tasks.update_production_task_advanced(task_id, values['expected_version_hash'],
                            config_yaml=values['config_yaml'],task_md=values['task_md'],email_spec_md=values['email_spec_md'],
                            expected_updated_at=values['expected_updated_at'])
                    except NotFoundError:
                        raise
                    except ResearchOpsError as exc:
                        return advanced_response(task_id,values=values,error=str(exc),status=400)
                    return 303,{"Location":f"/tasks/{task_id}?success="+quote_plus("고급 설정을 저장했습니다.")},b""
                if path == "/api/model-catalog/refresh":
                    try:
                        result = self.app.model_catalog.snapshot(refresh=True)
                    except ResearchOpsError:
                        return 503, {"Content-Type": "application/json"}, b'{"error":"Model catalog refresh unavailable"}'
                    return 200, {"Content-Type": "application/json"}, json.dumps(result, ensure_ascii=False).encode("utf-8")
                m_catalog = re.fullmatch(r"/delivery/catalog/(recipient_group|sender)/([a-zA-Z0-9_\-]+)/(rename|delete|restore)", path)
                if m_catalog:
                    kind, key, action = m_catalog.groups()
                    try:
                        if action == "rename":
                            self.app.catalog.rename(kind, key, get_field("display_name"))
                        elif action == "delete":
                            self.app.catalog.delete(kind, key)
                        else:
                            self.app.catalog.restore(kind, key)
                        message = {"rename": "표시 이름을 변경했습니다. 기존 Task 참조는 유지됩니다.",
                                   "delete": "선택 목록에서 삭제했습니다. 과거 이력은 보존됩니다.",
                                   "restore": "목록에 복구했습니다. Task를 자동 실행하지 않습니다."}[action]
                        return 303, {"Location": "/delivery?success=" + quote_plus(message)}, b""
                    except ResearchOpsError as exc:
                        return 303, {"Location": "/delivery?error=" + quote_plus(str(exc))}, b""

                m_task_catalog = re.fullmatch(r"/tasks/([a-zA-Z0-9_\-]+)/(rename|delete|restore)", path)
                if m_task_catalog:
                    task_id, action = m_task_catalog.groups()
                    try:
                        if action == "rename":
                            self.app.catalog.rename("task", task_id, get_field("display_name"))
                        elif action == "delete":
                            self.app.tasks.delete_task(task_id)
                        else:
                            self.app.tasks.restore_task(task_id)
                        message = {"rename": "Task 표시 이름을 변경했습니다.",
                                   "delete": "Task를 삭제 목록으로 옮기고 자동 예약을 껐습니다. 실행 이력은 보존됩니다.",
                                   "restore": "Task를 복구했습니다. 자동 예약은 꺼져 있습니다."}[action]
                        return 303, {"Location": "/tasks?success=" + quote_plus(message)}, b""
                    except ResearchOpsError as exc:
                        return 303, {"Location": "/tasks?error=" + quote_plus(str(exc))}, b""

                m_edit = re.fullmatch(r"/tasks/([a-zA-Z0-9_\-]+)/edit", path)
                if m_edit:
                    task_id = m_edit.group(1)
                    values = posted_editor_values()
                    values["task_id"] = task_id
                    try:
                        self.app.tasks.update_production_task(task_id,
                            expected_version_hash=get_field("expected_version_hash"),
                            expected_updated_at=get_field("expected_updated_at") or None,
                            name=get_field("name").strip(), instructions=values["task_md"],
                            email_spec_md=get_field("email_spec_md") or None,
                            runner_type=get_field("runner_type", "codex_exec"),
                            recipient_group_id=get_field("recipient_group_id").strip(),
                            recipient_routing_mode=get_field("recipient_routing_mode") or None,
                            sender_profile_id=get_field("sender_profile_id", "default"),
                            cron=cron_from_form(get_field), schedule_enabled=values["schedule_enabled"],
                            model=get_field("model").strip() or None,
                            **({"stage_settings": values["stage_settings"]} if "stage_settings" in values else {}))
                    except ResearchOpsError as e:
                        return task_editor_response(values, mode="edit", error=str(e), status=400)
                    return 303, {"Location": f"/tasks/{task_id}?success=" + quote_plus(
                        "Task를 수정했습니다. 같은 ID와 실행 이력을 보존하며 변경사항은 다음 실행부터 적용됩니다.")}, b""

                if path == "/tasks/production/create":
                    task_id = get_field("task_id").strip() if principal.role == "admin" else ""
                    values = posted_editor_values()
                    try:
                        launch_mode = get_field("launch_mode")
                        if launch_mode and launch_mode not in {"save", "run", "schedule"}:
                            raise ValidationError("저장 후 실행 방식을 선택하세요.")
                        scheduled = launch_mode == "schedule" if launch_mode else get_field("schedule_enabled") == "true"
                        values["schedule_enabled"] = scheduled
                        created_version = self.app.tasks.create_production_task(
                            task_id=task_id,
                            name=get_field("name").strip(),
                            instructions=values["task_md"],
                            task_md=values["task_md"] if "task_md" in form_data or "task_md" in json_data else None,
                            email_spec_md=get_field("email_spec_md") or None,
                            sender_profile_id=get_field("sender_profile_id", "default"),
                            source_task_id=get_field("source_task_id") or None,
                            source_version_hash=get_field("source_version_hash") or None,
                            runner_type=get_field("runner_type", "codex_exec"),
                            recipient_group_id=get_field("recipient_group_id").strip(),
                            recipient_routing_mode=get_field("recipient_routing_mode") or None,
                            cron=cron_from_form(get_field),
                            schedule_enabled=scheduled,
                            model=get_field("model").strip() or None,
                            request_key=get_field("request_key") or None,
                            **({"stage_settings": values["stage_settings"]} if "stage_settings" in values else {}),
                        )
                        task_id = created_version.task_id
                    except ResearchOpsError as e:
                        return task_editor_response(values, mode="clone" if values["source_task_id"] else "create",
                            error=str(e), status=400)
                    if launch_mode == "run" or (not launch_mode and get_field("action") == "create_and_run"):
                        try:
                            run = self.app.runs.enqueue_run(task_id, trigger_type="manual", force_dry_run=False,
                                request_key=get_field("request_key") or None)
                        except ResearchOpsError as e:
                            return 303, {"Location": f"/tasks/{task_id}?error=" + quote_plus(
                                "Task를 저장했지만 즉시 실행을 등록하지 못했습니다: " + str(e))}, b""
                        return 303, {"Location": f"/runs/{run.run_id}?success=" + quote_plus(
                            "운영 Task를 등록하고 실제 조사·메일 발송을 예약했습니다.")}, b""
                    return 303, {"Location": f"/tasks/{task_id}?success=" + quote_plus(
                        "예약을 시작했습니다." if scheduled else "Task를 저장했습니다. 필요할 때 실행할 수 있습니다.")}, b""

                m_en = re.match(r"^/tasks/([a-zA-Z0-9_\-]+)/enable$", path)
                if m_en:
                    task_id = m_en.group(1)
                    try:
                        self.app.tasks.set_task_enabled(task_id, True)
                        return 303, {"Location": f"/tasks/{task_id}?success=" + quote_plus("Task schedule enabled")}, b""
                    except Exception as e:
                        return 303, {"Location": f"/tasks/{task_id}?error=" + quote_plus(str(e))}, b""

                # 6. Disable Task Schedule
                m_dis = re.match(r"^/tasks/([a-zA-Z0-9_\-]+)/disable$", path)
                if m_dis:
                    task_id = m_dis.group(1)
                    try:
                        self.app.tasks.set_task_enabled(task_id, False)
                        return 303, {"Location": f"/tasks/{task_id}?success=" + quote_plus("Task schedule disabled")}, b""
                    except Exception as e:
                        return 303, {"Location": f"/tasks/{task_id}?error=" + quote_plus(str(e))}, b""

                # 7. Approve / Revoke Delivery
                m_appr = re.match(r"^/tasks/([a-zA-Z0-9_\-]+)/approve-delivery$", path)
                if m_appr:
                    task_id = m_appr.group(1)
                    approved = get_field("approved", "true").lower() not in ("false", "0", "no")
                    try:
                        self.app.tasks.approve_delivery(task_id, approved)
                        msg = "Live delivery approved" if approved else "Live delivery approval revoked"
                        return 303, {"Location": f"/tasks/{task_id}?success=" + quote_plus(msg)}, b""
                    except Exception as e:
                        return 303, {"Location": f"/tasks/{task_id}?error=" + quote_plus(str(e))}, b""

                # 8. Activate Candidate Version
                m_act = re.match(r"^/tasks/([a-zA-Z0-9_\-]+)/activate$", path)
                if m_act:
                    task_id = m_act.group(1)
                    version_hash = get_field("version_hash")
                    try:
                        self.app.tasks.activate_version(task_id, version_hash)
                        return 303, {"Location": f"/tasks/{task_id}?success=" + quote_plus(f"Version {version_hash[:16]} activated")}, b""
                    except Exception as e:
                        return 303, {"Location": f"/tasks/{task_id}?error=" + quote_plus(str(e))}, b""

                # 9. Trigger Manual Run (Run Now)
                m_run_exec = re.match(r"^/tasks/([a-zA-Z0-9_\-]+)/run$", path)
                if m_run_exec:
                    task_id = m_run_exec.group(1)
                    dry_run_val = get_field("dry_run", "false").lower() in ("true", "1", "on")
                    candidate_hash = get_field("candidate_version_hash") or None

                    try:
                        run = self.app.runs.enqueue_run(
                            task_id,
                            trigger_type="candidate_dry_run" if candidate_hash else "manual",
                            candidate_version_hash=candidate_hash,
                            force_dry_run=dry_run_val,
                            request_key=get_field("request_key") or None,
                        )

                        return 303, {"Location": f"/runs/{run.run_id}?success=" + quote_plus(f"Run {run.run_id} enqueued")}, b""
                    except Exception as e:
                        return 303, {"Location": f"/tasks/{task_id}?error=" + quote_plus(f"Run enqueue failed: {str(e)}")}, b""

                # 10. Cancel Run
                m_cancel = re.match(r"^/runs/([a-zA-Z0-9_\-]+)/cancel$", path)
                if m_cancel:
                    run_id = m_cancel.group(1)
                    try:
                        self.app.runs.cancel_run(run_id)
                        return 303, {"Location": f"/runs/{run_id}?success=" + quote_plus("Run cancelled")}, b""
                    except Exception as e:
                        return 303, {"Location": f"/runs/{run_id}?error=" + quote_plus(str(e))}, b""

                m_send_email = re.fullmatch(r"/runs/([a-zA-Z0-9_\-]+)/send-email", path)
                if m_send_email:
                    run_id = m_send_email.group(1)
                    request_key = get_field("request_key") or None
                    try:
                        new_run = self.app.runs.send_prepared_email(run_id, request_key=request_key)
                        return 303, {"Location": f"/runs/{new_run.run_id}?success=" + quote_plus(
                            "작성된 이메일의 전송을 등록했습니다. AI 모델은 다시 호출하지 않습니다.")}, b""
                    except ResearchOpsError as exc:
                        return run_detail_response(run_id, status=400, send_email_error=str(exc),
                            send_email_request_key=request_key)

                m_retry_configured = re.fullmatch(r"/runs/([a-zA-Z0-9_\-]+)/retry-configured", path)
                if m_retry_configured:
                    run_id = m_retry_configured.group(1)
                    values = {key: get_field(key) for key in
                              ("scope", "selection_source", "selection_version_hash", "request_key")}
                    try:
                        scope = values["scope"]
                        if scope not in {"compose_only", "full"}:
                            raise ValidationError("재실행 범위를 선택하세요.")
                        requested = ("compose",) if scope == "compose_only" else ("research", "compose")
                        values["execution_settings"] = posted_stage_settings(requested, key="execution_settings")
                        if values["execution_settings"] is None:
                            raise ValidationError("재실행할 단계의 AI 설정을 선택하세요.")
                        selection = values["selection_source"] or "previous"
                        if selection not in {"previous", "task", "custom"}:
                            raise ValidationError("AI 설정의 선택 출처를 확인하세요.")
                        source = {"kind": {"previous": "parent_run", "task": "task_version", "custom": "manual"}[selection]}
                        if selection == "task":
                            source["task_version_hash"] = values["selection_version_hash"]
                        elif selection == "previous":
                            source["run_id"] = run_id
                        new_run = self.app.runs.retry_run(run_id, request_key=values["request_key"] or None,
                            scope=scope, execution_settings=values["execution_settings"], selection_source=source)
                        return 303, {"Location": f"/runs/{new_run.run_id}?success=" + quote_plus(
                            "선택한 AI 설정으로 새 실행을 등록했습니다.")}, b""
                    except ResearchOpsError as exc:
                        return run_detail_response(run_id, retry_values=values, error=str(exc), status=400)

                # 11. Legacy command endpoint preserves the original settings.
                m_retry = re.match(r"^/runs/([a-zA-Z0-9_\-]+)/retry$", path)
                if m_retry:
                    run_id = m_retry.group(1)
                    try:
                        new_run = self.app.runs.retry_run(run_id, request_key=get_field("request_key") or None)
                        return 303, {"Location": f"/runs/{new_run.run_id}?success=" + quote_plus(f"Retried as {new_run.run_id}")}, b""
                    except Exception as e:
                        return 303, {"Location": f"/runs/{run_id}?error=" + quote_plus(str(e))}, b""

                # 12. Compose Only
                m_comp = re.match(r"^/runs/([a-zA-Z0-9_\-]+)/compose-only$", path)
                if m_comp:
                    run_id = m_comp.group(1)
                    try:
                        new_run = self.app.runs.compose_only(run_id, request_key=get_field("request_key") or None)
                        return 303, {"Location": f"/runs/{new_run.run_id}?success=" + quote_plus("Re-composition queued")}, b""
                    except Exception as e:
                        return 303, {"Location": f"/runs/{run_id}?error=" + quote_plus(str(e))}, b""

                # Retry the preserved email; the service only queues SMTP work.
                m_email_retry = re.fullmatch(r"/handoffs/([a-zA-Z0-9_\-]+)/retry-email", path)
                if m_email_retry:
                    handoff_id = m_email_retry.group(1)
                    return_path = "/runs"
                    try:
                        handoff = self.app.delivery.show_handoff(handoff_id)["handoff"]
                        return_path = "/runs/" + quote(handoff["run_id"], safe="")
                        request_key = get_field("request_key").strip()
                        if not request_key:
                            raise ValidationError("재전송 요청 정보가 없습니다. 실행 상세를 새로 열어 주세요.")
                        allow_uncertain = get_field("allow_uncertain").lower() == "true"
                        reason = get_field("reason").strip()
                        if allow_uncertain and not reason:
                            raise ValidationError("전달 여부 확인 내용과 재전송 사유를 입력하세요.")
                        if len(reason) > 500:
                            raise ValidationError("재전송 사유는 500자 이내로 입력하세요.")
                        self.app.delivery.retry_email(handoff_id, request_key=request_key,
                            allow_uncertain=allow_uncertain, reason=reason)
                        return 303, {"Location": return_path + "?success=" + quote_plus(
                            "원문 이메일 재전송 요청을 처리했습니다. 아래 전송 상태를 확인하세요.") + "&tab=email#smtp-delivery"}, b""
                    except ResearchOpsError as exc:
                        return 303, {"Location": return_path + "?error=" + quote_plus(str(exc)) + "&tab=email#smtp-delivery"}, b""

                # 13. Republish Handoff
                m_repub = re.match(r"^/handoffs/([a-zA-Z0-9_\-]+)/republish$", path)
                if m_repub:
                    handoff_id = m_repub.group(1)
                    try:
                        ho = self.app.delivery.republish_handoff(handoff_id)
                        return 303, {"Location": f"/runs/{ho.run_id}?success=" + quote_plus("Handoff republished with exact idempotency key")}, b""
                    except Exception as e:
                        return 303, {"Location": "/runs?error=" + quote_plus(str(e))}, b""

                # 14. Delivery & SMTP Settings Save
                if path == "/delivery/save":
                    sender_profile_id = get_field("sender_profile_id", "default").strip()
                    creating_sender = get_field("create_sender") == "true"
                    try:
                        from researchops.delivery.smtp_config import SmtpSettings
                        cfg = self.app.delivery.get_delivery_config()
                        smtp = SmtpSettings() if creating_sender else cfg.get_sender(sender_profile_id)
                        smtp.host = get_field("host", "smtp.gmail.com").strip() or "smtp.gmail.com"
                        port_str = get_field("port", "587").strip()
                        if not port_str.isdigit():
                            raise ValidationError("SMTP port must be a number in 1..65535")
                        smtp.port = int(port_str)
                        smtp.use_tls = (get_field("use_tls") == "true")
                        smtp.use_ssl = (get_field("use_ssl") == "true")
                        security = get_field("security")
                        if security:
                            if security not in {"starttls", "ssl"}:
                                raise ValidationError("SMTP 보안 방식을 확인하세요.")
                            smtp.use_tls, smtp.use_ssl = security == "starttls", security == "ssl"
                        smtp.username = get_field("username").strip()
                        pwd = get_field("password")
                        if pwd:
                            smtp.password = pwd.replace(" ", "") if smtp.host == "smtp.gmail.com" else pwd
                        smtp.sender_email = get_field("sender_email").strip()
                        smtp.sender_name = get_field("sender_name").strip()
                        display_name = get_field("display_name").strip()
                        if display_name:
                            self.app.catalog.validate_name(display_name)
                        if creating_sender and display_name:
                            smtp.sender_email = smtp.sender_email or smtp.username
                            sender_profile_id = self.app.delivery.create_sender_account(display_name, smtp,
                                enabled=(get_field("enabled") == "true") if principal.role == "admin" else None, request_key=get_field("request_key") or None)
                        else:
                            enabled = (get_field("enabled") == "true") if principal.role == "admin" else None
                            if creating_sender:
                                smtp.sender_email = smtp.sender_email or smtp.username
                                self.app.delivery.save_sender_profile(sender_profile_id,smtp,create=True,enabled=enabled)
                            else:
                                self.app.delivery.update_sender_account(sender_profile_id,smtp,display_name=display_name,enabled=enabled)
                        return 303, {"Location": "/delivery?sender=" + quote_plus(sender_profile_id)
                            + "&success=" + quote_plus("발신 계정과 메일 설정을 저장했습니다. Task에서 이 계정을 선택하세요.")}, b""
                    except Exception as e:
                        cfg = delivery_config_view()
                        # Retain operator-entered settings, never put the supplied
                        # password back into HTML or a redirect/query string.
                        values = {key: get_field(key) for key in ("host", "port", "username", "sender_email", "sender_name", "display_name")}
                        values.update({key: get_field(key) == "true" for key in ("use_tls", "use_ssl", "enabled")})
                        if get_field("security") in {"starttls", "ssl"}:
                            values.update(use_tls=get_field("security") == "starttls",use_ssl=get_field("security") == "ssl")
                        try:
                            state = self.app.delivery.operating_status(sender_profile_id=sender_profile_id)
                        except ResearchOpsError:
                            state = {"production": self.app.settings.environment == "production",
                                "missing": ["등록된 발신 계정을 선택하거나 새 계정의 설정을 완료하세요."]}
                        html_body = render_delivery_view(cfg,
                            flash={"type": "error", "message": "설정을 저장하지 못했습니다: " + str(e)},
                            operating_state=state, sender_profile_id=sender_profile_id,
                            creating_sender=creating_sender, form_values=values, catalog_entries=delivery_catalog())
                        return 400, {"Content-Type": "text/html; charset=utf-8"}, html_body.encode("utf-8")

                # 15. Delivery Test Connection
                if path == "/delivery/test-connection":
                    sender_profile_id = get_field("sender_profile_id", "default")
                    ok, msg = self.app.delivery.test_smtp_connection(sender_profile_id=sender_profile_id)
                    param = "success" if ok else "error"
                    return 303, {"Location": f"/delivery?sender={quote_plus(sender_profile_id)}&{param}=" + quote_plus(msg)}, b""

                # 16. Delivery Send Test Email
                if path == "/delivery/send-test":
                    to_email = get_field("to_email").strip()
                    if not to_email:
                        return 303, {"Location": "/delivery?error=" + quote_plus("Destination email is required")}, b""
                    ok, msg = self.app.delivery.send_test_email(to_email,
                        sender_profile_id=get_field("sender_profile_id", "default"))
                    param = "success" if ok else "error"
                    return 303, {"Location": f"/delivery?{param}=" + quote_plus(msg)}, b""

                # 17. Delivery Add Recipient to Group
                if path == "/delivery/groups/create":
                    group_id = get_field("group_id").strip()
                    addresses = list(dict.fromkeys(address.strip() for address in
                        re.split(r"[,;\r\n]+", get_field("emails")) if address.strip()))
                    if not addresses or len(addresses) > 100:
                        return 303, {"Location": "/delivery?error=" + quote_plus(
                            "새 그룹에는 실제 수신자 주소를 1개 이상, 100개 이하 입력하세요.")}, b""
                    try:
                        display_name = get_field("display_name").strip()
                        if display_name:
                            group_id = self.app.delivery.create_recipient_group(display_name, addresses,
                                request_key=get_field("request_key") or None)
                        else:
                            # Existing CLI/API callers may still provide stable legacy keys.
                            cfg = self.app.delivery.get_delivery_config()
                            if group_id in cfg.recipient_groups:
                                raise ValidationError("이미 있는 그룹입니다. 아래 수신자 추가를 사용하세요.")
                            cfg.recipient_groups[group_id] = addresses
                            self.app.delivery.save_delivery_config(cfg)
                        return 303, {"Location": "/delivery?success=" + quote_plus(
                            f"그룹 {display_name or group_id} 생성 완료 ({len(addresses)}명). Task 설정에서 선택할 수 있습니다.")}, b""
                    except ResearchOpsError as e:
                        return 303, {"Location": "/delivery?error=" + quote_plus(str(e))}, b""

                if path == "/delivery/groups/add":
                    group_id = get_field("group_id").strip()
                    email = get_field("email").strip()
                    if not group_id or not email:
                        return 303, {"Location": "/delivery?error=" + quote_plus("Group ID and email address are required")}, b""
                    try:
                        cfg = self.app.delivery.get_delivery_config()
                        self.app.catalog.require_active("recipient_group", group_id)
                        if group_id not in cfg.recipient_groups:
                            raise ValidationError("수신자 그룹을 찾을 수 없습니다.")
                        if email not in cfg.recipient_groups[group_id]:
                            cfg.recipient_groups[group_id].append(email)
                        self.app.delivery.save_recipient_group(group_id, cfg.recipient_groups[group_id])
                        return 303, {"Location": "/delivery?success=" + quote_plus(f"Recipient added to {group_id}")}, b""
                    except Exception as e:
                        return 303, {"Location": f"/delivery?error=" + quote_plus(str(e))}, b""

                # 18. Delivery Remove Recipient from Group
                if path == "/delivery/groups/remove":
                    group_id = get_field("group_id").strip()
                    email = get_field("email").strip()
                    try:
                        cfg = self.app.delivery.get_delivery_config()
                        self.app.catalog.require_active("recipient_group", group_id)
                        if group_id in cfg.recipient_groups and email in cfg.recipient_groups[group_id]:
                            cfg.recipient_groups[group_id].remove(email)
                        self.app.delivery.save_recipient_group(group_id, cfg.recipient_groups[group_id])
                        return 303, {"Location": "/delivery?success=" + quote_plus(f"Recipient removed from {group_id}")}, b""
                    except Exception as e:
                        return 303, {"Location": f"/delivery?error=" + quote_plus(str(e))}, b""

                # 19. Delivery Dispatch All Pending
                if path == "/delivery/dispatch-all":
                    return 303, {"Location": "/delivery?success=" + quote_plus("Pending mail is processed by the SMTP dispatcher service")}, b""

                return 404, {"Content-Type": "text/plain"}, b"Action Not Found"

        except NotFoundError:
            return 404, {"Content-Type": "text/plain"}, b"Not found"
        except (ValidationError, WorkspaceError):
            return 400, {"Content-Type": "text/plain"}, b"Invalid request or unsafe artifact"
        except Exception:
            return 500, {"Content-Type": "text/plain"}, b"Internal server error"
