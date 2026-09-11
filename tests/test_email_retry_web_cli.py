"""Mail-only retry adapters: explicit uncertainty, stable commands, no sending."""

from contextlib import nullcontext, redirect_stderr, redirect_stdout
from html.parser import HTMLParser
import http.client
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from researchops.cli.main import main
from researchops.errors import DeliveryError
from researchops.services.application import ApplicationService
from tests.web_support import AuthenticatedWebRouter as WebRouter, admin_session, authenticated_headers
from researchops.web.smtp_views import render_email_retry
from researchops.web.server import create_web_server
from researchops.web.views import render_run_detail
from tests.support import isolated_settings, fixture_runner


def retry_state(**changes):
    return {"eligible": True, "uncertain": False, "can_retry_uncertain": False,
            "can_republish": False, "block_reason": None, "next_attempt_at": None,
            "attempts": [{"job_id": "attempt-one", "attempt_number": 1, "status": "failed",
                "created_at": "2026-09-10T11:07:20+00:00", "updated_at": "2026-09-10T11:08:20+00:00",
                "error": "SMTP connection failed", "mime_bytes": "SECRET_SENTINEL",
                "envelope_json": "SECRET_SENTINEL"}], **changes}


def run_data(**changes):
    return {"run": {"run_id": "synthetic-run", "task_id": "synthetic-task", "status": "needs_attention",
                "phase": "finalize", "local_date": "2026-09-10"},
            "research": {"summary": "Synthetic research", "record_count": 0},
            "handoff": {"handoff_id": "synthetic-handoff", "status": "failed", "mode": "handoff"},
            "audit_events": [], "email_retry": retry_state(), **changes}


