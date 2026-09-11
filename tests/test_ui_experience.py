"""Regression checks for navigation, role presentation and lazy detail pages."""

from html.parser import HTMLParser
import shutil
import subprocess
import unittest

from researchops.delivery.smtp_config import BuiltinDeliveryConfig, SmtpSettings
from researchops.web.context import request_context
from researchops.web.views import render_base_layout, render_dashboard, render_delivery_view, render_run_detail, render_tasks_list


class Controls(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.forms, self.links, self.inputs, self.iframes = [], [], [], []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        target = {'form': self.forms, 'a': self.links, 'input': self.inputs, 'iframe': self.iframes}.get(tag)
        if target is not None:
            target.append(dict(attrs))


class TestUIExperience(unittest.TestCase):
    def setUp(self):
        self.run = {'run': {'run_id': 'run-fixture', 'task_id': 'task-fixture', 'status': 'failed', 'phase': 'compose'},
                    'research': {'record_count': 2, 'summary': 'Preserved research text'},
                    'composition': {'subject': 'Fixture email'},
                    'prepared_email': {'eligible': True},
                    'audit_events': [{'event_type': 'run_enqueued', 'details': {'record_count': 42}}]}
        self.artifacts = [{'filename': 'report.pdf', 'size_bytes': 12}, {'filename': 'logs/research.stdout', 'size_bytes': 17}]

    def test_role_shell_and_post_forms_are_not_exposed_to_viewers(self):
        body = '<form method="POST" action="/tasks/task-fixture/run"><button>지금 실행</button></form><a href="/tasks/task-fixture/edit">수정</a><a href="/delivery">설정</a><p>Readable record</p>'
        with request_context({'username': 'reader', 'role': 'viewer'}):
            result = render_base_layout('읽기', body)
        controls = Controls(result)
        self.assertEqual([item.get('action') for item in controls.forms], ['/logout'])
        self.assertNotIn('/delivery', [item.get('href') for item in controls.links])
        self.assertNotIn('/tasks/task-fixture/edit', [item.get('href') for item in controls.links])
        self.assertIn('Readable record', result)
        self.assertIn('aria-controls="main-navigation"', result)
        self.assertIn('href="#main-content"', result)

    def test_user_can_work_and_manage_mail_but_has_no_global_settings_navigation(self):
        with request_context({'username': 'writer', 'role': 'user'}):
            result = render_tasks_list([{'task_id': 'task-fixture', 'name': 'Fixture', 'active_version_hash': 'abc'}])
        controls = Controls(result)
        self.assertIn('/tasks/task-fixture/run', [item.get('action') for item in controls.forms])
        self.assertIn('/delivery', [item.get('href') for item in controls.links])
        self.assertNotIn('/settings/users', [item.get('href') for item in controls.links])
        self.assertNotIn('/doctor', [item.get('href') for item in controls.links])

    def test_viewer_can_change_own_password(self):
        with request_context({'username': 'reader', 'user_id': 'user-reader', 'role': 'viewer'}):
            result = render_base_layout('비밀번호 변경', '<form method="POST" action="/account/password"><input type="password" name="new_password"><button>변경</button></form>')
        self.assertIn('/account/password', [item.get('action') for item in Controls(result).forms])
        self.assertIn('data-auth-user-id="user-reader"', result)

    def test_admin_owner_labels_and_no_retired_draft_navigation(self):
        rows = [{'task_id':'task-owned','name':'Task','owner_name':'owner-private','active_version_hash':'abc'}]
        with request_context({'role':'admin'}):
            admin = render_tasks_list(rows)
        self.assertIn('소유자: owner-private',admin)
        self.assertNotIn('/tasks/drafts',admin)
        self.assertNotIn('value="draft"',admin)
        with request_context({'role':'user'}):
            user = render_tasks_list(rows)
        self.assertNotIn('owner-private',user)

    def test_run_tabs_only_render_selected_content_and_keep_all_evidence_reachable(self):
        with request_context(None):
            overview = render_run_detail(self.run, self.artifacts, [])
        self.assertIn('Preserved research text', overview)
        self.assertFalse(Controls(overview).iframes)
        self.assertNotIn('<small>run_enqueued</small>', overview)
        with request_context(None, {'tab': 'email'}):
            email = render_run_detail(self.run, self.artifacts, [])
        self.assertEqual(Controls(email).iframes[0]['sandbox'], '')
        self.assertIn('id="prepared-email-form"', email)
        with request_context(None, {'tab': 'logs'}):
            logs = render_run_detail(self.run, self.artifacts, [])
        self.assertIn('<small>run_enqueued</small>', logs)
        with request_context(None, {'tab': 'files'}):
            files = render_run_detail(self.run, self.artifacts, [])
        self.assertIn('/artifacts/report.pdf', files)

    def test_partial_response_is_fragment_and_viewer_gets_no_send_controls(self):
        with request_context({'role': 'viewer', 'username': 'reader'}, {'tab': 'email'}):
            result = render_run_detail(self.run, self.artifacts, [], partial=True)
        self.assertIn('data-run-view', result)
        self.assertNotIn('<html', result)
        self.assertFalse(Controls(result).forms)
        self.assertEqual(Controls(result).iframes[0]['sandbox'], '')

    def test_files_are_bounded_and_next_page_is_accessible(self):
        artifacts = [{'filename': f'file-{index:03d}.txt', 'size_bytes': 1} for index in range(205)]
        with request_context(None, {'tab': 'files'}):
            result = render_run_detail(self.run, artifacts, [])
        downloads = [item for item in Controls(result).links if '/artifacts/' in item.get('href', '')]
        self.assertEqual(len(downloads), 100)
        self.assertIn('tab=files&amp;page=2', result)
        with request_context(None, {'tab': 'files', 'page': '3'}):
            result = render_run_detail(self.run, artifacts, [])
        self.assertIn('file-204.txt', result)

    def test_sender_form_hides_secrets_and_security_is_one_choice(self):
        config = BuiltinDeliveryConfig(smtp=SmtpSettings(password='Never-render-this', password_configured=True))
        with request_context(None):
            result = render_delivery_view(config)
        self.assertNotIn('Never-render-this', result)
        self.assertIn('name="security"', result)
        self.assertNotIn('name="use_tls"', result)
        self.assertNotIn('name="use_ssl"', result)
        inputs = Controls(result).inputs
        self.assertEqual(next(item for item in inputs if item.get('name') == 'password')['value'], '')
        self.assertNotIn('name="emails"', result)
        self.assertIn('for="smtp-password">앱 비밀번호</label>', result)
        self.assertNotIn('/delivery/catalog/sender/default/rename', result)
        with request_context(None, {'tab': 'groups'}):
            result = render_delivery_view(config)
        self.assertIn('name="emails"', result)
        self.assertNotIn('name="password"', result)

    def test_user_without_owned_sender_gets_empty_create_form_and_no_global_setting(self):
        config = BuiltinDeliveryConfig(smtp=SmtpSettings(username='another-owner@example.test', password='hidden'))
        with request_context({'username': 'new-user', 'role': 'user'}):
            result = render_delivery_view(config, catalog_entries={'sender': [], 'recipient_group': []})
        inputs = {item.get('name'): item for item in Controls(result).inputs}
        self.assertEqual(inputs['create_sender']['value'], 'true')
        self.assertEqual(inputs['sender_profile_id']['value'], '')
        self.assertEqual(inputs['username']['value'], '')
        self.assertNotIn('enabled', inputs)
        self.assertNotIn('another-owner@example.test', result)
        self.assertNotIn('저장한 계정 연결 확인', result)

    def test_user_sender_list_and_group_list_only_use_supplied_owned_catalog(self):
        config = BuiltinDeliveryConfig(smtp=SmtpSettings(username='other@example.test'),
            sender_profiles={'sender-own': SmtpSettings(username='own@example.test'),
                             'sender-other': SmtpSettings(username='private@example.test')},
            recipient_groups={'group-own':['own-recipient@example.test'], 'group-other':['private-recipient@example.test']})
        catalog = {'sender':[{'legacy_key':'sender-own', 'display_name':'My sender'}],
                   'recipient_group':[{'legacy_key':'group-own','display_name':'My group'}]}
        with request_context({'role':'user'}, {}):
            result = render_delivery_view(config, catalog_entries=catalog)
        self.assertIn('own@example.test', result)
        self.assertNotIn('other@example.test', result)
        self.assertNotIn('private@example.test', result)
        with request_context({'role':'user'}, {'tab':'groups'}):
            result = render_delivery_view(config, catalog_entries=catalog)
        self.assertIn('own-recipient@example.test', result)
        self.assertNotIn('private-recipient@example.test', result)

    def test_user_management_distinguishes_owned_items_from_viewer_grants(self):
        from researchops.web.auth_views import users_page
        users = [{'user_id':'writer', 'username':'writer', 'role':'user', 'owned_task_count':3,
                  'owned_sender_count':2, 'owned_recipient_group_count':1},
                 {'user_id':'reader', 'username':'reader', 'role':'viewer', 'task_ids':['t1']}]
        with request_context({'role':'admin'}):
            result = users_page(users, [], selected=users[0])
        self.assertIn('value="user" selected>사용자', result)
        self.assertIn('Task 3개 · 발신 계정 2개 · 수신자 그룹 1개', result)
        self.assertIn('조회 Task 1개', result)
        self.assertIn('비활성화해도 기존 예약과 메일 작업은 계속됩니다.', result)

    def test_invalid_sender_error_preserves_nonsecret_fields_and_does_not_switch_identity(self):
        config = BuiltinDeliveryConfig()
        with request_context(None):
            result = render_delivery_view(config, sender_profile_id='missing-account', form_values={'host':'smtp.example.test', 'display_name':'Preserved name', 'username':'preserved@example.test'})
        controls = Controls(result)
        self.assertEqual(next(item for item in controls.inputs if item.get('name') == 'sender_profile_id')['value'], 'missing-account')
        self.assertEqual(next(item for item in controls.inputs if item.get('name') == 'username')['value'], 'preserved@example.test')

    def test_scoped_sender_error_does_not_fail_while_rendering_missing_account(self):
        from types import SimpleNamespace
        from researchops.errors import NotFoundError
        def missing(_):
            raise NotFoundError('Not found')
        config = SimpleNamespace(all_senders=lambda:{}, recipient_groups={}, enabled=False, get_sender=missing)
        with request_context({'role':'user'}):
            result = render_delivery_view(config, sender_profile_id='missing', form_values={
                'username':'kept@example.test','display_name':'Kept'}, catalog_entries={})
        inputs = {item.get('name'):item for item in Controls(result).inputs}
        self.assertEqual(inputs['sender_profile_id']['value'],'missing')
        self.assertEqual(inputs['username']['value'],'kept@example.test')
        self.assertEqual(inputs['password']['value'],'')

    def test_logs_preserve_omission_evidence_and_offer_chunked_original(self):
        self.run['audit_events_meta'] = {'total_count': 550, 'omitted_count': 500}
        with request_context(None, {'tab': 'logs', 'q': 'run_enqueued'}):
            result = render_run_detail(self.run, self.artifacts, [])
        self.assertIn('이전 500건 생략', result)
        self.assertIn('표시된 관리 기록 검색', result)
        self.assertIn('data-log-reader', result)
        self.assertIn('최대 65.5 KB씩 표시', result)
        self.assertIn('data-log-exact', result)

    def test_embedded_settings_keeps_panel_on_submit_and_reports_success(self):
        with request_context({'role': 'admin', 'username': 'admin'}, {'panel': 'group', 'task_id':'task-owned'}):
            result = render_delivery_view(BuiltinDeliveryConfig(), flash={'type': 'success', 'message': '저장됨'})
        controls = Controls(result)
        self.assertNotIn('id="main-navigation"', result)
        self.assertTrue(any(item.get('name') == 'panel' and item.get('value') == 'group' for item in controls.inputs))
        self.assertTrue(any(item.get('name') == 'task_id' and item.get('value') == 'task-owned' for item in controls.inputs))
        self.assertIn('data-settings-saved="true"', result)
        self.assertIn('data-authenticated="true"', result)

    def test_dashboard_uses_supplied_scoped_counts_and_task_names(self):
        with request_context({'role': 'viewer', 'username': 'reader'}):
            result = render_dashboard({}, [], [], dashboard_data={'counts': {'attention': 4, 'running': 2, 'scheduled': 1, 'completed': 7},
                'recent': [{'task_name': 'Visible task', 'task_id': 't1', 'run_id': 'r1', 'status': 'failed'}],
                'upcoming': [{'task_id': 't1', 'name': 'Upcoming task', 'scheduled_for': '2026-09-12T01:00:00+00:00'}]})
        self.assertIn('Visible task', result)
        self.assertIn('Upcoming task', result)
        self.assertIn('2026-09-12T10:00:00+09:00', result)
        self.assertNotIn('/tasks/new', [item.get('href') for item in Controls(result).links])

    @unittest.skipUnless(shutil.which('node'), 'Node.js is not installed')
    def test_shared_javascript_parses_and_never_reloads_the_page(self):
        from researchops.web.ui_assets import SCRIPT
        result = subprocess.run(['node', '--check'], input=SCRIPT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('location.reload', SCRIPT)
        self.assertIn('/api/session', SCRIPT)
        self.assertIn("current.dataset.dirty==='true'", SCRIPT)
        self.assertIn('selection.isCollapsed', SCRIPT)


if __name__ == '__main__':
    unittest.main()
