"""Agy lifecycle extensions must not enter Codex parsing or close Codex tools."""
import unittest
from unittest.mock import patch

from researchops.runners.production_trace import ProductionToolTraceCollector
from researchops.runners.tool_events import parse_tool_trace
from tests.test_runner_tool_events import codex_events, encode


def cases():
    tool = {"id": "command", "type": "command_execution", "status": "in_progress"}
    pending = codex_events()
    pending.insert(2, {"type": "item.started", "item": tool})
    mixed = pending.copy()
    mixed.insert(3, {"event": "step_update", "step_update": {
        "step_type": "finish", "state": "DONE", "step_index": 112}})
    return {
        "success": (codex_events(), True),
        "failed_terminal": (codex_events(status="turn.failed"), False),
        "cancelled_terminal": (codex_events(status="turn.cancelled"), None),
        "pending_tool": (pending, None),
        "foreign_finish_cannot_close_tool": (mixed, None),
        "missing_terminal": (codex_events()[:-1], None),
    }


class CodexFinishIsolationTests(unittest.TestCase):
    def test_codex_never_calls_agy_finish_handler(self):
        with patch("researchops.runners.tool_events.consume_agy_finish",
                   side_effect=AssertionError("Agy handler called for Codex")), patch(
                   "researchops.runners.production_trace.consume_agy_finish",
                   side_effect=AssertionError("Agy handler called for Codex")):
            for name, (events, expected) in cases().items():
                for mode in ("batch", "stream"):
                    with self.subTest(case=name, mode=mode):
                        raw = encode(events)
                        collector = ProductionToolTraceCollector("codex_exec")
                        def parse():
                            if mode == "batch":
                                return parse_tool_trace("codex_exec", raw)
                            collector.feed(raw)
                            return collector.finish()
                        if expected is None:
                            with self.assertRaises(ValueError):
                                parse()
                        else:
                            self.assertEqual(parse().successful_terminal, expected)
