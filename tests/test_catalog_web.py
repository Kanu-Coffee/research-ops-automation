"""Catalog administration through real rendered controls and application services."""

import unittest
from urllib.parse import parse_qs, urlparse

from researchops.delivery.smtp_config import delivery_revision, load_delivery_config
from tests import test_task_editor_web as editor_tests


class CatalogWebTests(unittest.TestCase):
    setUp = editor_tests.TaskEditorWebTests.setUp
    request = editor_tests.TaskEditorWebTests.request
    get_form = editor_tests.TaskEditorWebTests.get_form
    create = editor_tests.TaskEditorWebTests.create

    def config(self):
        return load_delivery_config(self.settings.paths.delivery_config_file)

    def post_form(self, page, action, **fields):
        if page == '/delivery' and '/recipient_group/' in action or action == '/delivery/groups/create':
            page = '/delivery?tab=groups'
        if page == '/tasks' and action.endswith(('/rename', '/delete')):
            page = action.rsplit('/', 1)[0]
        controls = self.get_form(page, action)
        controls.update(fields)
        response = self.request('POST', action, controls)
        self.assertEqual(response[0], 303, response[2][:200])
        return response, controls

    def test_group_auto_number_rename_delete_and_restore_preserve_routing_and_config(self):
        response, fields = self.post_form('/delivery', '/delivery/groups/create',
            display_name='상품 개발팀', emails='reader@example.test')
        self.assertIn('success=', response[1]['Location'])
        entry = next(item for item in self.app.catalog.list('recipient_group') if item['display_name'] == '상품 개발팀')
        key, number = entry['legacy_key'], entry['entity_id']
        self.assertEqual(key, f'group-{number}')
        self.assertNotIn('group_id', fields)
        original = self.settings.paths.delivery_config_file.read_bytes()
        revision = delivery_revision(self.config())
        self.post_form('/delivery', f'/delivery/catalog/recipient_group/{key}/rename', display_name='새 이름 <팀>')
        self.assertEqual(self.settings.paths.delivery_config_file.read_bytes(), original)
        self.assertEqual(delivery_revision(self.config()), revision)
        page = self.request('GET', '/tasks/new')[2].decode()
        self.assertIn(f'value="{key}"', page)
        self.assertIn('새 이름 &lt;팀&gt;', page)
        self.post_form('/delivery', f'/delivery/catalog/recipient_group/{key}/delete')
        self.assertNotIn(f'value="{key}"', self.request('GET', '/tasks/new')[2].decode())
        self.assertEqual(self.settings.paths.delivery_config_file.read_bytes(), original)
        self.post_form('/delivery', f'/delivery/catalog/recipient_group/{key}/restore')
        self.assertEqual(self.app.catalog.require_active('recipient_group', key)['entity_id'], number)
        self.assertIn(f'value="{key}"', self.request('GET', '/tasks/new')[2].decode())

    def test_sender_auto_number_cosmetic_rename_and_delete_keep_authentication(self):
        fields = self.get_form('/delivery?new_sender=true', '/delivery/save')
        self.assertEqual(fields['sender_profile_id'], '')
        fields.update(display_name='업무 Gmail', username='writer@example.test',
            sender_email='writer@example.test', password='private-app-password', sender_name='메일 발신팀')
        response = self.request('POST', '/delivery/save', fields)
        self.assertEqual(response[0], 303, response[2][:100])
        key = parse_qs(urlparse(response[1]['Location']).query)['sender'][0]
        entry = self.app.catalog.require_active('sender', key)
        self.assertEqual(key, f"sender-{entry['entity_id']}")
        original = self.settings.paths.delivery_config_file.read_bytes()
        revision = delivery_revision(self.config(), key)
        self.post_form(f'/delivery?sender={key}', '/delivery/save', display_name='상품팀 발송 계정')
        self.assertEqual(delivery_revision(self.config(), key), revision)
        self.assertEqual(self.settings.paths.delivery_config_file.read_bytes(), original)
        self.assertIn(f'<option value="{key}">상품팀 발송 계정 · #{entry["entity_id"]}</option>', self.request('GET', '/tasks/new')[2].decode())
        self.assertNotIn('private-app-password', self.request('GET', '/delivery')[2].decode())
        self.post_form('/delivery', f'/delivery/catalog/sender/{key}/delete')
        self.assertNotIn(f'<option value="{key}"', self.request('GET', '/tasks/new')[2].decode())
        self.post_form('/delivery', f'/delivery/catalog/sender/{key}/restore')
        self.assertEqual(self.config().get_sender(key).password, 'private-app-password')

    def test_name_only_save_keeps_legacy_empty_sender_address_and_config_bytes(self):
        cfg = self.config()
        cfg.smtp.sender_email = ''
        self.app.delivery.save_delivery_config(cfg)
        before = self.settings.paths.delivery_config_file.read_bytes()
        revision = delivery_revision(self.config(), 'default')
        self.post_form('/delivery?sender=default', '/delivery/save', display_name='이름만 수정')
        self.assertEqual(self.app.catalog.get('sender', 'default')['display_name'], '이름만 수정')
        self.assertEqual(self.settings.paths.delivery_config_file.read_bytes(), before)
        self.assertEqual(delivery_revision(self.config(), 'default'), revision)

    def test_task_auto_identity_rename_delete_restore_and_duplicate_submission(self):
        fields = self.get_form('/tasks/new', '/tasks/production/create')
        self.assertEqual(fields['task_id'], '')
        fields.update(name='반복 현황', task_md='공개 현황을 조사하세요.', email_spec_md='한국어 메일',
                      recipient_group_id='research-team', action='create', schedule_enabled='true')
        first = self.request('POST', '/tasks/production/create', fields)
        self.assertEqual(first[0], 303, first[2][:100])
        path = urlparse(first[1]['Location']).path
        key = path.split('/')[-1]
        entity = self.app.catalog.require_active('task', key)
        self.assertEqual(key, f"task-{entity['entity_id']}")
        duplicate = self.request('POST', '/tasks/production/create', fields)
        self.assertEqual(duplicate[0], 303)
        self.assertEqual(urlparse(duplicate[1]['Location']).path, path)
        version = self.app.task_repo.get_active_version(key)
        self.post_form('/tasks', f'/tasks/{key}/rename', display_name='정정한 현황')
        self.assertIn('정정한 현황', self.request('GET', '/tasks')[2].decode())
        self.assertEqual(self.app.task_repo.get_active_version(key).version_hash, version.version_hash)
        self.assertEqual(self.get_form(path + '/edit', path + '/edit')['name'], '정정한 현황')
        self.post_form('/tasks', f'/tasks/{key}/delete')
        self.assertNotIn(f'href="{path}"', self.request('GET', '/tasks')[2].decode())
        self.assertFalse(self.app.task_repo.get_task_status(key)['enabled'])
        historical = self.request('GET', path)
        self.assertEqual(historical[0], 200)
        self.assertNotIn(f'action="{path}/run"', historical[2].decode())
        self.assertEqual(self.app.task_repo.get_active_version(key).version_hash, version.version_hash)
        self.post_form('/tasks?view=deleted', path + '/restore')
        self.assertFalse(self.app.task_repo.get_task_status(key)['enabled'])
        self.assertEqual(self.app.runs.list_runs(), [])

    def test_used_group_and_busy_task_delete_are_explained_without_mutation(self):
        self.create(recipient_routing_mode="legacy_ids")
        response, _ = self.post_form('/delivery', '/delivery/catalog/recipient_group/research-team/delete')
        self.assertIn('error=', response[1]['Location'])
        self.assertIn('operator-task', response[1]['Location'])
        self.assertIsNone(self.app.catalog.get('recipient_group', 'research-team')['deleted_at'])
        run = self.app.runs.enqueue_run('operator-task', request_key='pending-ui-check')
        response, _ = self.post_form('/tasks', '/tasks/operator-task/delete')
        self.assertIn('error=', response[1]['Location'])
        self.assertIsNone(self.app.catalog.get('task', 'operator-task')['deleted_at'])
        self.assertEqual(self.app.run_repo.get_run(run.run_id).status, 'queued')

    def test_all_lifecycle_mutations_require_csrf(self):
        self.create()
        for route in ('/tasks/operator-task/rename', '/tasks/operator-task/delete', '/tasks/operator-task/restore',
                      '/delivery/catalog/recipient_group/research-team/rename',
                      '/delivery/catalog/recipient_group/research-team/delete',
                      '/delivery/catalog/recipient_group/research-team/restore',
                      '/delivery/catalog/sender/default/rename', '/delivery/catalog/sender/default/delete'):
            with self.subTest(route=route):
                self.assertEqual(self.request('POST', route, {'display_name': 'forged'})[0], 403)


if __name__ == '__main__':
    unittest.main()
