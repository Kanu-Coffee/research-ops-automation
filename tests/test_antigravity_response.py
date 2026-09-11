"""Synthetic regressions for the observed live Antigravity envelope shape."""

import json
import unittest

from researchops.runners.antigravity_response import (
    AntigravityResponseError, parse_antigravity_response,
)
from researchops.runners.streams import RunnerStreamError


def event_fixture():
    return [
        {"event": "init", "init": {"permission_mode": "request-review", "tools": ["finish"]}},
        {"event": "step_update", "step_update": {"step_type": "user_input", "state": "DONE"}},
        {"event": "step_update", "step_update": {"step_type": "agent_response", "state": "DONE"}},
        {"event": "step_update", "step_update": {"step_type": "finish", "state": "DONE"}},
        {"event": "result", "result": {
            "status": "SUCCESS", "response": '{"status":"ok"}\n{"toolAction":"completion"}\n',
            "structured_output": {"status": "ok", "sum": 5, "timezone": "Asia/Seoul"},
            "duration_seconds": 1.25, "num_turns": 2,
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }},
    ]


def encode(events):
    return ("\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n").encode("utf-8")


class TestAntigravityResponse(unittest.TestCase):
    def test_actual_envelope_shape_uses_only_structured_output(self):
        parsed = parse_antigravity_response(encode(event_fixture()))
        self.assertEqual(parsed.structured_output, {"status": "ok", "sum": 5, "timezone": "Asia/Seoul"})
        self.assertEqual(parsed.usage["total_tokens"], 15)
        self.assertEqual(parsed.duration_seconds, 1.25)
        self.assertEqual(parsed.num_turns, 2)
        self.assertEqual(parsed.event_count, 5)
        self.assertEqual(parsed.step_types, ("user_input", "agent_response", "finish"))
        self.assertIsNone(parsed.model)
        self.assertNotIn("toolAction", parsed.structured_output)

    def test_unstructured_text_is_never_a_fallback_import_source(self):
        for value in (None, [], "{\"status\":\"ok\"}", 1, True):
            events = event_fixture()
            events[-1]["result"]["structured_output"] = value
            with self.subTest(value=value), self.assertRaises(AntigravityResponseError):
                parse_antigravity_response(encode(events))

    def test_init_and_unique_terminal_result_are_required(self):
        events = event_fixture()
        invalid = [[], events[1:], events[:-1], events + [events[-1]],
                   events + [events[1]], [events[0], events[0], events[-1]],
                   [{"event": "init", "init": []}, events[-1]]]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(AntigravityResponseError):
                parse_antigravity_response(encode(data))

    def test_failed_result_and_tool_action_are_rejected_without_payload_echo(self):
        for status in ("ERROR", "CANCELLED", "TIMEOUT", "success", True, None):
            events = event_fixture()
            events[-1]["result"]["status"] = status
            events[-1]["result"]["response"] = "private credential payload"
            with self.subTest(status=status), self.assertRaises(AntigravityResponseError) as error:
                parse_antigravity_response(encode(events))
            self.assertNotIn("private", str(error.exception))
        for step in ("run_command", "view_file", "search_web", "call_mcp_tool", "future_action", None, [], {}):
            events = event_fixture()
            events[1]["step_update"]["step_type"] = step
            with self.subTest(step=step), self.assertRaisesRegex(AntigravityResponseError, "action step"):
                parse_antigravity_response(encode(events))

    def test_error_events_cannot_be_hidden_by_later_success(self):
        events = event_fixture()
        events.insert(2, {"event": "error", "message": "private fixture"})
        with self.assertRaises(AntigravityResponseError) as error:
            parse_antigravity_response(encode(events))
        self.assertNotIn("private", str(error.exception))

    def test_optional_evidence_is_not_fabricated(self):
        events = event_fixture()
        for key in ("usage", "duration_seconds", "num_turns"):
            del events[-1]["result"][key]
        parsed = parse_antigravity_response(encode(events))
        self.assertEqual(parsed.usage, {})
        self.assertIsNone(parsed.duration_seconds)
        self.assertIsNone(parsed.num_turns)

    def test_soft_denial_is_not_success_even_with_structured_output(self):
        for key, value in (("denied_actions", [{"action": "command"}]), ("error", "private error")):
            events = event_fixture()
            events[-1]["result"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(AntigravityResponseError, "denied actions") as error:
                parse_antigravity_response(encode(events))
            self.assertNotIn("private", str(error.exception))

    def test_invalid_metadata_is_rejected(self):
        invalid = [("usage", None), ("usage", {"input_tokens": True}),
                   ("usage", {"input_tokens": -1}), ("usage", {"input_tokens": 1.5}),
                   ("duration_seconds", -1), ("duration_seconds", "1"),
                   ("duration_seconds", True), ("duration_seconds", 10 ** 400),
                   ("num_turns", -1), ("num_turns", True), ("num_turns", "1")]
        for key, value in invalid:
            events = event_fixture()
            events[-1]["result"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(AntigravityResponseError):
                parse_antigravity_response(encode(events))

    def test_strict_stream_bounds_json_and_encoding_remain_enforced(self):
        for raw in (b'{"event":"init","event":"result"}\n', b'{"x":NaN}\n', b'\xff\n',
                    b'[]\n', b'{"event":"result"', b'{"x":1e400}\n'):
            with self.subTest(raw=raw), self.assertRaises(RunnerStreamError):
                parse_antigravity_response(raw)
        with self.assertRaisesRegex(RunnerStreamError, "total byte limit"):
            parse_antigravity_response(encode(event_fixture()), max_total_bytes=10)


if __name__ == "__main__":
    unittest.main()
