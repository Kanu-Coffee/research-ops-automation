"""Read-only MCP diagnostics must distinguish registration from call evidence."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops.services.application import ApplicationService
from researchops.web.mcp_views import render_mcp_inventory
from tests.web_support import AuthenticatedWebRouter as WebRouter, admin_session, authenticated_headers
from tests.support import isolated_settings, fixture_runner, register_fixture_task


def inventory(provider="codex_exec"):
    return {"provider": provider, "servers": [
        {"name": "registered", "enabled": True, "transport": "http", "authentication_configured": True},
        {"name": "disabled", "enabled": False, "transport": "stdio"},
        {"name": "missing-command", "enabled": True, "transport": "stdio",
         "executable": {"configured": True, "available": False}},
        {"name": "missing-env", "enabled": True, "transport": "http", "missing_env_names": ["API_TOKEN"]},
        {"name": "blocked-env", "enabled": True, "transport": "stdio", "blocked_env_names": ["SMTP_PASSWORD"]},
        {"name": "remote-env", "enabled": True, "transport": "stdio", "remote_env_names": ["REMOTE_TOKEN"]}],
        "config_sources": [], "issues": [], "connections_checked": False, "inventory_complete": False}


class TestMCPDiagnostics(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="researchops-mcp-diagnostics-")
        self.settings = isolated_settings(Path(self.temp.name))
        self.app = ApplicationService(self.settings, custom_runner=fixture_runner(self.settings))

    def tearDown(self):
        self.temp.cleanup()

    def test_doctor_keeps_optional_mcp_failure_out_of_readiness(self):
        with patch("researchops.services.doctor.shutil.which", return_value="/fixture/cli"), \
             patch("researchops.runners.native_mcp.inspect_native_mcp") as inspect, \
             patch.object(self.app.doctor, "_check_runners", return_value={"isolation": {"live_runner_ready": True}}), \
             patch.object(self.app.doctor, "_check_runtime_security", return_value={"ok": True}):
            # Use a matching two-argument fixture without invoking a real CLI.
            inspect.side_effect = lambda provider, binary: inventory(provider)
            health = self.app.doctor.check_all()
        self.assertEqual(inspect.call_count, 2)
        self.assertEqual(health["overall_status"], "ok")
        self.assertTrue(health["live_runner_ready"])
        self.assertFalse(health["mcp"]["affects_readiness"])
        self.assertFalse(health["mcp"]["connections_checked"])
        self.assertEqual(set(health["mcp"]["providers"]), {"codex_exec", "antigravity_exec"})

    def test_missing_cli_does_not_inspect_operator_configuration(self):
        with patch("researchops.services.doctor.shutil.which", return_value=None), \
             patch("researchops.runners.native_mcp.inspect_native_mcp") as inspect:
            result = self.app.doctor._check_mcp()
        inspect.assert_not_called()
        self.assertEqual(result["providers"]["codex_exec"]["issues"], [{"code": "cli_unavailable"}])

    def test_inspection_error_cannot_expose_exception_secret(self):
        with patch("researchops.services.doctor.shutil.which", return_value="/fixture/cli"), \
             patch("researchops.runners.native_mcp.inspect_native_mcp",
                   side_effect=ValueError("https://private.invalid?token=SECRET_SENTINEL")):
            result = self.app.doctor._check_mcp()
        encoded = json.dumps(result)
        self.assertNotIn("SECRET_SENTINEL", encoded)
        self.assertIn("inspection_failed", encoded)

    def test_inventory_renderer_explains_status_without_showing_config_values(self):
        configured = inventory()
        configured["servers"][0].update(url="https://private.invalid", args=["SECRET_SENTINEL"],
                                        headers={"Authorization": "SECRET_SENTINEL"})
        html = render_mcp_inventory({"providers": {"codex_exec": configured}})
        for label in ("MCP 연결 설정", "등록됨 · 연결 미확인", "비활성", "실행 파일 없음",
                      "필수 환경변수 없음", "환경변수 전달 제한", "원격 환경변수 확인 필요", "설정 있음 · 인증 미확인"):
            self.assertIn(label, html)
        for secret in ("private.invalid", "SECRET_SENTINEL", "API_TOKEN", "SMTP_PASSWORD", "REMOTE_TOKEN"):
            self.assertNotIn(secret, html)

    def test_partial_inventory_keeps_config_problems_visible(self):
        configured = inventory()
        configured["issues"] = [{"code": "server_transport_invalid", "server": "registered"}]
        configured["config_sources"] = [{"scope": "user", "status": "invalid", "source": "SECRET_SENTINEL"}]
        html = render_mcp_inventory({"providers": {"codex_exec": configured}})
        self.assertIn("연결 방식 확인 필요", html)
        self.assertIn("사용자 설정: 형식 확인 필요", html)
        self.assertNotIn("SECRET_SENTINEL", html)


class TestMCPRunAudit(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="researchops-mcp-run-audit-")
        self.settings = isolated_settings(Path(self.temp.name))
        self.app = ApplicationService(self.settings, custom_runner=fixture_runner(self.settings))
        register_fixture_task(self.app)
        self.run = self.app.runs.enqueue_run("software-releases")
        self.archive = self.settings.paths.run_archive_dir / self.run.task_id / self.run.run_id
        self.archive.mkdir(parents=True)
        self.report = self.archive / "validation-report.json"

    def tearDown(self):
        self.temp.cleanup()

    def write_report(self, tools=None):
        verified = {"server": "cardrag", "tool": "search", "status": "succeeded", "error_code": None,
                    "success": True, "provider_reported_success": True, "output_verified": True}
        self.report.write_text(json.dumps({"executions": [{"stage": "research", "isolation": {
            "mcp": inventory(), "mcp_tools": [verified] if tools is None else tools,
            "unrelated": "SECRET_SENTINEL"}}]}))
        return verified

    def test_old_runs_without_audit_remain_readable(self):
        self.assertEqual(self.app.runs.show_run(self.run.run_id)["mcp_audit"]["status"], "not_recorded")
        self.report.write_text(json.dumps({"executions": [{"stage": "research", "isolation": {}}]}))
        self.assertEqual(self.app.runs.get_run_mcp_audit(self.run.run_id)["status"], "not_recorded")

    def test_query_and_web_share_safe_observed_call_summary(self):
        tool = self.write_report()
        tool.update(arguments="SECRET_SENTINEL", output="SECRET_SENTINEL", error_message="SECRET_SENTINEL")
        failed = {**tool, "tool": "lookup", "status": "failed", "error_code": "MCP_PROTOCOL_ERROR",
                  "success": False, "output_verified": False}
        unverified = {**tool, "tool": "large-response", "status": "unverified",
                      "error_code": "MCP_OUTPUT_UNVERIFIED", "success": False, "output_verified": False}
        self.write_report([tool, failed, unverified])
        result = self.app.runs.show_run(self.run.run_id)["mcp_audit"]
        self.assertEqual(result["status"], "recorded")
        self.assertNotIn("SECRET_SENTINEL", json.dumps(result))
        self.assertEqual([item["status"] for item in result["phases"][0]["tools"]],
                         ["succeeded", "failed", "unverified"])
        router = WebRouter(self.app)
        status, _, body = router.handle_request("GET", f"/runs/{self.run.run_id}?tab=logs", headers={"Host": "localhost"})
        self.assertEqual(status, 200)
        html = body.decode()
        for value in ("MCP 호출 결과", "cardrag", "large-response", "응답 확인", "MCP 응답 오류", "응답 증거 없음"):
            self.assertIn(value, html)
        self.assertNotIn("SECRET_SENTINEL", html)

    def test_unverified_success_and_sensitive_identifiers_are_not_promoted(self):
        tool = self.write_report()
        tool.update(server="https://private.invalid?token=SECRET_SENTINEL",
                    tool="<script>SECRET_SENTINEL</script>", output_verified=False,
                    error_code="SECRET_SENTINEL")
        self.write_report([tool])
        audit = self.app.runs.get_run_mcp_audit(self.run.run_id)
        summary = audit["phases"][0]["tools"][0]
        self.assertEqual(summary["status"], "unverified")
        self.assertFalse(summary["success"])
        self.assertNotIn("SECRET_SENTINEL", json.dumps(audit))

    def test_corrupt_or_unsafe_audit_is_reported_without_raw_data(self):
        for raw in ("not json SECRET_SENTINEL", "[]", '{"executions": null}',
                    '{"executions": [], "executions": []}'):
            with self.subTest(raw=raw):
                self.report.write_text(raw)
                self.assertEqual(self.app.runs.get_run_mcp_audit(self.run.run_id)["status"], "unavailable")
        self.report.unlink()
        outside = Path(self.temp.name) / "secret.txt"
        outside.write_text("SECRET_SENTINEL")
        self.report.symlink_to(outside)
        self.assertEqual(self.app.runs.get_run_mcp_audit(self.run.run_id)["status"], "unavailable")

    def test_malformed_summary_fields_do_not_break_run_query_or_page(self):
        for value in ({"private": "SECRET_SENTINEL"}, ["SECRET_SENTINEL"], True, 12):
            tool = self.write_report()
            tool.update(status=value, error_code=value)
            document = {"executions": [{"stage": value, "isolation": {
                "mcp": {"provider": value}, "mcp_tools": [tool]}}]}
            self.report.write_text(json.dumps(document))
            audit = self.app.runs.show_run(self.run.run_id)["mcp_audit"]
            phase = audit["phases"][0]
            self.assertEqual(phase["provider"], "unknown")
            self.assertEqual(phase["stage"], "unknown")
            self.assertEqual(phase["tools"][0]["status"], "unverified")
            self.assertIsNone(phase["tools"][0]["error_code"])
            self.assertFalse(phase["tools"][0]["success"])
            self.assertNotIn("SECRET_SENTINEL", json.dumps(audit))
            status, _, body = WebRouter(self.app).handle_request("GET", f"/runs/{self.run.run_id}?tab=logs",
                                                                headers={"Host": "localhost"})
            self.assertEqual(status, 200)
            self.assertNotIn("SECRET_SENTINEL", body.decode())

    def test_large_call_list_is_bounded_and_omission_is_explicit(self):
        tool = self.write_report()
        self.write_report([copy.deepcopy(tool) for _ in range(501)])
        phase = self.app.runs.get_run_mcp_audit(self.run.run_id)["phases"][0]
        self.assertEqual(len(phase["tools"]), 500)
        self.assertEqual(phase["omitted_tool_count"], 1)

    def test_no_calls_is_not_rendered_as_connection_success(self):
        self.write_report([])
        router = WebRouter(self.app)
        _, _, body = router.handle_request("GET", f"/runs/{self.run.run_id}?tab=logs", headers={"Host": "localhost"})
        self.assertIn("관찰된 MCP 호출 없음", body.decode())


if __name__ == "__main__":
    unittest.main()
