"""Prepared-mail sending is an explicit, model-free Web and CLI command."""

from contextlib import nullcontext, redirect_stderr, redirect_stdout
from html.parser import HTMLParser
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from researchops.cli.main import main
from researchops.errors import ValidationError
from researchops.services.application import ApplicationService
from tests.web_support import AuthenticatedWebRouter as WebRouter, admin_session, authenticated_headers
from researchops.web.smtp_views import render_prepared_email
from researchops.web.views import render_run_detail
from tests.support import isolated_settings, fixture_runner


def prepared_state(**changes):
    return {"eligible": True, "block_reason": None, "source_run_id": "source-run", "source_revision": 2, **changes}


def run_data(**changes):
    stages = {stage: {"type": "codex_exec", "model": "synthetic-model", "reasoning_effort": None}
              for stage in ("research", "compose")}
    return {"run": {"run_id": "source-run", "task_id": "synthetic-task", "status": "failed", "phase": "finalize"},
        "audit_events": [], "research": {"summary": "Synthetic", "record_count": 1},
        "handoff": None, "execution_settings": stages, "execution_plan": {"scope": "full"},
        "retry_settings": {"default_scope": "compose_only", "compose_available": True,
                           "execution_settings": stages}, **changes}


class PreparedForm(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.active, self.action, self.fields, self.selects, self.buttons = False, None, {}, [], []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("id") == "prepared-email-form":
            self.active, self.action = True, attrs.get("action")
        if self.active and tag == "input" and attrs.get("name"):
            self.fields[attrs["name"]] = attrs.get("value", "")
        if self.active and tag == "select":
            self.selects.append(attrs)
        if self.active and tag == "button":
            self.buttons.append(attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self.active = False


class PreparedEmailViewTests(unittest.TestCase):
    def test_eligible_panel_has_only_send_action_and_source(self):
        body = render_prepared_email(prepared_state(), "source-run", run_status="failed")
        self.assertIn("작성된 이메일 보내기", body)
        self.assertIn("Research와 Compose 모델을 호출하지 않습니다", body)
        self.assertIn("작성 revision 2", body)
        form = PreparedForm(body)
        self.assertEqual(form.action, "/runs/source-run/send-email")
        self.assertRegex(form.fields["request_key"], r"^email-send-[0-9a-f]{32}$")
        self.assertEqual(form.selects, [])

    def test_no_handoff_always_has_email_status_with_escaped_reason(self):
        body = render_run_detail(run_data(prepared_email=prepared_state(eligible=False,
            block_reason='확정 이메일 없음 <img src=x>')), [], [], tab="email")
        self.assertIn('id="smtp-delivery"', body)
        self.assertIn("확정 이메일 없음 &lt;img", body)
        self.assertNotIn('<img src=x>', body)
        self.assertNotIn('id="prepared-email-form"', body)

    def test_prepared_send_precedes_collapsed_model_rewrite_options(self):
        body = render_run_detail(run_data(prepared_email=prepared_state()), [], [])
        self.assertLess(body.index('href="/runs/source-run?tab=email#smtp-delivery"'), body.index('id="change-email-content"'))
        self.assertIn('<details class="card" id="change-email-content">', body)
        email = render_run_detail(run_data(prepared_email=prepared_state()), [], [], tab="email")
        self.assertIn('id="prepared-email-form"', email)
        self.assertNotIn('id="change-email-content"', email)

    def test_delivery_only_history_does_not_claim_ai_was_invoked(self):
        data = run_data(execution_plan={"scope": "delivery_only", "source_message": {"run_id": "original-run"}})
        body = render_run_detail(data, [], [])
        self.assertIn("이메일 전송 전용 · AI 실행 없음", body)
        self.assertIn('href="/runs/original-run"', body)
        self.assertNotIn('data-ai-settings', body)
        self.assertNotIn('retry-configured', body)

    def test_send_error_remains_visible_if_handoff_appears_during_request(self):
        data = run_data(handoff={"handoff_id": "created-handoff", "status": "queued"},
                        email_retry={"eligible": False, "block_reason": "전송 대기 중"})
        body = render_run_detail(data, [], [], send_email_error="이미 전송 대기 중입니다.")
        self.assertIn('id="prepared-email-error"', body)
        self.assertIn("이미 전송 대기 중입니다.", body)
        self.assertNotIn('id="prepared-email-form"', body)


class PreparedEmailAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = isolated_settings(Path(temporary.name))
        self.app = ApplicationService(self.settings, custom_runner=fixture_runner(self.settings))
        self.router = WebRouter(self.app)
        self.headers = {"Host": "localhost", "Origin": "http://localhost", "X-CSRF-Token": self.router.csrf_token}
        self.patches = [patch.object(self.app.runs, "show_run", return_value=run_data()),
            patch.object(self.app.runs, "get_run_artifacts", return_value=[]),
            patch.object(self.app.runs, "prepared_email_status", return_value=prepared_state())]
        self.mocks = [item.start() for item in self.patches]
        self.addCleanup(lambda: [item.stop() for item in self.patches])

    def post(self, fields, headers=None):
        return self.router.handle_request("POST", "/runs/source-run/send-email", urlencode(fields).encode(),
            "application/x-www-form-urlencoded", headers=self.headers if headers is None else headers)

    def test_get_queries_eligibility_and_embeds_csrf(self):
        status, _, body = self.router.handle_request("GET", "/runs/source-run?tab=email", headers=self.headers)
        self.assertEqual(status, 200, body.decode())
        form = PreparedForm(body.decode())
        self.assertEqual(form.fields["csrf_token"], self.router.csrf_token)
        self.mocks[2].assert_called_once_with("source-run")

    def test_post_csrf_and_duplicate_request_key_reach_only_enqueue_command(self):
        with patch.object(self.app.runs, "send_prepared_email", return_value=SimpleNamespace(run_id="mail-child")) as send, \
             patch.object(self.app.runs, "execute_run") as execute:
            status, _, _ = self.post({"request_key": "same-key"}, headers={"Host": "localhost"})
            self.assertEqual(status, 403)
            send.assert_not_called()
            first = self.post({"request_key": "same-key"})
            replay = self.post({"request_key": "same-key"})
            self.assertEqual(first[0], 303)
            self.assertEqual(first[1]["Location"], replay[1]["Location"])
            self.assertTrue(first[1]["Location"].startswith("/runs/mail-child?success="))
            self.assertEqual(send.call_count, 2)
            for call in send.call_args_list:
                self.assertEqual(call.args, ("source-run",))
                self.assertEqual(call.kwargs, {"request_key": "same-key"})
            execute.assert_not_called()

    def test_validation_error_preserves_send_key_and_focuses_safe_message(self):
        with patch.object(self.app.runs, "send_prepared_email", side_effect=ValidationError("첨부 확인 필요 <script>")):
            status, _, body = self.post({"request_key": "preserved-key"})
        self.assertEqual(status, 400)
        page = body.decode()
        self.assertIn('id="prepared-email-error" role="alert" tabindex="-1"', page)
        self.assertIn('document.getElementById("prepared-email-error").focus()', page)
        self.assertIn("첨부 확인 필요 &lt;script&gt;", page)
        self.assertEqual(PreparedForm(page).fields["request_key"], "preserved-key")

    def test_eligibility_revocation_keeps_key_and_disables_the_failed_form(self):
        self.mocks[2].return_value = prepared_state(eligible=False, block_reason="현재 발송이 중지되어 있습니다.")
        with patch.object(self.app.runs, "send_prepared_email", side_effect=ValidationError("발송 권한이 변경되었습니다.")):
            status, _, body = self.post({"request_key": "before-revocation"})
        self.assertEqual(status, 400)
        page = body.decode()
        form = PreparedForm(page)
        self.assertEqual(form.fields["request_key"], "before-revocation")
        self.assertEqual(form.fields["csrf_token"], self.router.csrf_token)
        self.assertIn("disabled", form.buttons[0])
        self.assertEqual(form.buttons[0]["aria-describedby"], "prepared-email-block-reason")
        self.assertIn("현재 발송이 중지되어 있습니다.", page)
        self.assertIn("전송 가능 여부를 다시 확인", page)
        self.assertIn('document.getElementById("prepared-email-error").focus()', page)

    def cli(self, arguments, runs):
        output, error = io.StringIO(), io.StringIO()
        with patch("researchops.cli.main.load_settings", return_value=self.settings), \
             patch("researchops.cli.main.runtime_guard", return_value=nullcontext()), \
             patch("researchops.cli.main.create_application_service", return_value=SimpleNamespace(runs=runs)), \
             redirect_stdout(output), redirect_stderr(error):
            result = main(["run", *arguments, "--json"])
        return result, output.getvalue(), error.getvalue()

    def test_cli_send_and_status_never_execute_models(self):
        runs = MagicMock()
        runs.send_prepared_email.return_value = SimpleNamespace(run_id="mail-child")
        runs.show_run.return_value = {"run": {"run_id": "mail-child", "status": "queued"}}
        code, output, error = self.cli(["send-email", "source-run", "--request-key", "cli-key"], runs)
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["run"]["run_id"], "mail-child")
        runs.send_prepared_email.assert_called_once_with("source-run", request_key="cli-key")
        runs.execute_run.assert_not_called()
        runs.enqueue_run.assert_not_called()
        runs.reset_mock()
        runs.prepared_email_status.return_value = prepared_state()
        code, output, error = self.cli(["send-email-status", "source-run"], runs)
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(output)["eligible"])
        runs.prepared_email_status.assert_called_once_with("source-run")
        runs.send_prepared_email.assert_not_called()

    def test_cli_default_retry_allows_delivery_only_without_ai_override(self):
        runs = MagicMock()
        def retry(run_id, **kwargs):
            if kwargs.get("execution_settings") or kwargs.get("selection_source"):
                raise ValidationError("이메일 전송 전용 실행에는 AI 설정을 변경할 수 없습니다.")
            return SimpleNamespace(run_id="delivery-retry")
        runs.retry_run.side_effect = retry
        runs.show_run.return_value = {"run": {"run_id": "delivery-retry"},
                                      "execution_plan": {"scope": "delivery_only"}}
        code, output, error = self.cli(["retry", "failed-delivery", "--request-key", "retry-key"], runs)
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["execution_plan"]["scope"], "delivery_only")
        runs.retry_run.assert_called_once_with("failed-delivery", request_key="retry-key", scope=None,
                                              execution_settings=None, selection_source=None)
        runs.execute_run.assert_not_called()
