"""SMTP doubles exercise durable run timing without connecting to a server."""

import json
import smtplib
import unittest
from unittest.mock import patch

from researchops.errors import DeliveryError
from tests.delivery_fixtures import DeliveryFixture, smtp_server


class DeliveryTimelineTests(DeliveryFixture, unittest.TestCase):
    def events(self):
        conn = self.db.get_connection()
        try:
            return [dict(row) | {"details": json.loads(row["details_json"])} for row in
                    conn.execute("SELECT * FROM audit_events WHERE entity_type='run' AND entity_id=? ORDER BY event_id",
                                 (self.run.run_id,))]
        finally:
            conn.close()

    def server(self):
        server = smtp_server()
        return server

    def test_success_records_queue_start_data_finish_once_and_no_message_content(self):
        handoff = self.publish()
        times = ["2026-09-10T00:00:00+00:00", "2026-09-10T00:00:03+00:00",
                 "2026-09-10T00:00:05+00:00", "2026-09-10T00:00:06.250000+00:00"]
        clock = [times[0]]
        queue = self.dispatcher.queue
        claim, start_data, finish = queue.claim, queue.start_data, queue.finish

        def at_claim(*args, **kwargs):
            clock[0] = times[1]
            return claim(*args, **kwargs)

        def at_data(*args, **kwargs):
            clock[0] = times[2]
            return start_data(*args, **kwargs)

        def at_finish(*args, **kwargs):
            clock[0] = times[3]
            return finish(*args, **kwargs)

        # Progress diagnostics may sample time too. Bind timestamps to durable
        # transitions rather than the number of internal clock reads.
        with patch("researchops.delivery.queue._now", side_effect=lambda: clock[0]), \
                patch.object(queue, "claim", side_effect=at_claim), \
                patch.object(queue, "start_data", side_effect=at_data), \
                patch.object(queue, "finish", side_effect=at_finish), \
                patch("smtplib.SMTP", return_value=self.server()):
            ok, receipt, _ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok)
        self.assertEqual(receipt.status, "smtp_accepted")
        events = self.events()
        self.assertEqual([e["event_type"] for e in events],
                         ["run_delivery_queued", "run_step_started", "run_delivery_data_started", "run_step_finished"])
        final = events[-1]["details"]
        self.assertEqual(final["phase"], "smtp")
        self.assertEqual(final["state"], "succeeded")
        self.assertEqual(final["started_at"], times[1])
        self.assertEqual(final["finished_at"], times[-1])
        self.assertEqual(final["duration_ms"], 3250)
        self.assertGreater(final["summary"]["mime_bytes"], 0)
        raw = json.dumps(events, ensure_ascii=False)
        for secret in ("first@example.test", "second@example.test", "sender@example.test", "Seoul digest", "서울 보고서"):
            self.assertNotIn(secret, raw)
        with patch("smtplib.SMTP") as connect:
            self.assertFalse(self.dispatcher.dispatch_handoff(handoff.handoff_id)[0])
            connect.assert_not_called()
        self.assertEqual(len(self.events()), 4)

    def test_post_data_disconnect_finishes_attention_not_success(self):
        handoff = self.publish()
        server = self.server()
        server.getreply.side_effect = [(354, b"send body"), smtplib.SMTPServerDisconnected("sensitive reply must stay out of timeline")]
        with patch("smtplib.SMTP", return_value=server):
            ok, receipt, _ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertFalse(ok)
        self.assertEqual(receipt.status, "uncertain")
        final = self.events()[-1]
        self.assertEqual(final["details"]["state"], "needs_attention")
        self.assertNotIn("sensitive reply", json.dumps(self.events()))
        self.assertEqual(self.dispatcher.queue.pending(), [])

    def test_cancel_queued_does_not_fabricate_dispatch_start(self):
        handoff = self.publish()
        self.dispatcher.enqueue_handoff(handoff.handoff_id)
        with self.db.transaction() as conn:
            conn.execute("UPDATE execution_controls SET cancel_requested=1 WHERE run_id=?", (self.run.run_id,))
        self.assertTrue(self.dispatcher.queue.cancel_pending_for_run(self.run.run_id))
        final = self.events()[-1]["details"]
        self.assertEqual(final["state"], "cancelled")
        self.assertIsNone(final["started_at"])
        self.assertIsNone(final["duration_ms"])
        self.assertFalse(any(e["event_type"] == "run_step_started" for e in self.events()))

    def test_lost_ownership_does_not_append_false_completion(self):
        handoff = self.publish()
        self.dispatcher.enqueue_handoff(handoff.handoff_id)
        self.dispatcher.queue.claim(handoff.handoff_id)
        before = self.events()
        with self.assertRaises(DeliveryError):
            self.dispatcher.queue.finish(handoff.handoff_id, "not-the-owner", "failed")
        with self.assertRaises(DeliveryError):
            self.dispatcher.queue.start_data(handoff.handoff_id, "not-the-owner")
        self.assertEqual(self.events(), before)

    def test_pre_upgrade_dispatch_has_unknown_start_and_duration(self):
        handoff = self.publish()
        self.dispatcher.enqueue_handoff(handoff.handoff_id)
        token = self.dispatcher.queue.claim(handoff.handoff_id)
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM audit_events WHERE entity_type='run' AND entity_id=?", (self.run.run_id,))
        self.dispatcher.queue.finish(handoff.handoff_id, token, "failed")
        final = self.events()[-1]["details"]
        self.assertIsNone(final["started_at"])
        self.assertIsNone(final["duration_ms"])
        self.assertEqual(final["state"], "failed")

    def test_clock_change_keeps_observed_completion_with_unknown_duration(self):
        from researchops.engine.run_timeline import validate_step_event
        handoff = self.publish()
        self.dispatcher.enqueue_handoff(handoff.handoff_id)
        with patch("researchops.delivery.queue._now", return_value="2026-09-10T00:01:00+00:00"):
            token = self.dispatcher.queue.claim(handoff.handoff_id)
        with patch("researchops.delivery.queue._now", return_value="2026-09-10T00:00:00+00:00"):
            self.dispatcher.queue.finish(handoff.handoff_id, token, "failed")
        final = self.events()[-1]["details"]
        self.assertEqual(final["started_at"], "2026-09-10T00:01:00+00:00")
        self.assertEqual(final["finished_at"], "2026-09-10T00:00:00+00:00")
        self.assertIsNone(final["duration_ms"])
        validate_step_event("run_step_finished", final)


if __name__ == "__main__":
    unittest.main()
