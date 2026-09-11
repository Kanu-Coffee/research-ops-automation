"""Real local acquisition and PDF extraction through fake native CLIs and SMTP."""
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
import hashlib
import html
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from researchops.config import MediaProviderConfig
from researchops.delivery.smtp_config import BuiltinDeliveryConfig, SmtpSettings, save_delivery_config
from researchops.errors import DeliveryError
from researchops.services.application import ApplicationService
from tests.delivery_fixtures import smtp_server, smtp_message_bytes
from tests.media_fixture import SYNTHETIC_TOKEN
from tests.research_file_fixture import illustrated_source, target_image, task_instructions
from tests.support import isolated_settings
from tests.test_production_runner import response


WORKER_CODE = '''
import json, pathlib, subprocess, sys
data = json.loads(sys.stdin.read())
work = pathlib.Path(data['artifact_root'])
sys.path.insert(0, str(work / '.researchops-submit'))
from researchops.runners.file_acquisition import acquire_file
source = json.loads(data['files']['task.md'].split('Synthetic source descriptor:\\n', 1)[1].splitlines()[0])
got = acquire_file(source)
assert got['status'] == 'available', got
again = acquire_file(source)
assert got['acquisition_id'] == again['acquisition_id']
original = pathlib.Path(got['path'])
assert original.is_file()
subprocess.run(['pdfimages', '-png', str(original), str(work / 'figure')], check=True, capture_output=True, timeout=10)
image = work / 'images/figure.png'
image.parent.mkdir(exist_ok=True)
image.write_bytes((work / 'figure-001.png').read_bytes())
print(json.dumps({'status':'success', 'summary':'Selected the labelled figure, omitting the unrelated logo.',
 'records':[{'record_id':'figure-1','title':'Synthetic figure','description':'Three blue bars'}],
 'coverage':{'complete':True,'expected_target_count':1,'completed_target_count':1,'issues':[]},
 'warnings':[], 'artifacts':[
 {'artifact_id':'original','path':'documents/original.pdf','filename':'Synthetic source.pdf',
  'role':'attachment','scope':'record','record_ids':['figure-1'],'mime_type':'application/pdf','source':source},
 {'artifact_id':'figure','path':'images/figure.png','role':'inline_image','scope':'record',
  'record_ids':['figure-1'],'mime_type':'image/png','derived_from':[got['acquisition_id']]}]}))
'''


def compose_document(ci):
    sections = []
    for record in ci['reportable_records']:
        rid = record['record_id']
        images = ''.join('<img alt="Synthetic figure" src="cid:' + html.escape(item['cid']) + '">'
                         for item in ci['inline_artifacts'] if rid in item['record_ids'])
        sections.append('<section data-record-id="' + html.escape(rid) + '"><h2>'
                        + html.escape(record.get('title', rid)) + '</h2>' + images + '</section>')
    result = {'subject': 'Synthetic figure report', 'html_path': 'email.html', 'text_path': 'email.txt',
              'included_record_ids': [r['record_id'] for r in ci['reportable_records']],
              'recipient_group_reason': 'The synthetic task requested this group.'}
    if ci.get('recipient_routing_mode') == 'catalog_name':
        result['recipient_group_name'] = ci['recipient_groups'][0]['display_name']
    else:
        result['recipient_group_id'] = ci['allowed_recipient_group_ids'][0]
    return {'composition_result': result,
            'html': '<!DOCTYPE html><html><head><title>Synthetic report</title></head><body data-local-date="'
                    + ci['run']['local_date'] + '"><p>' + ci['run']['local_date_display'] + '</p>'
                    + ''.join(sections) + '<p>Original PDF attached.</p></body></html>',
            'text': ci['run']['local_date_display'] + '\n' + '\n'.join(result['included_record_ids'])
                    + '\nSynthetic figure with original PDF attached.\n'}


