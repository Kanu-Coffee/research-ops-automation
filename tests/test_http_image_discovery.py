"""Synthetic public HTTP discovery through import, Compose and SMTP."""

from email import policy
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import re
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
import unittest
from unittest.mock import patch
import urllib.request

from tests.delivery_fixtures import smtp_message_bytes, smtp_server
from tests.media_fixture import PDFS
from tests.support import isolated_settings
from tests.test_production_runner import response

from researchops.delivery.smtp_config import BuiltinDeliveryConfig, SmtpSettings, save_delivery_config
from researchops.runners.base import BaseRunner
from researchops.runners.codex import CodexRunner
from researchops.runners.fake import FakeRunner
from researchops.services.application import ApplicationService


def _png(red, green, blue):
    import struct
    import zlib
    def chunk(kind, content):
        return (struct.pack(">I", len(content)) + kind + content
                + struct.pack(">I", zlib.crc32(kind + content) & 0xffffffff))
    pixels = zlib.compress(bytes((0, red, green, blue)))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", pixels) + chunk(b"IEND", b""))


ALPHA = _png(12, 34, 56)
BETA = _png(210, 180, 90)


class SyntheticCatalogHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.state["requests"].append(
            {"method": "GET", "path": self.path, "body": b""})
        if self.path == "/catalog.html":
            self._send(200, b'<html><body><script src="/assets/catalog.js"></script></body></html>',
                       "text/html; charset=utf-8")
        elif self.path == "/assets/catalog.js":
            self._send(200, b"const API_ENDPOINT = '/api/catalog/search';\n"
                            b"const REQUEST_BODY = {\"product_codes\":[\"AX-1\",\"BY-2\"]};\n",
                       "application/javascript")
        elif self.path == "/media/alpha.png":
            self._send(200, ALPHA, "image/png")
        elif self.path == "/media/beta.png":
            self._send(200, BETA, "image/png")
        elif self.path == "/media/empty.png":
            self._send(200, b"", "image/png")
        elif self.path == "/media/spoof.png":
            self._send(200, b"<html>not an image</html>", "image/png")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.state["requests"].append(
            {"method": "POST", "path": self.path, "body": body})
        if self.path != "/api/catalog/search":
            self._send(404, b"not found", "text/plain")
            return
        self._send(200, json.dumps({
            "result": {
                "appliances": [{"sku": "AX-1", "name": "Synthetic Kettle",
                                "image": "/media/alpha.png"}],
                "books": {"id": "BY-2", "label": "Synthetic Atlas",
                          "picture": {"src": "/media/beta.png"}},
                "resilience": [
                    {"sku": "CX-3", "picture": "/media/missing.png"},
                    {"sku": "DX-4", "picture": "/media/empty.png"},
                    {"sku": "EX-5", "picture": "/media/spoof.png"},
                ],
            }
        }).encode(), "application/json")


class HybridRunner(BaseRunner):
    def __init__(self, test):
        self.test = test
        self.codex = CodexRunner()
        self.fake = FakeRunner()
        self.research_result = None

    def execute_research(self, input_dir, tmp_dir, output_dir, project_dir, context):
        self.research_result = self.codex.execute_research(
            input_dir, tmp_dir, output_dir, project_dir, context)
        return self.research_result

    def execute_compose(self, input_dir, tmp_dir, output_dir, project_dir, context):
        return self.fake.execute_compose(input_dir, tmp_dir, output_dir, project_dir, context)


class HttpImageDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = isolated_settings(self.root / "runtime")
        self.settings.environment = "production"
        self.settings.delivery.global_handoff_kill_switch = False
        save_delivery_config(BuiltinDeliveryConfig(enabled=True, auto_dispatch=False,
            smtp=SmtpSettings(host="smtp.example.test", sender_email="sender@example.test"),
            recipient_groups={"synthetic-team": ["recipient@example.test"]}),
            cast(Path, self.settings.paths.delivery_config_file))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticCatalogHandler)
        setattr(self.server, "state", {"requests": []})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.requests = getattr(self.server, "state")["requests"]
        self.runner = HybridRunner(self)

    def app(self):
        return ApplicationService(self.settings, custom_runner=self.runner)

    def create(self, app, task_id="http-discovery", *, allow_http_requests=True):
        app.tasks.create_production_task(task_id=task_id, name=task_id,
            instructions="Discover synthetic public product images and preserve their records.",
            runner_type="codex_exec", recipient_group_id="synthetic-team",
            schedule_enabled=False, allow_http_requests=allow_http_requests)

    def run_task(self, app, task_id="http-discovery"):
        run = app.runs.enqueue_run(task_id)
        result = app.runs.execute_run(run.run_id)
        archive = self.settings.paths.run_archive_dir / run.task_id / run.run_id
        return result, archive

    def deliver(self, app, run):
        smtp = smtp_server()
        handoff = app.delivery_repo.get_handoff_for_run(run.run_id)
        with patch("smtplib.SMTP", return_value=smtp), patch("smtplib.SMTP_SSL") as ssl:
            ok, _, message = app.smtp_dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok, message)
        ssl.assert_not_called()
        return BytesParser(policy=policy.default).parsebytes(smtp_message_bytes(smtp))

    def worker(self, *, resiliences=False):
        def process(*args, **kwargs):
            prompt = kwargs["stdin"].decode("utf-8")
            artifact_root = Path(re.search(r'"artifact_root": "([^"]+)"', prompt).group(1))
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(self.base_url + "/catalog.html", timeout=5) as opened:
                page = opened.read().decode("utf-8")
            script_path = re.search(r'<script src="([^"]+)"', page).group(1)
            with opener.open(self.base_url + script_path, timeout=5) as opened:
                script = opened.read().decode("utf-8")
            endpoint = re.search(r"API_ENDPOINT = '([^']+)'", script).group(1)
            request_body = re.search(r"REQUEST_BODY = (\{[^\n]+\});", script).group(1)
            request = urllib.request.Request(self.base_url + endpoint,
                data=request_body.encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with opener.open(request, timeout=5) as opened:
                catalog = json.loads(opened.read())

            products = [
                ("rec-alpha", "AX-1", "Synthetic Kettle",
                 catalog["result"]["appliances"][0]["image"]),
                ("rec-beta", "BY-2", "Synthetic Atlas",
                 catalog["result"]["books"]["picture"]["src"]),
            ]
            if resiliences:
                products.extend([
                    ("rec-missing", "CX-3", "Unavailable image", "/media/missing.png"),
                    ("rec-empty", "DX-4", "Empty image", "/media/empty.png"),
                    ("rec-spoof", "EX-5", "Spoofed image", "/media/spoof.png"),
                ])

            warnings, artifacts = [], []
            for record_id, sku, title, image_path in products:
                try:
                    with opener.open(self.base_url + image_path, timeout=5) as opened:
                        raw = opened.read()
                except Exception:
                    warnings.append(f"image unavailable for {sku}")
                    continue
                path = f"artifacts/{sku}.png"
                target = artifact_root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
                artifacts.append({"artifact_id": f"{sku}-image", "path": path,
                    "role": "inline_image", "mime_type": "image/png",
                    "record_ids": [record_id], "sha256": hashlib.sha256(raw).hexdigest(),
                    "size_bytes": len(raw), "source": None,
                    "on_failure": "continue", "announce_missing": False})

            document = {"status": "success" if not resiliences else "partial",
                "summary": "Synthetic catalog discovery completed.", "warnings": warnings,
                "records": [{"record_id": record_id, "title": title}
                            for record_id, _, title, _ in products],
                "coverage": {"complete": not resiliences, "expected_target_count": len(products),
                             "completed_target_count": len(products), "issues": []},
                "artifacts": artifacts}
            return response("codex_exec", document)
        return process

    def test_public_html_javascript_json_images_reach_compose_and_smtp_unchanged(self):
        app = self.app()
        self.create(app)
        with patch("shutil.which", return_value="/usr/bin/codex"), patch(
                "researchops.runners.production.run_bounded", side_effect=self.worker()):
            run, archive = self.run_task(app)
        self.assertEqual(run.status, "awaiting_receipt", run.error_message)
        self.assertTrue(self.runner.research_result.isolation["allow_http_requests"])
        self.assertTrue(self.runner.research_result.isolation["requested_shell_network_access"])

        posted = [item for item in self.requests if item["method"] == "POST"]
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["path"], "/api/catalog/search")
        self.assertEqual(json.loads(posted[0]["body"]),
                         {"product_codes": ["AX-1", "BY-2"]})
        paths = [item["path"] for item in self.requests]
        self.assertEqual(paths[:5], ["/catalog.html", "/assets/catalog.js",
                                     "/api/catalog/search", "/media/alpha.png",
                                     "/media/beta.png"])

        composition = app.run_repo.get_composition_input(run.run_id)
        self.assertEqual([item["record_ids"] for item in composition["inline_artifacts"]],
                         [["rec-alpha"], ["rec-beta"]])
        wire = self.deliver(app, run)
        images = [part for part in wire.walk() if part.get_content_type() == "image/png"]
        self.assertEqual([part.get_payload(decode=True) for part in images], [ALPHA, BETA])
        cids = [item["cid"] for item in composition["inline_artifacts"]]
        self.assertEqual([str(part["Content-ID"]) for part in images],
                         ["<" + cid + ">" for cid in cids])
        for record_id, cid in zip(("rec-alpha", "rec-beta"), cids):
            html = (archive / "email.html").read_text(encoding="utf-8")
            block = re.search(r'<div data-record-id="' + record_id + r'">(.*?)</div>', html)
            self.assertIn("cid:" + cid, block.group(1))

    def test_failed_empty_and_spoofed_images_are_excluded_but_mail_continues(self):
        app = self.app()
        self.create(app, task_id="http-resilience")
        with patch("shutil.which", return_value="/usr/bin/codex"), patch(
                "researchops.runners.production.run_bounded",
                side_effect=self.worker(resiliences=True)):
            run, archive = self.run_task(app, "http-resilience")
        self.assertEqual(run.status, "awaiting_receipt", run.error_message)
        composition = app.run_repo.get_composition_input(run.run_id)
        self.assertEqual([item["record_ids"] for item in composition["inline_artifacts"]],
                         [["rec-alpha"], ["rec-beta"]])
        self.assertIn("rec-missing",
                      [record["record_id"] for record in composition["reportable_records"]])
        report = json.loads((archive / "artifact-report.json").read_bytes())
        statuses = {entry["path"]: entry for entry in report["entries"]}
        self.assertNotIn("artifacts/CX-3.png", statuses)
        for path in ("artifacts/DX-4.png", "artifacts/EX-5.png"):
            self.assertEqual(statuses[path]["status"], "failed")
            self.assertEqual(statuses[path]["reason_code"], "invalid_media")
            self.assertFalse(statuses[path]["include_in_compose"])
        self.assertTrue(any("CX-3" in warning for warning in composition["result"]["warnings"]))
        wire = self.deliver(app, run)
        images = [part for part in wire.walk() if part.get_content_type() == "image/png"]
        self.assertEqual([part.get_payload(decode=True) for part in images], [ALPHA, BETA])
        html = [part.get_payload(decode=True) for part in wire.walk()
                if part.get_content_type() == "text/html"][0]
        self.assertIn(b'rec-missing', html)
        self.assertNotIn(b"<html>not an image</html>", html)

    def test_http_off_keeps_existing_local_attachment(self):
        app = self.app()
        self.create(app, task_id="http-off", allow_http_requests=False)
        pdf = PDFS["doc_first"]

        def process(*args, **kwargs):
            prompt = kwargs["stdin"].decode("utf-8")
            artifact_root = Path(re.search(r'"artifact_root": "([^"]+)"', prompt).group(1))
            target = artifact_root / "attachments/document.pdf"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(pdf)
            document = {"status": "success", "summary": "Existing attachment only.",
                "records": [{"record_id": "rec-attachment", "title": "Synthetic document"}],
                "coverage": {"complete": True, "expected_target_count": 1,
                             "completed_target_count": 1, "issues": []},
                "warnings": [], "artifacts": [{"artifact_id": "document",
                    "path": "attachments/document.pdf", "role": "attachment",
                    "mime_type": "application/pdf", "record_ids": ["rec-attachment"],
                    "sha256": hashlib.sha256(pdf).hexdigest(), "size_bytes": len(pdf),
                    "source": None}]}
            return response("codex_exec", document)

        with patch("shutil.which", return_value="/usr/bin/codex"), patch(
                "researchops.runners.production.run_bounded", side_effect=process):
            run, _ = self.run_task(app, "http-off")
        self.assertEqual(run.status, "awaiting_receipt", run.error_message)
        self.assertEqual(self.requests, [])
        research_result = self.runner.research_result
        self.assertIsNotNone(research_result)
        self.assertFalse(research_result.isolation["allow_http_requests"])
        self.assertFalse(research_result.isolation["requested_shell_network_access"])
        wire = self.deliver(app, run)
        attachments = [part for part in wire.walk()
                       if part.get_content_type() == "application/pdf"]
        self.assertEqual([part.get_payload(decode=True) for part in attachments], [pdf])
