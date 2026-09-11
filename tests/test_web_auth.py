"""Real session/CSRF and object permissions across the complete HTTP adapter."""

import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from researchops.services.application import ApplicationService
from researchops.services.web_access_service import WebAccessService
from researchops.web.router import WebRouter
from tests.support import isolated_settings, register_fixture_task, fixture_runner

PASSWORD = "Synthetic admin password 2026!"
TEMPORARY = "Temporary viewer password 2026!"
VIEW_PASSWORD = "Permanent viewer password 2026!"


class WebAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = isolated_settings(Path(self.tmp.name))
        self.app = ApplicationService(self.settings,custom_runner=fixture_runner(self.settings))
        register_fixture_task(self.app)
        self.router = WebRouter(self.app)

    def tearDown(self):
        self.tmp.cleanup()

    def admin(self):
        return self.app.auth.setup(self.app.auth.issue_setup_token(),"administrator",PASSWORD)

    def viewer(self, admin, grants=()):
        record = self.app.auth.create_user(admin.session.principal,"viewer","조사 조회자","viewer",TEMPORARY,grants)
        temp = self.app.auth.login("viewer",TEMPORARY)
        return record, self.app.auth.change_password(temp.session.principal,TEMPORARY,VIEW_PASSWORD)

    @staticmethod
    def headers(grant=None, *, https=False):
        headers = {"Host":"localhost","Origin":"https://localhost" if https else "http://localhost"}
        if https:
            headers["X-Forwarded-Proto"] = "https"
        if grant:
            name = "__Host-researchops_session" if https else "researchops_local_session"
            headers.update(Cookie=name+"="+grant.token, **{"X-CSRF-Token":grant.session.csrf_token})
        return headers

    def request(self,path,grant=None,*,method="GET",data=None,headers=None):
        return self.router.handle_request(method,path,urlencode(data or {},doseq=True).encode(),
            "application/x-www-form-urlencoded",headers=headers or self.headers(grant))

    def test_setup_login_and_secure_cookie_rotation(self):
        self.assertEqual(self.request("/dashboard")[0],302)
        self.assertEqual(self.request("/api/session")[0],401)
        code,headers,body = self.request("/setup",headers=self.headers(https=True))
        self.assertEqual(code,200)
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"',body)[1].decode()
        cookie = headers["Set-Cookie"].split(";",1)[0]
        token = self.app.auth.issue_setup_token()
        headers = {**self.headers(https=True),"Cookie":cookie}
        code,response,_ = self.request("/setup",method="POST",data={"setup_token":token,"username":"administrator","password":PASSWORD,"csrf_token":csrf,"next":"//evil.invalid"},headers=headers)
        self.assertEqual(code,303)
        self.assertEqual(response["Location"],"/dashboard")
        self.assertIn("Secure",response["Set-Cookie"])
        self.assertIn("HttpOnly",response["Set-Cookie"])
        self.assertIn("SameSite=Lax",response["Set-Cookie"])
        self.assertNotEqual(response["Set-Cookie"].split(";",1)[0],cookie)
        self.assertIsNone(self.app.auth.resolve_session(cookie.split("=",1)[1]))
        self.assertNotIn(PASSWORD.encode(),body)

    def test_viewer_grants_apply_to_lists_api_archives_and_mutations(self):
        admin = self.admin()
        user,viewer = self.viewer(admin,["software-releases"])
        run = self.app.runs.enqueue_run("software-releases")
        root = self.settings.paths.run_archive_dir / "software-releases" / run.run_id
        root.mkdir(parents=True)
        (root/"email.txt").write_text("Visible synthetic report")
        with self.app.db.transaction() as conn:
            conn.execute("INSERT INTO tasks(task_id,updated_at) VALUES('hidden-task','2026-09-11')")
            conn.execute("INSERT INTO entity_catalog(kind,legacy_key,display_name,created_at,updated_at) VALUES('task','hidden-task','CONFIDENTIAL TASK','2026-09-11','2026-09-11')")
            conn.execute("INSERT INTO task_versions(version_hash,task_id,definition_json,package_files_json,sealed_at) VALUES('hidden-version','hidden-task','{}','{}','2026-09-11')")
            columns = run.to_dict()
            columns.update(run_id="hidden-run",task_id="hidden-task",task_version_hash="hidden-version")
            conn.execute("INSERT INTO scheduled_runs("+",".join(columns)+") VALUES("+",".join("?" for _ in columns)+")",list(columns.values()))
        for path in ("/dashboard","/tasks","/runs","/tasks/software-releases",f"/runs/{run.run_id}",f"/runs/{run.run_id}/preview/text",f"/runs/{run.run_id}/artifacts/email.txt",f"/api/runs/{run.run_id}/status"):
            with self.subTest(path=path):
                code,_,body = self.request(path,viewer)
                self.assertEqual(code,200)
                self.assertNotIn(b"CONFIDENTIAL TASK",body)
                self.assertNotIn(b"hidden-run",body)
                if "text/html" not in str(_):
                    continue
                self.assertNotIn(b'action="/tasks/software-releases/run"',body)
        for path in ("/tasks/hidden-task","/runs/hidden-run","/runs/hidden-run/preview/text","/runs/hidden-run/artifacts/email.txt","/api/runs/hidden-run/status"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path,viewer)[0],404)
        for path in ("/delivery","/doctor","/tasks/new","/tasks/software-releases/edit","/settings/users","/api/task-options","/api/model-catalog"):
            self.assertEqual(self.request(path,viewer)[0],403,path)
        for path in ("/tasks/software-releases/run",f"/runs/{run.run_id}/cancel",f"/runs/{run.run_id}/send-email","/delivery/save"):
            self.assertEqual(self.request(path,viewer,method="POST")[0],403,path)
        self.app.auth.update_user(admin.session.principal,user["user_id"],task_ids=[])
        self.assertEqual(self.request(f"/runs/{run.run_id}",viewer)[0],404)
        self.assertNotIn(b"software-releases",self.request("/tasks",viewer)[2])

    def test_must_change_password_blocks_business_and_disable_expires_cookie(self):
        admin = self.admin()
        user = self.app.auth.create_user(admin.session.principal,"reader","Reader","viewer",TEMPORARY,[])
        login = self.app.auth.login("reader",TEMPORARY)
        self.assertEqual(self.request("/dashboard",login)[1]["Location"],"/account/password")
        self.assertEqual(self.request("/api/runs/unknown/status",login)[0],403)
        self.assertEqual(self.request("/account/password",login)[0],200)
        self.assertIn(b'action="/account/password"',self.request("/account/password",login)[2])
        self.app.auth.update_user(admin.session.principal,user["user_id"],active=False)
        self.assertEqual(self.request("/api/session",login)[0],401)

    def test_session_csrf_isolation_duplicate_cookie_and_logout(self):
        admin = self.admin()
        _,viewer = self.viewer(admin)
        headers = self.headers(admin)
        headers["X-CSRF-Token"] = viewer.session.csrf_token
        self.assertEqual(self.request("/tasks/software-releases/enable",method="POST",headers=headers)[0],403)
        headers = self.headers(admin)
        headers["Cookie"] += "; " + headers["Cookie"]
        self.assertEqual(self.request("/dashboard",headers=headers)[0],400)
        self.assertEqual(self.request("/logout",admin,method="POST")[0],303)
        self.assertEqual(self.request("/api/session",admin)[0],401)

    def test_polling_does_not_extend_idle_and_admin_ui_creates_scoped_user(self):
        admin = self.admin()
        original = self.app.auth.resolve_session(admin.token,touch=False).last_seen_at
        with patch.object(self.app.auth,"_clock",return_value=original+300):
            self.assertEqual(self.request("/api/session",admin)[0],200)
            self.assertEqual(self.app.auth.resolve_session(admin.token,touch=False).last_seen_at,original)
        code,_,body = self.request("/settings/users",admin)
        self.assertEqual(code,200)
        self.assertIn(b'name="task_ids"',body)
        self.assertEqual(self.request("/settings/users",admin,method="POST",data={"username":"web-reader","display_name":"Web reader","role":"viewer","temporary_password":TEMPORARY,"task_ids":["software-releases"]})[0],303)
        user = next(u for u in self.app.auth.list_users(admin.session.principal) if u["username"] == "web-reader")
        self.assertEqual(user["task_ids"],["software-releases"])

    def test_production_cookie_requires_https_unless_explicit_isolated_loopback(self):
        self.settings.environment = "production"
        self.assertEqual(self.request("/login")[0],403)
        self.assertIn(self.request("/login",headers=self.headers(https=True))[0],(200,302))
        self.settings.web.allow_insecure_local_auth = True
        self.assertIn(self.request("/login")[0],(200,302))

    def test_permission_scope_precedes_pagination_search_and_totals(self):
        admin = self.admin()
        _,viewer = self.viewer(admin,["software-releases"])
        access = WebAccessService(self.app)
        with self.app.db.transaction() as conn:
            for index in range(30):
                conn.execute("INSERT INTO tasks(task_id,updated_at) VALUES(?,?)",(f"hidden-{index}","2026-09-11"))
        result = access.tasks(viewer.session.principal,page_size=1)
        self.assertEqual(result["total"],1)
        self.assertEqual(result["items"][0]["task_id"],"software-releases")
        self.assertEqual(access.tasks(viewer.session.principal,q="hidden")["total"],0)
        self.assertEqual(access.tasks(viewer.session.principal,page=2,page_size=1)["items"],[])

    def test_bounded_raw_log_windows_preserve_unicode_and_original_bytes(self):
        admin = self.admin()
        run = self.app.runs.enqueue_run("software-releases")
        root = self.settings.paths.run_archive_dir / "software-releases" / run.run_id / "logs"
        root.mkdir(parents=True)
        raw = ("x" * 65535 + "한글 로그\n" * 14000).encode() + b"\xe2\x82"
        path = root / "research.stdout"
        path.write_bytes(raw)
        offset, text = 0, ""
        while offset < len(raw):
            code,_,body = self.request(f"/api/runs/{run.run_id}/logs?file=research.stdout&offset={offset}",admin)
            self.assertEqual(code,200)
            value = json.loads(body)
            self.assertLessEqual(value["next_offset"] - offset,65536)
            self.assertGreater(value["next_offset"],offset)
            text += value["text"]
            offset = value["next_offset"]
        self.assertEqual(text,raw.decode("utf-8",errors="replace"))
        self.assertEqual(path.read_bytes(),raw)
        for file in ("../../secret", "email.html"):
            self.assertEqual(self.request(f"/api/runs/{run.run_id}/logs?"+urlencode({"file":file}),admin)[0],400)


if __name__ == "__main__":
    unittest.main()
