"""Real browser authorization for independent, trusted internal owners."""
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlencode, urlparse

from researchops.delivery.smtp_config import BuiltinDeliveryConfig, SmtpSettings, load_delivery_config
from researchops.services.application import ApplicationService
from researchops.services.ownership import creation_owner, entity_owner
from researchops.services.web_access_service import WebAccessService
from researchops.web.router import WebRouter
from tests.support import isolated_settings
from tests.test_task_editor_web import OperatorForms

PASSWORD = "Synthetic user browser password"
PERMANENT = "Permanent user browser password"


class UserWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = isolated_settings(Path(self.tmp.name))
        self.settings.environment = 'production'
        self.settings.web.allow_insecure_local_auth = True
        self.settings.delivery.global_handoff_kill_switch = False
        self.app = ApplicationService(self.settings)
        self.app.delivery.save_delivery_config(BuiltinDeliveryConfig(enabled=True,
            smtp=SmtpSettings(username='admin@example.test',password='admin-synthetic'),
            recipient_groups={'admin-private':['admin-reader@example.test']}))
        self.admin = self.app.auth.setup(self.app.auth.issue_setup_token(),'administrator',PASSWORD)
        self.users = {}
        for name in ('owner-a','owner-b'):
            self.app.auth.create_user(self.admin.session.principal,name,name,'user',PASSWORD,[])
            temp = self.app.auth.login(name,PASSWORD)
            self.users[name] = self.app.auth.change_password(temp.session.principal,PASSWORD,PERMANENT)
        self.a, self.b = self.users.values()
        self.router = WebRouter(self.app)

    def request(self,path,grant=None,*,method='GET',data=None):
        headers = {'Host':'localhost','Origin':'http://localhost'}
        if grant:
            headers.update(Cookie='researchops_local_session='+grant.token,
                **{'X-CSRF-Token':grant.session.csrf_token})
        return self.router.handle_request(method,path,urlencode(data or {},doseq=True).encode(),
            'application/x-www-form-urlencoded',headers=headers)

    def form(self,path,action,grant):
        code,_,body = self.request(path,grant)
        self.assertEqual(code,200,body[:100])
        return OperatorForms(body).find(action)

    def resources(self,grant):
        owner = grant.session.principal.user_id
        with creation_owner(owner):
            sender = self.app.delivery.create_sender_account('Sender '+owner,
                SmtpSettings(username=owner+'@example.test',password='Synthetic app secret'),request_key='same-sender-key')
            group = self.app.delivery.create_recipient_group('Group '+owner,[owner+'-reader@example.test'],request_key='same-group-key')
        fields = self.form('/tasks/new','/tasks/production/create',grant)
        fields.update(name='Task '+owner,task_md='Synthetic public research.',email_spec_md='Synthetic email.',
            sender_profile_id=sender,recipient_group_id=group,recipient_routing_mode='legacy_ids',
            launch_mode='save',request_key='same-task-key',owner_user_id=self.admin.session.principal.user_id,
            task_id='forged-global-id')
        response = self.request('/tasks/production/create',grant,method='POST',data=fields)
        self.assertEqual(response[0],303,response[2][:400])
        task = urlparse(response[1]['Location']).path.rsplit('/',1)[-1]
        self.assertNotEqual(task,'forged-global-id')
        self.assertEqual(entity_owner(self.app.db,'task',task),owner)
        duplicate = self.request('/tasks/production/create',grant,method='POST',data=fields)
        self.assertEqual(urlparse(duplicate[1]['Location']).path,'/tasks/'+task)
        return task,sender,group

    def test_empty_user_defaults_and_global_settings_are_private(self):
        for path in ('/dashboard','/tasks','/runs','/tasks/new','/delivery','/api/task-options'):
            response = self.request(path,self.a)
            self.assertEqual(response[0],200,response[2][:100])
            for private in (b'admin@example.test',b'admin-private',b'admin-reader@example.test'):
                self.assertNotIn(private,response[2])
        form = self.form('/delivery','/delivery/save',self.a)
        self.assertEqual(form['create_sender'],'true')
        self.assertEqual(form['sender_profile_id'],'')
        self.assertNotIn('enabled',form)
        form.update(display_name='First account',username='first@example.test',password='synthetic',enabled='false')
        response = self.request('/delivery/save',self.a,method='POST',data=form)
        self.assertEqual(response[0],303,response[2][:100])
        self.assertTrue(load_delivery_config(self.settings.paths.delivery_config_file).enabled)
        for path in ('/doctor','/settings/users'):
            self.assertEqual(self.request(path,self.a)[0],403)
        self.assertEqual(self.request('/delivery?sender=default',self.a)[0],404)

    def test_cross_owner_paths_forms_catalog_search_and_idempotency(self):
        a,sa,ga = self.resources(self.a)
        b,sb,gb = self.resources(self.b)
        self.assertNotEqual(a,b)
        self.assertNotEqual(sa,sb)
        self.assertNotEqual(ga,gb)
        run = self.app.runs.enqueue_run(b,request_key='synthetic-queued')
        for path in (f'/tasks/{b}',f'/tasks/{b}/edit',f'/tasks/{b}/advanced',f'/tasks/new?clone={b}',
                f'/api/task-options?task_id={b}',f'/delivery?sender={sb}',f'/delivery?panel=sender&task_id={b}',
                f'/runs/{run.run_id}',f'/api/runs/{run.run_id}/status',f'/api/runs/{run.run_id}/logs',
                f'/runs/{run.run_id}/artifacts/email.txt'):
            self.assertEqual(self.request(path,self.a)[0],404,path)
        for path,data in ((f'/tasks/{b}/delete',{}),(f'/runs/{run.run_id}/cancel',{}),
                ('/delivery/save',{'sender_profile_id':sb,'username':'forged@example.test'}),
                ('/delivery/test-connection',{'sender_profile_id':sb}),
                ('/delivery/send-test',{'sender_profile_id':sb,'to_email':'synthetic@example.test'}),
                ('/delivery/groups/add',{'group_id':gb,'email':'synthetic@example.test'}),
                (f'/delivery/catalog/recipient_group/{gb}/delete',{})):
            self.assertEqual(self.request(path,self.a,method='POST',data=data)[0],404,path)
        access = WebAccessService(self.app)
        for grant,task in ((self.a,a),(self.b,b)):
            page = access.tasks(grant.session.principal,page_size=1)
            self.assertEqual(page['total'],1)
            self.assertEqual(page['items'][0]['task_id'],task)
        self.assertEqual(access.tasks(self.a.session.principal,q=b)['total'],0)
        self.assertEqual(access.dashboard(self.a.session.principal)['counts']['running'],0)
        self.assertEqual(access.dashboard(self.b.session.principal)['counts']['running'],1)
        options = json.loads(self.request('/api/task-options',self.a)[2])
        self.assertEqual([s['id'] for s in options['senders']],[sa])
        for path in ('/dashboard','/tasks','/runs','/delivery'):
            self.assertNotIn(self.b.session.principal.user_id.encode(),self.request(path,self.a)[2])
        for path in ('/tasks/drafts','/tasks/drafts/old/edit'):
            self.assertEqual(self.request(path,self.a)[0],410)

    def test_admin_task_options_and_inline_create_use_task_owner(self):
        task,sender,group = self.resources(self.a)
        options = json.loads(self.request('/api/task-options?task_id='+task,self.admin)[2])
        self.assertEqual([s['id'] for s in options['senders']],[sender])
        self.assertEqual([g['id'] for g in options['groups']],[group])
        fields = self.form(f'/delivery?panel=group&task_id={task}','/delivery/groups/create',self.admin)
        fields.update(display_name='Admin creates for owner',emails='inline@example.test')
        response = self.request('/delivery/groups/create',self.admin,method='POST',data=fields)
        self.assertEqual(response[0],303)
        self.assertIn('task_id='+task,response[1]['Location'])
        created = next(x for x in self.app.catalog.list('recipient_group') if x['display_name']=='Admin creates for owner')
        self.assertEqual(created['owner_user_id'],self.a.session.principal.user_id)
        advanced = self.form('/tasks/'+task+'/advanced','/tasks/'+task+'/advanced',self.a)
        advanced['task_md'] += '\nChanged without a draft.'
        saved = self.request('/tasks/'+task+'/advanced',self.a,method='POST',data=advanced)
        self.assertEqual(saved[0],303,saved[2][:200])
        self.assertEqual(self.app.tasks.get_task_advanced_editor(task)['task_md'],advanced['task_md'])
        self.assertEqual(self.app.runs.list_runs(),[])

    def test_brand_assets_are_public_without_preauth_session(self):
        with self.app.db.get_connection() as conn:
            before = conn.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0]
        for path,mime in (('/favicon.ico','image/x-icon'),('/assets/brand/icon-32.png','image/png'),('/apple-touch-icon.png','image/png')):
            status,headers,body = self.request(path)
            self.assertEqual(status,200)
            self.assertEqual(headers['Content-Type'],mime)
            self.assertNotIn('Set-Cookie',headers)
            self.assertGreater(len(body),100)
        self.assertEqual(self.request('/assets/brand/source.png')[0],302)
        with self.app.db.get_connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0],before)
