"""Raw trace persistence and HTTP streaming without model/SMTP calls."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from researchops.engine.archive import RunArchive
from researchops.errors import WorkspaceError
from researchops.web.file_response import StreamingFileBody
from tests.web_support import AuthenticatedWebRouter as WebRouter, admin_session, authenticated_headers
from researchops.workspace.security import safe_file_info
from tests.test_archive_queries import TestArchiveQueries


class TraceArchiveTests(unittest.TestCase):
    setUp = TestArchiveQueries.setUp

    def test_large_raw_log_download_streams_and_preserves_bisected_unicode(self):
        raw = b'x' * 2_100_000 + '서울'.encode()[:4]
        path = self.archive / 'logs/research.stdout'
        path.write_bytes(raw)
        self.settings.web.enabled = True
        router = WebRouter(self.app)
        status, headers, body = router.handle_request('GET',
            f'/runs/{self.run.run_id}/artifacts/logs/research.stdout',
            headers={'Host': 'localhost'}, client_ip='127.0.0.1')
        self.assertEqual(status, 200)
        self.assertIsInstance(body, StreamingFileBody)
        self.assertEqual(int(headers['Content-Length']), len(raw))
        self.assertTrue(headers['Content-Disposition'].startswith('attachment;'))
        digest = hashlib.sha256()
        try:
            for chunk in body.chunks():
                self.assertLessEqual(len(chunk), 65536)
                digest.update(chunk)
        finally:
            body.close()
        self.assertTrue(body.stream.closed)
        self.assertEqual(digest.hexdigest(), hashlib.sha256(raw).hexdigest())
        self.assertEqual(safe_file_info(path, self.archive, len(raw)), (digest.hexdigest(), len(raw)))

    def test_log_archive_and_index_hash_do_not_decode_raw_bytes(self):
        run = self.app.runs.enqueue_run('software-releases', force_dry_run=True)
        archive = RunArchive(self.settings, run)
        source = self.settings.paths.data_dir / 'raw-capture.bin'
        raw = b'{"x":"' + '서울'.encode()[:4]
        source.write_bytes(raw)
        archive.capture_log('logs/research.stdout', source, len(raw))
        target = archive.staging / 'logs/research.stdout'
        self.assertEqual(target.read_bytes(), raw)
        archive.capture_log('logs/research.stdout', target, len(raw))
        manifest = json.loads((self.archive / 'run-manifest.json').read_text())
        manifest['run_id'] = run.run_id
        archive.finish(manifest)
        indexed = json.loads((archive.final / 'artifact-manifest.json').read_text())['artifacts']
        item = next(item for item in indexed if item['relative_path'] == 'logs/research.stdout')
        self.assertEqual(item['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(item['size_bytes'], len(raw))

    def test_stream_rejects_links_and_detects_changed_file(self):
        path = self.archive / 'logs/research.stdout'
        alias = self.archive / 'unsafe-link'
        alias.symlink_to(path)
        with self.assertRaises(WorkspaceError):
            StreamingFileBody(alias, self.archive)
        alias.unlink()
        os.link(path, alias)
        with self.assertRaises(WorkspaceError):
            StreamingFileBody(path, self.archive)
        alias.unlink()
        body = StreamingFileBody(path, self.archive)
        path.write_bytes(b'changed')
        with self.assertRaises(WorkspaceError):
            body.close()
        self.assertTrue(body.stream.closed)


class TraceConfigTests(unittest.TestCase):
    def test_trace_budgets_are_independent_positive_config_values(self):
        from researchops.config import load_settings
        from researchops.errors import ConfigError
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            for value in (0, -1, True, '10'):
                path.write_text(json.dumps({'runner': {'trace_max_bytes': value}}))
                with self.subTest(value=value), self.assertRaises(ConfigError):
                    load_settings(path)
            path.write_text(json.dumps({'runner': {'trace_max_bytes': 10,
                'trace_max_event_bytes': 11, 'trace_preview_bytes': 1}}))
            with self.assertRaises(ConfigError):
                load_settings(path)
            path.write_text('{}')
            settings = load_settings(path)
            self.assertEqual(settings.runner.trace_max_bytes, 64 * 1024**2)
            self.assertEqual(settings.runner.trace_max_event_bytes, 8 * 1024**2)
            self.assertEqual(settings.delivery.max_message_bytes, 20_000_000)
