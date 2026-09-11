"""Hermetic isolation checks and opt-in uncredentialed kernel acceptance."""

import io
import os
from pathlib import Path
import signal
import socket
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from researchops.runners.base import RunnerInvocationContext, RunnerExecutionResult
from researchops.runners.codex import CodexRunner
from researchops.runners.antigravity import AntigravityRunner
from researchops.runners.launcher import IsolatedProcessLauncher
from researchops.runners.probe import RunnerCapabilityProbe


class TestRunnerCapabilityProbe(unittest.TestCase):
    def test_cleanup_proof_is_never_inferred_from_process_success(self):
        result = RunnerExecutionResult(True, 0, "", "")
        self.assertFalse(result.cleanup_verified)

    def test_installed_binary_is_not_execution_readiness(self):
        with patch("shutil.which", return_value="/usr/bin/installed"), patch("subprocess.run") as execute:
            info = RunnerCapabilityProbe().probe_all()
        execute.assert_not_called()
        self.assertTrue(info["fake_runner"]["ready"])
        self.assertTrue(info["codex"]["available"])
        self.assertFalse(info["codex"]["ready"])
        self.assertFalse(info["antigravity"]["ready"])
        self.assertFalse(info["isolation"]["live_runner_ready"])
        self.assertFalse(info["isolation"]["credential_broker_ready"])

    def test_real_adapters_reject_overlapping_workspace_without_spawning(self):
        context = RunnerInvocationContext("t", "r", 1, "research")
        with tempfile.TemporaryDirectory() as root, patch("subprocess.Popen") as spawn, patch("shutil.which", return_value="/installed/cli"):
            path = Path(root)
            for runner in (CodexRunner(), AntigravityRunner()):
                for method in (runner.execute_research, runner.execute_compose):
                    result = method(path, path, path, path, context)
                    self.assertFalse(result.success)
                    self.assertTrue(result.cleanup_verified)
                    self.assertTrue("non-overlapping" in result.error_message or "stage mismatch" in result.error_message)
            self.assertEqual(list(path.iterdir()), [])
        spawn.assert_not_called()


