"""Stage pickers and explicit retry commands use isolated, synthetic fixtures."""

from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from researchops.services.application import ApplicationService
from researchops.web.ai_settings import render_ai_controls, render_retry_panel
from tests.web_support import AuthenticatedWebRouter as WebRouter, admin_session, authenticated_headers
from researchops.web.task_editor import task_editor_body
from tests.support import isolated_settings, fixture_runner, register_fixture_task


CATALOG = {"revision": "synthetic", "stale": False, "providers": [
    {"type": "codex_exec", "label": "Codex", "status": "ready", "models": [
        {"id": "test-codex", "label": "Test Codex", "family": False, "efforts": [
            {"value": "", "label": "모델 기본값", "model": "test-codex"},
            {"value": "high", "label": "high", "model": "test-codex"}]}]},
    {"type": "antigravity_exec", "label": "Agy", "status": "ready", "models": [
        {"id": "test-family", "label": "Test family", "family": True, "default_effort": "high", "efforts": [
            {"value": "low", "label": "low", "model": "test-family-low"},
            {"value": "high", "label": "high", "model": "test-family-high"}]},
        {"id": "test-fixed", "label": "Fixed", "family": False, "efforts": [
            {"value": "", "label": "모델 기본값", "model": "test-fixed"}]}]}]}
STAGES = {"research": {"type": "antigravity_exec", "model": "test-family-low", "reasoning_effort": None},
          "compose": {"type": "codex_exec", "model": "test-codex", "reasoning_effort": "high"}}


class FormElements(HTMLParser):
    def __init__(self, raw):
        super().__init__()
        self.selects, self.inputs, self.buttons, self.current = {}, [], [], None
        self.feed(raw)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "select":
            self.current = {"attrs": attrs, "options": []}
            self.selects[attrs.get("name")] = self.current
        elif tag == "option" and self.current is not None:
            self.current["options"].append(attrs)
        elif tag == "input":
            self.inputs.append(attrs)
        elif tag == "button":
            self.buttons.append(attrs)

    def handle_endtag(self, tag):
        if tag == "select":
            self.current = None

    def selected(self, name):
        return next(item["value"] for item in self.selects[name]["options"] if "selected" in item)


class AiControlTests(unittest.TestCase):
    def test_task_has_independent_stage_dropdowns_and_separate_delivery(self):
        raw = task_editor_body({"stage_settings": STAGES}, model_catalog=CATALOG,
            recipient_groups={"team": ["recipient@example.test"]})
        form = FormElements(raw)
        self.assertTrue(all(stage + "_" + field in form.selects
            for stage in STAGES for field in ("provider", "model", "effort")))
        self.assertEqual(form.selected("research_provider"), "antigravity_exec")
        self.assertEqual(form.selected("compose_provider"), "codex_exec")
        self.assertFalse(any(item.get("name") in {"model", "research_model", "compose_model"} for item in form.inputs))
        self.assertIn("Research 설정을 Compose에 복사", raw)
        self.assertNotIn("hidden", next(item for item in form.buttons if "data-ai-copy" in item))
        self.assertIn("메일·수신자", raw)

    def test_agy_variant_maps_to_family_without_inventing_a_default_effort(self):
        form = FormElements(render_ai_controls(STAGES, CATALOG))
        self.assertEqual(form.selected("research_model"), "test-family")
        self.assertEqual(form.selected("research_effort"), "low")
        family = deepcopy(STAGES)
        family["research"]["model"] = "test-family"
        form = FormElements(render_ai_controls(family, CATALOG))
        effort = form.selects["research_effort"]
        self.assertEqual(form.selected("research_effort"), "")
        self.assertIn("required", effort["attrs"])
        self.assertIn("disabled", effort["options"][0])
        family["research"]["model"] = "test-fixed"
        self.assertIn("disabled", FormElements(render_ai_controls(family, CATALOG)).selects["research_effort"]["attrs"])

    def test_legacy_model_is_visible_and_script_escaped(self):
        values = deepcopy(STAGES)
        values["compose"]["model"] = "old-model</script><img src=x>"
        raw = render_ai_controls(values, CATALOG)
        self.assertEqual(FormElements(raw).selected("compose_model"), values["compose"]["model"])
        self.assertIn("기존 저장값", raw)
        self.assertNotIn("</script><img", raw)
        self.assertIn("\\u003c/script\\u003e", raw)

    def test_compose_retry_disables_research_and_retains_it_after_error(self):
        data = {"run": {"run_id": "run-test", "status": "failed"}, "execution_settings": STAGES,
                "retry_settings": {"default_scope": "compose_only", "compose_available": True,
                                   "execution_settings": STAGES, "source_composition": {"run_id": "parent", "revision": 1}}}
        raw = render_retry_panel(data, CATALOG, values={"scope": "compose_only",
            "execution_settings": {"compose": {"type": "codex_exec", "model": "bad", "reasoning_effort": None}}})
        form = FormElements(raw)
        self.assertEqual(form.selected("research_provider"), "antigravity_exec")
        self.assertIn("disabled", form.selects["research_provider"]["attrs"])
        self.assertNotIn("disabled", form.selects["compose_provider"]["attrs"])
        self.assertIn("메일 재작성·발송", raw)
        self.assertIn("기존 조사 결과를 재사용", raw)
        self.assertIn("hidden", next(item for item in form.buttons if "data-ai-copy" in item))
        self.assertNotIn('/compose-only"', raw)


class AiSettingsRoutesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = isolated_settings(Path(temporary.name))
        self.app = ApplicationService(self.settings, custom_runner=fixture_runner(self.settings))
        register_fixture_task(self.app)
        self.router = WebRouter(self.app)
        self.headers = {"Host": "localhost", "Origin": "http://localhost", "X-CSRF-Token": self.router.csrf_token}
        self.snapshot = patch.object(self.app.model_catalog, "snapshot", return_value=deepcopy(CATALOG)).start()
        self.addCleanup(patch.stopall)

    def source(self):
        run = self.app.runs.enqueue_run("software-releases", force_dry_run=True)
        finished = self.app.runs.execute_run(run.run_id)
        self.assertEqual(finished.status, "succeeded", finished.error_message)
        return finished

    def post(self, path, data):
        return self.router.handle_request("POST", path, urlencode(data).encode(),
            "application/x-www-form-urlencoded", headers=self.headers)

    def test_catalog_refresh_is_explicit_post_and_requires_csrf(self):
        status, _, raw = self.router.handle_request("GET", "/api/model-catalog", headers=self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["revision"], "synthetic")
        self.snapshot.assert_called_once_with(refresh=False)
        self.snapshot.reset_mock()
        status, _, _ = self.router.handle_request("POST", "/api/model-catalog/refresh", headers={"Host": "localhost"})
        self.assertEqual(status, 403)
        self.snapshot.assert_not_called()
        status, _, _ = self.post("/api/model-catalog/refresh", {})
        self.assertEqual(status, 200)
        self.snapshot.assert_called_once_with(refresh=True)

    def test_run_page_exposes_settings_without_immediate_retry_forms(self):
        source = self.source()
        status, _, raw = self.router.handle_request("GET", "/runs/" + source.run_id, headers=self.headers)
        self.assertEqual(status, 200, raw.decode())
        rendered = raw.decode()
        self.assertIn("이 실행의 AI 설정 · 변경 불가", rendered)
        self.assertIn('/retry-configured"', rendered)
        self.assertNotIn('/compose-only"', rendered)
        self.assertNotIn(f'/runs/{source.run_id}/retry"', rendered)

    def test_configured_compose_retry_queues_one_new_plan_and_preserves_parent(self):
        source = self.source()
        before = self.app.run_repo.get_composition_input(source.run_id)
        fields = {"scope": "compose_only", "selection_source": "custom", "request_key": "chosen-compose",
                  "compose_provider": "codex_exec", "compose_model": "test-codex", "compose_effort": "high"}
        first = self.post(f"/runs/{source.run_id}/retry-configured", fields)
        replay = self.post(f"/runs/{source.run_id}/retry-configured", fields)
        self.assertEqual(first[0], 303, first[2].decode())
        self.assertEqual(first[1]["Location"], replay[1]["Location"])
        child_id = first[1]["Location"].split("?")[0].split("/")[-1]
        child = self.app.run_repo.get_run(child_id)
        self.assertEqual(child.status, "queued")
        self.assertEqual(child.task_version_hash, source.task_version_hash)
        plan = self.app.run_repo.get_execution_plan(child_id)
        self.assertEqual(plan["scope"], "compose_only")
        self.assertEqual(plan["stages"]["compose"], {"type": "codex_exec", "model": "test-codex", "reasoning_effort": "high"})
        self.assertEqual(plan["stages"]["research"]["type"], "fake")
        self.assertEqual(self.app.run_repo.get_composition_input(source.run_id), before)

    def test_invalid_retry_preserves_form_and_creates_no_child(self):
        source = self.source()
        status, _, raw = self.post(f"/runs/{source.run_id}/retry-configured", {
            "scope": "compose_only", "selection_source": "custom", "request_key": "invalid-choice",
            "compose_provider": "codex_exec", "compose_model": "unknown-model", "compose_effort": "high"})
        self.assertEqual(status, 400, raw.decode())
        form = FormElements(raw.decode())
        self.assertEqual(form.selected("compose_model"), "unknown-model")
        self.assertTrue(any(item.get("name") == "request_key" and item.get("value") == "invalid-choice" for item in form.inputs))
        self.assertEqual(len(self.app.runs.list_runs()), 1)

    def test_malformed_json_stage_returns_validation_page_instead_of_server_error(self):
        source = self.source()
        for malformed in (42, ["invalid"], {"type": "codex_exec", "model": ["invalid"]}):
            with self.subTest(malformed=malformed):
                status, _, raw = self.router.handle_request("POST", f"/runs/{source.run_id}/retry-configured",
                    json.dumps({"scope": "compose_only", "selection_source": "custom",
                                "execution_settings": {"compose": malformed}}).encode(),
                    "application/json", headers=self.headers)
                self.assertEqual(status, 400, raw.decode())
                self.assertIn("설정 확인 후 재실행".encode(), raw)
        self.assertEqual(len(self.app.runs.list_runs()), 1)
