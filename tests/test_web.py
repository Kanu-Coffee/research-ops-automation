"""Unit and integration tests for ResearchOps Web UI (Phases 6 & 7)."""

from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.parse
import urllib.request
import urllib.error

from researchops.config import load_settings
from researchops.domain.events import AuditEvent
from researchops.services.application import ApplicationService
from tests.web_support import AuthenticatedWebRouter as WebRouter, admin_session, authenticated_headers
from researchops.web.server import create_web_server, ResearchOpsServer
from tests.package_support import register_template
from tests.support import isolated_settings, fixture_runner, register_fixture_task


class TestWebUI(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_path = Path(self.tmp_dir)

        # Setup isolated DB and archives
        db_path = self.tmp_path / "test.db"
        archive_dir = self.tmp_path / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)

        settings = isolated_settings(self.tmp_path)
        settings.paths.database = db_path
        settings.paths.run_archive_dir = archive_dir

        self.app = ApplicationService(settings, custom_runner=fixture_runner(settings))
        register_fixture_task(self.app)

        # Enqueue and mark run as succeeded
        run_record = self.app.runs.enqueue_run("software-releases")
        self.run_id = run_record.run_id
        now = datetime.now(timezone.utc).isoformat()
        _, lease = self.app.run_repo.claim_next_run("web-fixture", run_id=self.run_id)
        self.app.run_repo.update_run_status(self.run_id, status="succeeded", phase="finalize",
                                            finished_at=now, fencing_token=lease.fencing_token)
        self.app.run_repo.mark_cleanup_verified(self.run_id, lease.fencing_token)
        self.app.run_repo.release_lease(self.run_id, lease.fencing_token)
        self.app.state_repo.save_audit_event(
            AuditEvent(
                entity_type="run",
                entity_id=self.run_id,
                event_type="run_completed",
                details={"status": "succeeded"}
            )
        )

        # Create sample run archive files in run_archive_dir / task_id / run_id
        run_arch_dir = archive_dir / "software-releases" / self.run_id
        run_arch_dir.mkdir(parents=True, exist_ok=True)
        (run_arch_dir / "result.json").write_text(json.dumps({"test": "result"}), encoding="utf-8")
        (run_arch_dir / "email.html").write_text("<html><body><h1>Hello Market</h1></body></html>", encoding="utf-8")
        (run_arch_dir / "email.txt").write_text("Hello Market Plain Text", encoding="utf-8")

        self.router = WebRouter(self.app)
        self.raw_router = self.router
        router = self.raw_router
        class Browser:
            def handle_request(_self, *args, **kwargs):
                kwargs.setdefault("headers", {"Host": "localhost", "Origin": "http://localhost",
                                               "X-CSRF-Token": router.csrf_token})
                return router.handle_request(*args, **kwargs)
        self.router = Browser()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_mutations_require_origin_csrf_and_trusted_proxy(self):
        route = "/tasks/software-releases/enable"
        for headers, client in [
            ({"Host": "localhost"}, "127.0.0.1"),
            ({"Host": "localhost", "Origin": "https://evil.invalid", "X-CSRF-Token": self.raw_router.csrf_token}, "127.0.0.1"),
            ({"Host": "localhost", "Origin": "http://localhost", "X-CSRF-Token": "wrong"}, "127.0.0.1"),
            ({"Host": "localhost", "Origin": "http://localhost", "X-CSRF-Token": self.raw_router.csrf_token}, "192.0.2.8"),
            ({"Host": "evil.invalid", "Origin": "http://evil.invalid", "X-CSRF-Token": self.raw_router.csrf_token}, "127.0.0.1"),
        ]:
            with self.subTest(headers=headers, client=client):
                status, _, _ = self.raw_router.handle_request("POST", route, headers=headers, client_ip=client)
                self.assertEqual(status, 403)

    def test_public_bind_and_large_body_rejected(self):
        from researchops.errors import ConfigError
        with self.assertRaises(ConfigError):
            create_web_server(self.app, host="0.0.0.0", port=0)
        status, _, _ = self.raw_router.handle_request("POST", "/tasks/software-releases/run",
            b"x" * (self.app.settings.web.max_request_bytes + 1))
        self.assertEqual(status, 413)

    def test_remote_proxy_explicit_bind_and_cli_override_gate(self):
        from researchops.errors import ConfigError
        config = self.app.settings.web
        config.bind = "192.168.50.10"
        config.trusted_proxy_cidrs = ["10.50.0.2/32"]
        with self.assertRaises(ConfigError):
            create_web_server(self.app)
        config.allow_remote_proxy = True
        with patch("researchops.web.server.ResearchOpsServer") as server:
            create_web_server(self.app)
            self.assertEqual(server.call_args.args[0], ("192.168.50.10", 8765))
            for host in ("0.0.0.0", "8.8.8.8", "::"):
                with self.subTest(host=host), self.assertRaises(ConfigError):
                    create_web_server(self.app, host=host)
            self.assertEqual(server.call_count, 1)

    def test_remote_https_proxy_host_origin_csrf_and_actual_peer(self):
        config = self.app.settings.web
        config.allow_remote_proxy = True
        config.trusted_proxy_cidrs = ["10.50.0.2/32"]
        config.allowed_hosts = ["research.example.test"]
        headers = {"Host": "research.example.test", "X-Forwarded-Host": "research.example.test",
                   "X-Forwarded-Proto": "https", "Origin": "https://research.example.test",
                   "X-CSRF-Token": self.raw_router.csrf_token}
        status, _, _ = self.raw_router.handle_request("GET", "/dashboard", headers=headers, client_ip="10.50.0.2")
        self.assertEqual(status, 200)
        # Unknown route proves guards passed, without mutating task/SMTP state.
        self.assertEqual(self.raw_router.handle_request("POST", "/missing", headers=headers,
                                                       client_ip="10.50.0.2")[0], 404)
        for change, peer in (({"X-Forwarded-For": "10.50.0.2"}, "192.168.50.20"),
                             ({"Host": "evil.invalid"}, "10.50.0.2"),
                             ({"Origin": "https://evil.invalid"}, "10.50.0.2"),
                             ({"X-CSRF-Token": "wrong"}, "10.50.0.2"),
                             ({"X-Forwarded-Host": "evil.invalid"}, "10.50.0.2")):
            with self.subTest(change=change, peer=peer):
                self.assertEqual(self.raw_router.handle_request("POST", "/missing",
                    headers={**headers, **change}, client_ip=peer)[0], 403)

    def test_delivery_page_hides_password_and_offers_connection_check(self):
        from researchops.delivery.smtp_config import BuiltinDeliveryConfig, SmtpSettings
        self.app.delivery.save_delivery_config(BuiltinDeliveryConfig(smtp=SmtpSettings(password="test-secret-do-not-render")))
        status, _, body = self.router.handle_request("GET", "/delivery")
        self.assertEqual(status, 200)
        self.assertNotIn(b"test-secret-do-not-render", body)
        self.assertIn(b'action="/delivery/test-connection"', body)
        self.assertIn(b'name="csrf_token"', body)
        self.assertNotIn(b'name="auto_dispatch"', body)

    def test_operator_form_policy_preserves_same_origin_post(self):
        for route in ("/dashboard", "/delivery", "/tasks/new", "/tasks/software-releases"):
            with self.subTest(route=route):
                status, headers, _ = self.router.handle_request("GET", route)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Referrer-Policy"], "same-origin")

    def test_https_delivery_form_save_and_null_origin_rejection(self):
        import re
        from researchops.delivery.smtp_config import load_delivery_config
        self.app.settings.web.trusted_proxy_cidrs = ["10.50.0.2/32"]
        self.app.settings.web.allowed_hosts = ["research.example.test"]
        proxy_headers = {"Host": "research.example.test", "X-Forwarded-Proto": "https",
                         "X-Forwarded-Host": "research.example.test"}
        status, headers, body = self.raw_router.handle_request("GET", "/delivery",
            headers=proxy_headers, client_ip="10.50.0.2")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Referrer-Policy"], "same-origin")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', body).group(1).decode()
        form = urllib.parse.urlencode({"csrf_token": token, "host": "smtp.gmail.com",
            "port": "587", "use_tls": "true", "enabled": "true",
            "username": "operator@example.test", "password": "fixture-app-password"}).encode()
        # Browser origin for same-origin form navigation remains the HTTPS site;
        # no forged Origin header is needed in the page's HTML or JavaScript.
        status, headers, _ = self.raw_router.handle_request("POST", "/delivery/save", form,
            "application/x-www-form-urlencoded", client_ip="10.50.0.2",
            headers={**proxy_headers, "Origin": "https://research.example.test"})
        self.assertEqual(status, 303)
        self.assertIn("success=", headers["Location"])
        self.assertEqual(load_delivery_config(self.app.settings.paths.delivery_config_file).smtp.username,
                         "operator@example.test")
        before = self.app.settings.paths.delivery_config_file.read_bytes()
        for origin in ("null", "https://evil.invalid", "http://research.example.test"):
            status, _, _ = self.raw_router.handle_request("POST", "/delivery/save", form,
                "application/x-www-form-urlencoded", client_ip="10.50.0.2",
                headers={**proxy_headers, "Origin": origin})
            self.assertEqual(status, 403)
            self.assertEqual(self.app.settings.paths.delivery_config_file.read_bytes(), before)

    def test_preview_and_artifact_keep_no_referrer(self):
        for route in (f"/runs/{self.run_id}/preview/html", f"/runs/{self.run_id}/artifacts/email.html"):
            with self.subTest(route=route):
                with patch.object(self.raw_router, "_dispatch", return_value=(200,
                    {"Content-Type": "text/html"}, b"<html><body>Untrusted document</body></html>")):
                    _, headers, _ = self.router.handle_request("GET", route)
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_production_ui_creates_first_recipient_group_and_preserves_secret(self):
        from researchops.delivery.smtp_config import BuiltinDeliveryConfig, SmtpSettings, load_delivery_config
        self.app.delivery.save_delivery_config(BuiltinDeliveryConfig(
            smtp=SmtpSettings(password="private-group-secret"), recipient_groups={}))
        status, _, body = self.router.handle_request("GET", "/delivery?tab=groups")
        self.assertEqual(status, 200)
        self.assertIn(b'action="/delivery/groups/create"', body)
        self.assertIn(b'name="emails"', body)
        self.assertNotIn(b'private-group-secret', body)
        post = urllib.parse.urlencode({"group_id": "research-team",
            "emails": "first@example.test\nsecond@example.test,first@example.test"}).encode()
        status, headers, _ = self.router.handle_request("POST", "/delivery/groups/create", post)
        self.assertEqual(status, 303)
        self.assertIn("success=", headers["Location"])
        self.assertNotIn("example.test", headers["Location"])
        config = load_delivery_config(self.app.settings.paths.delivery_config_file)
        self.assertEqual(config.recipient_groups, {"research-team": ["first@example.test", "second@example.test"]})
        self.assertEqual(config.smtp.password, "private-group-secret")
        _, _, body = self.router.handle_request("GET", "/tasks/new")
        self.assertIn(b'<option value="research-team">', body)
        self.assertNotIn(b'first@example.test', body)

    def test_group_creation_rejects_invalid_or_existing_group_without_partial_save(self):
        from researchops.delivery.smtp_config import BuiltinDeliveryConfig
        self.app.delivery.save_delivery_config(BuiltinDeliveryConfig(recipient_groups={"existing-group": ["existing@example.test"]}))
        before = self.app.delivery.get_delivery_config().recipient_groups
        for group, emails in [("bad/group", "valid@example.test"), ("new-group", "valid@example.test\ninvalid"),
                              ("existing-group", "different@example.test"), ("new-group", "")]:
            with self.subTest(group=group, emails=emails):
                post = urllib.parse.urlencode({"group_id": group, "emails": emails}).encode()
                status, headers, _ = self.router.handle_request("POST", "/delivery/groups/create", post)
                self.assertEqual(status, 303)
                self.assertIn("error=", headers["Location"])
                self.assertEqual(self.app.delivery.get_delivery_config().recipient_groups, before)

    def test_gmail_save_defaults_sender_and_normalizes_app_password(self):
        from researchops.delivery.smtp_config import load_delivery_config
        post = urllib.parse.urlencode({"host": "smtp.gmail.com", "port": "587", "use_tls": "true",
            "username": "operator@example.test", "password": "abcd efgh ijkl mnop", "enabled": "true"}).encode()
        status, headers, _ = self.router.handle_request("POST", "/delivery/save", post)
        self.assertEqual(status, 303)
        self.assertIn("success=", headers["Location"])
        cfg = load_delivery_config(self.app.settings.paths.delivery_config_file)
        self.assertEqual(cfg.smtp.sender_email, "operator@example.test")
        self.assertEqual(cfg.smtp.password, "abcdefghijklmnop")
        self.assertTrue(cfg.enabled)
        _, _, body = self.router.handle_request("GET", "/delivery")
        self.assertIn(b'support.google.com/mail/answer/185833', body)
        self.assertNotIn(b'abcdefghijklmnop', body)
        self.assertNotIn(b'action="/delivery/send-test"', body)
        self.assertNotIn(b'action="/delivery/dispatch-all"', body)

    def test_invalid_smtp_port_is_not_silently_replaced(self):
        status, headers, body = self.router.handle_request("POST", "/delivery/save", b"port=banana&use_tls=true")
        self.assertEqual(status, 400)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b'SMTP port must be a number', body)

    def test_primary_run_forms_do_not_force_dry_run(self):
        for route in ("/tasks", "/tasks/software-releases"):
            with self.subTest(route=route):
                status, _, body = self.router.handle_request("GET", route)
                self.assertEqual(status, 200)
                self.assertIn("지금 실행".encode(), body)
                self.assertNotIn(b'name="dry_run"', body)

    def test_production_create_and_run_is_direct_and_duplicate_idempotent(self):
        from researchops.delivery.smtp_config import BuiltinDeliveryConfig
        self.app.settings.environment = "production"
        self.app.settings.delivery.global_handoff_kill_switch = False
        self.app.delivery.save_delivery_config(BuiltinDeliveryConfig(enabled=True,
            recipient_groups={"research-team": ["reader@example.test"]}))
        post = urllib.parse.urlencode({"task_id": "production-ui", "name": "운영 조사",
            "instructions": "공식 공개 출처를 조사하고 핵심 변경사항과 링크를 한국어로 정리하라.",
            "runner_type": "codex_exec", "recipient_group_id": "research-team", "cron": "0 9 * * 1-5",
            "schedule_enabled": "true", "action": "create_and_run", "request_key": "production-ui-double-click"}).encode()
        first = self.router.handle_request("POST", "/tasks/production/create", post)
        self.assertEqual(first[0], 303)
        self.assertIn("/runs/run-", first[1]["Location"])
        second = self.router.handle_request("POST", "/tasks/production/create", post)
        self.assertEqual(first[1]["Location"], second[1]["Location"])
        runs = self.app.runs.list_runs(task_id="production-ui")
        self.assertEqual(len(runs), 1)
        self.assertFalse(self.app.run_repo.get_execution_controls(runs[0].run_id)["force_dry_run"])
        self.assertEqual(runs[0].trigger_type, "manual")
        status = self.app.task_repo.get_task_status("production-ui")
        self.assertEqual(status["enabled"], 1)
        self.assertEqual(status["delivery_mode"], "handoff")
        _, _, body = self.router.handle_request("GET", "/tasks/production-ui")
        self.assertNotIn(b"Approve Live Delivery", body)
        self.assertIn(b"Asia/Seoul", body)

    def test_production_create_missing_group_explains_input_and_creates_nothing(self):
        from researchops.delivery.smtp_config import BuiltinDeliveryConfig
        self.app.settings.environment = "production"
        self.app.delivery.save_delivery_config(BuiltinDeliveryConfig(recipient_groups={}))
        status, _, body = self.router.handle_request("GET", "/tasks/new")
        self.assertEqual(status, 200)
        self.assertIn(b'action="/tasks/production/create"', body)
        self.assertIn(b'href="/delivery?panel=group#recipient-groups"', body)
        post = urllib.parse.urlencode({"task_id": "missing-group", "name": "Missing", "instructions": "Research official sources",
            "runner_type": "codex_exec", "recipient_group_id": "absent-group"}).encode()
        response = self.router.handle_request("POST", "/tasks/production/create", post)
        self.assertEqual(response[0], 400)
        self.assertIn(b'Research official sources', response[2])
        self.assertIsNone(self.app.task_repo.get_active_version("missing-group"))

    def test_duplicate_run_submission_is_idempotent(self):
        body = urllib.parse.urlencode({"request_key": "test-browser-double-click"}).encode()
        first = self.router.handle_request("POST", "/tasks/software-releases/run", body,
                                          "application/x-www-form-urlencoded")
        second = self.router.handle_request("POST", "/tasks/software-releases/run", body,
                                           "application/x-www-form-urlencoded")
        self.assertEqual(first[0], 303)
        self.assertEqual(first[1]["Location"], second[1]["Location"])

    def test_preview_is_sandboxed_and_artifact_is_download_only(self):
        status, headers, _ = self.router.handle_request("GET", f"/runs/{self.run_id}/preview/html")
        self.assertEqual(status, 200)
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        status, headers, _ = self.router.handle_request("GET", f"/runs/{self.run_id}/artifacts/result.json")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        nested = self.app.settings.paths.run_archive_dir / "software-releases" / self.run_id / "logs" / "research.stdout"
        nested.parent.mkdir()
        nested.write_text("phase evidence", encoding="utf-8")
        status, headers, body = self.router.handle_request("GET", f"/runs/{self.run_id}/artifacts/logs/research.stdout")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"phase evidence")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="research.stdout"; filename*=UTF-8\'\'research.stdout')


    # ----------------------------------------------------
    # Phase 6 Read-only View Tests
    # ----------------------------------------------------
    def test_router_root_redirect(self):
        status, headers, _ = self.router.handle_request("GET", "/")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/dashboard")

    def test_router_dashboard(self):
        status, headers, body = self.router.handle_request("GET", "/dashboard")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        html = body.decode("utf-8")
        self.assertIn("ResearchOps", html)
        self.assertIn("대시보드", html)
        self.assertIn(self.run_id, html)

    def test_router_tasks_list_and_detail(self):
        status, _, body = self.router.handle_request("GET", "/tasks")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn("software-releases", html)

        # Detail of existing task
        status, _, body = self.router.handle_request("GET", "/tasks/software-releases")
        self.assertEqual(status, 200)
        detail_html = body.decode("utf-8")
        self.assertIn("software-releases", detail_html)
        self.assertIn(self.run_id, detail_html)
        self.assertIn("지금 실행", detail_html)

        # Detail of non-existing task
        status, _, _ = self.router.handle_request("GET", "/tasks/nonexistent-task")
        self.assertEqual(status, 404)

    def test_router_runs_list_and_detail(self):
        status, _, body = self.router.handle_request("GET", "/runs")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn(self.run_id, html)

        # Filtered runs
        status, _, body = self.router.handle_request("GET", "/runs?status=succeeded")
        self.assertEqual(status, 200)
        self.assertIn(self.run_id, body.decode("utf-8"))

        status, _, body = self.router.handle_request("GET", "/runs?status=failed")
        self.assertEqual(status, 200)
        self.assertNotIn(self.run_id, body.decode("utf-8"))

        # Run detail
        status, _, body = self.router.handle_request("GET", f"/runs/{self.run_id}?tab=files")
        self.assertEqual(status, 200)
        run_html = body.decode("utf-8")
        self.assertIn(self.run_id, run_html)
        self.assertIn("email.html", run_html)
        self.assertIn("result.json", run_html)

        # Non-existing run
        status, _, _ = self.router.handle_request("GET", "/runs/run-99999999-xxxx")
        self.assertEqual(status, 404)

    def test_router_email_preview_sandboxed(self):
        status, headers, body = self.router.handle_request("GET", f"/runs/{self.run_id}/preview/html")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("default-src 'none'", headers.get("Content-Security-Policy", ""))
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("<h1>Hello Market</h1>", body.decode("utf-8"))

        # Plain text preview
        status, headers, body = self.router.handle_request("GET", f"/runs/{self.run_id}/preview/text")
        self.assertEqual(status, 200)
        self.assertIn("text/plain", headers["Content-Type"])
        self.assertEqual(body.decode("utf-8"), "Hello Market Plain Text")

    def test_router_artifacts_and_security(self):
        # Valid artifact
        status, headers, body = self.router.handle_request("GET", f"/runs/{self.run_id}/artifacts/result.json")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(json.loads(body.decode("utf-8")), {"test": "result"})

        # Traversal attempt rejected
        status, _, _ = self.router.handle_request("GET", f"/runs/{self.run_id}/artifacts/../test.db")
        self.assertEqual(status, 400)

        # Missing artifact
        status, _, _ = self.router.handle_request("GET", f"/runs/{self.run_id}/artifacts/missing.json")
        self.assertEqual(status, 404)

    def test_router_doctor_and_api(self):
        # Doctor
        status, _, body = self.router.handle_request("GET", "/doctor")
        self.assertEqual(status, 200)
        self.assertIn("시스템 상태", body.decode("utf-8"))

        # Polling API
        status, headers, body = self.router.handle_request("GET", f"/api/runs/{self.run_id}/status")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data.get("run_id"), self.run_id)
        self.assertEqual(data.get("status"), "succeeded")

    def test_router_method_not_allowed(self):
        status, _, _ = self.router.handle_request("DELETE", "/dashboard")
        self.assertEqual(status, 405)

    # ----------------------------------------------------
    # Phase 7 Interactive Action & Authoring Tests
    # ----------------------------------------------------
    def test_task_create_view_and_retired_working_copy_routes(self):
        status, _, body = self.router.handle_request("GET", "/tasks/new")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        for name in ("task_md", "email_spec_md", "schedule_preset"):
            self.assertIn(f'name="{name}"', html)
        self.assertNotIn("/tasks/drafts", html)
        for method, path in (("GET", "/tasks/drafts"), ("GET", "/tasks/drafts/legacy"),
                             ("POST", "/tasks/drafts/create"), ("POST", "/tasks/drafts/legacy/save"),
                             ("POST", "/tasks/drafts/legacy/validate"), ("POST", "/tasks/drafts/legacy/seal")):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.router.handle_request(method, path)[0], 410)

    def test_candidate_package_workflow_remains_available_without_working_copy(self):
        version = register_template(self.app, "test-created-task")
        candidate = self.app.runs.enqueue_run(version.task_id, trigger_type="candidate_dry_run",
                                              candidate_version_hash=version.version_hash)
        self.assertEqual(self.app.runs.execute_run(candidate.run_id).status, "succeeded")
        act_body = f"version_hash={version.version_hash}".encode("utf-8")
        status, headers, _ = self.router.handle_request("POST", f"/tasks/{version.task_id}/activate",
                                                       act_body, "application/x-www-form-urlencoded")
        self.assertEqual(status, 303)
        self.assertIn("success=", headers.get("Location", ""))
        self.assertEqual(self.app.task_repo.get_active_version(version.task_id).version_hash, version.version_hash)
        with self.app.db.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_drafts").fetchone()[0], 0)

    def test_task_toggle_actions(self):
        # Enable schedule
        status, headers, _ = self.router.handle_request("POST", "/tasks/software-releases/enable")
        self.assertEqual(status, 303)
        st = self.app.task_repo.get_task_status("software-releases")
        self.assertEqual(st["enabled"], 1)

        # Disable schedule
        status, headers, _ = self.router.handle_request("POST", "/tasks/software-releases/disable")
        self.assertEqual(status, 303)
        st = self.app.task_repo.get_task_status("software-releases")
        self.assertEqual(st["enabled"], 0)

        # Approve delivery
        status, headers, _ = self.router.handle_request(
            "POST", "/tasks/software-releases/approve-delivery", b"approved=true", "application/x-www-form-urlencoded"
        )
        self.assertEqual(status, 303)
        st = self.app.task_repo.get_task_status("software-releases")
        self.assertEqual(st["delivery_approved"], 0)  # no valid SMTP config / candidate evidence

    def test_run_trigger_and_actions(self):
        # POST /tasks/software-releases/run (Run Now)
        post_body = b"dry_run=true"
        status, headers, _ = self.router.handle_request(
            "POST", "/tasks/software-releases/run", post_body, "application/x-www-form-urlencoded"
        )
        self.assertEqual(status, 303)
        loc = headers.get("Location", "")
        self.assertIn("/runs/run-", loc)
        new_run_id = loc.split("/runs/")[1].split("?")[0]

        # Verify run was enqueued
        run = self.app.run_repo.get_run(new_run_id)
        self.assertIsNotNone(run)

        self.assertEqual(run.status, "queued")  # HTTP never starts the worker

        # Cancel action
        status, headers, _ = self.router.handle_request("POST", f"/runs/{new_run_id}/cancel")
        self.assertEqual(status, 303)

        # Retry action
        status, headers, _ = self.router.handle_request("POST", f"/runs/{new_run_id}/retry")
        self.assertEqual(status, 303)
        retried_loc = headers.get("Location", "")
        self.assertIn("/runs/run-", retried_loc)

    def test_router_delivery_settings_and_actions(self):
        # 1. GET /delivery
        status, _, body = self.router.handle_request("GET", "/delivery")
        self.assertEqual(status, 200)
        html_content = body.decode("utf-8")
        self.assertIn("메일 설정", html_content)
        self.assertIn('name="sender_profile_id"', html_content)

        # 2. POST /delivery/save
        save_body = b"host=smtp.example.test&port=587&use_tls=true&username=webuser&password=webpwd&sender_email=web@example.test&sender_name=WebSender&auto_dispatch=true"
        status, headers, _ = self.router.handle_request(
            "POST", "/delivery/save", save_body, "application/x-www-form-urlencoded"
        )
        self.assertEqual(status, 303)
        self.assertIn("success=", headers.get("Location", ""))

        cfg = self.app.delivery.get_delivery_config()
        self.assertEqual(cfg.smtp.host, "smtp.example.test")
        self.assertEqual(cfg.smtp.port, 587)
        self.assertEqual(cfg.smtp.username, "webuser")

        # 3. POST /delivery/groups/add
        add_body = b"group_id=release-team&email=lead@example.test"
        status, headers, _ = self.router.handle_request(
            "POST", "/delivery/groups/add", add_body, "application/x-www-form-urlencoded"
        )
        self.assertEqual(status, 303)
        cfg = self.app.delivery.get_delivery_config()
        self.assertIn("lead@example.test", cfg.recipient_groups["release-team"])

        # 4. POST /delivery/groups/remove
        remove_body = b"group_id=release-team&email=lead@example.test"
        status, headers, _ = self.router.handle_request(
            "POST", "/delivery/groups/remove", remove_body, "application/x-www-form-urlencoded"
        )
        self.assertEqual(status, 303)
        cfg = self.app.delivery.get_delivery_config()
        self.assertNotIn("lead@example.test", cfg.recipient_groups["release-team"])

        # 5. POST /delivery/test-connection (mocked)
        with patch.object(self.app.delivery, "test_smtp_connection", return_value=(True, "Connected!")):
            status, headers, _ = self.router.handle_request("POST", "/delivery/test-connection")
            self.assertEqual(status, 303)
            self.assertIn("success=Connected%21", headers.get("Location", ""))


