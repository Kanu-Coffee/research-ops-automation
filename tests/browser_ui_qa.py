"""Opt-in Firefox QA against an isolated ResearchOps HTTP server.

Run: python3 tests/browser_ui_qa.py --output /tmp/researchops-web-ui-qa
Requires installed Firefox, geckodriver and Node (built-in WebSocket), no packages.
--package-root can point to an installed release for the same acceptance checks.
Only synthetic accounts/configs and a fake-runner dry-run fixture are used. SMTP
constructors are blocked; no background worker, scheduler or live model starts.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
ELEMENT = "element-6066-11e4-a52e-4f735466cecf"
ADMIN_PASSWORD = "synthetic-admin-password-only"
VIEWER_PASSWORD = "synthetic-viewer-password-only"
VIEWER_CHANGED_PASSWORD = "synthetic-viewer-changed-only"


class Browser:
    def __init__(self, output, *, dpr=1):
        output.mkdir(parents=True, exist_ok=True)
        self.output, self.session, self.driver, self.console = output, None, None, None
        self.console_path = output / "browser-console.jsonl"
        self.console_file = self.console_path.open("w")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        self.driver_log = (output / "geckodriver.log").open("w")
        self.driver = subprocess.Popen(["geckodriver", "--host", "127.0.0.1", "--port", str(port)],
                                       stdout=self.driver_log, stderr=subprocess.STDOUT)
        try:
            self.wait(lambda: self.request("/status"), "geckodriver startup")
            result = self.request("/session", {"capabilities": {"alwaysMatch": {
                "browserName": "firefox", "webSocketUrl": True,
                "moz:firefoxOptions": {"args": ["-headless"], "prefs":{"layout.css.devPixelsPerPx":str(dpr)}}, "unhandledPromptBehavior": "dismiss"}}})
            self.session, self.version = result["sessionId"], result["capabilities"]["browserVersion"]
            script = """const s=new WebSocket(process.argv[1]);
              s.addEventListener('open',()=>s.send(JSON.stringify({id:1,method:'session.subscribe',params:{events:['log.entryAdded']}})));
              s.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id===1)process.stdout.write(JSON.stringify({ready:m.type==='success'})+'\\n');
                if(m.method==='log.entryAdded'){const p=m.params;process.stdout.write(JSON.stringify({level:p.level,type:p.type,text:String(p.text||''),context:p.source?.context||''})+'\\n');}});"""
            self.console = subprocess.Popen(["node", "--input-type=module", "-e", script,
                result["capabilities"]["webSocketUrl"]], stdout=self.console_file, stderr=subprocess.DEVNULL)
            self.wait(lambda: '"ready":true' in self.console_path.read_text(), "Firefox BiDi logging")
        except Exception:
            self.close()
            raise

    def request(self, path, value=None, method=None):
        raw = None if value is None else json.dumps(value).encode()
        request = urllib.request.Request(self.base + path, data=raw,
            method=method or ("GET" if value is None else "POST"), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.load(response)["value"]
        except urllib.error.HTTPError as error:
            result = json.load(error).get("value", {})
            raise RuntimeError(f"WebDriver {result.get('error', error.code)}: {str(result.get('message', ''))[:300]}") from None

    def cmd(self, path, value=None, method=None):
        return self.request("/session/" + self.session + path, value, method)

    def js(self, script, *args):
        return self.cmd("/execute/sync", {"script": script, "args": list(args)})

    def wait(self, predicate, label, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value:
                    return value
            except (RuntimeError, urllib.error.URLError, ConnectionError):
                pass
            time.sleep(.15)
        raise AssertionError("Timed out: " + label)

    def navigate(self, url):
        self.cmd("/url", {"url": url})
        self.wait(lambda: self.js("return document.readyState==='complete'"), "page load")

    def element(self, selector):
        return self.wait(lambda: self.js("return document.querySelector(arguments[0])", selector), selector)

    def click(self, selector):
        element = self.element(selector)
        self.js("arguments[0].scrollIntoView({block:'center'})", element)
        self.cmd("/element/" + element[ELEMENT] + "/click", {})

    def fill(self, selector, value):
        element = self.element(selector)
        self.cmd("/element/" + element[ELEMENT] + "/clear", {})
        self.cmd("/element/" + element[ELEMENT] + "/value", {"text": value})

    def select(self, selector, value):
        self.js("const e=document.querySelector(arguments[0]);e.value=arguments[1];e.dispatchEvent(new Event('change',{bubbles:true}));", selector, value)

    def active_editor_tab(self):
        return self.js("return document.querySelector('[data-editor-tab][aria-selected=true]')?.dataset.editorTab")

    def wait_editor_tab(self, tab):
        self.wait(lambda: self.active_editor_tab() == tab, "editor " + tab)

    def fetch(self, path, *, method="GET", data=None):
        return self.cmd("/execute/async", {"script": """const [path,method,data,done]=arguments;
          fetch(path,{method,credentials:'same-origin',...(data?{headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams(data)}:{})})
          .then(async r=>done({status:r.status,text:await r.text()})).catch(e=>done({status:0,error:String(e)}));""",
          "args": [path, method, data]})

    def swap_cookies(self, cookies):
        # Sessions are obtained through normal login, kept only in memory.
        self.cmd("/cookie", method="DELETE")
        for cookie in cookies:
            self.cmd("/cookie", {"cookie": cookie})

    def screenshot(self, name, element=None):
        value = self.cmd("/element/" + element[ELEMENT] + "/screenshot" if element else "/screenshot")
        (self.output / name).write_bytes(base64.b64decode(value))

    def console_errors(self):
        self.console_file.flush()
        return [json.loads(line) for line in self.console_path.read_text().splitlines()
                if line.strip() and json.loads(line).get("level") in {"warn", "error"}]

    def close(self):
        if self.session:
            try:
                self.request("/session/" + self.session, method="DELETE")
            except Exception:
                pass
        for process in (self.console, self.driver):
            if process:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)
        self.console_file.close(); self.driver_log.close()


def run(output, package_root=None):
    sys.path.insert(0, str(SOURCE))
    if package_root:
        sys.path.insert(0, str(package_root))
    from researchops.services.application import ApplicationService
    from researchops.web.server import ResearchOpsHTTPRequestHandler, ResearchOpsServer
    from tests.support import isolated_settings, fixture_runner, register_fixture_task
    import researchops
    output.mkdir(parents=True, exist_ok=True)
    checks, browser = [], None
    report = {"package": str(Path(researchops.__file__).resolve()), "scope": "isolated fake-runner / blocked SMTP", "checks": checks,
              "viewport_validation":"Firefox CSS viewport/DPR emulation; physical iPhone Safari, keyboard and browser chrome not exercised"}
    def check(name, condition=True):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print(name, flush=True)

    with tempfile.TemporaryDirectory(prefix="researchops-browser-qa-") as temporary, \
         patch("smtplib.SMTP", side_effect=AssertionError("SMTP connection forbidden in browser QA")) as smtp, \
         patch("smtplib.SMTP_SSL", side_effect=AssertionError("SMTP connection forbidden in browser QA")) as smtp_ssl:
        settings = isolated_settings(Path(temporary))
        app = ApplicationService(settings, custom_runner=fixture_runner(settings))
        register_fixture_task(app)
        seed = app.runs.enqueue_run("software-releases", force_dry_run=True)
        result = app.runs.execute_run(seed.run_id, force_dry_run=True)
        check("dry-run fake fixture completed", result.status == "succeeded")
        settings.environment = "production"
        settings.web.allow_insecure_local_auth = True
        settings.delivery.global_handoff_kill_switch = False
        setup_token = app.auth.issue_setup_token()

        class Handler(ResearchOpsHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path != "/__qa_viewport":
                    return super().do_GET()
                query = urllib.parse.parse_qs(parsed.query)
                width = int(query.get("width", [390])[0])
                height = int(query.get("height", [844])[0])
                if width not in {360,390,402,768,874,1280} or height not in {402,844,874,1024}:
                    self.send_error(400); return
                import html
                route = query.get("route", ["/tasks/new"])[0]
                if not route.startswith("/") or route.startswith("//"):
                    self.send_error(400); return
                content = f'<!doctype html><title>QA viewport</title><body style="margin:0"><iframe id="qa-viewport" src="{html.escape(route,quote=True)}" style="border:0;width:{width}px;height:{height}px"></iframe>'.encode()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content))); self.end_headers(); self.wfile.write(content)
        server = ResearchOpsServer(("127.0.0.1", 0), Handler, app)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True); server_thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            browser = b = Browser(output)
            report["browser"] = "Firefox " + b.version
            b.cmd("/window/rect", {"width": 1440, "height": 1100})
            b.navigate(base + "/dashboard")
            check("uninitialized request opens setup", "/setup" in b.cmd("/url"))
            b.fill("#setup-token", setup_token); b.fill("#username", "qa-admin"); b.fill("#password", ADMIN_PASSWORD)
            b.click('form[action="/setup"] button[type="submit"]')
            b.wait(lambda: "/dashboard" in b.cmd("/url"), "admin setup completion")
            check("first administrator created", app.auth.initialized())
            check("dashboard renders meaningful content", b.js("return document.querySelector('h1')?.textContent.includes('대시보드')"))
            b.screenshot("dashboard-desktop.png")
            b.navigate(base + "/tasks/new")
            check("new task starts in research and save-only", b.active_editor_tab() == "research" and b.js("return document.querySelector('[name=launch_mode]:checked').value") == "save")
            check("desktop action bar immediately visible", b.js("const r=document.querySelector('.editor-actions').getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight"))
            b.click("[data-editor-next]")
            check("required name focuses without leaving research", b.js("return document.activeElement.id") == "task-name")
            b.fill("#task-name", "QA 업무 조사"); b.fill("#task-md", "# QA 조사\n공식 합성 출처를 비교합니다.")
            b.click("[data-editor-next]"); b.wait_editor_tab("email")
            b.fill("#email-spec-md", "# QA 메일\n원본 메일 규격을 보존합니다.")
            b.click("[data-settings-panel=group]")
            panel = b.element("#editor-settings-frame"); b.cmd("/frame", {"id": panel})
            b.fill("#new-recipient-group", "QA 조회 그룹"); b.fill("#new-group-emails", "reader@example.test")
            b.click('form[action="/delivery/groups/create"] button[type="submit"]')
            b.wait(lambda: b.js("return document.body.dataset.settingsSaved==='true'"), "inline group saved")
            b.cmd("/frame", {"id": None}); b.click("[data-settings-close]")
            b.wait(lambda: b.js("return document.querySelector('#task-recipient').selectedOptions[0].textContent.includes('QA 조회 그룹')"), "new group selected")
            check("inline group save refreshes options without losing task", b.js("return document.querySelector('#task-md').value") == "# QA 조사\n공식 합성 출처를 비교합니다.")
            b.click("[data-settings-panel=sender]"); b.cmd("/frame", {"id": b.element("#editor-settings-frame")})
            b.fill("#smtp-display_name", "QA 발신 계정"); b.fill("#smtp-username", "sender@example.test")
            b.fill("#smtp-password", "synthetic-smtp-secret"); b.fill("#smtp-sender_name", "QA 보고서")
            b.click('form[action="/delivery/save"] button[type="submit"]')
            b.wait(lambda: b.js("return document.body.dataset.settingsSaved==='true'"), "inline sender saved")
            b.cmd("/frame", {"id": None}); b.click("[data-settings-close]")
            b.wait(lambda: b.js("return document.querySelector('#task-sender').selectedOptions[0].textContent.includes('QA 발신 계정')"), "new sender selected")
            check("inline sender save retains email text", b.js("return document.querySelector('#email-spec-md').value") == "# QA 메일\n원본 메일 규격을 보존합니다.")
            b.wait(lambda: b.js("return !document.querySelector('#editor-operating-status').textContent.includes('주소') && !document.querySelector('#editor-operating-status').textContent.includes('그룹')"), "stale setup notice cleared")
            check("inline settings clear resolved warnings and retain real disabled status", b.js("return document.querySelector('#editor-operating-status').textContent.includes('메일 자동 발송') || document.querySelector('#editor-operating-status').textContent.includes('중지')"))
            b.click("[data-editor-next]"); b.wait_editor_tab("execution")
            b.wait(lambda: b.js("return document.querySelectorAll('#schedule-occurrences li').length===3"), "three schedule occurrences")
            check("independent model controls present", b.js("return document.querySelectorAll('[data-ai-stage]').length") == 2)
            b.screenshot("task-execution-desktop.png")
            b.click("[data-editor-next]"); b.wait_editor_tab("review"); b.screenshot("task-review-desktop.png")
            before_runs = len(app.runs.list_runs(limit=100))
            b.click("[data-editor-submit]")
            b.wait(lambda: b.js("return !document.querySelector('#task-editor-form')"), "new task saved")
            tasks = app.tasks.list_tasks(); created = next(t for t in tasks if t.get("name") == "QA 업무 조사")
            task_id = created["task_id"]
            editor = app.tasks.get_task_editor(task_id)
            check("new save-only task persisted without schedule or queue", not editor["schedule_enabled"] and len(app.runs.list_runs(limit=100)) == before_runs)
            check("both documents persisted separately", "# QA 조사" in editor["task_md"] and "# QA 메일" in editor["email_spec_md"])
            b.navigate(base + f"/tasks/{task_id}/edit"); b.click("[data-editor-tab=email]")
            b.fill("#email-spec-md", "# 수정 메일\n조사 지시와 독립 변경입니다."); b.click("[data-editor-tab=execution]")
            b.click("[data-task-publish]"); b.wait(lambda: b.js("return !document.querySelector('#task-editor-form')"), "task edit saved")
            changed = app.tasks.get_task_editor(task_id)
            check("edit preserves id research and schedule", changed["task_id"] == task_id and changed["task_md"] == editor["task_md"] and not changed["schedule_enabled"])
            check("edit updates only requested email document", changed["email_spec_md"].startswith("# 수정 메일"))

            # Session expiration must keep input in the existing DOM and require re-login.
            b.navigate(base + f"/tasks/{task_id}/edit"); b.fill("#task-name", "QA 만료 후 보존")
            with app.db.transaction() as connection:
                connection.execute("UPDATE auth_sessions SET expires_at=0 WHERE kind='authenticated'")
            b.click("[data-task-publish]")
            b.wait(lambda: b.js("return !!document.querySelector('#session-expired-notice')"), "session expiry preservation")
            check("expired session preserves edited fields and does not mutate task", b.js("return document.querySelector('#task-name').value") == "QA 만료 후 보존" and app.tasks.get_task_editor(task_id)["name"] != "QA 만료 후 보존")
            original_window = b.cmd("/window")
            login_window = b.cmd("/window/new", {"type": "tab"})["handle"]; b.cmd("/window", {"handle": login_window})
            b.navigate(base + "/login"); b.fill("#username", "qa-admin"); b.fill("#password", ADMIN_PASSWORD)
            b.click('form[action="/login"] button[type="submit"]'); b.wait(lambda: "/dashboard" in b.cmd("/url"), "same admin re-login")
            b.cmd("/window", {"handle": original_window}); b.click("[data-task-publish]")
            b.wait(lambda: app.tasks.get_task_editor(task_id)["name"] == "QA 만료 후 보존", "same-user form resubmit")
            check("same-user login refreshes CSRF and saves retained form")

            # User creation/Task assignment use the actual manager screen.
            b.navigate(base + "/settings/users?new=true")
            b.fill("#user-name", "qa-viewer"); b.fill("#display-name", "QA 조회자")
            b.select("#user-role", "viewer")
            b.fill("#temporary-password", VIEWER_PASSWORD)
            b.click('input[name="task_ids"][value="software-releases"]')
            b.click('#user-editor form button[type="submit"]')
            b.wait(lambda: "success=" in b.cmd("/url"), "viewer created")
            with app.db.transaction() as connection:
                viewer_id = connection.execute("SELECT user_id FROM auth_users WHERE username='qa-viewer'").fetchone()[0]
            check("viewer created through UI with assigned task")
            admin_cookies = b.cmd("/cookie")
            b.cmd("/cookie", method="DELETE"); b.navigate(base + "/login")
            b.fill("#username", "qa-viewer"); b.fill("#password", VIEWER_PASSWORD); b.click('form[action="/login"] button[type="submit"]')
            b.wait(lambda: "/account/password" in b.cmd("/url"), "mandatory initial password change")
            b.fill("#current-password", VIEWER_PASSWORD); b.fill("#new-password", VIEWER_CHANGED_PASSWORD); b.fill("#confirm-password", VIEWER_CHANGED_PASSWORD)
            b.click('form[action="/account/password"] button[type="submit"]'); b.wait(lambda: "/dashboard" in b.cmd("/url"), "viewer password changed")
            check("temporary password requires replacement before normal access")
            b.navigate(base + "/tasks")
            check("viewer sees assigned task and no hidden task", b.js("return !!document.querySelector('a[href=\"/tasks/software-releases\"]')") and not b.js("return document.body.textContent.includes('QA 만료 후 보존')"))
            check("viewer has no settings or task write links", b.js("return !document.querySelector('a[href=\"/delivery\"],a[href=\"/settings/users\"],a[href=\"/tasks/new\"]')"))
            check("unassigned task responds 404", b.fetch(f"/tasks/{task_id}")["status"] == 404)
            check("viewer cannot execute assigned task", b.fetch("/tasks/software-releases/run", method="POST", data={})["status"] in {403, 404})
            b.navigate(base + f"/runs/{seed.run_id}")
            for tab in ("overview", "email", "files", "logs"):
                b.click(f'nav[aria-label="실행 상세 영역"] a[href$="tab={tab}"]')
                b.wait(lambda: b.js("return document.querySelector('[data-run-view]')?.dataset.runTab",) == tab, "run " + tab)
                check("viewer run tab " + tab, b.js("return document.querySelector('[data-run-view]').textContent.length>20"))
            check("viewer original email accessible", b.fetch(f"/runs/{seed.run_id}/preview/text")["status"] == 200)
            check("viewer raw archive accessible", b.fetch(f"/runs/{seed.run_id}/artifacts/result.json")["status"] == 200)
            check("viewer archive HEAD authorized", b.fetch(f"/runs/{seed.run_id}/artifacts/result.json", method="HEAD")["status"] == 200)
            b.screenshot("viewer-run-logs-desktop.png")
            viewer_cookies = b.cmd("/cookie")
            b.swap_cookies(admin_cookies)
            b.navigate(base + "/settings/users?user=" + viewer_id)
            b.click('input[name="task_ids"][value="software-releases"]'); b.click('#user-editor form button[type="submit"]')
            b.wait(lambda: "success=" in b.cmd("/url"), "grant revoked")
            b.swap_cookies(viewer_cookies)
            check("revocation takes effect on existing viewer session next request", b.fetch("/tasks/software-releases")["status"] == 404 and b.fetch(f"/runs/{seed.run_id}/artifacts/result.json")["status"] == 404)
            check("revoked archive HEAD blocked", b.fetch(f"/runs/{seed.run_id}/artifacts/result.json", method="HEAD")["status"] == 404)
            b.swap_cookies(admin_cookies)

            # Two independent resource owners create real scoped settings and Tasks.
            owners = []
            for suffix in ('a', 'b'):
                username, password = 'qa-owner-' + suffix, 'synthetic-owner-' + suffix + '-password'
                changed_password = password + '-changed'
                b.swap_cookies(admin_cookies); b.navigate(base + '/settings/users?new=true')
                b.fill('#user-name', username); b.fill('#display-name', 'QA 사용자 ' + suffix.upper())
                b.select('#user-role', 'user'); b.fill('#temporary-password', password)
                b.click('#user-editor form button[type="submit"]')
                b.wait(lambda: 'success=' in b.cmd('/url'), 'owner account created')
                b.cmd('/cookie', method='DELETE'); b.navigate(base + '/login')
                b.fill('#username', username); b.fill('#password', password)
                b.click('form[action="/login"] button[type="submit"]')
                b.wait(lambda: '/account/password' in b.cmd('/url'), 'owner password change required')
                b.fill('#current-password', password); b.fill('#new-password', changed_password)
                b.fill('#confirm-password', changed_password); b.click('form[action="/account/password"] button')
                b.wait(lambda: '/dashboard' in b.cmd('/url'), 'owner dashboard')
                check(username + ' only sees owned empty dashboard', not b.js("return document.querySelector('#main-content').textContent.includes('QA 만료 후 보존')"))
                check(username + ' mail link present; system and manager absent', b.js("return !!document.querySelector('.app-nav a[href=\"/delivery\"]')&&!document.querySelector('.app-nav a[href=\"/doctor\"]')&&!document.querySelector('.app-nav a[href=\"/settings/users\"]')"))
                b.navigate(base + '/delivery')
                check(username + ' opens fresh sender with no global switch', b.js("return document.querySelector('[name=create_sender]').value==='true'&&!document.querySelector('[name=enabled]')&&document.querySelector('#smtp-username').value===''"))
                check(username + ' no admin sender or redundant rename', not b.js("return document.querySelector('#main-content').textContent.includes('QA 발신 계정')||!!document.querySelector('form[action*=\"/catalog/sender/\"][action$=\"/rename\"]')"))
                b.fill('#smtp-display_name', '소유 발신 ' + suffix); b.fill('#smtp-username', suffix + '@example.test')
                b.fill('#smtp-password', 'synthetic-owner-smtp-' + suffix)
                b.click('form[action="/delivery/save"] button[type="submit"]')
                b.wait(lambda: 'success=' in b.cmd('/url'), 'owner sender saved')
                sender_id = urllib.parse.parse_qs(urllib.parse.urlsplit(b.cmd('/url')).query)['sender'][0]
                b.navigate(base + '/delivery?tab=groups')
                b.fill('#new-recipient-group', '소유 그룹 ' + suffix); b.fill('#new-group-emails', 'recipient-' + suffix + '@example.test')
                b.click('form[action="/delivery/groups/create"] button[type="submit"]')
                b.wait(lambda: 'success=' in b.cmd('/url'), 'owner group saved')
                options = json.loads(b.fetch('/api/task-options')['text'])
                check(username + ' sees exactly own sender and group options', len(options['senders']) == 1 and len(options['groups']) == 1)
                b.navigate(base + '/tasks/new'); b.fill('#task-name', '소유 Task ' + suffix)
                b.fill('#task-md', '# 소유 조사 ' + suffix + '\n합성 공개자료 조사')
                b.click('[data-editor-next]'); b.wait_editor_tab('email')
                b.fill('#email-spec-md', '# 소유 메일 ' + suffix + '\n원문 규격 보존')
                b.click('[data-editor-next]'); b.wait_editor_tab('execution')
                b.click('[data-editor-next]'); b.wait_editor_tab('review')
                b.click('[data-editor-submit]'); b.wait(lambda: not b.js("return !!document.querySelector('#task-editor-form')"), 'owner task saved')
                owner_task = next(task for task in app.tasks.list_tasks() if task.get('name') == '소유 Task ' + suffix)
                own_id = owner_task['task_id']
                check(username + ' creates Task directly without a Draft', '/draft' not in b.cmd('/url') and '/tasks/' + own_id in b.cmd('/url'))
                with app.db.transaction() as connection:
                    drafts_before = connection.execute('SELECT COUNT(*) FROM task_drafts').fetchone()[0]
                before_advanced = app.tasks.get_task_editor(own_id)
                b.navigate(base + '/tasks/' + own_id + '/advanced')
                b.click('[data-advanced-tab=email_spec_md]')
                b.fill('#advanced-email_spec_md', '# 직접 저장 ' + suffix + '\n조사와 예약을 유지합니다.')
                b.click('#task-advanced-form button[type=submit]')
                b.wait(lambda: not b.js("return !!document.querySelector('#task-advanced-form')"), 'advanced direct save')
                after_advanced = app.tasks.get_task_editor(own_id)
                with app.db.transaction() as connection:
                    drafts_after = connection.execute('SELECT COUNT(*) FROM task_drafts').fetchone()[0]
                check(username + ' advanced save retains identity research and schedule without Draft', drafts_before == drafts_after and after_advanced['task_id'] == own_id and after_advanced['task_md'] == before_advanced['task_md'] and after_advanced['schedule_enabled'] == before_advanced['schedule_enabled'] and after_advanced['email_spec_md'].startswith('# 직접 저장'))
                check(username + ' cannot access admin routes or original admin Task', b.fetch('/doctor')['status'] == 403 and b.fetch('/settings/users')['status'] == 403 and b.fetch('/tasks/software-releases')['status'] == 404)
                owners.append({'cookies':b.cmd('/cookie'), 'task_id':own_id, 'sender_id':sender_id})
            for owner, other in ((owners[0], owners[1]), (owners[1], owners[0])):
                b.swap_cookies(owner['cookies']); b.navigate(base + '/tasks')
                check(owner['task_id'] + ' other owner Task hidden and direct edit blocked', other['task_id'] not in b.js("return document.querySelector('#main-content').innerHTML") and b.fetch('/tasks/' + other['task_id'] + '/edit')['status'] == 404 and b.fetch('/tasks/' + other['task_id'] + '/advanced')['status'] == 404)
                check(owner['task_id'] + ' other owner sender direct access blocked', b.fetch('/delivery?sender=' + other['sender_id'])['status'] == 404)
            b.swap_cookies(admin_cookies)

            for width in (360, 390, 768):
                route = f"/tasks/{task_id}/edit?qa_width={width}"
                b.navigate(base + "/__qa_viewport?" + urllib.parse.urlencode({"width": width, "route": route}))
                frame = b.element("#qa-viewport"); b.cmd("/frame", {"id": frame})
                b.element("#task-editor-form")
                check(f"{width}px no page horizontal overflow", b.js("return document.documentElement.scrollWidth<=innerWidth"))
                check(f"{width}px 16px inputs", float(b.js("return getComputedStyle(document.querySelector('#task-name')).fontSize").replace("px", "")) >= 16)
                check(f"{width}px 44px primary action visible", b.js("const r=document.querySelector('[data-task-publish]').getBoundingClientRect();return r.height>=44&&r.top>=0&&r.bottom<=innerHeight+1"))
                b.click("[data-editor-tab=email]"); b.wait_editor_tab("email")
                check(f"{width}px edit tab preserves research", b.js("return document.querySelector('#task-md').value") == editor["task_md"].replace("\r\n", "\n"))
                b.cmd("/frame", {"id": None}); b.screenshot(f"task-edit-{width}.png", frame)
                b.cmd("/frame", {"id": frame}); b.click("[data-task-publish]")
                b.wait(lambda: b.js("return !document.querySelector('#task-editor-form')"), f"{width}px save")
                check(f"{width}px edit submitted through real service")
                b.cmd("/frame", {"id": None})
            surfaces = [("dashboard", "/dashboard"), ("tasks", "/tasks"),
                        ("smtp-accounts", "/delivery"), ("smtp-groups", "/delivery?tab=groups"),
                        ("users", "/settings/users"), ("password", "/account/password"),
                        ("task-edit", f"/tasks/{task_id}/edit"), ("task-advanced", f"/tasks/{task_id}/advanced")]
            surfaces += [("run-" + tab, f"/runs/{seed.run_id}?tab={tab}") for tab in ("overview", "email", "files", "logs")]
            b.cmd('/window/rect', {'width':1440,'height':1200})
            previous_errors = []
            for width, height in ((1280,1024), (402,874), (874,402)):
                if width == 402:
                    previous_errors = b.console_errors(); b.close(); browser = None
                    browser = b = Browser(output / 'dpr3', dpr=3)
                    b.cmd('/window/rect', {'width':1440,'height':1200})
                    b.navigate(base + '/login'); b.fill('#username','qa-admin'); b.fill('#password',ADMIN_PASSWORD)
                    b.click('form[action="/login"] button[type="submit"]')
                    b.wait(lambda: '/dashboard' in b.cmd('/url'), 'DPR3 login')
                    check('mobile profile uses DPR3', b.js('return devicePixelRatio') == 3)
                for label, route in surfaces:
                    b.navigate(base + "/__qa_viewport?" + urllib.parse.urlencode({"width":width,"height":height,"route":route}))
                    frame = b.element("#qa-viewport"); b.cmd("/frame", {"id": frame})
                    b.element("#main-content")
                    check(f'{width}x{height} {label} exact CSS viewport', b.js('return [innerWidth,innerHeight]') == [width,height])
                    check(f"{width}px {label} meaningful content", b.js("return document.querySelector('#main-content').textContent.trim().length>20"))
                    check(f"{width}px {label} no page overflow", b.js("return document.documentElement.scrollWidth<=innerWidth"))
                    if width < 900:
                        check(f'{width}px {label} mobile shell', b.js("return getComputedStyle(document.querySelector('.mobile-header')).display!=='none'&&getComputedStyle(document.querySelector('.app-sidebar')).visibility==='hidden'"))
                        check(f'{width}px {label} header stays within viewport', b.js("const r=document.querySelector('.mobile-header').getBoundingClientRect();return r.top>=-1&&r.bottom<=innerHeight"))
                    if label in {'users','password','smtp-accounts','task-edit','task-advanced'}:
                        check(f'{width}px {label} readable inputs', b.js("return [...document.querySelectorAll('input:not([type=hidden],[type=checkbox],[type=radio],[type=file]),select,textarea')].filter(e=>e.offsetParent!==null).every(e=>parseFloat(getComputedStyle(e).fontSize)>=" + ('16' if width < 900 else '13') + ")"))
                        if width < 900:
                            check(f'{width}px {label} 44px input targets', b.js("return [...document.querySelectorAll('input:not([type=hidden],[type=checkbox],[type=radio],[type=file]),select,textarea')].filter(e=>e.offsetParent!==null).every(e=>e.getBoundingClientRect().height>=44)"))
                    if label == 'dashboard' and width < 900:
                        b.click('[data-menu-toggle]')
                        check(f'{width}px menu opens and its links remain reachable', b.js("return document.querySelector('[data-menu-toggle]').getAttribute('aria-expanded')==='true'&&getComputedStyle(document.querySelector('.app-sidebar')).visibility==='visible'"))
                        b.click('[data-menu-toggle]')
                    if label == 'run-files':
                        toggle = b.element('[data-size-toggle]')
                        if width < 900:
                            check(f'{width}px exact size has 44px target',b.js('return arguments[0].getBoundingClientRect().height>=44',toggle))
                        b.click('[data-size-toggle]')
                        check(f'{width}px exact size click reveals original byte count',b.js("const button=arguments[0],exact=document.getElementById(button.getAttribute('aria-controls'));return button.getAttribute('aria-expanded')==='true'&&!exact.hidden&&exact.textContent==='('+button.closest('.size-value').title+')'",toggle))
                        check(f'{width}px expanded exact size does not overflow page',b.js('return document.documentElement.scrollWidth<=innerWidth'))
                        b.cmd('/frame',{'id':None}); b.screenshot(f'exact-size-{width}x{height}.png',frame); b.cmd('/frame',{'id':frame})
                        old_size_target=b.js("window.__sizeQaPage=true;return arguments[0].getAttribute('aria-controls')",toggle)
                        b.click('[data-refresh-now]')
                        b.wait(lambda:b.js("return document.querySelector('[data-size-toggle]').getAttribute('aria-controls')!==arguments[0]",old_size_target),'size partial refresh')
                        toggle=b.element('[data-size-toggle]')
                        check(f'{width}px exact size remains expanded after partial refresh',b.js("return window.__sizeQaPage&&arguments[0].getAttribute('aria-expanded')==='true'&&!document.getElementById(arguments[0].getAttribute('aria-controls')).hidden",toggle))
                        b.cmd('/element/'+toggle[ELEMENT]+'/value',{'text':'\ue007'})
                        check(f'{width}px exact size keyboard Enter collapses it',b.js("return arguments[0].getAttribute('aria-expanded')==='false'&&document.getElementById(arguments[0].getAttribute('aria-controls')).hidden",toggle))
                        b.js('window.scrollTo(0,0)')
                    if frame:
                        b.cmd("/frame", {"id": None})
                    b.screenshot(f"{label}-{width}x{height}.png", frame)
                    if label == 'smtp-accounts':
                        b.cmd('/frame', {'id':frame})
                        b.click('#sender-accounts .actions-cell a[href$="#sender-form"]')
                        b.element('#smtp-password')
                        if width < 900:
                            check(f'{width}px sender edit anchor clears sticky header', b.js("return document.querySelector('#sender-form').getBoundingClientRect().top>=document.querySelector('.mobile-header').getBoundingClientRect().bottom"))
                        check(f'{width}px app password label', b.js("return document.querySelector('label[for=smtp-password]').textContent==='앱 비밀번호'"))
                        b.cmd('/frame',{'id':None}); b.screenshot(f'smtp-edit-{width}x{height}.png',frame)
            b.navigate(base + '/dashboard'); b.click('form[action="/logout"] button')
            b.wait(lambda: "/login" in b.cmd("/url"), "final logout")
            check("logout returns to login and revokes access", b.fetch("/api/session")["status"] == 401)
            time.sleep(.2)
            errors = previous_errors + b.console_errors()
            report["console_errors"] = errors
            check("browser console has no application warnings or errors", not errors)
            check("SMTP never connected", smtp.call_count == 0 and smtp_ssl.call_count == 0)
            check("no business run enqueued by browser QA", len(app.runs.list_runs(limit=100)) == before_runs)
            report["status"] = "passed"
        except Exception as error:
            report["status"], report["failure"] = "failed", str(error)
            if browser:
                try:
                    browser.screenshot("failure.png"); report["console_errors"] = browser.console_errors()
                    report["failure_url"] = browser.cmd("/url")
                except Exception:
                    pass
            raise
        finally:
            report["count"] = len(checks)
            (output / "checks.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
            if browser:
                browser.close()
            server.shutdown(); server.server_close(); server_thread.join(timeout=5)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--package-root", type=Path)
    args = parser.parse_args()
    missing = [name for name in ("firefox", "geckodriver", "node") if shutil.which(name) is None]
    if missing:
        raise SystemExit("Required installed tools missing: " + ", ".join(missing))
    run(args.output.resolve(), args.package_root.resolve() if args.package_root else None)
