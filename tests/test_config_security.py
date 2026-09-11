"""Strict settings validation without consulting or modifying live runtime."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from researchops.config import load_settings
from researchops.errors import ConfigError


class TestConfigSecurity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "settings.yaml"

    def tearDown(self):
        self.temp.cleanup()

    def load(self, data):
        self.config_path.write_text(yaml.safe_dump(data))
        with patch("researchops.config.find_repo_root", return_value=self.root):
            return load_settings(self.config_path)

    def test_defaults_are_seoul_dryrun_and_send_blocked(self):
        settings = self.load({})
        self.assertEqual(settings.timezone, "Asia/Seoul")
        self.assertEqual(settings.delivery.default_mode, "dry_run")
        self.assertTrue(settings.delivery.global_handoff_kill_switch)
        self.assertTrue(settings.web.csrf_protection)
        self.assertEqual(settings.runner.global_concurrency, 2)

    def test_string_booleans_and_bad_numbers_fail(self):
        for data in ({"delivery": {"global_handoff_kill_switch": "false"}},
                     {"runner": {"global_concurrency": True}},
                     {"web": {"port": "8765"}}, {"runner": {"default_timeout_seconds": 0}}):
            with self.subTest(data=data), self.assertRaises(ConfigError):
                self.load(data)

    def test_two_korean_markdown_fields_fit_default_and_explicit_limit_is_preserved(self):
        from urllib.parse import urlencode
        from researchops.config import WebConfig
        body = urlencode({'task_md': '가' * 100_000, 'email_spec_md': '나' * 100_000,
                          'csrf_token': 'c' * 64, 'request_key': 'r' * 64}).encode()
        self.assertLess(len(body), self.load({}).web.max_request_bytes)
        self.assertEqual(WebConfig().max_request_bytes, 2_000_000)
        self.assertEqual(self.load({'web': {'max_request_bytes': 500_000}}).web.max_request_bytes, 500_000)
        with self.assertRaises(ConfigError):
            self.load({'web': {'max_request_bytes': False}})

    def test_unsupported_timezone_profile_and_web_security_fail(self):
        for data in ({"timezone": "UTC"}, {"runner": {"default_network_profile": "direct"}},
                     {"web": {"csrf_protection": False}}, {"web": {"bind": "0.0.0.0"}},
                     {"web": {"trusted_proxy_cidrs": ["invalid"]}},
                     {"network_profiles": {"public": {"mode": "direct"}}}):
            with self.subTest(data=data), self.assertRaises(ConfigError):
                self.load(data)

    def test_audit_or_secret_inside_workspace_fails(self):
        for data in ({"paths": {"run_archive_dir": "var/task-workspaces/archive"}},
                     {"paths": {"database": "var/task-workspaces/db.sqlite"}},
                     {"paths": {"schemas_dir": "var/task-workspaces/schemas"}},
                     {"paths": {"data_dir": "/"}},
                     {"paths": {"database": "var/delivery_config.yaml"}},
                     {"paths": {"tasks_dir": "."}}):
            with self.subTest(data=data), self.assertRaises(ConfigError):
                self.load(data)

    def test_missing_explicit_config_is_an_error(self):
        with self.assertRaises(ConfigError):
            load_settings(self.root / "missing.yaml")

    def test_remote_proxy_requires_opt_in_and_exact_peer(self):
        config = {"bind": "192.168.50.10", "allow_remote_proxy": True,
                  "trusted_proxy_cidrs": ["10.50.0.2/32", "127.0.0.1/32"],
                  "allowed_hosts": ["research.example.test"]}
        self.assertTrue(self.load({"web": config}).web.allow_remote_proxy)
        for change in ({"allow_remote_proxy": False}, {"allow_remote_proxy": "true"},
                       {"trusted_proxy_cidrs": ["10.50.0.0/24"]},
                       {"trusted_proxy_cidrs": ["0.0.0.0/0"]},
                       {"trusted_proxy_cidrs": ["127.0.0.1/32"]},
                       {"trusted_proxy_cidrs": ["0.0.0.0/32"]},
                       {"trusted_proxy_cidrs": ["224.0.0.1/32"]},
                       {"trusted_proxy_cidrs": ["169.254.1.1/32"]},
                       {"trusted_proxy_cidrs": ["10.50.0.2/32"] * 9},
                       {"bind": "0.0.0.0"}, {"bind": "8.8.8.8"},
                       {"bind": "169.254.1.1"}, {"bind": "::"},
                       {"bind": "::ffff:192.168.50.10"}, {"csrf_protection": False},
                       {"require_origin_check": False}):
            with self.subTest(change=change), self.assertRaises(ConfigError):
                self.load({"web": {**config, **change}})

    def test_remote_proxy_private_ipv6_host_peer(self):
        web = self.load({"web": {"allow_remote_proxy": True, "bind": "fd99::4",
                                  "trusted_proxy_cidrs": ["fd99::1/128"]}}).web
        self.assertEqual(web.bind, "fd99::4")
        with self.assertRaises(ConfigError):
            self.load({"web": {"allow_remote_proxy": True, "bind": "fd99::4",
                               "trusted_proxy_cidrs": ["fd99::/64"]}})

    def test_insecure_auth_opt_in_is_strictly_local(self):
        self.assertFalse(self.load({}).web.allow_insecure_local_auth)
        self.assertTrue(self.load({"web": {"allow_insecure_local_auth": True}}).web.allow_insecure_local_auth)
        for web in ({"allow_insecure_local_auth": "true"},
                    {"allow_insecure_local_auth": True, "allow_remote_proxy": True},
                    {"allow_insecure_local_auth": True, "bind": "192.168.50.10",
                     "allow_remote_proxy": True, "trusted_proxy_cidrs": ["10.50.0.2/32"]}):
            with self.subTest(web=web), self.assertRaises(ConfigError):
                self.load({"web": web})


if __name__ == "__main__":
    unittest.main()
