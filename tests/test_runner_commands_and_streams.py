"""Offline CLI contracts: simulated streams are not real-provider evidence."""

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from researchops.runners.commands import build_antigravity_command, build_codex_command
from researchops.runners.streams import JsonLineEventCollector, RunnerStreamError


class TestPureRunnerCommands(unittest.TestCase):
    def test_codex_fresh_jsonl_uses_exact_stdin_and_no_shell(self):
        prompt = "한국어 자료 분석\n$(this is task data, not shell code)"
        with patch("subprocess.Popen") as spawn:
            command = build_codex_command("/usr/bin/codex", prompt=prompt, cwd=Path("/task/project"),
                writable_dirs=[Path("/task/tmp"), Path("/task/output")], model="configured-model",
                output_schema=Path("/task/input/result.schema.json"))
        spawn.assert_not_called()
        self.assertEqual(command.stdin, prompt.encode("utf-8"))
        self.assertEqual(command.argv[-1], "-")
        self.assertEqual(command.argv[0:3], ("/usr/bin/codex", "exec", "--json"))
        self.assertEqual(command.cwd, Path("/task/project"))
        self.assertIn("--ephemeral", command.argv)
        self.assertIn("--ignore-user-config", command.argv)
        self.assertIn("--ignore-rules", command.argv)
        self.assertIn("workspace-write", command.argv)
        self.assertEqual(command.argv.count("--add-dir"), 2)
        self.assertNotIn(prompt, command.argv)
        self.assertNotIn("danger-full-access", command.argv)

    def test_agy_print_stream_uses_process_cwd_and_verified_options(self):
        prompt = "자료 요약\nKeep the original records."
        with patch("subprocess.Popen") as spawn:
            command = build_antigravity_command("/usr/bin/agy", prompt=prompt, cwd=Path("/task/project"),
                writable_dirs=[Path("/task/output")], reasoning_effort="medium", model="configured-model",
                output_schema=Path("/task/input/result.schema.json"), timeout_seconds=120)
        spawn.assert_not_called()
        self.assertIsNone(command.stdin)
        self.assertEqual(command.argv[:3], ("/usr/bin/agy", "--print", prompt))
        self.assertEqual(command.cwd, Path("/task/project"))
        self.assertIn("stream-json", command.argv)
        self.assertIn("--sandbox", command.argv)
        self.assertIn("--disable-slash-commands", command.argv)
        self.assertIn("120s", command.argv)
        self.assertIn("--effort", command.argv)
        self.assertNotIn("--cd", command.argv)
        self.assertNotIn("--yolo", command.argv)

    def test_unverified_or_invalid_options_are_not_silently_dropped(self):
        with self.assertRaisesRegex(ValueError, "capability-verified"):
            build_codex_command("codex", prompt="task", cwd=Path("/task/project"), reasoning_effort="high")
        for bad in ("xhigh", "invalid", "--another-option"):
            with self.subTest(effort=bad), self.assertRaises(ValueError):
                build_antigravity_command("agy", prompt="task", cwd=Path("/task/project"), reasoning_effort=bad)
        for bad in (True, -1, 0, "120"):
            with self.subTest(timeout=bad), self.assertRaises(ValueError):
                build_antigravity_command("agy", prompt="task", cwd=Path("/task/project"), timeout_seconds=bad)

    def test_argument_and_path_boundaries(self):
        for builder in (build_codex_command, build_antigravity_command):
            for cwd in (Path("relative"), Path("/"), Path("/task/../host")):
                with self.subTest(builder=builder.__name__, cwd=cwd), self.assertRaises(ValueError):
                    builder("cli", prompt="task", cwd=cwd)
            with self.assertRaises(ValueError):
                builder("cli", prompt="task\x00secret", cwd=Path("/task/project"))
            with self.assertRaises(ValueError):
                builder("cli", prompt="task", cwd=Path("/task/project"), model="--bad")
        with self.assertRaises(ValueError):
            build_antigravity_command("agy", prompt="--unexpected-option", cwd=Path("/task/project"))