class Buttons(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.buttons = []
        self.current = None
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag == "button":
            self.current = {"attrs": dict(attrs), "text": ""}

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data

    def handle_endtag(self, tag):
        if tag == "button" and self.current is not None:
            self.buttons.append(self.current)
            self.current = None


class RetryForm(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.active = False
        self.fields = {}
        self.request_keys = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.active = attrs.get("id") == "email-retry-form"
        elif self.active and tag == "input" and attrs.get("type") == "hidden":
            self.fields[attrs["name"]] = attrs.get("value", "")
            if attrs["name"] == "request_key":
                self.request_keys.append(attrs.get("value", ""))

    def handle_endtag(self, tag):
        if tag == "form":
            self.active = False


class TestEmailRetryViews(unittest.TestCase):
    def test_failed_email_has_mail_only_form_and_safe_attempts(self):
        page = render_email_retry(retry_state(), "synthetic-handoff", run_status="failed")
        self.assertIn('action="/handoffs/synthetic-handoff/retry-email"', page)
        self.assertIn("작성된 이메일만 다시 전송", page)
        self.assertRegex(page, r'name="request_key" value="email-retry-[0-9a-f]{32}"')
        self.assertIn("2026-09-10 20:07:20", page)
        self.assertIn("SMTP connection failed", page)
        self.assertNotIn("SECRET_SENTINEL", page)
        self.assertNotIn('name="allow_uncertain"', page)
        self.assertIn('class="smtp-attempt-scroll" tabindex="0"', page)
        self.assertIn('min-width:720px; white-space:nowrap;', page)

    def test_uncertain_requires_separate_explicit_checkbox_and_reason(self):
        state = retry_state(eligible=False, uncertain=True, can_retry_uncertain=True)
        page = render_email_retry(state, "synthetic-handoff", run_status="needs_attention")
        self.assertIn('<details id="uncertain-email-retry">', page)
        self.assertRegex(page, r'type="checkbox" name="allow_uncertain" value="true" required')
        self.assertNotIn("checked", page)
        self.assertIn('name="reason"', page)
        self.assertIn('maxlength="500" required', page)
        self.assertIn("중복 발송 위험", page)
        self.assertIn("자동 재전송하지 않습니다", page)

    def test_other_gate_or_acceptance_hides_uncertain_override(self):
        for status, state in [("needs_attention", retry_state(eligible=False, uncertain=True,
                                can_retry_uncertain=False, block_reason="Configuration changed")),
                              ("succeeded", retry_state(eligible=True, uncertain=True, can_retry_uncertain=True))]:
            with self.subTest(status=status):
                page = render_email_retry(state, "synthetic-handoff", run_status=status)
                self.assertNotIn('id="email-retry-form"', page)
        state = retry_state(attempts=[{"status": "smtp_accepted"}])
        page = render_email_retry(state, "synthetic-handoff", run_status="awaiting_receipt")
        self.assertNotIn('id="email-retry-form"', page)
        self.assertIn("이미 SMTP 서버가 수락", page)

    def test_next_attempt_and_block_reason_are_visible_and_escaped(self):
        state = retry_state(eligible=False, block_reason='<img src=x onerror="alert(1)">',
                            next_attempt_at="2026-09-10T11:09:20Z")
        page = render_email_retry(state, "synthetic-handoff")
        self.assertIn("2026-09-10 20:09:20", page)
        self.assertIn("다음 전송 예정", page)
        self.assertIn("&lt;img", page)
        self.assertNotIn("<img", page)
        self.assertNotIn('id="email-retry-form"', page)

    def test_uncertain_run_disables_whole_run_actions_and_hides_republish(self):
        data = run_data(email_retry=retry_state(eligible=False, uncertain=True, can_retry_uncertain=True))
        data["retry_settings"] = {"default_scope": "compose_only", "compose_available": True,
            "execution_settings": {stage: {"type": "codex_exec", "model": None, "reasoning_effort": None}
                                   for stage in ("research", "compose")}}
        data["handoff"]["status"] = "uncertain"
        page = render_run_detail(data, [], [])
        buttons = {item["text"]: item["attrs"] for item in Buttons(page).buttons}
        self.assertIn("disabled", buttons["메일 재작성·발송"])
        self.assertNotIn("Retry Run", buttons)
        self.assertNotIn("Re-compose Only", buttons)
        self.assertNotIn("Republish Handoff", buttons)
        email = render_run_detail(data, [], [], tab="email")
        email_buttons = {item["text"]: item["attrs"] for item in Buttons(email).buttons}
        self.assertNotIn("disabled", email_buttons["작성된 이메일만 다시 전송"])
        self.assertNotIn('/republish"', email)

    def test_republish_is_only_shown_when_service_reports_eligibility(self):
        for allowed in (False, True):
            data = run_data(email_retry=retry_state(eligible=False, can_republish=allowed))
            page = render_run_detail(data, [], [], tab="email")
            self.assertEqual('action="/handoffs/synthetic-handoff/republish"' in page, allowed)


class TestEmailRetryAdapters(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="researchops-email-retry-ui-")
        self.settings = isolated_settings(Path(self.temp.name))
        self.app = ApplicationService(self.settings, custom_runner=fixture_runner(self.settings))
        self.router = WebRouter(self.app)
        self.headers = {"Host": "localhost", "Origin": "http://localhost", "X-CSRF-Token": self.router.csrf_token}

    def tearDown(self):
        self.temp.cleanup()

    def post(self, values, headers=None):
        return self.router.handle_request("POST", "/handoffs/synthetic-handoff/retry-email",
            urlencode(values).encode(), "application/x-www-form-urlencoded", headers=self.headers if headers is None else headers)

    def test_get_run_wires_retry_status_and_csrf_form(self):
        with patch.object(self.app.runs, "show_run", return_value=run_data()), \
             patch.object(self.app.runs, "get_run_artifacts", return_value=[]), \
             patch.object(self.app.delivery, "email_retry_status", return_value=retry_state()) as status_query:
            status, _, body = self.router.handle_request("GET", "/runs/synthetic-run?tab=email", headers={"Host": "localhost"})
        self.assertEqual(status, 200)
        self.assertIn("이메일 전송 · 재시도", body.decode())
        self.assertIn(self.router.csrf_token, body.decode())
        self.assertEqual(len(RetryForm(body.decode()).request_keys), 1)
        status_query.assert_called_once_with("synthetic-handoff")

    def test_form_security_preserves_existing_key_and_adds_missing_key_generically(self):
        for explicit in (True, False):
            with self.subTest(explicit=explicit):
                fields = "<input type='hidden' name='request_key' value='existing-key'>" if explicit else ""
                markup = "<form id='email-retry-form' method='POST'>" + fields + "<button>Submit</button></form>"
                with patch.object(self.app.runs, "show_run", return_value=run_data()), \
                     patch.object(self.app.runs, "get_run_artifacts", return_value=[]), \
                     patch.object(self.app.delivery, "email_retry_status", return_value=retry_state()), \
                     patch("researchops.web.router.render_run_detail", return_value=markup):
                    status, _, body = self.router.handle_request("GET", "/runs/synthetic-run", headers={"Host": "localhost"})
                self.assertEqual(status, 200)
                form = RetryForm(body.decode())
                self.assertEqual(len(form.request_keys), 1)
                self.assertEqual(form.fields["csrf_token"], self.router.csrf_token)
                if explicit:
                    self.assertEqual(form.request_keys, ["existing-key"])
                else:
                    self.assertTrue(form.request_keys[0])

    def test_post_keeps_same_request_key_on_duplicate_click(self):
        with patch.object(self.app.delivery, "show_handoff", return_value=run_data()), \
             patch.object(self.app.delivery, "retry_email", return_value={"job_id": "attempt-two", "status": "queued"}) as retry:
            # The handoff query contains the original run identity.
            self.app.delivery.show_handoff.return_value["handoff"]["run_id"] = "synthetic-run"
            for _ in range(2):
                status, headers, _ = self.post({"request_key": "same-form-request"})
                self.assertEqual(status, 303)
                self.assertTrue(headers["Location"].startswith("/runs/synthetic-run?success="))
        self.assertEqual(retry.call_count, 2)
        for call in retry.call_args_list:
            self.assertEqual(call.kwargs, {"request_key": "same-form-request", "allow_uncertain": False, "reason": ""})

    def test_loopback_http_roundtrip_uses_rendered_csrf_and_request_key(self):
        self.settings.web.enabled = True
        server = create_web_server(self.app, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        data = run_data()
        data["handoff"]["run_id"] = "synthetic-run"
        state = retry_state()

        def queue(handoff_id, **kwargs):
            data["run"]["status"] = "awaiting_receipt"
            state.update(eligible=False, block_reason="Email is already queued for immediate dispatch")
            return {"job_id": "attempt-two", "status": "queued"}

        try:
            with patch.object(self.app.runs, "show_run", return_value=data), \
                 patch.object(self.app.runs, "get_run_artifacts", return_value=[]), \
                 patch.object(self.app.delivery, "show_handoff", return_value=data), \
                 patch.object(self.app.delivery, "email_retry_status", return_value=state), \
                 patch.object(self.app.delivery, "retry_email", side_effect=queue) as retry:
                client.request("GET", "/runs/synthetic-run?tab=email", headers=authenticated_headers(self.app, {"Host": "localhost"}))
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                form = RetryForm(response.read().decode())
                self.assertEqual(form.fields["csrf_token"], admin_session(self.app).session.csrf_token)
                self.assertRegex(form.fields["request_key"], r"^email-retry-[0-9a-f]{32}$")
                for _ in range(2):
                    client.request("POST", "/handoffs/synthetic-handoff/retry-email",
                        body=urlencode(form.fields), headers=authenticated_headers(self.app, {"Host": "localhost", "Origin": "http://localhost",
                        "Content-Type": "application/x-www-form-urlencoded"}, csrf=False))
                    response = client.getresponse()
                    self.assertEqual(response.status, 303)
                    location = response.getheader("Location")
                    response.read()
                next_path = location.split("#")[0]
                next_path += ("&" if "?" in next_path else "?") + "tab=email"
                client.request("GET", next_path, headers=authenticated_headers(self.app, {"Host": "localhost"}))
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                page = response.read().decode()
                self.assertIn("이메일이 이미 전송 대기 중입니다", page)
                self.assertNotIn('id="email-retry-form"', page)
                self.assertIn('data-refresh-active="true"', page)
                self.assertIn("url.searchParams.set('partial','1')", page)
                self.assertEqual(retry.call_count, 2)
                self.assertTrue(all(call.kwargs["request_key"] == form.fields["request_key"]
                                    for call in retry.call_args_list))
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_csrf_and_required_manual_reason_precede_queueing(self):
        with patch.object(self.app.delivery, "show_handoff", return_value={"handoff": {"run_id": "synthetic-run"}}), \
             patch.object(self.app.delivery, "retry_email") as retry:
            self.assertEqual(self.post({"request_key": "key"}, headers={"Host": "localhost"})[0], 403)
            for values in ({"allow_uncertain": "true", "request_key": "key"},
                           {"allow_uncertain": "true", "request_key": "key", "reason": " "},
                           {"allow_uncertain": "true", "request_key": "key", "reason": "x" * 501},
                           {"allow_uncertain": "true", "reason": "Confirmed duplicate risk"}):
                status, headers, _ = self.post(values)
                self.assertEqual(status, 303)
                self.assertIn("?error=", headers["Location"])
            retry.assert_not_called()

    def test_manual_uncertainty_is_explicitly_forwarded_and_backend_refusals_are_shown(self):
        with patch.object(self.app.delivery, "show_handoff", return_value={"handoff": {"run_id": "synthetic-run"}}), \
             patch.object(self.app.delivery, "retry_email", side_effect=DeliveryError("Already accepted")) as retry:
            status, headers, _ = self.post({"allow_uncertain": "true", "request_key": "confirmed-key", "reason": "Checked delivery; accept duplicate risk"})
        self.assertEqual(status, 303)
        self.assertIn("?error=Already+accepted", headers["Location"])
        retry.assert_called_once_with("synthetic-handoff", request_key="confirmed-key", allow_uncertain=True,
                                      reason="Checked delivery; accept duplicate risk")

    def test_delivery_service_only_delegates_queue_and_status(self):
        dispatcher = MagicMock()
        dispatcher.retry_email.return_value = {"job_id": "attempt-two", "status": "queued"}
        dispatcher.retry_status.return_value = retry_state()
        with patch.object(self.app.delivery, "smtp_dispatcher", dispatcher):
            self.assertEqual(self.app.delivery.retry_email("synthetic-handoff", request_key="key")["status"], "queued")
            self.assertTrue(self.app.delivery.email_retry_status("synthetic-handoff")["eligible"])
        dispatcher.retry_email.assert_called_once_with("synthetic-handoff", request_key="key", allow_uncertain=False, reason="")
        dispatcher.retry_status.assert_called_once_with("synthetic-handoff")
        dispatcher._dispatch_job.assert_not_called()

    def cli(self, args, delivery):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("researchops.cli.main.load_settings", return_value=self.settings), \
             patch("researchops.cli.main.runtime_guard", return_value=nullcontext()), \
             patch("researchops.cli.main.create_application_service", return_value=SimpleNamespace(delivery=delivery)), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["delivery", *args, "--json"])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_generates_key_and_preserves_explicit_key(self):
        delivery = MagicMock()
        delivery.retry_email.return_value = {"job_id": "attempt-two", "status": "queued"}
        code, output, _ = self.cli(["retry-email", "synthetic-handoff"], delivery)
        self.assertEqual(code, 0)
        key = json.loads(output)["request_key"]
        self.assertRegex(key, r"^email-retry-[0-9a-f]{32}$")
        self.assertEqual(delivery.retry_email.call_args.kwargs["request_key"], key)
        code, _, _ = self.cli(["retry-email", "synthetic-handoff", "--request-key", "same-key"], delivery)
        self.assertEqual(code, 0)
        self.assertEqual(delivery.retry_email.call_args.kwargs["request_key"], "same-key")

    def test_cli_uncertain_requires_flag_plus_reason_and_status_is_readonly(self):
        delivery = MagicMock()
        delivery.retry_email.return_value = {"job_id": "attempt-two", "status": "queued"}
        code, _, error = self.cli(["retry-email", "synthetic-handoff", "--allow-uncertain"], delivery)
        self.assertNotEqual(code, 0)
        self.assertIn("--reason", error)
        delivery.retry_email.assert_not_called()
        code, _, _ = self.cli(["retry-email", "synthetic-handoff", "--allow-uncertain", "--reason", "Confirmed risk"], delivery)
        self.assertEqual(code, 0)
        self.assertTrue(delivery.retry_email.call_args.kwargs["allow_uncertain"])
        self.assertEqual(delivery.retry_email.call_args.kwargs["reason"], "Confirmed risk")
        delivery.email_retry_status.return_value = retry_state()
        code, output, _ = self.cli(["retry-status", "synthetic-handoff"], delivery)
        self.assertEqual(code, 0)
        self.assertIn("attempts", json.loads(output))
        delivery.email_retry_status.assert_called_once_with("synthetic-handoff")


if __name__ == "__main__":
    unittest.main()
