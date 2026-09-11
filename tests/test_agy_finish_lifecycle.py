"""Synthetic forms of the four September 11 failures; no model or network."""
import unittest
from researchops.runners.production_trace import ProductionToolTraceCollector
from researchops.runners.tool_events import parse_tool_trace
from tests.test_runner_tool_events import agy_events, agy_step, encode


def finish(index=112, **changes):
    return {'event':'step_update','step_update':{'step_type':'finish','state':'DONE','step_index':index,**changes}}


class FinishLifecycleTests(unittest.TestCase):
    def both(self, events):
        raw=encode(events)
        full=parse_tool_trace('antigravity_exec',raw)
        stream=ProductionToolTraceCollector('antigravity_exec')
        for offset in range(0,len(raw),17): stream.feed(raw[offset:offset+17])
        return full,stream.finish(),stream

    def test_all_observed_finish_indices_complete_without_tool_success_evidence(self):
        for index in (96,112,229):
            full,stream,collector=self.both(agy_events([agy_step(index,'finish',state='ACTIVE'),finish(index)]))
            self.assertTrue(full.successful_terminal)
            self.assertTrue(stream.successful_terminal)
            self.assertEqual(collector.diagnostics()['pending_tool_count'],0)
            self.assertEqual(full.tools,())
            self.assertEqual(stream.tools,())

    def test_standalone_finish_preserves_previous_success_shape(self):
        for index in (None,140):
            event=finish(index)
            if index is None: del event['step_update']['step_index']
            self.assertTrue(self.both(agy_events([event]))[0].successful_terminal)

    def test_bad_pairs_cannot_hide_actual_tool_or_pending_finish(self):
        cases=[ [agy_step(112,'finish',state='ACTIVE'),finish(113)],
                [agy_step(112,'run_command',state='ACTIVE'),finish()],
                [agy_step(112,'call_mcp_tool',state='ACTIVE'),finish()],
                [agy_step(112,'finish',state='ACTIVE'),finish(state='ACTIVE')],
                [agy_step(112,'finish',state='ACTIVE'),finish(),finish()],
                [finish(-1)], [finish(True)], [finish(tool_name='finish')]]
        for steps in cases:
            raw=encode(agy_events(steps))
            with self.subTest(steps=steps):
                with self.assertRaises(ValueError):parse_tool_trace('antigravity_exec',raw)
                collector=ProductionToolTraceCollector('antigravity_exec')
                with self.assertRaises(ValueError):collector.feed(raw);collector.finish()
                self.assertIsNotNone(collector.diagnostics()['lifecycle_error_code'])

    def test_finish_does_not_override_error_or_missing_terminal(self):
        for status in ('ERROR','CANCELED','TIMEOUT'):
            full,stream,_=self.both(agy_events([agy_step(112,'finish',state='ACTIVE'),finish()],status=status))
            self.assertFalse(full.successful_terminal)
            self.assertFalse(stream.successful_terminal)
        collector=ProductionToolTraceCollector('antigravity_exec')
        collector.feed(encode(agy_events([agy_step(112,'finish',state='ACTIVE'),finish()])[:-1]))
        with self.assertRaises(ValueError):collector.finish()

    def test_other_pending_tool_and_changed_conversation_still_fail(self):
        for steps in ([agy_step(1,'run_command',state='ACTIVE'),agy_step(112,'finish',state='ACTIVE'),finish()],
                      [agy_step(112,'finish',state='ACTIVE'),finish(conversation_id='other')]):
            with self.assertRaises(ValueError): self.both(agy_events(steps))
