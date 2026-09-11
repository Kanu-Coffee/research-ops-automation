"""Operator form interactions using the real router/services, without live side effects."""

from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from urllib.parse import urlencode

from researchops.delivery.smtp_config import BuiltinDeliveryConfig, SmtpSettings, load_delivery_config
from researchops.errors import ValidationError
from researchops.services.application import ApplicationService
from tests.web_support import AuthenticatedWebRouter as WebRouter, admin_session, authenticated_headers
from researchops.web.schedule_form import cron_from_form, schedule_fields, schedule_summary
from tests.support import isolated_settings


class OperatorForms(HTMLParser):
    """Collect actual rendered successful controls for POST navigation checks."""
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.forms, self.current, self.select, self.textarea = [], None, None, None
        self.feed(body.decode() if isinstance(body, bytes) else body)

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "form":
            self.current = {"action": attr.get("action"), "fields": {}}
            self.forms.append(self.current)
        if self.current is None:
            return
        fields = self.current["fields"]
        if tag == "input" and attr.get("name") and "disabled" not in attr:
            if attr.get("type") not in ("checkbox", "radio") or "checked" in attr:
                fields[attr["name"]] = attr.get("value", "")
        elif tag == "select":
            self.select = attr.get("name") if "disabled" not in attr else None
        elif tag == "option" and self.select:
            if self.select not in fields or "selected" in attr:
                fields[self.select] = attr.get("value", "")
        elif tag == "textarea":
            self.textarea = attr.get("name")
            fields[self.textarea] = ""

    def handle_endtag(self, tag):
        if tag == "form": self.current = None
        if tag == "select": self.select = None
        if tag == "textarea": self.textarea = None

    def handle_data(self, data):
        if self.current is not None and self.textarea:
            self.current["fields"][self.textarea] += data

    def find(self, action):
        return next(form["fields"] for form in self.forms if form["action"] == action)


class ScheduleFormTests(unittest.TestCase):
    def test_all_presets_validate_and_round_trip(self):
        for preset, extra, expected in [
            ("daily", {}, "45 8 * * *"), ("weekdays", {}, "45 8 * * 1-5"),
            ("weekly", {"schedule_weekday":"0"}, "45 8 * * 0"),
            ("monthly", {"schedule_monthday":"31"}, "45 8 31 * *"),
            ("hourly", {"schedule_minute":"17"}, "17 * * * *"),
            ("custom", {"cron":"*/15 8-18 * * 1,3,5"}, "*/15 8-18 * * 1,3,5"),
        ]:
            with self.subTest(preset=preset):
                fields = {"schedule_preset":preset,"schedule_time":"08:45", **extra}
                self.assertEqual(cron_from_form(fields.get), expected)
                detected = schedule_fields(expected)
                self.assertEqual(detected["schedule_preset"], preset)
                self.assertEqual(cron_from_form(detected.get), expected)
                self.assertIn("서울 시간", schedule_summary(expected))

    def test_invalid_values_rejected_even_without_javascript(self):
        for fields in [{"schedule_preset":"unexpected"},
                       {"schedule_preset":"daily","schedule_time":"24:00"},
                       {"schedule_preset":"weekly","schedule_weekday":"7"},
                       {"schedule_preset":"monthly","schedule_monthday":"0"},
                       {"schedule_preset":"hourly","schedule_minute":"60"},
                       {"schedule_preset":"custom","cron":"* * *"}]:
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                cron_from_form(fields.get)


class TaskEditorWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = isolated_settings(Path(self.tmp.name))
        self.settings.environment = "production"
        self.settings.delivery.global_handoff_kill_switch = False
        self.app = ApplicationService(self.settings)
        self.app.delivery.save_delivery_config(BuiltinDeliveryConfig(enabled=True,
            smtp=SmtpSettings(username="default@example.test",sender_email="default@example.test",password="default-secret"),
            recipient_groups={"research-team":["reader@example.test"]}))
        self.router = WebRouter(self.app, inject_csrf=False)

    def request(self, method, path, fields=None):
        return self.router.handle_request(method,path,urlencode(fields or {}).encode(),
            "application/x-www-form-urlencoded", headers={"Host":"localhost","Origin":"http://localhost"})

    def get_form(self, path, action):
        status, headers, body = self.request("GET",path)
        self.assertEqual(status,200)
        self.assertEqual(headers["Referrer-Policy"],"same-origin")
        return OperatorForms(body).find(action)

    def create(self, task_id="operator-task", **changes):
        fields = self.get_form("/tasks/new","/tasks/production/create")
        fields.update(task_id=task_id,name="운영 연구",task_md="# 조사\n공식 공개 출처를 확인합니다.\n",
            email_spec_md="# 메일 규격\n제목과 표, 출처를 포함합니다.\n",recipient_group_id="research-team",action="create")
        fields.update(changes)
        response = self.request("POST","/tasks/production/create",fields)
        self.assertEqual(response[0],303, response[2][:100])
        self.assertIn("success=",response[1]["Location"])
        return fields

    def test_create_edit_keeps_distinct_files_id_history_and_detects_stale_editor(self):
        created = self.create()
        detail = self.request("GET","/tasks/operator-task")[2]
        self.assertIn(b'href="/tasks/operator-task/edit"',detail)
        self.assertIn(b'href="/tasks/new?clone=operator-task"',detail)
        fields = self.get_form("/tasks/operator-task/edit","/tasks/operator-task/edit")
        self.assertEqual(fields["task_md"],created["task_md"])
        self.assertEqual(fields["email_spec_md"],created["email_spec_md"])
        old_hash=fields["expected_version_hash"]
        fields["task_md"] += "오탈자를 수정했습니다.\n"
        fields["email_spec_md"] += "제목은 짧게 작성합니다.\n"
        response=self.request("POST","/tasks/operator-task/edit",fields)
        self.assertEqual(response[0],303)
        saved=self.app.tasks.get_task_editor("operator-task")
        self.assertEqual(saved["task_md"],fields["task_md"])
        self.assertEqual(saved["email_spec_md"],fields["email_spec_md"])
        self.assertNotEqual(saved["expected_version_hash"],old_hash)
        self.assertEqual(len(self.app.tasks.show_task("operator-task")["versions"]),2)
        self.assertEqual(self.app.runs.list_runs(task_id="operator-task"),[])
        fields["name"]="stale changes retained in form"
        response=self.request("POST","/tasks/operator-task/edit",fields)
        self.assertEqual(response[0],400)
        self.assertIn(b'stale changes retained in form',response[2])
        self.assertEqual(self.app.tasks.get_task_editor("operator-task")["name"],saved["name"])

    def test_clone_loads_real_content_without_mutation_then_creates_schedule_off_copy(self):
        self.create(research_provider="antigravity_exec",compose_provider="antigravity_exec",schedule_preset="weekly",schedule_weekday="2")
        before=self.app.tasks.get_task_editor("operator-task")
        fields=self.get_form("/tasks/new?clone=operator-task","/tasks/production/create")
        self.assertEqual(fields["task_id"],"")
        self.assertEqual(fields["task_md"],before["task_md"])
        self.assertEqual(fields["email_spec_md"],before["email_spec_md"])
        self.assertEqual(fields["research_provider"],"antigravity_exec")
        self.assertEqual(fields["compose_provider"],"antigravity_exec")
        self.assertEqual(fields["schedule_weekday"],"2")
        self.assertNotIn("schedule_enabled",fields)
        self.assertEqual(len(self.app.tasks.list_tasks()),1)
        fields.update(task_id="cloned-task",name="복제된 조사",action="create")
        response=self.request("POST","/tasks/production/create",fields)
        self.assertEqual(response[0],303)
        clone=self.app.tasks.get_task_editor("cloned-task")
        self.assertEqual(clone["task_md"],before["task_md"])
        self.assertEqual(clone["email_spec_md"],before["email_spec_md"])
        self.assertFalse(clone["schedule_enabled"])
        self.assertEqual(clone["cron"],before["cron"])
        self.assertEqual(self.app.tasks.get_task_editor("operator-task"),before)

    def test_custom_cron_survives_editor_round_trip_and_errors_preserve_input(self):
        cron="*/15 8-18 * * 1,3,5"
        self.create(schedule_preset="custom",cron=cron)
        fields=self.get_form("/tasks/operator-task/edit","/tasks/operator-task/edit")
        self.assertEqual(fields["schedule_preset"],"custom")
        self.assertEqual(fields["cron"],cron)
        fields["name"]="renamed"
        self.assertEqual(self.request("POST","/tasks/operator-task/edit",fields)[0],303)
        self.assertEqual(self.app.tasks.get_task_editor("operator-task")["cron"],cron)
        fields=self.get_form("/tasks/new","/tasks/production/create")
        fields.update(task_id="invalid-schedule",name="보존할 이름",task_md="작성한 조사내용을 잃지 않습니다.",
            recipient_group_id="research-team",schedule_preset="weekly",schedule_weekday="17")
        response=self.request("POST","/tasks/production/create",fields)
        self.assertEqual(response[0],400)
        self.assertIn("작성한 조사내용을 잃지 않습니다.".encode(),response[2])
        self.assertIsNone(self.app.task_repo.get_active_version("invalid-schedule"))

    def test_account_create_select_and_blank_secret_update_are_profile_scoped(self):
        fields=self.get_form("/delivery?new_sender=true","/delivery/save")
        self.assertEqual(fields["username"],"")
        fields.update(sender_profile_id="second-gmail",username="second@example.test",sender_email="second@example.test",
            sender_name="별도 발신자",password="second-secret")
        response=self.request("POST","/delivery/save",fields)
        self.assertEqual(response[0],303)
        self.assertIn("sender=second-gmail",response[1]["Location"])
        fields=self.get_form("/delivery?sender=second-gmail","/delivery/save")
        self.assertEqual(fields["password"],"")
        self.assertEqual(fields["username"],"second@example.test")
        fields["sender_name"]="이름만 변경"
        response=self.request("POST","/delivery/save",fields)
        self.assertEqual(response[0],303)
        cfg=load_delivery_config(self.settings.paths.delivery_config_file)
        self.assertEqual(cfg.smtp.password,"default-secret")
        self.assertEqual(cfg.get_sender("second-gmail").password,"second-secret")
        for route in ("/delivery","/delivery?sender=second-gmail","/tasks/new"):
            body=self.request("GET",route)[2]
            self.assertNotIn(b'default-secret',body)
            self.assertNotIn(b'second-secret',body)
        self.create(sender_profile_id="second-gmail")
        fields=self.get_form("/tasks/operator-task/edit","/tasks/operator-task/edit")
        self.assertEqual(fields["sender_profile_id"],"second-gmail")

    def test_missing_sender_is_actionable_and_retains_both_task_documents(self):
        fields=self.get_form("/tasks/new","/tasks/production/create")
        fields.update(task_id="missing-sender",name="계정 선택 복구",task_md="보존할 조사 내용입니다.",
            email_spec_md="보존할 메일 규격입니다.",recipient_group_id="research-team",sender_profile_id="not-registered")
        response=self.request("POST","/tasks/production/create",fields)
        self.assertEqual(response[0],400)
        self.assertIn(fields["task_md"].encode(),response[2])
        self.assertIn(fields["email_spec_md"].encode(),response[2])
        self.assertNotIn(b'value="not-registered"',response[2])
        retained = OperatorForms(response[2]).find("/tasks/production/create")
        self.assertNotEqual(retained["sender_profile_id"], "not-registered")
        self.assertEqual(self.request("GET","/delivery?sender=not-registered")[0],404)

    def test_sender_save_errors_preserve_nonsecret_fields_and_clear_password(self):
        for create, profile_id, port in [(True,"default","587"),(True,"new-account","banana"),
                                         (False,"default","70000")]:
            with self.subTest(create=create,profile_id=profile_id,port=port):
                fields=self.get_form("/delivery?new_sender=true" if create else "/delivery","/delivery/save")
                fields.update(create_sender="true" if create else "false",sender_profile_id=profile_id,
                    username="preserved@example.test",sender_email="sender@example.test",sender_name="보존할 발신자",
                    host="smtp.gmail.com",port=port,password="secret-never-rerender")
                before=load_delivery_config(self.settings.paths.delivery_config_file).to_dict()
                response=self.request("POST","/delivery/save",fields)
                self.assertEqual(response[0],400)
                self.assertEqual(response[1]["Referrer-Policy"],"same-origin")
                self.assertNotIn(b'secret-never-rerender',response[2])
                self.assertIn("새 비밀번호는 다시 입력하세요".encode(),response[2])
                retained=OperatorForms(response[2]).find("/delivery/save")
                for key in ("username","sender_email","sender_name","host","port","sender_profile_id","create_sender"):
                    self.assertEqual(retained[key],fields[key])
                self.assertEqual(retained["password"],"")
                self.assertEqual(load_delivery_config(self.settings.paths.delivery_config_file).to_dict(),before)

    def test_unknown_sender_edit_has_same_not_found_response_as_inaccessible_sender(self):
        fields = self.get_form("/delivery?sender=default", "/delivery/save")
        fields.update(sender_profile_id="not-registered", password="secret-never-rerender")
        before = self.settings.paths.delivery_config_file.read_bytes()
        response = self.request("POST", "/delivery/save", fields)
        self.assertEqual(response[0], 404)
        self.assertNotIn(b"secret-never-rerender", response[2])
        self.assertEqual(self.settings.paths.delivery_config_file.read_bytes(), before)

    def test_retired_working_copy_routes_return_gone_without_mutation(self):
        self.create()
        token = self.get_form("/tasks/operator-task/edit", "/tasks/operator-task/edit")["csrf_token"]
        for method, path in (("GET", "/tasks/drafts"), ("GET", "/tasks/drafts/legacy"),
                             ("POST", "/tasks/drafts/create"), ("POST", "/tasks/drafts/legacy/form"),
                             ("POST", "/tasks/drafts/legacy/save"), ("POST", "/tasks/drafts/legacy/validate"),
                             ("POST", "/tasks/drafts/legacy/seal"), ("POST", "/tasks/drafts/legacy/publish")):
            with self.subTest(method=method, path=path):
                response = self.request(method, path, {"csrf_token": token})
                self.assertEqual(response[0], 410)
        with self.app.db.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_drafts").fetchone()[0], 0)
        self.assertEqual(len(self.app.tasks.list_tasks()), 1)
        self.assertEqual(self.app.runs.list_runs(), [])
        body = self.request("GET", "/tasks")[2]
        self.assertNotIn(b"/tasks/drafts", body)
        self.assertNotIn("임시 저장".encode(), body)

    def test_advanced_get_reads_current_documents_and_guards_without_mutation(self):
        created = self.create()
        route = "/tasks/operator-task/advanced"
        before = self.app.task_repo.get_task_status("operator-task")
        version = self.app.task_repo.get_active_version("operator-task")
        fields = self.get_form(route, route)
        self.assertEqual(fields["task_md"], created["task_md"])
        self.assertEqual(fields["email_spec_md"], created["email_spec_md"])
        self.assertEqual(fields["config_yaml"], version.package_files["task.yaml"])
        self.assertEqual(fields["expected_version_hash"], version.version_hash)
        self.assertEqual(fields["expected_updated_at"], before["updated_at"])
        self.assertEqual(self.app.task_repo.get_task_status("operator-task"), before)
        self.assertEqual(len(self.app.task_repo.list_versions("operator-task")), 1)

    def test_advanced_direct_save_preserves_identity_documents_and_schedule_without_running(self):
        self.create()
        self.app.tasks.set_task_enabled("operator-task", True)
        route = "/tasks/operator-task/advanced"
        fields = self.get_form(route, route)
        old_hash = fields["expected_version_hash"]
        fields.update(task_md="# 고친 조사\n원문 수정.\n", email_spec_md="# 메일 규칙\n별도 문서.\n")
        fields["config_yaml"] += "\n# Keep exact YAML bytes.\n"
        response = self.request("POST", route, fields)
        self.assertEqual(response[0], 303, response[2][:200])
        self.assertTrue(response[1]["Location"].startswith("/tasks/operator-task?success="))
        active = self.app.task_repo.get_active_version("operator-task")
        for name, field in (("task.yaml", "config_yaml"), ("task.md", "task_md"), ("email_spec.md", "email_spec_md")):
            self.assertEqual(active.package_files[name], fields[field])
        self.assertNotEqual(active.version_hash, old_hash)
        self.assertTrue(self.app.task_repo.get_task_status("operator-task")["enabled"])
        self.assertEqual(self.app.runs.list_runs(task_id="operator-task"), [])
        self.assertEqual(len(self.app.task_repo.list_versions("operator-task")), 2)
        with self.app.db.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_drafts").fetchone()[0], 0)

    def test_advanced_stale_and_invalid_submissions_keep_input_and_request_key(self):
        self.create()
        route = "/tasks/operator-task/advanced"
        fields = self.get_form(route, route)
        newer = dict(fields, task_md="다른 창의 최신 내용")
        self.assertEqual(self.request("POST", route, newer)[0], 303)
        fields["task_md"] = "보존해야 하는 입력"
        response = self.request("POST", route, fields)
        self.assertEqual(response[0], 400)
        retained = OperatorForms(response[2]).find(route)
        for key in ("task_md", "config_yaml", "email_spec_md", "expected_version_hash", "expected_updated_at", "request_key"):
            self.assertEqual(retained[key], fields[key])
        self.assertEqual(self.app.task_repo.get_active_version("operator-task").package_files["task.md"], newer["task_md"])
        fields = self.get_form(route, route)
        fields.update(config_yaml="invalid: [yaml", task_md="YAML 오류에도 내용 보존")
        response = self.request("POST", route, fields)
        self.assertEqual(response[0], 400)
        self.assertEqual(OperatorForms(response[2]).find(route)["config_yaml"], fields["config_yaml"])
        self.assertEqual(self.app.task_repo.get_active_version("operator-task").package_files["task.md"], newer["task_md"])

    def test_advanced_form_round_trip_through_real_http_transport(self):
        import http.client
        import threading
        from researchops.web.server import create_web_server
        self.create()
        route = "/tasks/operator-task/advanced"
        server = create_web_server(self.app, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            conn.request("GET", route, headers=authenticated_headers(self.app, {"Host": "localhost"}))
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            fields = OperatorForms(response.read()).find(route)
            fields["task_md"] = "# HTTP에서 수정한 원문\n두 문서 분리 유지.\n"
            conn.request("POST", route, urlencode(fields), headers=authenticated_headers(self.app, {"Host": "localhost",
                "Origin": "http://localhost", "Content-Type": "application/x-www-form-urlencoded"}, csrf=False))
            response = conn.getresponse()
            self.assertEqual(response.status, 303)
            response.read()
            self.assertEqual(self.app.task_repo.get_active_version("operator-task").package_files["task.md"], fields["task_md"])
            self.assertEqual(self.app.runs.list_runs(task_id="operator-task"), [])
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @unittest.skipUnless(shutil.which("node"), "Node is unavailable for embedded JavaScript syntax check")
    def test_editor_javascript_has_valid_syntax_without_browser_dependencies(self):
        body=self.request("GET","/tasks/new")[2].decode()
        scripts=re.findall(r"<script>(.*?)</script>",body,re.S)
        self.assertGreaterEqual(len(scripts),2)
        for script in scripts:
            result=subprocess.run(["node","--check"],input=script,text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('data-import-target="task-md"',body)
        self.assertIn('data-import-target="email-spec-md"',body)

    @unittest.skipUnless(shutil.which("node"), "Node is unavailable for isolated JavaScript interaction check")
    def test_javascript_schedule_changes_and_two_file_import_controls(self):
        import json
        body=self.request("GET","/tasks/new")[2].decode()
        script=next(script for script in re.findall(r"<script>(.*?)</script>",body,re.S)
                    if "const preset = document.getElementById('schedule-preset')" in script)
        harness=r'''
const vm = require('node:vm'), assert = require('node:assert/strict');
const el=(value='',text='')=>({value,textContent:text,hidden:false,options:[{text:'매일'}],selectedIndex:0,
  callbacks:{},addEventListener(type,handler){this.callbacks[type]=handler;}});
const nodes={'schedule-preset':el('daily'),'schedule-time':el('09:00'),'schedule-weekday':el('2'),
 'schedule-monthday':el('31'),'schedule-minute':el('17'),'schedule-cron':el('*/15 8-18 * * 1,3,5'),
 'schedule-summary':el(),'schedule-editor':el(),'task-md':el(),'email-spec-md':el(),'file-import-status':el(),
 'recipient-routing-mode':el('catalog_name'),'recipient-legacy-fields':el(),
 'recipient-catalog-fields':el(),'task-recipient':el()};
nodes['schedule-weekday'].options=[{text:'화요일'}];
const schedule=['daily weekdays weekly monthly','weekly','monthly','hourly','custom'].map(data=>({...el(),dataset:{schedule:data}}));
const imports=['task-md','email-spec-md'].map(target=>({...el(),dataset:{importTarget:target},files:[]}));
const document={getElementById:id=>nodes[id],querySelectorAll:selector=>selector==='[data-schedule]'?schedule:imports};
vm.runInNewContext(SCRIPT,{document,TextDecoder,window:{confirm:()=>true}});
assert.equal(nodes['recipient-legacy-fields'].hidden,true);assert.equal(nodes['task-recipient'].required,false);
nodes['recipient-routing-mode'].value='legacy_ids';nodes['recipient-routing-mode'].callbacks.change();
assert.equal(nodes['recipient-catalog-fields'].hidden,true);assert.equal(nodes['task-recipient'].required,true);
nodes['recipient-routing-mode'].value='catalog_name';nodes['recipient-routing-mode'].callbacks.change();
assert.equal(nodes['recipient-catalog-fields'].hidden,false);assert.equal(nodes['task-recipient'].required,false);
assert.equal(nodes['schedule-summary'].textContent,'매일 09:00 · 서울 시간');
assert.equal(schedule[1].hidden,true);
nodes['schedule-preset'].value='weekly';nodes['schedule-preset'].options=[{text:'매주'}];
nodes['schedule-editor'].callbacks.change();
assert.equal(schedule[1].hidden,false);assert.equal(schedule[2].hidden,true);
assert.equal(nodes['schedule-summary'].textContent,'매주 화요일 09:00 · 서울 시간');
nodes['schedule-preset'].value='custom';nodes['schedule-editor'].callbacks.change();
assert.equal(schedule[4].hidden,false);
assert.equal(nodes['schedule-cron'].value,'*/15 8-18 * * 1,3,5');
(async()=>{
 for(const input of imports){
  input.files=[{name:input.dataset.importTarget+'.md',size:8,arrayBuffer:async()=>Buffer.from(input.dataset.importTarget+' 별도 문서')}];
  await input.callbacks.change();
  assert.equal(nodes[input.dataset.importTarget].value,input.dataset.importTarget+' 별도 문서');
 }
 const before=nodes['task-md'].value;
 imports[0].files=[{name:'large.md',size:500000,arrayBuffer:async()=>{throw new Error('must not read');}}];
 await imports[0].callbacks.change();
 assert.equal(nodes['task-md'].value,before);assert.ok(nodes['file-import-status'].textContent.includes('400 KB'));
 imports[0].files=[{name:'invalid.md',size:1,arrayBuffer:async()=>Buffer.from([255])}];
 await imports[0].callbacks.change();
 assert.equal(nodes['task-md'].value,before);assert.ok(nodes['file-import-status'].textContent.includes('UTF-8'));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result=subprocess.run(["node","-e",harness.replace("SCRIPT",json.dumps(script))],
            text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__ == "__main__":
    unittest.main()
