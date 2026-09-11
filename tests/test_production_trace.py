"""Large synthetic JSONL and partial evidence; no model, SMTP or live data."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from researchops.runners.development_process import run_bounded
from researchops.runners.mcp_audit import summarize_mcp_tools, validate_agy_mcp_reads
from researchops.runners.production_trace import (
    ProductionToolTraceCollector, TRUNCATED_RESPONSE_WARNING, TraceStreamError,
    parse_production_trace_file,
)
from researchops.runners.tool_events import WEB_SEARCH_ID_COMPAT_WARNING, parse_tool_trace
from tests.test_runner_tool_events import agy_events, agy_step, codex_events, encode


def mcp_item(index, *, text="synthetic", status="completed"):
    return {"id": f"item_{index}", "type": "mcp_tool_call", "status": status,
            "server": "synthetic_server", "tool": "search_contracts",
            "result": {"content": [{"type": "text", "text": text}], "isError": False}}


def parse(provider, events, **limits):
    collector = ProductionToolTraceCollector(provider, **limits)
    raw = events if isinstance(events, bytes) else encode(events)
    for offset in range(0, len(raw), 65_536):
        collector.feed(raw[offset:offset + 65_536])
    return collector, collector.finish()


class ProductionTraceTests(unittest.TestCase):
    def test_fifteen_completed_calls_survive_truncated_sixteenth_response(self):
        events = codex_events([mcp_item(i, text="x" * 1_133_664 if i == 14 else "synthetic") for i in range(15)])[:-2]
        events.append({"type": "item.started", "item": mcp_item(15, status="in_progress")})
        prefix = encode(events)
        tail = encode([{"type": "item.completed", "item": mcp_item(15, text="y" * 1_133_664)}])[:683_140]
        collector = ProductionToolTraceCollector("codex_exec")
        collector.feed(prefix)
        collector.feed(tail)
        with self.assertRaisesRegex(TraceStreamError, "trace_incomplete_event"):
            collector.finish()
        trace, diagnostic = collector.partial_trace(), collector.diagnostics("output_limit_exceeded")
        self.assertEqual(len(trace.tools), 15)
        self.assertTrue(all(tool["success"] for tool in trace.tools))
        self.assertTrue(trace.model_activity_observed)
        self.assertFalse(trace.terminal)
        self.assertFalse(trace.successful_terminal)
        self.assertIsNone(trace.response)
        self.assertEqual(diagnostic["completed_mcp_count"], 15)
        self.assertEqual(diagnostic["mcp_status_counts"], {"succeeded": 15})
        self.assertEqual(diagnostic["parsed_bytes"], len(prefix))
        self.assertEqual(diagnostic["unparsed_bytes"], len(tail))
        self.assertEqual(diagnostic["termination_reason"], "output_limit_exceeded")
        self.assertEqual(diagnostic["pending_tool_count"], 1)
        self.assertEqual(diagnostic["pending_tools"][0]["tool"], "search_contracts")
        self.assertGreater(diagnostic["largest_event_bytes"], 1_133_664)

    def test_large_responses_do_not_change_final_response_limit(self):
        events = codex_events([mcp_item(i, text="약관 서울 😀" * 100_000) for i in range(3)])
        collector, trace = parse("codex_exec", events)
        self.assertTrue(trace.successful_terminal)
        self.assertEqual(trace.response["status"], "ok")
        self.assertGreater(collector.diagnostics()["received_stdout_bytes"], 2_000_000)
        self.assertTrue(all(len(json.dumps(tool)) < 700 for tool in trace.tools))
        events[-2]["item"]["text"] = json.dumps({"response_json": json.dumps({"payload": "x" * 1_000_000})})
        _, oversized = parse("codex_exec", events)
        self.assertIsNone(oversized.response)
        self.assertEqual(oversized.response_diagnostic["code"], "outer_size_exceeded")
        self.assertEqual(oversized.response_diagnostic["stage"], "outer_json")

    def test_raw_utf8_bytes_are_the_budget_not_ascii_reserialization(self):
        raw = encode(codex_events([mcp_item(0, text="서울😀" * 1000)]))
        collector, trace = parse("codex_exec", raw, max_total_bytes=len(raw), max_line_bytes=max(map(len, raw.splitlines())))
        self.assertTrue(trace.successful_terminal)
        self.assertEqual(collector.diagnostics()["parsed_bytes"], len(raw))
        # The legacy probe parser also budgets original bytes now.
        wide = encode(codex_events([mcp_item(0, text="서울" * 250_000)]))
        self.assertLess(len(wide), 2_000_000)
        self.assertTrue(parse_tool_trace("codex_exec", wide).successful_terminal)

    def test_event_total_and_count_limits_are_exact(self):
        raw = encode(codex_events())
        longest = max(map(len, raw.splitlines()))
        collector, trace = parse("codex_exec", raw, max_total_bytes=len(raw), max_line_bytes=longest, max_events=4)
        self.assertTrue(trace.successful_terminal)
        for limits, expected in (({"max_total_bytes": len(raw) - 1}, "trace_total_limit_exceeded"),
                                 ({"max_line_bytes": longest - 1}, "trace_event_limit_exceeded"),
                                 ({"max_events": 3}, "trace_event_count_exceeded")):
            collector = ProductionToolTraceCollector("codex_exec", **limits)
            with self.subTest(limits=limits), self.assertRaisesRegex(TraceStreamError, expected):
                collector.feed(raw)
            self.assertFalse(collector.partial_trace().successful_terminal)

    def test_utf8_splits_and_last_line_without_newline(self):
        raw = encode(codex_events([mcp_item(0, text="서울😀")])).rstrip(b"\n")
        collector = ProductionToolTraceCollector("codex_exec")
        for byte in raw:
            collector.feed(bytes([byte]))
        self.assertTrue(collector.finish().successful_terminal)
        self.assertEqual(collector.diagnostics()["parsed_bytes"], len(raw))

    def test_invalid_middle_event_preserves_completed_prefix_and_stops(self):
        prefix = encode(codex_events([mcp_item(0)]))
        prefix = prefix[:prefix.index(b'{"type": "item.completed", "item": {"id": "final"')]
        for bad in (b'{"type":"item.completed","type":"error"}\n',
                    b'{"type":"item.completed","x":NaN}\n',
                    b'{"type":"item.completed","x":1e9999}\n',
                    b'{"type":"item.completed","x":"\\ud800"}\n',
                    b'{"type":"item.completed","x":"\xff"}\n'):
            collector = ProductionToolTraceCollector("codex_exec")
            with self.subTest(bad=bad), self.assertRaisesRegex(TraceStreamError, "trace_invalid_event"):
                collector.feed(prefix + bad + encode(codex_events()))
            self.assertEqual(len(collector.partial_trace().tools), 1)
            self.assertEqual(collector.diagnostics()["parsed_bytes"], len(prefix))
            with self.assertRaises(TraceStreamError):
                collector.feed(b"\n")

    def test_invalid_lifecycle_is_rejected_during_feed(self):
        started = {"type": "item.started", "item": mcp_item(0, status="in_progress")}
        for middle in ([started, started],
                       [started, {"type": "turn.completed"}],
                       [started, {"type": "item.completed", "item": {
                           "id": "item_0", "type": "agent_message", "text": "changed item kind"}}],
                       [{"type": "item.completed", "item": mcp_item(0)}] * 2,
                       [{"type": "item.completed", "item": {"id": "x", "type": "future_mutation"}}]):
            collector = ProductionToolTraceCollector("codex_exec")
            with self.subTest(middle=middle), self.assertRaisesRegex(TraceStreamError, "trace_invalid_lifecycle"):
                collector.feed(encode(codex_events()[:2] + middle))
            self.assertFalse(collector.partial_trace().terminal)

    def test_codex_wire_compatibility_remains_narrow_and_observable(self):
        upstream = "exec-11111111-1111-4111-8111-111111111111"
        item = ('{"type":"item.completed","item":{"id":"item_2","type":"web_search","id":"' + upstream + '"}}\n').encode()
        collector, trace = parse("codex_exec", encode(codex_events()[:2]) + item + encode(codex_events()[2:]))
        self.assertEqual(trace.warnings, (WEB_SEARCH_ID_COMPAT_WARNING,))
        self.assertTrue(trace.tools[0]["success"])
        self.assertEqual(trace.tools[0]["details"]["upstream_search_id"], upstream)
        invalid = item.replace(b'"web_search"', b'"mcp_tool_call"')
        with self.assertRaisesRegex(TraceStreamError, "trace_invalid_event"):
            parse("codex_exec", encode(codex_events()[:2]) + invalid)

    def test_truncation_warning_does_not_remove_call_or_invent_complete_source(self):
        _, trace = parse("codex_exec", codex_events([mcp_item(0, text="…375661 chars truncated…")]))
        self.assertTrue(trace.successful_terminal)
        self.assertIn(TRUNCATED_RESPONSE_WARNING, trace.warnings)
        summary = summarize_mcp_tools("codex_exec", trace.tools)[0]
        self.assertTrue(summary["success"])
        self.assertTrue(summary["content_truncated"])
        self.assertGreater(summary["event_bytes"], 0)

    def test_compact_memory_does_not_grow_with_total_response_bytes(self):
        payload = "x" * 1_200_000
        collector = ProductionToolTraceCollector("codex_exec", capture_timing=True)
        collector.feed(encode(codex_events()[:2]))
        tracemalloc.start()
        try:
            for index in range(30):
                raw = encode([{"type": "item.completed", "item": mcp_item(index, text=payload)}])
                for offset in range(0, len(raw), 65_536):
                    collector.feed(raw[offset:offset + 65_536])
                del raw
            collector.feed(encode(codex_events()[2:]))
            collector.finish()
            retained, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertGreater(collector.diagnostics()["received_stdout_bytes"], 36_000_000)
        self.assertLess(retained, 1_000_000)
        self.assertLess(peak, 12_000_000)

    def test_500_public_pending_limit_preserves_all_counts(self):
        collector = ProductionToolTraceCollector("codex_exec")
        collector.feed(encode(codex_events()[:2]))
        for index in range(510):
            collector.feed(encode([{"type": "item.started", "item": mcp_item(index, status="in_progress")}]))
        with self.assertRaisesRegex(TraceStreamError, "trace_missing_terminal"):
            collector.finish()
        self.assertEqual(collector.diagnostics()["pending_tool_count"], 510)
        self.assertEqual(len(collector.diagnostics()["pending_tools"]), 500)


class ProductionToolTimingTests(unittest.TestCase):
    def feed_at(self, collector, events, stamp, nanos):
        with patch("researchops.runners.production_trace._observe_time", return_value=(stamp, nanos)):
            collector.feed(encode(events))

    def test_codex_interleaved_tools_keep_first_start_and_monotonic_duration(self):
        collector = ProductionToolTraceCollector("codex_exec", capture_timing=True)
        collector.feed(encode(codex_events()[:2]))
        start_a = {"type": "item.started", "item": mcp_item(0, status="in_progress")}
        start_b = {"type": "item.started", "item": {"id": "command", "type": "command_execution", "status": "in_progress"}}
        self.feed_at(collector, [start_a], "2026-09-10T08:00:01.000000Z", 1_000_000_000)
        self.feed_at(collector, [start_b], "2026-09-10T08:00:01.500000Z", 1_500_000_000)
        self.feed_at(collector, [{"type": "item.updated", "item": start_a["item"]}],
                     "2026-09-10T08:00:02.000000Z", 2_000_000_000)
        # The wall clock moved backwards; the observed duration is still valid.
        self.feed_at(collector, [{"type": "item.completed", "item": mcp_item(0)}],
                     "2026-09-10T08:00:00.000000Z", 2_234_999_999)
        self.feed_at(collector, [{"type": "item.completed", "item": {
            "id": "command", "type": "command_execution", "status": "completed", "exit_code": 0}}],
                     "2026-09-10T08:00:03.500000Z", 3_500_000_000)
        collector.feed(encode(codex_events()[2:]))
        trace = collector.finish()
        mcp, command = trace.tools
        self.assertEqual(mcp["observed_started_at"], "2026-09-10T08:00:01.000000Z")
        self.assertEqual(mcp["observed_finished_at"], "2026-09-10T08:00:00.000000Z")
        self.assertEqual(mcp["duration_ms"], 1234)
        self.assertEqual(command["duration_ms"], 2000)
        summary = summarize_mcp_tools("codex_exec", trace.tools)[0]
        self.assertEqual(summary["duration_ms"], 1234)
        self.assertTrue(summary["success"])
        self.assertNotIn("monotonic", json.dumps(trace.tools))

    def test_completed_only_and_updated_only_do_not_invent_start(self):
        for provider in ("codex_exec", "antigravity_exec"):
            for updated in (False, True) if provider == "codex_exec" else (False,):
                with self.subTest(provider=provider, updated=updated):
                    collector = ProductionToolTraceCollector(provider, capture_timing=True)
                    initial = codex_events()[:2] if provider == "codex_exec" else agy_events()[:2]
                    collector.feed(encode(initial))
                    if updated:
                        collector.feed(encode([{"type": "item.updated", "item": mcp_item(0, status="in_progress")}]))
                    completed = ({"type": "item.completed", "item": mcp_item(0)} if provider == "codex_exec"
                                 else agy_step(2, "run_command"))
                    self.feed_at(collector, [completed], "2026-09-10T08:00:01.000000Z", 2_000_000_000)
                    tool = collector.partial_trace().tools[0]
                    self.assertIsNone(tool["observed_started_at"])
                    self.assertIsNone(tool["duration_ms"])
                    self.assertEqual(tool["observed_finished_at"], "2026-09-10T08:00:01.000000Z")

    def test_agy_repeated_active_and_late_denial_preserve_observed_interval(self):
        collector = ProductionToolTraceCollector("antigravity_exec", capture_timing=True)
        collector.feed(encode(agy_events()[:2]))
        active = agy_step(2, "call_mcp_tool", state="ACTIVE")
        active["step_update"]["tool_info"]["parameters"] = {"ServerName": "s", "ToolName": "t"}
        self.feed_at(collector, [active], "2026-09-10T08:00:00.000000Z", 0)
        self.feed_at(collector, [active], "2026-09-10T08:00:01.000000Z", 1_000_000_000)
        completed = agy_step(2, "call_mcp_tool")
        completed["step_update"]["duration_seconds"] = 999_999  # Provider duration is not the app clock.
        completed["step_update"]["tool_info"].update(parameters={"ServerName": "s", "ToolName": "t"}, output="synthetic")
        self.feed_at(collector, [completed], "2026-09-10T08:00:02.000000Z", 2_000_999_999)
        terminal = agy_events()[-1]
        terminal["result"]["denied_actions"] = [{"action": "mcp"}]
        collector.feed(encode([terminal]))
        trace = collector.finish()
        summary = summarize_mcp_tools("antigravity_exec", trace.tools)[0]
        self.assertEqual(summary["status"], "denied")
        self.assertEqual(summary["duration_ms"], 2000)
        self.assertEqual(summary["observed_started_at"], "2026-09-10T08:00:00.000000Z")
        self.assertEqual(summary["observed_finished_at"], "2026-09-10T08:00:02.000000Z")
        self.assertFalse(trace.successful_terminal)

    def test_truncated_call_keeps_start_but_never_estimates_finish(self):
        for provider in ("codex_exec", "antigravity_exec"):
            with self.subTest(provider=provider):
                collector = ProductionToolTraceCollector(provider, capture_timing=True)
                initial = codex_events()[:2] if provider == "codex_exec" else agy_events()[:2]
                collector.feed(encode(initial))
                active = ({"type": "item.started", "item": mcp_item(0, status="in_progress")}
                          if provider == "codex_exec" else agy_step(2, "call_mcp_tool", state="ACTIVE"))
                self.feed_at(collector, [active], "2026-09-10T08:00:00.000000Z", 0)
                collector.feed(b'{"truncated":')
                with self.assertRaisesRegex(TraceStreamError, "trace_incomplete_event"):
                    collector.finish()
                diagnostic = collector.diagnostics("timeout")
                pending = diagnostic["pending_tools"][0]
                self.assertEqual(pending["observed_started_at"], "2026-09-10T08:00:00.000000Z")
                self.assertIsNone(pending["observed_finished_at"])
                self.assertIsNone(pending["duration_ms"])
                self.assertEqual(diagnostic["pending_tool_count"], 1)
                self.assertNotIn("monotonic", json.dumps(diagnostic))
                self.assertFalse(collector.partial_trace().successful_terminal)

    def test_split_event_uses_complete_event_receipt_and_not_first_chunk(self):
        collector = ProductionToolTraceCollector("codex_exec", capture_timing=True)
        collector.feed(encode(codex_events()[:2]))
        raw = encode([{"type": "item.started", "item": mcp_item(0, status="in_progress")}])
        with patch("researchops.runners.production_trace._observe_time", side_effect=AssertionError("not a full event")):
            collector.feed(raw[:-1])
        with patch("researchops.runners.production_trace._observe_time", return_value=("2026-09-10T08:00:02Z", 2_000_000_000)):
            collector.feed(raw[-1:])
        self.assertEqual(collector.diagnostics()["pending_tools"][0]["observed_started_at"], "2026-09-10T08:00:02Z")

    def test_archive_reparse_and_default_collector_never_sample_current_time(self):
        raw = encode(codex_events([mcp_item(0)]))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stdout"
            path.write_bytes(raw)
            with patch("researchops.runners.production_trace._observe_time", side_effect=AssertionError("archive time invented")):
                trace, _ = parse_production_trace_file("codex_exec", path)
                _, direct = parse("codex_exec", raw)
        for actual in (trace, direct):
            self.assertTrue(actual.successful_terminal)
            self.assertNotIn("observed_started_at", actual.tools[0])
            self.assertNotIn("duration_ms", summarize_mcp_tools("codex_exec", actual.tools)[0])


class ProductionAgyTraceTests(unittest.TestCase):
    def test_same_conversation_transcript_is_not_authorized_generated_output(self):
        conversation = "11111111-1111-4111-8111-111111111111"
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            transcript = home / ".gemini/antigravity-cli/brain" / conversation / ".system_generated/logs/transcript.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("synthetic protected transcript")
            command = agy_step(2, "run_command")
            command["step_update"]["tool_info"]["output"] = "150"
            view = agy_step(4, "view_file")
            view["step_update"]["tool_info"]["parameters"] = {"AbsolutePath": str(transcript)}
            events = agy_events([command, view])
            events[0]["conversation_id"] = conversation
            _, trace = parse("antigravity_exec", events)
            self.assertTrue(trace.terminal)
            self.assertEqual(trace.tools[-1]["details"]["parameters"]["AbsolutePath"], str(transcript))
            with self.assertRaisesRegex(ValueError, "AGY_MCP_SPILL_PROVENANCE_INVALID"):
                validate_agy_mcp_reads(trace, set(), home_dir=home)

    def test_large_nested_protocol_error_is_not_success(self):
        step = agy_step(2, "call_mcp_tool")
        step["step_update"]["tool_info"].update(parameters={"ServerName": "s", "ToolName": "t"},
            output=json.dumps({"content": [{"type": "text", "text": "x" * 1_133_664}], "isError": True}))
        _, trace = parse("antigravity_exec", agy_events([step]))
        self.assertTrue(trace.terminal)
        self.assertTrue(trace.successful_terminal)
        self.assertTrue(trace.tools[0]["provider_reported_success"])
        self.assertFalse(trace.tools[0]["success"])
        self.assertEqual(summarize_mcp_tools("antigravity_exec", trace.tools)[0]["error_code"], "MCP_PROTOCOL_ERROR")
        self.assertLess(len(json.dumps(trace.tools)), 1000)

    def test_late_denial_changes_earlier_compact_tools(self):
        events = agy_events([agy_step(2, "call_mcp_tool"), agy_step(3, "view_file")])
        events[-1]["result"]["denied_actions"] = [{"action": "mcp"}, {"action": "read_file"}]
        _, trace = parse("antigravity_exec", events)
        self.assertTrue(trace.denied)
        self.assertFalse(trace.successful_terminal)
        self.assertTrue(all(tool["denied"] and not tool["success"] for tool in trace.tools))

    def test_late_denial_and_full_counts_extend_beyond_public_call_limit(self):
        events = agy_events([agy_step(i, "call_mcp_tool") for i in range(510)])
        events[-1]["result"]["denied_actions"] = [{"action": "mcp"}]
        collector, trace = parse("antigravity_exec", events)
        self.assertEqual(len(trace.tools), 510)
        self.assertTrue(all(tool["denied"] for tool in trace.tools))
        self.assertEqual(collector.diagnostics()["mcp_status_counts"], {"denied": 510})

    def test_missing_terminal_does_not_invent_remote_cleanup(self):
        collector = ProductionToolTraceCollector("antigravity_exec")
        collector.feed(encode(agy_events([agy_step(2, "call_mcp_tool")])[:-1]))
        with self.assertRaisesRegex(TraceStreamError, "trace_missing_terminal"):
            collector.finish()
        trace = collector.partial_trace()
        self.assertFalse(trace.terminal)
        self.assertFalse(trace.successful_terminal)
        self.assertTrue(trace.model_activity_observed)
        self.assertEqual(len(trace.tools), 1)

    def test_native_generated_file_provenance_survives_compaction(self):
        conversation = "11111111-1111-4111-8111-111111111111"
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            spill = home / ".gemini/antigravity-cli/brain" / conversation / ".system_generated/steps/2/output.txt"
            spill.parent.mkdir(parents=True)
            spill.write_text("synthetic generated response")
            mcp = agy_step(2, "call_mcp_tool")
            mcp["step_update"]["tool_info"]["parameters"] = {"ServerName": "s", "ToolName": "t"}
            view = agy_step(3, "view_file")
            view["step_update"]["tool_info"]["parameters"] = {"AbsolutePath": str(spill)}
            events = agy_events([mcp, view])
            events[0]["conversation_id"] = conversation
            _, trace = parse("antigravity_exec", events)
            validate_agy_mcp_reads(trace, {"s"}, home_dir=home)
            self.assertTrue(trace.tools[0]["mcp_output_verified"])
            self.assertTrue(summarize_mcp_tools("antigravity_exec", trace.tools)[0]["success"])
            events[-1]["result"]["denied_actions"] = [{"action": "mcp"}]
            _, denied = parse("antigravity_exec", events)
            with self.assertRaisesRegex(ValueError, "AGY_MCP_SPILL_PROVENANCE_INVALID"):
                validate_agy_mcp_reads(denied, {"s"}, home_dir=home)


@unittest.skipUnless(os.name == "posix", "POSIX capture")
class ProductionFileCaptureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def run_source(self, source, **options):
        defaults = {"timeout_seconds": 3, "stdout_path": self.root / "stdout", "stderr_path": self.root / "stderr"}
        defaults.update(options)
        return run_bounded([sys.executable, "-I", "-c", source], cwd=self.root, env={}, **defaults)

    def test_large_successful_process_stream_is_parsed_once_and_preserved(self):
        raw = encode(codex_events([mcp_item(i, text="x" * 1_133_664) for i in range(3)]))
        collector = ProductionToolTraceCollector("codex_exec")
        result = self.run_source("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())", stdin=raw,
            max_output_bytes=64 * 1024 * 1024, stdout_consumer=collector.feed)
        self.assertIsNone(result.error)
        self.assertTrue(result.cleanup_verified)
        self.assertTrue(collector.finish().successful_terminal)
        self.assertEqual(collector.diagnostics()["completed_mcp_count"], 3)
        self.assertEqual(result.stdout_bytes, len(raw))
        self.assertEqual((self.root / "stdout").read_bytes(), raw)
        self.assertEqual(result.stdout, raw[:65_536])

    def test_process_limit_still_preserves_fifteen_completed_calls(self):
        prefix = codex_events([mcp_item(i, text="x" * 1_133_664 if i == 14 else "synthetic") for i in range(15)])[:-2]
        prefix += [{"type": "item.started", "item": mcp_item(15, status="in_progress")},
                   {"type": "item.completed", "item": mcp_item(15, text="y" * 1_133_664)}]
        raw = encode(prefix + codex_events()[2:])
        collector = ProductionToolTraceCollector("codex_exec")
        result = self.run_source("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())", stdin=raw,
            max_output_bytes=2_000_000, stdout_consumer=collector.feed)
        self.assertEqual(result.error, "output_limit_exceeded")
        self.assertTrue(result.cleanup_verified)
        with self.assertRaisesRegex(TraceStreamError, "trace_incomplete_event"):
            collector.finish()
        self.assertEqual(collector.diagnostics(result.error)["completed_mcp_count"], 15)
        self.assertEqual(collector.diagnostics()["pending_tool_count"], 1)
        self.assertEqual((self.root / "stdout").read_bytes(), raw[:2_000_000])

    def test_timeout_and_cancellation_keep_complete_event_evidence(self):
        raw = encode(codex_events([mcp_item(0)]))
        prefix = raw[:raw.index(b'{"type": "item.completed", "item": {"id": "final"')]
        source = "import sys,time; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data); sys.stdout.buffer.flush(); time.sleep(60)"
        for mode in ("timeout", "cancelled"):
            collector = ProductionToolTraceCollector("codex_exec")
            options = {"timeout_seconds": 1} if mode == "timeout" else {
                "cancellation_check": lambda: len(collector.partial_trace().tools) == 1}
            with self.subTest(mode=mode):
                result = self.run_source(source, stdin=prefix, stdout_consumer=collector.feed,
                    stdout_path=self.root / (mode + ".stdout"), stderr_path=self.root / (mode + ".stderr"), **options)
                self.assertEqual(result.error, mode)
                self.assertTrue(result.cleanup_verified)
                with self.assertRaisesRegex(TraceStreamError, "trace_missing_terminal"):
                    collector.finish()
                self.assertEqual(collector.diagnostics(result.error)["completed_mcp_count"], 1)
                self.assertFalse(collector.partial_trace().terminal)
                self.assertEqual(result.stdout_path.read_bytes(), prefix)

    def test_raw_files_hashes_and_bounded_previews(self):
        result = self.run_source("import os; os.write(1, '서울😀'.encode()*150000); os.write(2, b'e'*120000)",
                                 max_output_bytes=4_000_000)
        self.assertIsNone(result.error)
        self.assertTrue(result.cleanup_verified)
        for name, expected in (("stdout", "서울😀".encode() * 150_000), ("stderr", b"e" * 120_000)):
            self.assertEqual(len(getattr(result, name)), 65_536)
            self.assertEqual(getattr(result, name + "_bytes"), len(expected))
            self.assertEqual(getattr(result, name + "_path"), self.root / name)
            with (self.root / name).open("rb") as stream:
                actual_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            self.assertEqual(actual_hash, hashlib.sha256(expected).hexdigest())
            self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o600)

    def test_original_utf8_bytes_survive_combined_limit(self):
        result = self.run_source("import os; os.write(1, '서울😀'.encode())", max_output_bytes=8)
        self.assertEqual(result.error, "output_limit_exceeded")
        self.assertEqual((self.root / "stdout").read_bytes(), "서울😀".encode()[:8])
        self.assertEqual(result.stdout_bytes, 8)
        self.assertTrue(result.cleanup_verified)

    def test_capture_does_not_retry_failed_consumer_while_draining(self):
        calls = []

        def fail(chunk):
            calls.append(chunk)
            raise TraceStreamError("trace_invalid_event")

        result = self.run_source("import os;\nwhile True: os.write(1, b'x'*65536)", stdout_consumer=fail)
        self.assertEqual(result.error, "trace_invalid_event")
        self.assertEqual(len(calls), 1)
        self.assertTrue(result.cleanup_verified)
        self.assertTrue(result.cleanup_evidence["captured_pipes_eof"])

    def test_log_write_failure_preserves_first_error_and_drains(self):
        import researchops.runners.development_process as process_module
        original = process_module.os.fdopen
        attempts = []

        class FailedLog:
            def __init__(self, stream):
                self.stream = stream

            def write(self, _):
                attempts.append(True)
                raise OSError("private disk details")

            def close(self):
                return self.stream.close()

            def fileno(self):
                return self.stream.fileno()

        def fdopen(*args, **kwargs):
            return FailedLog(original(*args, **kwargs))

        with patch.object(process_module.os, "fdopen", side_effect=fdopen):
            result = self.run_source("import os;\nwhile True: os.write(1, b'x'*65536)")
        self.assertEqual(result.error, "log_write_failed: OSError")
        self.assertEqual(attempts, [True])
        self.assertTrue(result.cleanup_verified)
        self.assertEqual(result.stdout_bytes, 0)

    def test_log_sync_failure_retains_raw_capture_and_cleanup_result(self):
        with patch("researchops.runners.development_process.os.fsync", side_effect=OSError("private disk details")):
            result = self.run_source("import os; os.write(1,b'captured')")
        self.assertEqual(result.error, "log_write_failed: OSError")
        self.assertTrue(result.cleanup_verified)
        self.assertEqual(result.stdout_bytes, 8)
        self.assertEqual(result.stdout_path.read_bytes(), b"captured")

    def test_existing_file_and_symlink_are_never_overwritten(self):
        target = self.root / "protected"
        target.write_bytes(b"keep")
        (self.root / "stdout").symlink_to(target)
        result = self.run_source("raise AssertionError")
        self.assertFalse(result.spawned)
        self.assertEqual(result.error, "log_open_failed: FileExistsError")
        self.assertEqual(target.read_bytes(), b"keep")

    def test_read_only_file_diagnosis_leaves_bytes_unchanged(self):
        original = encode(codex_events([mcp_item(0)]))[:-20]
        path = self.root / "original.stdout"
        path.write_bytes(original)
        trace, diagnostics = parse_production_trace_file("codex_exec", path)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(trace.successful_terminal)
        self.assertFalse(diagnostics["complete"])
        self.assertEqual(len(trace.tools), 1)


if __name__ == "__main__":
    unittest.main()
