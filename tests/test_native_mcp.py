"""Native config metadata without connections, auth-store reads or secret echo."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops.runners.native_mcp import inspect_native_mcp, production_control_environment


class NativeMcpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.env = {"HOME": str(self.home), "PATH": "/untrusted/path", "USER": "fixture"}
        self.codex = self.home / ".codex/config.toml"
        self.agy = self.home / ".gemini/config/mcp_config.json"
        self.project = self.home / "project"
        self.project.mkdir()

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def inspect(self, provider="codex_exec", **kwargs):
        return inspect_native_mcp(provider, "/usr/bin/codex", environ=self.env, **kwargs)

    def test_inventory_masks_values_and_never_claims_connection_success(self):
        self.write(self.codex, '''
[mcp_servers.remote]
url = "https://SECRET_ENDPOINT.invalid/PRIVATE_PATH"
bearer_token_env_var = "MCP_TOKEN"
http_headers = { Authorization = "SECRET_HEADER" }
enabled_tools = ["search", "read/item"]
disabled_tools = ["write"]
default_tools_approval_mode = "prompt"
[mcp_servers.local]
command = "/bin/true"
args = ["SECRET_ARGUMENT"]
env = { TOKEN = "SECRET_INLINE_ENV" }
env_vars = ["LOCAL_AUTH"]
''')
        self.env.update(MCP_TOKEN="SECRET_TOKEN", LOCAL_AUTH="SECRET_LOCAL_AUTH")
        with patch("subprocess.Popen") as spawn:
            result = self.inspect()
        spawn.assert_not_called()
        encoded = json.dumps(result)
        for marker in ("SECRET_", "https://", "Authorization", "PRIVATE_PATH", "TOKEN\": \"SECRET"):
            self.assertNotIn(marker, encoded)
        self.assertFalse(result["connections_checked"])
        self.assertFalse(result["inventory_complete"])
        remote = next(s for s in result["servers"] if s["name"] == "remote")
        self.assertEqual(remote["required_env_names"], ["MCP_TOKEN"])
        self.assertEqual(remote["missing_env_names"], [])
        self.assertTrue(remote["authentication_configured"])
        self.assertEqual(remote["tool_policy"]["disabled_tools"], ["write"])
        self.assertIsNone(remote["executable"]["available"])
        self.assertTrue(next(s for s in result["servers"] if s["name"] == "local")["executable"]["available"])

    def test_codex_only_declared_unprotected_environment_reaches_control(self):
        self.write(self.codex, '''
[mcp_servers.remote]
url = "https://example.invalid"
bearer_token_env_var = "MCP_TOKEN"
env_http_headers = { "X-Account" = "ACCOUNT_TOKEN" }
[mcp_servers.local]
command = "/bin/true"
env_vars = ["LOCAL_TOKEN", "SMTP_PASSWORD", "RESEARCHOPS_CONFIG", "SSH_AUTH_SOCK", "PATH", "LD_PRELOAD", "NODE_OPTIONS", "INLINE_TOKEN"]
env = { INLINE_TOKEN = "native-value" }
[mcp_servers.disabled]
command = "/bin/true"
enabled = false
env_vars = ["DISABLED_TOKEN"]
''')
        self.env.update({name: "sentinel-" + name for name in ("MCP_TOKEN", "ACCOUNT_TOKEN", "LOCAL_TOKEN", "SMTP_PASSWORD",
                         "RESEARCHOPS_CONFIG", "SSH_AUTH_SOCK", "LD_PRELOAD", "NODE_OPTIONS", "INLINE_TOKEN", "DISABLED_TOKEN", "OPENAI_API_KEY")})
        env = production_control_environment("codex_exec", "/usr/bin/codex", self.home / "control", environ=self.env)
        for name in ("MCP_TOKEN", "ACCOUNT_TOKEN", "LOCAL_TOKEN"):
            self.assertEqual(env[name], self.env[name])
        for name in ("SMTP_PASSWORD", "RESEARCHOPS_CONFIG", "SSH_AUTH_SOCK", "LD_PRELOAD", "NODE_OPTIONS", "INLINE_TOKEN", "DISABLED_TOKEN", "OPENAI_API_KEY"):
            self.assertNotIn(name, env)
        self.assertNotIn("/untrusted/path", env["PATH"])
        self.assertEqual(env["HOME"], str(self.home))
        self.assertEqual(env["CODEX_HOME"], str(self.home / ".codex"))
        issues = {i["code"] for i in self.inspect()["issues"]}
        self.assertIn("protected_environment_reference", issues)

    def test_agy_keeps_native_auth_and_project_metadata_without_token_env_injection(self):
        self.write(self.agy, json.dumps({"mcpServers": {"cardrag-mcp": {"serverUrl": "https://SECRET_ENDPOINT", "headers": {"Authorization": "SECRET_HEADER"}, "disabledTools": ["write"]}}}))
        self.write(self.project / ".agents/mcp_config.json", json.dumps({"mcpServers": {"local": {"command": "/bin/true", "env": {"TOKEN": "SECRET_INLINE"}, "disabled": True}}}))
        self.env.update(AGY_TOKEN="SECRET_TOKEN", GOOGLE_APPLICATION_CREDENTIALS="SECRET_FILE", SMTP_PASSWORD="SECRET_SMTP")
        result = self.inspect("antigravity_exec", project_dir=self.project)
        self.assertEqual({s["name"] for s in result["servers"]}, {"cardrag-mcp", "local"})
        self.assertFalse(next(s for s in result["servers"] if s["name"] == "local")["enabled"])
        self.assertNotIn("SECRET", json.dumps(result))
        env = production_control_environment("antigravity_exec", "/usr/bin/agy", self.home / "control", environ=self.env)
        self.assertEqual(env["HOME"], str(self.home))
        self.assertNotIn("CODEX_HOME", env)
        for name in ("AGY_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS", "SMTP_PASSWORD"):
            self.assertNotIn(name, env)
        self.assertTrue(any("not forwarded" in text for text in result["limitations"]))

    def test_codex_structured_env_sources_do_not_replace_remote_values_with_local_tokens(self):
        self.write(self.codex, '''
[mcp_servers.local]
command = "/bin/true"
env_vars = [{ name = "LOCAL_TOKEN", source = "local" }, { name = "REMOTE_TOKEN", source = "remote" }]
''')
        self.env.update(LOCAL_TOKEN="SECRET_LOCAL", REMOTE_TOKEN="SECRET_REMOTE")
        result = self.inspect()
        entry = result["servers"][0]
        self.assertEqual(entry["required_env_names"], ["LOCAL_TOKEN", "REMOTE_TOKEN"])
        self.assertEqual(entry["remote_env_names"], ["REMOTE_TOKEN"])
        self.assertEqual(entry["missing_env_names"], [])
        self.assertEqual({i["code"] for i in result["issues"]}, {"remote_environment_reference_not_forwarded"})
        env = production_control_environment("codex_exec", "/usr/bin/codex", self.home / "control", environ=self.env)
        self.assertEqual(env["LOCAL_TOKEN"], "SECRET_LOCAL")
        self.assertNotIn("REMOTE_TOKEN", env)
        self.assertNotIn("SECRET", json.dumps(result))

    def test_http_inline_env_cannot_satisfy_or_override_http_authentication_references(self):
        self.write(self.codex, '''
[mcp_servers.remote]
url = "https://example.invalid"
bearer_token_env_var = "MCP_TOKEN"
env_http_headers = { "X-Account" = "ACCOUNT_TOKEN" }
env = { MCP_TOKEN = "SECRET_INLINE_TOKEN", ACCOUNT_TOKEN = "SECRET_INLINE_ACCOUNT" }
''')
        result = self.inspect()
        self.assertEqual(result["servers"][0]["missing_env_names"], ["ACCOUNT_TOKEN", "MCP_TOKEN"])
        self.env.update(MCP_TOKEN="SECRET_CONTROL_TOKEN", ACCOUNT_TOKEN="SECRET_CONTROL_ACCOUNT")
        env = production_control_environment("codex_exec", "/usr/bin/codex", self.home / "control", environ=self.env)
        self.assertEqual(env["MCP_TOKEN"], "SECRET_CONTROL_TOKEN")
        self.assertEqual(env["ACCOUNT_TOKEN"], "SECRET_CONTROL_ACCOUNT")
        self.assertEqual(self.inspect()["servers"][0]["missing_env_names"], [])

    def test_missing_and_malformed_configs_are_diagnostic_without_raw_error_echo(self):
        absent = self.inspect()
        self.assertEqual(absent["servers"], [])
        self.assertEqual(absent["issues"], [])
        self.assertEqual(absent["config_sources"][0]["status"], "missing")
        for provider, path, content in [("codex_exec", self.codex, 'SECRET_CONFIG = "unterminated'),
                                        ("antigravity_exec", self.agy, '{"mcpServers": {}, "mcpServers": "SECRET_DUPLICATE"}')]:
            self.write(path, content)
            result = self.inspect(provider)
            self.assertEqual(result["servers"], [])
            self.assertIn("config_invalid", {i["code"] for i in result["issues"]})
            self.assertNotIn("SECRET", json.dumps(result))

    def test_symlink_ancestor_hardlink_fifo_and_size_limit_are_not_read(self):
        outside = self.home / "outside.toml"
        self.write(outside, '[mcp_servers.secret]\nurl="https://SECRET"')
        self.codex.parent.mkdir()
        self.codex.symlink_to(outside)
        result = self.inspect()
        self.assertEqual(result["config_sources"][0]["status"], "unsafe")
        self.codex.unlink()
        os.link(outside, self.codex)
        self.assertEqual(self.inspect()["servers"], [])
        self.codex.unlink()
        os.mkfifo(self.codex)
        self.assertEqual(self.inspect()["servers"], [])
        self.codex.unlink()
        self.codex.parent.rmdir()
        self.codex.parent.symlink_to(self.home / "outside-directory")
        self.assertEqual(self.inspect()["config_sources"][0]["status"], "unsafe")
        self.codex.parent.unlink()
        self.write(self.codex, "# SECRET_OVERSIZED" * 100)
        with patch("researchops.runners.native_mcp.MAX_CONFIG_BYTES", 32):
            self.assertEqual(self.inspect()["config_sources"][0]["status"], "unsafe")

    def test_add_disable_remove_and_missing_executable_are_visible(self):
        self.write(self.codex, '[mcp_servers.first]\ncommand="researchops-command-does-not-exist"\nenv_vars=["MISSING_TOKEN"]\n')
        first = self.inspect()
        self.assertEqual({i["code"] for i in first["issues"]}, {"executable_unavailable", "required_environment_missing"})
        self.write(self.codex, '[mcp_servers.second]\ncommand="/bin/true"\nenabled=false\n')
        second = self.inspect()
        self.assertNotEqual(first["config_revision"], second["config_revision"])
        self.assertEqual([s["name"] for s in second["servers"]], ["second"])
        self.assertFalse(second["servers"][0]["enabled"])
        self.codex.unlink()
        self.assertEqual(self.inspect()["servers"], [])

    def test_known_existing_binary_directories_are_added_without_arbitrary_path(self):
        for relative in (".local/bin", ".npm-global/bin"):
            (self.home / relative).mkdir(parents=True)
        env = production_control_environment("antigravity_exec", "/usr/bin/agy", self.home / "control", environ=self.env)
        self.assertIn(str(self.home / ".local/bin"), env["PATH"].split(":"))
        self.assertIn(str(self.home / ".npm-global/bin"), env["PATH"].split(":"))
        self.assertNotIn(".", env["PATH"].split(":"))
        self.assertNotIn("/untrusted/path", env["PATH"])

    def test_custom_codex_home_uses_config_only_and_ignores_project_config(self):
        custom = self.home / "custom"
        self.env["CODEX_HOME"] = str(custom)
        self.write(custom / "config.toml", '[mcp_servers.custom]\ncommand="/bin/true"\n')
        self.write(custom / "auth.json", "SECRET_AUTH_INVALID_JSON")
        self.write(self.project / ".codex/config.toml", '[mcp_servers.untrusted]\nurl="https://SECRET_PROJECT"\n')
        result = self.inspect(project_dir=self.project)
        self.assertEqual([s["name"] for s in result["servers"]], ["custom"])
        self.assertEqual(next(s for s in result["config_sources"] if s["scope"] == "project")["status"], "not_loaded")
        env = production_control_environment("codex_exec", "/usr/bin/codex", self.home / "control", environ=self.env)
        self.assertEqual(env["CODEX_HOME"], str(custom))
        self.assertNotIn("SECRET", json.dumps(result))

    def test_invalid_metadata_does_not_echo_arbitrary_names_or_policy_values(self):
        self.write(self.agy, json.dumps({"mcpServers": {"https://SECRET_SERVER": {}, "valid": {"serverUrl": "https://SECRET_ENDPOINT", "disabled": "SECRET_BOOLEAN", "disabledTools": ["https://SECRET_TOOL"]}}}))
        result = self.inspect("antigravity_exec")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertFalse(result["servers"][0]["enabled"])
        self.write(self.codex, '''
[mcp_servers.invalid]
command = "/bin/true"
env_vars = [{ name = "MCP_TOKEN", source = ["SECRET_INVALID_SOURCE"] }]
''')
        self.assertIn("server_metadata_invalid", {i["code"] for i in self.inspect()["issues"]})
        self.assertNotIn("SECRET", json.dumps(self.inspect()))
        self.env["HOME"] = "https://SECRET_HOME"
        self.assertEqual(self.inspect()["servers"], [])
        self.assertNotIn("SECRET", json.dumps(self.inspect()))


if __name__ == "__main__":
    unittest.main()