@unittest.skipUnless(shutil.which('pdfimages'), 'PDF extraction tool is unavailable')
class ResearchFilePipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = isolated_settings(self.root / 'runtime')
        self.settings.environment = 'production'
        self.settings.delivery.global_handoff_kill_switch = False
        # Both CLI processes are doubled below; use an existing metadata path.
        self.settings.runner.codex_binary = sys.executable
        self.settings.runner.antigravity_binary = sys.executable
        save_delivery_config(BuiltinDeliveryConfig(enabled=True, auto_dispatch=False,
            smtp=SmtpSettings(host='smtp.example.test', sender_email='sender@example.test'),
            recipient_groups={'synthetic-team': ['recipient@example.test']}), self.settings.paths.delivery_config_file)
        self.app = ApplicationService(self.settings)
        self.calls = []
        self.expected_png = target_image(self.root / 'expected')
        self.counter = 0

    def task(self, source, provider, subject='general document figure'):
        self.counter += 1
        name = 'synthetic-files-' + str(self.counter)
        self.app.tasks.create_production_task(task_id=name, name=subject,
            instructions=task_instructions(source, subject=subject), runner_type=provider,
            recipient_group_id='synthetic-team', schedule_enabled=False)
        return name

    def fake_cli(self, argv, **kwargs):
        provider = 'antigravity_exec' if '--print' in argv else 'codex_exec'
        prompt = argv[argv.index('--print') + 1] if '--print' in argv else kwargs['stdin'].decode()
        if prompt.startswith('Read the complete ResearchOps invocation instructions from '):
            name = prompt.split(' from ', 1)[1].split('. This is ', 1)[0]
            prompt = Path(name).read_text()
        self.assertNotIn(SYNTHETIC_TOKEN, prompt)
        data = json.loads(prompt.split('\n', 1)[1])
        stage = data['context']['invocation_stage']
        self.calls.append((provider, stage))
        if stage == 'research':
            completed = subprocess.run([sys.executable, '-c', WORKER_CODE], input=json.dumps(data).encode(),
                cwd=data['project_dir'], capture_output=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            document = json.loads(completed.stdout)
        else:
            document = compose_document(json.loads(data['files']['composition-input.json']))
        return response(provider, document)

    @contextmanager
    def doubles(self):
        with patch('researchops.runners.production.inspect_native_mcp', return_value={'servers': []}), \
                patch('researchops.runners.production.run_bounded', side_effect=self.fake_cli):
            yield

    def archive(self, run):
        return self.settings.paths.run_archive_dir / run.task_id / run.run_id

    def verify_files(self, run, raw_pdf):
        archive = self.archive(run)
        self.assertEqual((archive / 'documents/original.pdf').read_bytes(), raw_pdf)
        self.assertEqual((archive / 'images/figure.png').read_bytes(), self.expected_png)
        ci = self.app.run_repo.get_composition_input(run.run_id)
        self.assertEqual(len(ci['inline_artifacts']), 1)
        reference = ci['inline_artifacts'][0]['derived_from'][0]
        self.assertEqual(reference['source_sha256'], hashlib.sha256(raw_pdf).hexdigest())
        self.assertEqual(ci['inline_artifacts'][0]['record_ids'], ['figure-1'])
        self.assertIn('cid:' + ci['inline_artifacts'][0]['cid'], (archive / 'email.html').read_text())
        for path in archive.rglob('*'):
            if path.is_file():
                self.assertNotIn(SYNTHETIC_TOKEN.encode(), path.read_bytes(), str(path))
        return archive

    def deliver(self, run):
        server = smtp_server()
        handoff = self.app.delivery_repo.get_handoff_for_run(run.run_id)
        with patch('smtplib.SMTP', return_value=server):
            ok, _, reason = self.app.delivery.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok, reason)
        mime = BytesParser(policy=policy.default).parsebytes(smtp_message_bytes(server))
        images = [p for p in mime.walk() if p.get_content_type() == 'image/png']
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].get_payload(decode=True), self.expected_png)
        return mime

    def test_both_native_adapters_extract_then_deliver_for_different_task_subjects(self):
        with illustrated_source(self.root) as state:
            self.settings.media.providers = {'cardrag': MediaProviderConfig(state['base_url'], state['token'])}
            for provider, subject in [('codex_exec', 'general document figure'),
                                      ('antigravity_exec', 'product illustration')]:
                task_id = self.task(state['source'], provider, subject)
                run = self.app.runs.enqueue_run(task_id)
                with self.doubles():
                    finished = self.app.runs.execute_run(run.run_id)
                self.assertEqual(finished.status, 'awaiting_receipt', finished.error_message)
                self.verify_files(finished, state['raw'])
                mime = self.deliver(finished)
                pdfs = [p for p in mime.walk() if p.get_content_type() == 'application/pdf']
                self.assertEqual([p.get_payload(decode=True) for p in pdfs], [state['raw']])
            self.assertEqual(len(state['requests']), 4, 'one metadata and PDF GET per Run; no repeated downloads')
        self.assertEqual(self.calls, [('codex_exec', 'research'), ('codex_exec', 'compose'),
                                     ('antigravity_exec', 'research'), ('antigravity_exec', 'compose')])

    def test_recompose_preserves_provenance_without_research_or_network(self):
        with illustrated_source(self.root) as state:
            self.settings.media.providers = {'cardrag': MediaProviderConfig(state['base_url'], state['token'])}
            task_id = self.task(state['source'], 'antigravity_exec')
            parent = self.app.runs.enqueue_run(task_id, force_dry_run=True)
            with self.doubles():
                parent = self.app.runs.execute_run(parent.run_id)
            self.assertEqual(parent.status, 'succeeded', parent.error_message)
            original = self.verify_files(parent, state['raw'])
            before = list(state['requests'])
            child = self.app.runs.compose_only(parent.run_id, execution_settings={
                'compose': {'type': 'codex_exec', 'model': None, 'reasoning_effort': None}})
            with self.doubles():
                child = self.app.runs.execute_run(child.run_id)
            self.assertEqual(child.status, 'succeeded', child.error_message)
            derived = self.verify_files(child, state['raw'])
            self.assertEqual(state['requests'], before)
            self.assertEqual(self.calls[-1], ('codex_exec', 'compose'))
            self.assertEqual(sum(stage == 'research' for _, stage in self.calls), 1)
            self.assertEqual((child.local_date, child.scheduled_for), (parent.local_date, parent.scheduled_for))
            self.assertEqual((original / 'research-acquisition/ledger.json').read_bytes(),
                             (derived / 'research-acquisition/ledger.json').read_bytes())

    def test_prepared_email_delivery_only_preserves_derived_image(self):
        with illustrated_source(self.root) as state:
            self.settings.media.providers = {'cardrag': MediaProviderConfig(state['base_url'], state['token'])}
            task_id = self.task(state['source'], 'codex_exec')
            parent = self.app.runs.enqueue_run(task_id)
            with self.doubles(), patch.object(self.app.orchestrator.handoff_publisher,
                    'create_and_publish_handoff', side_effect=DeliveryError('Synthetic publication interruption')):
                parent = self.app.runs.execute_run(parent.run_id)
            self.assertEqual(parent.status, 'failed')
            self.assertTrue(self.app.runs.prepared_email_status(parent.run_id)['eligible'])
            calls = list(self.calls)
            requests = list(state['requests'])
            child = self.app.runs.send_prepared_email(parent.run_id, request_key='send-derived-image')
            with self.doubles():
                child = self.app.runs.execute_run(child.run_id)
            self.assertEqual(child.status, 'awaiting_receipt', child.error_message)
            self.verify_files(child, state['raw'])
            self.assertEqual(self.calls, calls)
            self.assertEqual(state['requests'], requests)
            self.deliver(child)
