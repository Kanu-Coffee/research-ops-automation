"""Role-aware shared shell; authorization itself remains in the request layer."""

import html
from html.parser import HTMLParser
from urllib.parse import urlsplit

from researchops.web.ui_assets import SCRIPT, STYLE


def principal():
    from researchops.web.context import get_principal
    return get_principal()


def can_write():
    value = principal()
    return value is None or value.get("role") in {"admin", "user"}


def is_admin():
    value = principal()
    return value is None or value.get("role") == "admin"


def owner_note(value):
    name = value.get('owner_name')
    return f'<span class="form-help owner-note">소유자: {html.escape(str(name))}</span>' if is_admin() and name else ''


class _VisibleControls(HTMLParser):
    """Remove unavailable controls from old and shared server-rendered panels."""

    def __init__(self, *, writable, admin):
        super().__init__(convert_charrefs=False)
        self.writable, self.admin = writable, admin
        self.parts, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        omit = bool(self.stack and self.stack[-1][1])
        if (tag == "form" and attrs.get("method", "get").lower() == "post" and not self.writable
                and urlsplit(attrs.get("action", "")).path not in {"/account/password", "/logout"}):
            omit = True
        if tag == "a":
            path = urlsplit(attrs.get("href", "")).path
            if not self.admin and path.startswith(("/doctor", "/settings")):
                omit = True
            if not self.writable and path.startswith("/delivery"):
                omit = True
            if not self.writable and (path in {"/tasks/new", "/tasks/drafts"} or
                    path.endswith(("/edit", "/advanced", "/retry", "/compose-only")) or
                    attrs.get("href", "").startswith(("#run-retry", "#change-email-content"))):
                omit = True
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, omit))
        if not omit:
            self.parts.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        omit = bool(self.stack and self.stack[-1][1])
        if self.stack:
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    omit = self.stack[index][1]
                    del self.stack[index:]
                    break
        if not omit:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(data)

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")

    def handle_comment(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(f"<!--{data}-->")


def visible_controls(body):
    if is_admin():
        return body
    parser = _VisibleControls(writable=can_write(), admin=False)
    parser.feed(body)
    return "".join(parser.parts)


def render_layout(title, body, active_nav="dashboard", flash=None):
    from researchops.web.context import get_query
    user, query = principal(), get_query()
    panel = query.get("panel") in {"sender", "group"}
    body = visible_controls(body)
    links = [("dashboard", "/dashboard", "대시보드"),
             ("tasks", "/tasks", "Task"),
             ("runs", "/runs", "실행 이력")]
    if can_write():
        links.append(("delivery", "/delivery", "메일 설정"))
    if is_admin():
        links.extend([("doctor", "/doctor", "시스템"),
                      ("users", "/settings/users", "사용자 관리")])
    nav = "".join(('<span class="nav-caption">설정</span>' if key == 'delivery' else '')
        + f'<a href="{href}"' + (' aria-current="page"' if key == active_nav else '') + f'>{label}</a>'
        for key, href, label in links)
    account = ""
    if user:
        name = html.escape(str(user.get("display_name") or user.get("username") or "내 계정"))
        role = {"admin": "관리자", "user": "사용자", "viewer": "조회자"}.get(user.get("role"), "")
        account = f'''<div class="sidebar-account"><p><strong>{name}</strong> · {role}</p>
          <div class="account-actions"><a class="btn btn-quiet" href="/account/password">비밀번호 변경</a>
          <form method="POST" action="/logout"><button class="btn btn-quiet">로그아웃</button></form></div></div>'''
    flash_html = ""
    if flash:
        kind = flash.get("type", "info")
        kind = kind if kind in {"error", "success"} else "info"
        flash_html = f'<div class="alert alert-{kind}" role="{"alert" if kind == "error" else "status"}"><span>{html.escape(str(flash.get("message", "")))}</span><button type="button" class="icon-button" aria-label="알림 닫기" data-dismiss-alert>×</button></div>'
    brand = '<a href="/dashboard" class="brand"><img class="brand-mark" src="/assets/brand/icon-32.png" width="32" height="32" alt="">ResearchOps</a>'
    shell = '' if panel else f'''<a class="skip-link" href="#main-content">본문으로 건너뛰기</a>
      <header class="mobile-header">{brand}<button class="btn btn-secondary" type="button" aria-label="메뉴 열기" aria-controls="main-navigation" aria-expanded="false" data-menu-toggle>메뉴</button></header>
      <button class="nav-overlay" type="button" aria-label="메뉴 닫기"></button>
      <aside class="app-sidebar">{brand}<nav class="app-nav" id="main-navigation" aria-label="주 메뉴">{nav}</nav>{account}</aside>'''
    saved = panel and flash and flash.get("type") == "success"
    return f'''<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
      <link rel="icon" href="/favicon.ico" sizes="any"><link rel="icon" type="image/png" sizes="32x32" href="/assets/brand/icon-32.png"><link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
      <title>{html.escape(str(title))} - ResearchOps</title><style>{STYLE}</style></head>
      <body class="{'embedded' if panel else ''}" data-authenticated="{'true' if user else 'false'}" data-auth-user-id="{html.escape(str((user or {}).get('user_id', '')))}" data-settings-saved="{'true' if saved else 'false'}">{shell}
      <main id="main-content" class="app-main" tabindex="-1">{flash_html}{body}</main><script>{SCRIPT}</script></body></html>'''
