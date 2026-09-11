"""Local subprocess tests only: no provider, credential, network, or SMTP use."""

import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from researchops.runners.development_process import run_bounded


@unittest.skipUnless(os.name == "posix", "development CLI execution requires POSIX")
class DevelopmentProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cwd = Path(self.temporary.name)

    def execute(self, source, **options):
        defaults = {"cwd": self.cwd, "env": {"TZ": "Asia/Seoul"}, "timeout_seconds": 3}
        defaults.update(options)
        return run_bounded([sys.executable, "-I", "-c", source], **defaults)

    def test_success_preserves_bytes_and_explicit_environment(self):
        with patch.dict(os.environ, {"RESEARCHOPS_TEST_SECRET": "not-for-child"}):
            result = self.execute(
                "import os,sys; assert 'RESEARCHOPS_TEST_SECRET' not in os.environ; "
                "assert os.environ['TZ']=='Asia/Seoul'; "
                "sys.stdout.buffer.write('서울'.encode()); sys.stderr.buffer.write(b'notice')")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "서울".encode())
        self.assertEqual(result.stderr, b"notice")
        self.assertTrue(result.spawned)
        self.assertTrue(result.cleanup_verified)
        self.assertIsNone(result.error)
        self.assertGreater(result.cleanup_evidence["direct_child_pid"], 1)
        for key in ("group_signal_succeeded", "direct_child_reaped", "captured_pipes_eof", "process_group_quiescent"):
            self.assertTrue(result.cleanup_evidence[key], key)

    def test_nonzero_exit_does_not_invent_transport_error(self):
        result = self.execute("import sys; print('bad', file=sys.stderr); sys.exit(7)")
        self.assertEqual(result.exit_code, 7)
        self.assertEqual(result.stderr, b"bad\n")
        self.assertIsNone(result.error)
        self.assertTrue(result.cleanup_verified)

    def test_spawn_failure_is_distinct_and_does_not_expose_arguments(self):
        result = run_bounded(["/nonexistent/development-cli", "sensitive-value"],
                             cwd=self.cwd, env={}, timeout_seconds=1)
        self.assertFalse(result.spawned)
        self.assertTrue(result.cleanup_verified)
        self.assertEqual(result.error, "spawn_failed: FileNotFoundError")
        self.assertEqual(result.stdout + result.stderr, b"")

    def test_timeout_reaps_child(self):
        start = time.monotonic()
        result = self.execute("import time; print('started', flush=True); time.sleep(60)",
                              timeout_seconds=1)
        self.assertLess(time.monotonic() - start, 4)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.error, "timeout")
        self.assertEqual(result.exit_code, -signal.SIGKILL)
        self.assertEqual(result.stdout, b"started\n")
        self.assertTrue(result.cleanup_verified)

    def test_cancel_before_spawn(self):
        with patch("subprocess.Popen") as spawn:
            result = self.execute("raise AssertionError", cancellation_check=lambda: True)
        spawn.assert_not_called()
        self.assertTrue(result.cancelled)
        self.assertFalse(result.spawned)
        self.assertTrue(result.cleanup_verified)

    def test_cancel_running_child(self):
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            return calls >= 4

        result = self.execute("import time; time.sleep(60)", cancellation_check=cancelled)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.timed_out)
        self.assertTrue(result.spawned)
        self.assertTrue(result.cleanup_verified)

    def test_callback_exception_terminates_child_and_hides_exception_message(self):
        calls = 0

        def broken_callback():
            nonlocal calls
            calls += 1
            if calls > 2:
                raise RuntimeError("secret callback diagnostic")
            return False

        result = self.execute("import time; time.sleep(60)", cancellation_check=broken_callback)
        self.assertEqual(result.error, "cancellation_check_failed: RuntimeError")
        self.assertTrue(result.spawned)
        self.assertTrue(result.cleanup_verified)

    def test_callback_exception_before_spawn_is_cleanly_reported(self):
        def broken_callback():
            raise ValueError("private callback state")

        with patch("subprocess.Popen") as spawn:
            result = self.execute("pass", cancellation_check=broken_callback)
        spawn.assert_not_called()
        self.assertEqual(result.error, "cancellation_check_failed: ValueError")
        self.assertTrue(result.cleanup_verified)

    def test_output_limit_is_combined_and_kills_infinite_writer(self):
        result = self.execute("import os;\nwhile True: os.write(1,b'x'*8192); os.write(2,b'y'*8192)",
                              max_output_bytes=1025)
        self.assertEqual(len(result.stdout) + len(result.stderr), 1025)
        self.assertEqual(result.error, "output_limit_exceeded")
        self.assertTrue(result.cleanup_verified)
        self.assertFalse(result.timed_out)

    def test_exact_output_limit_is_not_overflow(self):
        result = self.execute("import os; os.write(1,b'abc'); os.write(2,b'de')",
                              max_output_bytes=5)
        self.assertEqual(result.stdout, b"abc")
        self.assertEqual(result.stderr, b"de")
        self.assertIsNone(result.error)
        self.assertTrue(result.cleanup_verified)

    def test_bounded_raw_bytes_may_end_inside_utf8_character(self):
        result = self.execute("import os; os.write(1, '서울'.encode())", max_output_bytes=4)
        self.assertEqual(result.stdout, "서울".encode()[:4])
        self.assertEqual(result.error, "output_limit_exceeded")
        with self.assertRaises(UnicodeDecodeError):
            result.stdout.decode("utf-8", "strict")

    def test_concurrent_stdin_and_output_do_not_deadlock(self):
        content = b"a" * 300_000
        result = self.execute(
            "import os; os.write(2,b'e'*100000); "
            "data=b''\nwhile True:\n chunk=os.read(0,8192)\n"
            " if not chunk: break\n data+=chunk\n"
            "os.write(1,str(len(data)).encode())", stdin=content)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, b"300000")
        self.assertEqual(result.stderr, b"e" * 100_000)
        self.assertTrue(result.cleanup_verified)

    def test_closed_stdin_does_not_abort_output_collection(self):
        result = self.execute("import os; os.close(0); os.write(1,b'done')", stdin=b"x" * 300_000)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, b"done")
        self.assertIsNone(result.error)

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "Linux process-state verification")
    def test_normal_leader_exit_cleans_its_descendant_group(self):
        result = self.execute(
            "import os,subprocess,sys; "
            "child=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(60)']); "
            "print(child.pid,flush=True)")
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.cleanup_verified)
        child_pid = int(result.stdout.strip())
        child_stat = Path(f"/proc/{child_pid}/stat")
        if child_stat.exists():
            value = child_stat.read_text()
            self.assertIn(value[value.rfind(")") + 2:].split()[0], {"Z", "X"})

    def test_escaped_descendant_holding_pipe_is_not_claimed_clean(self):
        # An intentionally escaped local test process illustrates, rather than
        # hides, the limitation of process-group cleanup. No model is involved.
        try:
            started = time.monotonic()
            result = self.execute(
                "import pathlib,subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(60)'], "
                "start_new_session=True); "
                "pathlib.Path('escaped.pid').write_text(str(child.pid))")
            self.assertLess(time.monotonic() - started, 4)
            self.assertEqual(result.exit_code, 0)
            self.assertFalse(result.cleanup_verified)
            self.assertEqual(result.error, "process_group_cleanup_unverified")
            self.assertTrue(result.cleanup_evidence["direct_child_reaped"])
            self.assertFalse(result.cleanup_evidence["captured_pipes_eof"])
        finally:
            pid_file = self.cwd / "escaped.pid"
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_initial_pipe_setup_failure_still_reaps_process_and_closes_fds(self):
        with patch("os.set_blocking", side_effect=OSError("private OS detail")):
            result = self.execute("import time; time.sleep(60)")
        self.assertTrue(result.spawned)
        self.assertEqual(result.exit_code, -signal.SIGKILL)
        self.assertEqual(result.error, "process_io_failed: OSError")
        self.assertTrue(result.cleanup_verified)

    def test_invalid_limits_are_rejected_before_spawn(self):
        for options in ({"timeout_seconds": 0}, {"timeout_seconds": True},
                        {"max_output_bytes": 0}, {"max_output_bytes": -1}):
            with self.subTest(options=options), patch("subprocess.Popen") as spawn:
                with self.assertRaises(ValueError):
                    self.execute("pass", **options)
                spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