class TestIsolatedProcessLauncher(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.tmp = self.root / "tmp"
        self.output = self.root / "output"
        self.input = self.root / "input"
        for path in (self.project, self.tmp, self.output, self.input):
            path.mkdir()
        self.launcher = IsolatedProcessLauncher()

    def tearDown(self):
        self.temp.cleanup()

    def test_environment_does_not_copy_credentials_or_accept_hooks(self):
        extra = {"OPENAI_API_KEY": "test", "SMTP_PASSWORD": "test", "LD_PRELOAD": "/bad",
                 "PATH": "/host", "HOME": "/host", "LANG": "C.UTF-8"}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test", "PATH": "/host"}):
            env = self.launcher.build_isolated_env(self.project, self.tmp, extra_env=extra)
        self.assertEqual(env["HOME"], str(self.project))
        self.assertEqual(env["TZ"], "Asia/Seoul")
        self.assertEqual(env["PATH"], "/usr/local/bin:/usr/bin:/bin")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("SMTP_PASSWORD", env)
        self.assertNotIn("LD_PRELOAD", env)
        self.assertNotIn("RESEARCHOPS_ISOLATED", env)
        self.assertEqual(list(self.project.iterdir()), [])

    def test_namespace_command_exposes_only_allowlisted_roots(self):
        with patch("shutil.which", return_value="/usr/bin/bwrap"):
            command = self.launcher.build_namespace_command(
                ["/usr/bin/true"], self.output, self.project, self.tmp, self.input)
        self.assertIn("--unshare-all", command)
        self.assertIn("--cap-drop", command)
        self.assertNotIn("--share-net", command)
        self.assertNotIn("/etc", command)
        self.assertNotIn(str(Path.home()), command)
        idx = command.index(str(self.input))
        self.assertEqual(command[idx - 1], "--ro-bind")
        self.assertNotIn(str(self.root), command)

    def test_legacy_credential_symlink_blocks_mount_without_following(self):
        (self.project / ".gemini").symlink_to(self.root / "nonexistent")
        with patch("shutil.which", return_value="/usr/bin/bwrap"):
            with self.assertRaisesRegex(Exception, "quarantine"):
                self.launcher.build_namespace_command(
                    ["/usr/bin/true"], self.output, self.project, self.tmp, self.input)

    def test_unavailable_isolation_never_falls_back_to_process_group(self):
        with patch("subprocess.Popen") as spawn:
            result = self.launcher.run(["/usr/bin/true"], self.output, self.project, self.tmp)
        self.assertFalse(result.success)
        self.assertFalse(result.isolation["spawned"])
        self.assertTrue(result.cleanup_verified)
        spawn.assert_not_called()

    def test_startup_failures_close_pipes_before_drain_ownership(self):
        for failure in ("helper_exit", "cgroup_assignment", "reader_start"):
            with self.subTest(failure=failure):
                proc = MagicMock()
                proc.pid = 12345
                proc.returncode = None
                proc.stdout = io.BytesIO()
                proc.stderr = io.BytesIO()
                group = MagicMock()
                stopped_status = (signal.SIGSTOP << 8) | 0x7f
                status = 0 if failure == "helper_exit" else stopped_status
                if failure == "cgroup_assignment":
                    group.__truediv__.return_value.write_text.side_effect = OSError("denied")
                with patch.object(self.launcher, "readiness", return_value={"ready": True}), \
                     patch.object(self.launcher, "build_namespace_command", return_value=["/mock/bwrap"]), \
                     patch.object(self.launcher, "_create_group", return_value=group), \
                     patch.object(self.launcher, "_kill_group", return_value=True) as kill_group, \
                     patch("researchops.runners.launcher.subprocess.Popen", return_value=proc), \
                     patch("researchops.runners.launcher.os.waitpid", return_value=(proc.pid, status)), \
                     patch("researchops.runners.launcher.threading.Thread") as reader_type:
                    reader_type.return_value.start.side_effect = RuntimeError("cannot start thread")
                    result = self.launcher.run(["/mock/true"], self.output, self.project, self.tmp)
                self.assertFalse(result.success)
                self.assertTrue(result.cleanup_verified)
                self.assertTrue(proc.stdout.closed)
                self.assertTrue(proc.stderr.closed)
                kill_group.assert_called_once_with(group, proc)
                group.rmdir.assert_called_once()
                reader_type.return_value.join.assert_not_called()

    def test_live_drain_keeps_cleanup_unverified_without_blocking_close(self):
        proc = MagicMock()
        proc.pid = 12345
        proc.returncode = 0
        proc.poll.return_value = 0
        proc.stdout = io.BytesIO()
        proc.stderr = io.BytesIO()
        group = MagicMock()
        with patch.object(self.launcher, "readiness", return_value={"ready": True}), \
             patch.object(self.launcher, "build_namespace_command", return_value=["/mock/bwrap"]), \
             patch.object(self.launcher, "_create_group", return_value=group), \
             patch.object(self.launcher, "_kill_group", return_value=True), \
             patch("researchops.runners.launcher.subprocess.Popen", return_value=proc), \
             patch("researchops.runners.launcher.os.waitpid",
                   return_value=(proc.pid, (signal.SIGSTOP << 8) | 0x7f)), \
             patch("researchops.runners.launcher.os.kill"), \
             patch("researchops.runners.launcher.threading.Thread") as reader_type:
            reader_type.return_value.is_alive.return_value = True
            result = self.launcher.run(["/mock/true"], self.output, self.project, self.tmp)
        self.assertFalse(result.success)
        self.assertFalse(result.cleanup_verified)
        self.assertFalse(proc.stdout.closed)
        self.assertFalse(proc.stderr.closed)
        self.assertIn("workspace must remain locked", result.error_message)
        group.rmdir.assert_not_called()
        proc.stdout.close()
        proc.stderr.close()

    @unittest.skipUnless(os.environ.get("RESEARCHOPS_TEST_CGROUP_ROOT"),
                         "Requires operator-provisioned delegated cgroup v2 root; no live AI")
    def test_kernel_namespace_containment_and_descendant_cleanup(self):
        launcher = IsolatedProcessLauncher(cgroup_root=Path(os.environ["RESEARCHOPS_TEST_CGROUP_ROOT"]))
        self.assertTrue(launcher.readiness()["ready"])
        secret = self.root / "host-only"
        secret.write_text("must not enter sandbox")
        result = launcher.run(
            ["/usr/bin/python3", "-c",
             "import os,pathlib,socket; "
             f"assert not pathlib.Path({str(secret)!r}).exists(); "
             "assert os.environ['TZ']=='Asia/Seoul'; "
             "print('contained')"],
            self.output, self.project, self.tmp, input_dir=self.input, timeout_seconds=5)
        self.assertTrue(result.success, result.error_message)
        self.assertTrue(result.cleanup_verified)
        self.assertEqual(result.stdout.strip(), "contained")

    @unittest.skipUnless(os.environ.get("RESEARCHOPS_TEST_CGROUP_ROOT"),
                         "Requires operator-provisioned delegated cgroup v2 root; no live AI")
    def test_kernel_timeout_and_detached_descendants(self):
        launcher = IsolatedProcessLauncher(cgroup_root=Path(os.environ["RESEARCHOPS_TEST_CGROUP_ROOT"]))
        result = launcher.run(
            ["/usr/bin/python3", "-c",
             "import subprocess,time; subprocess.Popen(['/usr/bin/sleep','30'],start_new_session=True); time.sleep(30)"],
            self.output, self.project, self.tmp, input_dir=self.input, timeout_seconds=1)
        self.assertFalse(result.success)
        self.assertTrue(result.cleanup_verified)
        self.assertIn("Timeout", result.error_message)

    @unittest.skipUnless(os.environ.get("RESEARCHOPS_TEST_CGROUP_ROOT"),
                         "Requires operator-provisioned delegated cgroup v2 root; no live AI")
    def test_kernel_network_namespace_cannot_connect_to_host_loopback(self):
        launcher = IsolatedProcessLauncher(cgroup_root=Path(os.environ["RESEARCHOPS_TEST_CGROUP_ROOT"]))
        self.assertTrue(launcher.readiness()["ready"])
        # This test only addresses its own temporary host-local listener. It
        # neither resolves a name nor sends traffic to an external destination.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(1)
            address = listener.getsockname()
            # Prove that the same listener is reachable outside the namespace.
            with socket.create_connection(address, timeout=1):
                connection, _ = listener.accept()
                connection.close()
            source = (
                "import errno,socket\n"
                "client=socket.socket(socket.AF_INET,socket.SOCK_STREAM)\n"
                "client.settimeout(1)\n"
                "try:\n"
                f" client.connect(('127.0.0.1',{address[1]}))\n"
                "except OSError as exc:\n"
                " assert isinstance(exc,TimeoutError) or exc.errno in "
                "{errno.ECONNREFUSED,errno.ENETUNREACH,errno.EHOSTUNREACH,"
                "errno.EACCES,errno.EPERM,errno.ETIMEDOUT}\n"
                " print('network-isolated')\n"
                "else:\n"
                " raise AssertionError('Host loopback listener was reachable')\n"
                "finally:\n"
                " client.close()\n"
            )
            result = launcher.run(["/usr/bin/python3", "-c", source], self.output,
                self.project, self.tmp, input_dir=self.input, timeout_seconds=5)
            self.assertTrue(result.success, result.error_message)
            self.assertTrue(result.cleanup_verified)
            self.assertEqual(result.stdout.strip(), "network-isolated")
            listener.settimeout(0.1)
            with self.assertRaises(TimeoutError):
                connection, _ = listener.accept()
                connection.close()

    @unittest.skipUnless(os.environ.get("RESEARCHOPS_TEST_CGROUP_ROOT"),
                         "Requires operator-provisioned delegated cgroup v2 root; no live AI")
    def test_kernel_memory_limit_reports_oom_and_cleans_group(self):
        launcher = IsolatedProcessLauncher(
            cgroup_root=Path(os.environ["RESEARCHOPS_TEST_CGROUP_ROOT"]),
            memory_bytes=64 * 1024 * 1024, max_pids=16)
        self.assertTrue(launcher.readiness()["ready"])
        observed = {}
        actual_cleanup = launcher._kill_group

        def capture_kernel_events(group, process):
            # Observe the real kernel counters while preserving the production
            # cleanup implementation. No launcher/kernel result is simulated.
            verified = actual_cleanup(group, process)
            observed["memory_max"] = int((group / "memory.max").read_text())
            observed["events"] = dict(line.split() for line in
                                       (group / "memory.events").read_text().splitlines())
            observed["path"] = group
            return verified

        source = (
            "print('allocation-started',flush=True)\n"
            "allocation=bytearray(256*1024*1024)\n"
            # Touch each page so virtual address reservation cannot masquerade
            # as proof that the cgroup enforces resident-memory consumption.
            "for offset in range(0,len(allocation),4096):\n"
            " allocation[offset]=1\n"
            "raise AssertionError('Memory allocation exceeded the configured limit')\n"
        )
        with patch.object(launcher, "_kill_group", side_effect=capture_kernel_events):
            result = launcher.run(["/usr/bin/python3", "-c", source], self.output,
                self.project, self.tmp, input_dir=self.input, timeout_seconds=10)
        self.assertFalse(result.success)
        self.assertNotEqual(result.exit_code, 0)
        self.assertTrue(result.cleanup_verified, result.error_message)
        self.assertEqual(result.stdout.strip(), "allocation-started")
        self.assertEqual(observed["memory_max"], 64 * 1024 * 1024)
        self.assertGreater(int(observed["events"]["oom_kill"]), 0)
        self.assertFalse(observed["path"].exists())

    @unittest.skipUnless(os.environ.get("RESEARCHOPS_TEST_CGROUP_ROOT"),
                         "Requires operator-provisioned delegated cgroup v2 root; no live AI")
    def test_kernel_pid_limit_rejects_bounded_fork_and_cleans_children(self):
        launcher = IsolatedProcessLauncher(
            cgroup_root=Path(os.environ["RESEARCHOPS_TEST_CGROUP_ROOT"]),
            memory_bytes=128 * 1024 * 1024, max_pids=16)
        self.assertTrue(launcher.readiness()["ready"])
        observed = {}
        actual_cleanup = launcher._kill_group

        def capture_kernel_events(group, process):
            verified = actual_cleanup(group, process)
            observed["pids_max"] = int((group / "pids.max").read_text())
            observed["events"] = dict(line.split() for line in
                                       (group / "pids.events").read_text().splitlines())
            observed["path"] = group
            return verified

        source = (
            "import errno,os,time\n"
            "children=[]\n"
            # This is deliberately capped even if an implementation regression
            # were to remove the stricter kernel pids.max=16 limit.
            "for attempt in range(32):\n"
            " try:\n"
            "  child=os.fork()\n"
            " except OSError as exc:\n"
            "  assert exc.errno==errno.EAGAIN\n"
            "  print('pids-limited:'+str(len(children)),flush=True)\n"
            "  break\n"
            " if child==0:\n"
            "  time.sleep(30)\n"
            "  os._exit(0)\n"
            " children.append(child)\n"
            "else:\n"
            " raise AssertionError('Configured PID limit did not reject bounded forks')\n"
        )
        with patch.object(launcher, "_kill_group", side_effect=capture_kernel_events):
            result = launcher.run(["/usr/bin/python3", "-c", source], self.output,
                self.project, self.tmp, input_dir=self.input, timeout_seconds=5)
        self.assertTrue(result.success, result.error_message)
        self.assertTrue(result.cleanup_verified)
        self.assertTrue(result.stdout.strip().startswith("pids-limited:"))
        child_count = int(result.stdout.strip().split(":")[1])
        self.assertGreater(child_count, 0)
        self.assertLess(child_count, 16)
        self.assertEqual(observed["pids_max"], 16)
        self.assertGreater(int(observed["events"]["max"]), 0)
        self.assertFalse(observed["path"].exists())


if __name__ == "__main__":
    unittest.main()
