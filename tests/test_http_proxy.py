"""Domain routing, pinned CONNECT/TLS, public JSON queries and media delivery."""
from contextlib import contextmanager
from email import policy as email_policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import select
import shutil
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from researchops.runners import research_fetch as fetch
from researchops.runners.http_routing import resolve_http_profile, validate_profiles
from tests.test_http_image_discovery import ALPHA
from tests import test_media_pipeline as media_fixtures

PUBLIC = '93.184.216.34'
PROFILE = {'proxy_url': 'http://127.0.0.1:18888', 'host_patterns': ['example.org', '*.example.org']}


class ProxyPolicyTests(unittest.TestCase):
    def test_patterns_are_label_bounded_and_apex_is_explicit(self):
        policy = fetch.FetchPolicy(proxy_url=PROFILE['proxy_url'], proxy_host_patterns=PROFILE['host_patterns'])
        for host, expected in [('example.org', True), ('origin.example.org', True),
                ('deep.origin.example.org', True), ('evil-example.org', False),
                ('example.org.evil.test', False), ('unrelated.test', False)]:
            self.assertEqual(fetch.uses_proxy(host, policy), expected)
        wildcard = fetch.FetchPolicy(proxy_url=PROFILE['proxy_url'], proxy_host_patterns=['*.example.org'])
        self.assertFalse(fetch.uses_proxy('example.org', wildcard))

    def test_bad_profiles_and_credentials_are_rejected(self):
        for endpoint in ['http://user:secret@127.0.0.1:18888', 'https://127.0.0.1:18888',
                         'http://example.org:8080', 'http://127.0.0.1:18888/path', 'http://127.0.0.1:0']:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                validate_profiles({'test': {**PROFILE, 'proxy_url': endpoint}})
        for pattern in ['*example.org', 'https://example.org', 'example.org:443', '*', '*.Example.org']:
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                validate_profiles({'test': {**PROFILE, 'host_patterns': [pattern]}})

    def test_selection_never_silently_falls_back_for_unknown_profile(self):
        from types import SimpleNamespace
        settings = SimpleNamespace(raw_config={'http_proxy_profiles': {'test': PROFILE}})
        runner = {'network_profile': 'public-research', 'allow_http_requests': True, 'http_proxy_profile': 'test'}
        self.assertEqual(resolve_http_profile(settings, runner)['name'], 'test')
        self.assertEqual(resolve_http_profile(settings, {**runner, 'allow_http_requests': False}), {})
        self.assertEqual(resolve_http_profile(settings, {}), {})
        with self.assertRaises(ValueError):
            resolve_http_profile(settings, {**runner, 'http_proxy_profile': 'missing'})

    def test_proxy_does_not_relax_destination_and_header_rules(self):
        policy = fetch.FetchPolicy(proxy_url=PROFILE['proxy_url'], proxy_host_patterns=PROFILE['host_patterns'],
                                   require_https=True, allow_json_post=True)
        for url in ['https://127.0.0.1/', 'https://origin.example.org:8443/', 'http://origin.example.org/',
                    'https://user@origin.example.org/']:
            with self.subTest(url=url), patch.object(fetch.subprocess, 'Popen') as process:
                with self.assertRaises(fetch.ResearchFetchError):
                    fetch.PublicResearchFetcher(policy).fetch(url)
                process.assert_not_called()
        for headers in [{'Cookie':'secret'}, {'Host':'evil.test'}, {'X-Test':'bad\r\nHeader: injected'},
                        {'Authorization':'Bearer secret'}, {'Transfer-Encoding':'chunked'}]:
            with self.subTest(headers=headers), self.assertRaises(fetch.ResearchFetchError):
                fetch.PublicResearchFetcher(policy).fetch('https://origin.example.org/', headers=headers)
        with self.assertRaises(fetch.ResearchFetchError):
            fetch.PublicResearchFetcher().fetch('https://origin.example.org/', method='POST', json_body={})


class OriginHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append(('GET', self.path, b''))
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', 'https://other.example.net/image.png')
            self.end_headers()
            return
        if self.path == '/spoof':
            body = b'<html>not an image</html>'
        else:
            body = ALPHA
        self.send_response(200)
        self.send_header('Content-Type', 'image/png')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.server.requests.append(('POST', self.path, body))
        response = json.dumps({'received': json.loads(body)}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)


class ConnectHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline(8192).decode('ascii')
            self.server.connects.append(line.strip())
            while self.rfile.readline(8192) not in (b'\r\n', b''):
                pass
            with socket.create_connection(('127.0.0.1', self.server.origin_port), timeout=3) as origin:
                self.connection.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                peers = [self.connection, origin]
                while True:
                    ready, _, _ = select.select(peers, [], [], 3)
                    if not ready:
                        return
                    for source in ready:
                        body = source.recv(65536)
                        if not body:
                            return
                        (origin if source is self.connection else self.connection).sendall(body)
        except OSError:
            return


class ConnectServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ProxyTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('openssl'):
            raise unittest.SkipTest('TLS fixture requires installed openssl')
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        cls.cert, key = root/'cert.pem', root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-subj','/CN=origin.example.org','-addext','subjectAltName=DNS:origin.example.org,DNS:other.example.net',
            '-keyout',str(key),'-out',str(cls.cert)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cls.cert), str(key))
        cls.origin = ThreadingHTTPServer(('127.0.0.1', 0), OriginHandler)
        cls.origin.socket = context.wrap_socket(cls.origin.socket, server_side=True)
        cls.origin.requests = []
        cls.proxy = ConnectServer(('127.0.0.1', 0), ConnectHandler)
        cls.proxy.origin_port = cls.origin.server_port
        cls.proxy.connects = []
        for server in (cls.origin, cls.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            cls.addClassCleanup(server.server_close)
            cls.addClassCleanup(server.shutdown)
        cls.real_context = staticmethod(ssl.create_default_context)

    def setUp(self):
        self.proxy.connects.clear()
        self.origin.requests.clear()
        self.policy = fetch.FetchPolicy(require_https=True, allow_json_post=True,
            proxy_url=f'http://127.0.0.1:{self.proxy.server_address[1]}',
            proxy_host_patterns=('*.example.org',))

    @contextmanager
    def fixture(self):
        def context():
            return self.real_context(cafile=str(self.cert))
        with patch.object(fetch, '_resolve', return_value=(PUBLIC,)), patch.object(
                fetch.ssl, 'create_default_context', side_effect=context):
            yield

    def test_real_connect_is_numeric_and_tls_certificate_is_verified(self):
        with self.fixture():
            result = fetch._fetch_direct('https://origin.example.org/image.png','GET',self.policy)
        self.assertEqual(result.body, ALPHA)
        self.assertEqual(self.proxy.connects, [f'CONNECT {PUBLIC}:443 HTTP/1.1'])
        self.assertEqual(result.audit['hops'][0]['route'], 'proxy')
        self.assertEqual(result.audit['hops'][0]['connected_address'], PUBLIC)
        with patch.object(fetch, '_resolve', return_value=(PUBLIC,)), self.assertRaisesRegex(
                fetch.ResearchFetchError, 'tls_verification_failed'):
            fetch._fetch_direct('https://origin.example.org/image.png','GET',self.policy)

    def test_json_post_is_transported_and_redirect_reselects_direct_route(self):
        with self.fixture():
            result = fetch._fetch_direct('https://origin.example.org/query','POST',self.policy,
                {'product':'AX-1'}, {'X-Requested-With':'XMLHttpRequest'})
        self.assertEqual(json.loads(result.body), {'received':{'product':'AX-1'}})
        self.assertEqual(self.origin.requests[0], ('POST','/query',b'{"product":"AX-1"}'))
        self.proxy.connects.clear()
        def direct(target, addresses, policy, deadline):
            connection = socket.create_connection(('127.0.0.1', self.origin.server_port), timeout=3)
            return self.real_context(cafile=str(self.cert)).wrap_socket(connection, server_hostname=target.hostname), PUBLIC
        with self.fixture(), patch.object(fetch, '_connect', side_effect=direct) as connect:
            result = fetch._fetch_direct('https://origin.example.org/redirect','GET',self.policy)
        self.assertEqual([hop['route'] for hop in result.audit['hops']], ['proxy','direct'])
        self.assertEqual(len(self.proxy.connects), 1)
        connect.assert_called_once()

    def test_proxy_failure_has_no_direct_fallback_and_body_limit_is_kept(self):
        with self.fixture(), patch.object(fetch.socket, 'socket', side_effect=OSError), patch.object(fetch, '_connect') as direct:
            with self.assertRaises(fetch.ResearchFetchError):
                fetch._fetch_direct('https://origin.example.org/image.png','GET',self.policy)
            direct.assert_not_called()
        from dataclasses import replace
        with self.fixture(), self.assertRaisesRegex(fetch.ResearchFetchError,'body_too_large'):
            fetch._fetch_direct('https://origin.example.org/image.png','GET',replace(self.policy,max_body_bytes=8))

    def test_media_pipeline_selects_proxy_and_keeps_same_cid_bytes_in_smtp(self):
        case = media_fixtures.MediaPipelineTests('test_two_authenticated_pdfs_and_png_preserve_bytes_and_product_links_in_smtp')
        case.setUp()
        try:
            case.settings.media.official_image_hosts = ('origin.example.org',)
            case.settings.raw_config['http_proxy_profiles'] = {'test': {
                'proxy_url': self.policy.proxy_url, 'host_patterns':['*.example.org']}}
            form = case.app.tasks.get_task_advanced_editor('synthetic-media')
            import yaml
            config = yaml.safe_load(form['config_yaml'])
            config['runner'].update(allow_http_requests=True, http_proxy_profile='test')
            case.app.tasks.update_production_task_advanced('synthetic-media', form['expected_version_hash'],
                config_yaml=yaml.safe_dump(config,allow_unicode=True),task_md=form['task_md'],
                email_spec_md=form['email_spec_md'],expected_updated_at=form['expected_updated_at'])
            case.research['artifacts'] = [{'artifact_id':'plate','path':'inline/plate.png',
                'role':'inline_image','record_ids':['product-1'],'mime_type':'image/png',
                'source':{'kind':'official_image','url':'https://origin.example.org/image.png'}}]
            policies = []
            def local_fetch(client, url, **kwargs):
                policies.append(client.policy)
                return fetch._fetch_direct(url, kwargs.get('method','GET'), client.policy)
            with self.fixture(), patch.object(fetch.PublicResearchFetcher,'fetch',local_fetch):
                run, archive = case.execute()
            self.assertEqual(run.status,'awaiting_receipt',run.error_message)
            self.assertEqual(policies[0].proxy_url,self.policy.proxy_url)
            wire = BytesParser(policy=email_policy.default).parsebytes(case.deliver(run))
            image = next(part for part in wire.walk() if part.get_content_type()=='image/png')
            self.assertEqual(image.get_payload(decode=True),ALPHA)
            composition = case.app.run_repo.get_composition_input(run.run_id)
            cid = composition['inline_artifacts'][0]['cid']
            self.assertEqual(str(image['Content-ID']), '<'+cid+'>')
            self.assertIn('cid:'+cid,(archive/'email.html').read_text())
        finally:
            case.doCleanups()
