"""Real-adapter contract tests; never invokes an installed CLI or provider."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops.runners.antigravity import AntigravityRunner
from researchops.runners.base import RunnerInvocationContext
from researchops.runners.codex import CodexRunner
from researchops.runners.launcher import IsolatedProcessLauncher
from researchops.runners.probe import RunnerCapabilityProbe
from researchops.services.application import ApplicationService
from tests.support import isolated_settings, register_fixture_task


def host_readiness(ready=False):
    return {"ready": ready, "blockers": [] if ready else ["delegated cgroup is not configured"],
            "tier": "namespace-cgroup" if ready else "blocked", "bwrap_path": "/usr/bin/bwrap",
            "credential_broker_ready": False, "filesystem_quota_ready": False}


class TestRealRunnerPreflightContract(unittest.TestCase):
    def test_missing_binary_and_internal_and_host_requirements_are_separate(self):
        with patch("shutil.which", return_value=None), \
             patch.object(IsolatedProcessLauncher, "readiness", return_value=host_readiness()):
            report = RunnerCapabilityProbe().probe_codex()
        by_id = {entry["id"]: entry for entry in report["blockers"]}
        self.assertFalse(report["available"])
        self.assertFalse(report["ready"])
        self.assertIsNone(report["authenticated"])
        self.assertEqual(report["authentication_status"], "not_checked")
        self.assertEqual(by_id["RUNNER-BINARY-UNAVAILABLE"]["owner"], "operator")
        self.assertEqual(by_id["EXT-RUNNER-ISOLATION"]["category"], "host_prerequisite")
        self.assertEqual(set(report["internal_missing"]), {
            "INT-CREDENTIAL-BROKER", "INT-RESEARCH-PROXY", "INT-WORKSPACE-QUOTA"})
        for entry in by_id.values():
            self.assertTrue(entry["resume_condition"])

    def test_installed_binary_enables_trusted_production_not_hostile_isolation(self):
        with patch("shutil.which", return_value="/installed/codex"), \
             patch.object(IsolatedProcessLauncher, "readiness", return_value=host_readiness(True)):
            report = CodexRunner().preflight()
        self.assertTrue(report["available"])
        self.assertEqual(report["operator_missing"], [])
        self.assertEqual(report["internal_missing"], [])
        self.assertTrue(report["ready"])
        self.assertEqual(report["tier"], "trusted-operator-production")
        self.assertFalse(report["hostile_process_isolation"])
        self.assertFalse(report["spawned"])

    def test_invalid_workspace_is_held_without_process_or_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            for runner in (CodexRunner(), AntigravityRunner()):
                for stage in ("research", "compose"):
                    with self.subTest(provider=type(runner).__name__, stage=stage), \
                         patch("shutil.which", return_value="/installed/cli"), \
                         patch.object(IsolatedProcessLauncher, "readiness", return_value=host_readiness(True)), \
                         patch("subprocess.Popen") as popen, patch("subprocess.run") as execute, \
                         patch.object(Path, "read_text", side_effect=AssertionError("no auth file reads")), \
                         patch.object(Path, "read_bytes", side_effect=AssertionError("no auth file reads")):
                        context = RunnerInvocationContext("task", "run", 2, stage, fencing_token="lease-token")
                        result = getattr(runner, "execute_" + stage)(path, path, path, path, context)
                        self.assertFalse(result.success)
                        self.assertEqual(result.exit_code, 78)
                        self.assertTrue(result.cleanup_verified)
                        self.assertFalse(result.isolation["spawned"])
                        self.assertEqual(result.events[0]["stage"], stage)
                        self.assertEqual(result.events[0]["fencing_token"], "lease-token")
                        self.assertEqual(result.events[0]["attempt"], 2)
                        self.assertEqual(result.events[0]["authentication_status"], "not_checked")
                        popen.assert_not_called()
                        execute.assert_not_called()
            self.assertEqual(list(path.iterdir()), [])

    def test_configured_cgroup_is_consistently_used_by_probe_and_adapter(self):
        group = Path("/sys/fs/cgroup/operator-provisioned")
        runner = AntigravityRunner(cgroup_root=group)
        self.assertEqual(runner.launcher.cgroup_root, group)
        observed = []

        def check(instance):
            observed.append(instance.cgroup_root)
            return host_readiness()

        with patch.object(IsolatedProcessLauncher, "readiness", check), patch("shutil.which", return_value=None):
            runner.preflight()
            RunnerCapabilityProbe(cgroup_root=group).probe_antigravity()
        # Host diagnostics remain available, but do not gate trusted native CLI.
        self.assertEqual(observed, [group])


class TestRealRunnerHeldLifecycle(unittest.TestCase):
    def check_held_lifecycle(self, runner):
        with tempfile.TemporaryDirectory() as temporary:
            settings = isolated_settings(Path(temporary))
            app = ApplicationService(settings, custom_runner=runner)
            register_fixture_task(app)
            queued = app.runs.enqueue_run("software-releases", force_dry_run=True)
            with patch("subprocess.Popen") as spawn:
                result = app.runs.execute_run(queued.run_id, force_dry_run=True)
            spawn.assert_not_called()
            self.assertEqual(result.status, "needs_attention", result.error_message)
            self.assertIn("RUNNER-BINARY-UNAVAILABLE", result.error_message)
            self.assertIsNone(app.delivery_repo.get_handoff_for_run(result.run_id))
            self.assertFalse(app.workspace_mgr.is_locked(result.task_id)[0])
            self.assertIsNone(app.run_repo.get_lease(result.run_id))
            archive = settings.paths.run_archive_dir / result.task_id / result.run_id
            manifest = json.loads((archive / "run-manifest.json").read_text())
            events = json.loads((archive / "logs/research.events.json").read_text())
            self.assertEqual(manifest["status"], "needs_attention")
            self.assertEqual(events[0]["type"], "runner_blocked")
            self.assertFalse(events[0]["spawned"])
            self.assertFalse((archive / "result.json").exists())
            self.assertFalse((archive / "email.html").exists())

    def test_codex_held_run_is_not_a_failed_provider_invocation(self):
        self.check_held_lifecycle(CodexRunner("/nonexistent/codex"))

    def test_antigravity_held_run_is_not_a_failed_provider_invocation(self):
        self.check_held_lifecycle(AntigravityRunner("/nonexistent/agy"))