class TestWebServerLive(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_path = Path(self.tmp_dir)

        db_path = self.tmp_path / "test.db"
        archive_dir = self.tmp_path / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)

        settings = isolated_settings(self.tmp_path)
        settings.paths.database = db_path
        settings.paths.run_archive_dir = archive_dir

        self.app = ApplicationService(settings, custom_runner=fixture_runner(settings))
        register_fixture_task(self.app)

        # Bind to port 0 for an OS-assigned ephemeral free port
        self.server = create_web_server(self.app, host="127.0.0.1", port=0)
        self.host, self.port = self.server.server_address
        self.browser_headers = authenticated_headers(self.app)

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_live_http_dashboard_and_preview(self):
        url = f"http://{self.host}:{self.port}/dashboard"
        with urllib.request.urlopen(urllib.request.Request(url, headers=self.browser_headers), timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            content = resp.read().decode("utf-8")
            self.assertIn("ResearchOps", content)
            self.assertIn("최근 실행", content)
            self.assertIn('href="/tasks/new"', content)

    def test_live_http_put_rejection(self):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        conn.request("PUT", "/dashboard", body=b"test")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 405)
        conn.close()

    def test_untrusted_socket_peer_rejected_before_body_read_or_route(self):
        self.app.settings.web.trusted_proxy_cidrs = ["10.50.0.2/32"]
        conn = http.client.HTTPConnection(self.host, self.port, timeout=2)
        try:
            with patch.object(self.server.router, "handle_request") as route:
                conn.request("POST", "/delivery/send-test", headers={
                    "Content-Length": "1000", "X-Forwarded-For": "10.50.0.2",
                    "X-Real-IP": "10.50.0.2"})  # no body: must reject immediately
                response = conn.getresponse()
                self.assertEqual(response.status, 403)
                self.assertIn(b"Untrusted proxy peer", response.read())
                route.assert_not_called()
        finally:
            conn.close()

    def test_live_http_post_run_and_redirect(self):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        post_data = urllib.parse.urlencode({"dry_run": "true"}).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "Origin": f"http://{self.host}:{self.port}",
                   **self.browser_headers}

        conn.request("POST", "/tasks/software-releases/run", body=post_data, headers=headers)
        resp = conn.getresponse()
        self.assertEqual(resp.status, 303)
        loc = resp.getheader("Location")
        self.assertIn("/runs/run-", loc)
        conn.close()

    def test_live_http_production_group_task_schedule_flow(self):
        self.app.settings.environment = "production"
        self.app.settings.web.allow_insecure_local_auth = True
        self.app.settings.delivery.global_handoff_kill_switch = False
        origin = f"http://{self.host}:{self.port}"
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Origin": origin,
                   **self.browser_headers}
        requests = [
            ("/delivery/save", {"host": "smtp.gmail.com", "port": "587", "use_tls": "true",
                "username": "operator@example.test", "password": "http-test-app-password", "enabled": "true"}),
            ("/delivery/groups/create", {"group_id": "http-operators", "emails": "operator@example.test"}),
            ("/tasks/production/create", {"task_id": "http-research", "name": "HTTP Research",
                "instructions": "Research official public documents and summarize changes with source links.",
                "runner_type": "antigravity_exec", "recipient_group_id": "http-operators",
                "cron": "30 8 * * 1-5", "schedule_enabled": "true", "action": "create",
                "request_key": "http-production-publication"}),
        ]
        for path, fields in requests:
            with self.subTest(path=path):
                conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
                try:
                    conn.request("POST", path, body=urllib.parse.urlencode(fields).encode(), headers=headers)
                    response = conn.getresponse()
                    response.read()
                    self.assertEqual(response.status, 303)
                    self.assertIn("success=", response.getheader("Location"))
                finally:
                    conn.close()
        with urllib.request.urlopen(urllib.request.Request(origin + "/tasks/http-research", headers=self.browser_headers), timeout=5) as response:
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assertIn(b"30 8 * * 1-5", body)
            self.assertIn(b"Antigravity (Agy)", body)
            self.assertIn("Research · 조사".encode(), body)
            self.assertIn("Compose · 메일 작성".encode(), body)
            self.assertIn(b"http-operators", body)
            self.assertIn("예약 중".encode(), body)
            self.assertNotIn(b"http-test-app-password", body)
            self.assertNotIn(b"operator@example.test", body)
        self.assertEqual(self.app.runs.list_runs(task_id="http-research"), [])


if __name__ == "__main__":
    unittest.main()