class TestBoundedJsonLineStreams(unittest.TestCase):
    def test_codex_lifecycle_preserves_complete_raw_events_across_byte_chunks(self):
        source = [
            {"type": "thread.started", "thread_id": "fixture-thread"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "서울 자료"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}},
        ]
        raw = "\n".join(json.dumps(item, ensure_ascii=False) for item in source).encode("utf-8")
        collector = JsonLineEventCollector("codex")
        for byte in raw:
            collector.feed(bytes([byte]))
        events = collector.finish()
        self.assertEqual([event["raw"] for event in events], source)
        self.assertEqual([event["category"] for event in events], ["session", "progress", "progress", "completed"])
        self.assertNotIn("success", events[-1])
        with self.assertRaises(RunnerStreamError):
            collector.feed(b"{}")

    def test_codex_provider_error_is_not_lost_and_unknown_event_is_retained(self):
        collector = JsonLineEventCollector("codex")
        collector.feed(b'{"type":"error","message":"fixture error"}\n'
                       b'{"type":"turn.failed","error":{"message":"fixture failed"}}\n'
                       b'{"type":"future.event","payload":{"keep":true}}\n')
        events = collector.finish()
        self.assertEqual([event["category"] for event in events], ["error", "error", "unknown"])
        self.assertEqual(events[-1]["raw"]["payload"], {"keep": True})

    def test_agy_unknown_schema_is_preserved_without_inventing_success_semantics(self):
        collector = JsonLineEventCollector("antigravity")
        collector.feed(b'{"type":"turn.completed","success":true}\r\n\n{"value":"untyped"}')
        events = collector.finish()
        self.assertEqual([event["category"] for event in events], ["unknown", "unknown"])
        self.assertTrue(events[0]["raw"]["success"])
        self.assertNotIn("success", events[0])

    def test_invalid_stream_is_rejected_without_echoing_raw_payload(self):
        payloads = [b'not-json-secret\n', b'{"type":"error","message":"\xff"}\n',
                    b'[]\n', b'{"x":NaN}\n', b'{"x":Infinity}\n', b'{"x":1,"x":2}\n',
                    b'{"type":false}\n', b'{"type":""}\n']
        for payload in payloads:
            with self.subTest(payload=payload):
                collector = JsonLineEventCollector("codex")
                with self.assertRaises(RunnerStreamError) as error:
                    collector.feed(payload)
                self.assertNotIn("secret", str(error.exception))

    def test_truncated_final_record_is_rejected(self):
        collector = JsonLineEventCollector("codex")
        collector.feed(b'{"type":"turn.completed"')
        with self.assertRaises(RunnerStreamError):
            collector.finish()

    def test_numeric_overflow_is_rejected_and_specific_diagnostics_are_preserved(self):
        for token in (b"1e400", b"-1e400", b"NaN", b"Infinity"):
            with self.subTest(token=token):
                collector = JsonLineEventCollector("codex")
                with self.assertRaisesRegex(RunnerStreamError, "Non-finite JSON"):
                    collector.feed(b'{"nested":{"number":' + token + b'}}\n')
                with self.assertRaisesRegex(RunnerStreamError, "closed"):
                    collector.feed(b'{}\n')
        collector = JsonLineEventCollector("codex")
        with self.assertRaisesRegex(RunnerStreamError, "Duplicate JSON object key"):
            collector.feed(b'{"x":1,"x":2}\n')

    def test_integer_digit_limit_error_remains_within_stream_error_boundary(self):
        limit = sys.get_int_max_str_digits()
        collector = JsonLineEventCollector("codex")
        if limit and limit < collector.max_line_bytes - 20:
            payload = b'{"number":' + b"9" * (limit + 1) + b'}\n'
            with self.assertRaisesRegex(RunnerStreamError, "Invalid UTF-8 or JSON event data") as error:
                collector.feed(payload)
        else:
            # Preserve the same exception-boundary test when the host disables
            # integer limits or sets one above the stricter event-size limit.
            with patch("researchops.runners.streams.json.loads", side_effect=ValueError("integer conversion limit")):
                with self.assertRaisesRegex(RunnerStreamError, "Invalid UTF-8 or JSON event data") as error:
                    collector.feed(b'{"number":1}\n')
        self.assertIsInstance(error.exception.__cause__, ValueError)
        with self.assertRaisesRegex(RunnerStreamError, "closed"):
            collector.feed(b'{}\n')

    def test_total_line_and_event_bounds_are_independent(self):
        collector = JsonLineEventCollector("codex", max_total_bytes=4)
        with self.assertRaisesRegex(RunnerStreamError, "total"):
            collector.feed(b'{}\n{}\n')
        for payload in (b'{"long":"payload"}', b'{"long":"payload"}\n'):
            collector = JsonLineEventCollector("codex", max_line_bytes=4)
            with self.assertRaisesRegex(RunnerStreamError, "line"):
                collector.feed(payload)
        collector = JsonLineEventCollector("codex", max_events=1)
        collector.feed(b'{}\n')
        with self.assertRaisesRegex(RunnerStreamError, "count"):
            collector.feed(b'{}\n')

    def test_invalid_collector_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            JsonLineEventCollector("unknown")
        for setting in ("max_total_bytes", "max_line_bytes", "max_events"):
            with self.subTest(setting=setting), self.assertRaises(ValueError):
                JsonLineEventCollector("codex", **{setting: True})
