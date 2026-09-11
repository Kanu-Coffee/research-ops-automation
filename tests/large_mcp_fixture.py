"""Explicit live-test fixture: bounded synthetic MCP over STDIO or loopback HTTP.

Never reads credentials or business data. Its only writes are private receipts.
Not started by unittest discovery or by any production task.
"""

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--receipts', type=Path, required=True)
    parser.add_argument('--http', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    nonce = secrets.token_hex(16)
    calls = 0
    audit = args.receipts / (str(os.getpid()) + '.jsonl')
    descriptor = os.open(audit, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)

    def record(value):
        data = (json.dumps(value) + '\n').encode()
        os.write(descriptor, data)
        os.fsync(descriptor)

    record({'event': 'start', 'pid': os.getpid(), 'nonce': nonce})

    def dispatch(request):
        nonlocal calls
        identity, method = request.get('id'), request.get('method')
        record({'event': 'request', 'method': method})
        if identity is None:
            return None
        if method == 'initialize':
            result = {'protocolVersion': request['params']['protocolVersion'],
                'capabilities': {'tools': {}}, 'serverInfo': {'name': 'researchops-large-fixture', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'fetch_page', 'description': 'Read one synthetic large response page. Padding is transport test data; retain only the receipt and fact.',
                'inputSchema': {'type': 'object', 'properties': {'page': {'type': 'integer', 'enum': [1, 2, 3]}},
                    'required': ['page'], 'additionalProperties': False},
                'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False}}]}
        elif method == 'tools/call':
            parameters = request.get('params', {})
            page = parameters.get('arguments', {}).get('page')
            if parameters.get('name') != 'fetch_page' or type(page) is not int or page not in (1, 2, 3) or calls >= 6:
                return {'jsonrpc': '2.0', 'id': identity, 'error': {'code': -32602, 'message': 'Invalid bounded fixture request'}}
            calls += 1
            receipt = f'{nonce}:{page}'
            text = json.dumps({'receipt': receipt, 'page': page, 'fact': page * 7,
                               'padding': 'x' * 1_200_000}, separators=(',', ':'))
            result = {'content': [{'type': 'text', 'text': text}], 'isError': False}
            record({'event': 'tool_call', 'page': page, 'receipt': receipt,
                    'payload_bytes': len(text.encode()), 'payload_sha256': hashlib.sha256(text.encode()).hexdigest()})
        elif method == 'ping':
            result = {}
        else:
            return {'jsonrpc': '2.0', 'id': identity, 'error': {'code': -32601, 'message': 'Unknown method'}}
        return {'jsonrpc': '2.0', 'id': identity, 'result': result}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size <= 65536:
                self.send_error(413)
                return
            response = dispatch(json.loads(self.rfile.read(size)))
            raw = json.dumps(response).encode() if response is not None else b''
            self.send_response(200 if response is not None else 202)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.send_error(405)

        do_DELETE = do_GET

        def log_message(self, *args):
            pass

    try:
        if args.http:
            with HTTPServer(('127.0.0.1', 0), Handler) as server:
                print(json.dumps({'port': server.server_port}), flush=True)
                server.serve_forever()
        else:
            for line in sys.stdin.buffer:
                if len(line) > 65536:
                    break
                response = dispatch(json.loads(line))
                if response is not None:
                    sys.stdout.buffer.write((json.dumps(response, separators=(',', ':')) + '\n').encode())
                    sys.stdout.buffer.flush()
    finally:
        os.close(descriptor)


if __name__ == '__main__':
    main()
