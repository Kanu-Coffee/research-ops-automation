"""Independent user-boundary regressions, always using temporary synthetic data."""

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import threading
import unittest
from unittest.mock import patch

import yaml

from researchops.services.ownership import entity_owner
from researchops.services.web_access_service import WebAccessService
from researchops.web.server import create_web_server
from tests import test_user_web as fixtures


class UserWebReviewTests(unittest.TestCase):
    setUp = fixtures.UserWebTests.setUp
    request = fixtures.UserWebTests.request
    form = fixtures.UserWebTests.form
    resources = fixtures.UserWebTests.resources

    def test_residual_viewer_grants_do_not_expand_user_ownership_or_aggregates(self):
        a, _, _ = self.resources(self.a)
        b, _, _ = self.resources(self.b)
        own = self.app.runs.enqueue_run(a, request_key='owned-fixture-run')
        other = self.app.runs.enqueue_run(b, request_key='other-fixture-run')
        with self.app.db.transaction() as conn:
            # Simulate a historical viewer grant left behind after a role change.
            conn.execute('INSERT INTO auth_user_tasks VALUES(?,?)', (self.a.session.principal.user_id, b))
            conn.execute("UPDATE scheduled_runs SET status='failed' WHERE run_id=?", (other.run_id,))
        access = WebAccessService(self.app)
        principal = self.a.session.principal
        tasks = access.tasks(principal, page_size=1)
        self.assertEqual(tasks['total'], 1)
        self.assertEqual(tasks['items'][0]['task_id'], a)
        self.assertEqual(access.tasks(principal, page=2, page_size=1)['items'], [])
        self.assertEqual(access.tasks(principal, q=b)['total'], 0)
        runs = access.runs(principal, page_size=1)
        self.assertEqual(runs['total'], 1)
        self.assertEqual(runs['items'][0]['run_id'], own.run_id)
        self.assertEqual(access.runs(principal, task_id=b)['total'], 0)
        self.assertEqual(access.runs(principal, q=other.run_id)['total'], 0)
        self.assertEqual(access.dashboard(principal)['counts']['attention'], 0)
        self.assertEqual(access.dashboard(principal)['counts']['running'], 1)
        for path in (f'/tasks/{b}', f'/runs/{other.run_id}', f'/api/runs/{other.run_id}/status'):
            self.assertEqual(self.request(path, self.a)[0], 404)

    def test_advanced_foreign_sender_delivery_group_and_alert_group_are_not_found(self):
        a, _, _ = self.resources(self.a)
        _, sender, group = self.resources(self.b)
        route = '/tasks/' + a + '/advanced'
        form = self.form(route, route, self.a)
        original_hash = form['expected_version_hash']
        changes = [lambda config, key: config['delivery'].update(sender_profile_id=key),
                   lambda config, key: config['delivery'].update(allowed_recipient_group_ids=[key]),
                   lambda config, key: config.update(alerting={'events':['failed'], 'recipient_group_id':key})]
        for mutate, foreign in zip(changes, (sender, group, group)):
            for selected in (foreign, 'missing-resource'):
                config = yaml.safe_load(form['config_yaml'])
                mutate(config, selected)
                response = self.request(route, self.a, method='POST',
                    data={**form, 'config_yaml':yaml.safe_dump(config), 'task_md':'Unpublished attempted change'})
                self.assertEqual(response[0], 404, (selected, response[0]))
                self.assertEqual(response[2], b'Not found')
        self.assertEqual(self.app.task_repo.get_active_version(a).version_hash, original_hash)
        self.assertEqual(len(self.app.task_repo.list_versions(a)), 1)
        self.assertEqual(self.app.runs.list_runs(), [])

    def test_admin_advanced_edit_keeps_task_owner_and_rejects_admin_sender(self):
        task, sender, _ = self.resources(self.a)
        route = '/tasks/' + task + '/advanced'
        form = self.form(route, route, self.admin)
        config = yaml.safe_load(form['config_yaml'])
        config['delivery']['sender_profile_id'] = 'default'
        invalid = self.request(route, self.admin, method='POST', data={**form, 'config_yaml':yaml.safe_dump(config)})
        self.assertEqual(invalid[0], 400)
        self.assertEqual(self.app.task_repo.get_active_version(task).definition.delivery['sender_profile_id'], sender)
        valid = self.request(route, self.admin, method='POST', data={**form, 'task_md':'Administrator edits for original owner'})
        self.assertEqual(valid[0], 303)
        self.assertEqual(entity_owner(self.app.db, 'task', task), self.a.session.principal.user_id)

    def test_parallel_http_creations_keep_owner_scoped_idempotency_and_host_context(self):
        def create(grant):
            data = {'display_name':'Identical display name', 'emails':grant.session.principal.username+'@example.test',
                    'request_key':'identical-review-request', 'owner_user_id':self.admin.session.principal.user_id}
            response = self.request('/delivery/groups/create', grant, method='POST', data=data)
            self.assertEqual(response[0], 303)
            self.assertIn('success=', response[1]['Location'])
            repeated = self.request('/delivery/groups/create', grant, method='POST', data=data)
            self.assertEqual(repeated[0], 303)
            self.assertIn('success=', repeated[1]['Location'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(create, (self.a, self.b)))
        groups = [row for row in self.app.catalog.list('recipient_group') if row['display_name']=='Identical display name']
        self.assertEqual(len(groups), 2)
        self.assertEqual({row['owner_user_id'] for row in groups},
                         {self.a.session.principal.user_id, self.b.session.principal.user_id})
        self.assertEqual(len({row['legacy_key'] for row in groups}), 2)
        # The completed Web request never lends its owner context to host CLI work.
        host = self.app.delivery.create_recipient_group('Host context after Web', ['host@example.test'])
        self.assertEqual(entity_owner(self.app.db, 'recipient_group', host), self.admin.session.principal.user_id)

    def test_forged_inline_task_context_is_checked_before_form_processing(self):
        _, _, _ = self.resources(self.a)
        b, _, _ = self.resources(self.b)
        before = self.settings.paths.delivery_config_file.read_bytes()
        for path, data in (
            ('/delivery/groups/create', {'task_id':b, 'display_name':'Forbidden', 'emails':'forged@example.test'}),
            ('/delivery/save', {'task_id':b, 'create_sender':'true', 'display_name':'Forbidden',
                               'username':'forged@example.test', 'password':'synthetic-ignored-secret'})):
            response = self.request(path, self.a, method='POST', data=data)
            self.assertEqual(response[0], 404)
        self.assertEqual(self.settings.paths.delivery_config_file.read_bytes(), before)
        self.assertEqual(self.request('/delivery?panel=group&task_id='+b, self.a)[0], 404)

    def test_disabled_owner_cannot_request_but_admin_inline_creation_preserves_schedule_and_owner(self):
        task, _, _ = self.resources(self.a)
        self.app.tasks.set_task_enabled(task, True)
        before = self.app.task_repo.get_task_status(task)
        owner = self.a.session.principal
        self.app.auth.update_user(self.admin.session.principal, owner.user_id,
            display_name=owner.display_name, role='user', active=False, task_ids=[])
        self.assertEqual(self.request('/api/session', self.a)[0], 401)
        route = '/delivery?panel=group&task_id='+task
        form = self.form(route, '/delivery/groups/create', self.admin)
        response = self.request('/delivery/groups/create', self.admin, method='POST',
            data={**form, 'display_name':'Created for disabled owner', 'emails':'offline@example.test'})
        self.assertEqual(response[0], 303)
        created = next(row for row in self.app.catalog.list('recipient_group') if row['display_name']=='Created for disabled owner')
        self.assertEqual(created['owner_user_id'], owner.user_id)
        self.assertEqual(self.app.task_repo.get_task_status(task), before)

    def test_head_uses_get_authorization_and_public_assets_do_not_create_sessions(self):
        a, _, _ = self.resources(self.a)
        b, _, _ = self.resources(self.b)
        own = self.app.runs.enqueue_run(a, request_key='head-owned')
        other = self.app.runs.enqueue_run(b, request_key='head-other')
        server = create_web_server(self.app, host='127.0.0.1', port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            headers = {'Host':'localhost', 'Cookie':'researchops_local_session='+self.a.token}
            with patch.object(self.app.runs, 'get_run_archive_file', side_effect=AssertionError('unauthorized archive opened')):
                for path, expected in ((f'/api/runs/{own.run_id}/status', 200),
                    (f'/api/runs/{other.run_id}/status', 404),
                    (f'/runs/{other.run_id}/artifacts/email.txt', 404),
                    (f'/runs/{other.run_id}/preview/html', 404)):
                    conn.request('HEAD', path, headers=headers)
                    response = conn.getresponse()
                    self.assertEqual(response.status, expected, path)
                    self.assertEqual(response.read(), b'')
            with self.app.db.get_connection() as db:
                sessions = db.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0]
            for path in ('/favicon.ico', '/assets/brand/icon-32.png', '/apple-touch-icon.png'):
                conn.request('HEAD', path, headers={'Host':'localhost'})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertGreater(int(response.getheader('Content-Length')), 0)
                self.assertIsNone(response.getheader('Set-Cookie'))
                self.assertEqual(response.read(), b'')
            with self.app.db.get_connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0], sessions)
            conn.request('HEAD', '/favicon.ico', headers={'Host':'untrusted.example'})
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            self.assertEqual(response.read(), b'')
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
