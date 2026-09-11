"""Adversarial SMTP tests using temporary runtime and fake transport only."""

from dataclasses import replace
from email import policy
from email.parser import BytesParser
import hashlib
import json
import os
import smtplib
import unittest
from unittest.mock import patch

from researchops.delivery.smtp_config import load_delivery_config, save_delivery_config, BuiltinDeliveryConfig
from researchops.delivery.system_alert import SystemAlertManager
from researchops.errors import DeliveryError
from tests.delivery_fixtures import DeliveryFixture, smtp_server, smtp_message_bytes


class TestSmtpDelivery(DeliveryFixture, unittest.TestCase):
    def server(self):
        server = smtp_server()
        return server

    def test_config_permissions_redaction_strict_types_and_revision(self):
        self.config.smtp.password = "example-test-password"
        save_delivery_config(self.config,self.settings.paths.delivery_config_file)
        self.assertEqual(self.settings.paths.delivery_config_file.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.settings.paths.delivery_config_file.parent.stat().st_mode & 0o777,0o700)
        loaded = load_delivery_config(self.settings.paths.delivery_config_file)
        self.assertEqual(loaded.smtp.password,"example-test-password")
        self.assertEqual(loaded.smtp.masked_password(),"****")
        self.assertEqual(loaded.to_dict()["smtp"]["password"],"")
        self.assertNotIn("example-test-password",repr(loaded))
        self.assertFalse(BuiltinDeliveryConfig().enabled)
        loaded.enabled = "false"
        with self.assertRaises(DeliveryError):
            save_delivery_config(loaded,self.settings.paths.delivery_config_file)
        os.chmod(self.settings.paths.delivery_config_file,0o644)
        with self.assertRaises(DeliveryError):
            load_delivery_config(self.settings.paths.delivery_config_file)

    def test_tls_configuration_cannot_enable_plaintext_or_both_modes(self):
        for tls, implicit in ((False,False),(True,True)):
            config = replace(self.config.smtp,use_tls=tls,use_ssl=implicit)
            with self.assertRaises(DeliveryError):
                config.validate()

    def test_all_rcpt_before_data_original_mime_bytes_stable_id_and_atomic_success(self):
        handoff = self.publish()
        server = self.server()
        with patch("smtplib.SMTP",return_value=server) as connect:
            ok,receipt,message = self.dispatcher.dispatch_handoff(handoff.handoff_id)
            self.assertTrue(ok,message)
            self.assertEqual(receipt.status,"smtp_accepted")
            self.assertIn("SMTP 서버 수락",message)
            server.sendmail.assert_not_called()
            server.rcpt.assert_any_call("first@example.test")
            server.rcpt.assert_any_call("second@example.test")
            server.send.assert_called_once()
            snapshot = self.dispatcher.queue.get(handoff.handoff_id)
            self.assertEqual(smtp_message_bytes(server), snapshot["mime_bytes"])
            server.putcmd.assert_called_once_with("data")
            commands = [call[0] for call in server.method_calls]
            self.assertLess(max(i for i, name in enumerate(commands) if name == "rcpt"),
                            commands.index("putcmd"))
            self.assertLess(commands.index("putcmd"), commands.index("send"))
            parsed = BytesParser(policy=policy.default).parsebytes(snapshot["mime_bytes"])
            body_parts = {p.get_content_type():p.get_payload(decode=True) for p in parsed.walk() if not p.is_multipart()}
            self.assertEqual(body_parts["text/html"],self.html)
            self.assertEqual(body_parts["text/plain"],self.plain)
            self.assertIn("+0900",str(parsed["Date"]))
            self.assertEqual(self.run_repo.get_run(self.run.run_id).status,"succeeded")
            self.assertEqual(len(self.state_repo.get_reported_items(self.task.id)),1)
            self.assertEqual(self.delivery_repo.get_handoff(handoff.handoff_id).receipt_trust_status,"verified_local_smtp")
            evidence = self.settings.paths.receipts_dir/handoff.handoff_id
            self.assertTrue((evidence/"attempt-manifest.json").exists())
            self.assertEqual(json.loads((evidence/"receipt.json").read_text())["status"],"smtp_accepted")
            self.assertNotIn("first@example.test",(evidence/"attempt.json").read_text())
            self.assertTrue(self.dispatcher.archive_attempt(handoff.handoff_id))
            ok,_,_ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
            self.assertFalse(ok)
            connect.assert_called_once()

    def test_partial_rcpt_rejection_sends_no_data_and_never_reports_success(self):
        handoff = self.publish()
        server = self.server()
        server.rcpt.side_effect = [(250,b"ok"),(550,b"not accepted")]
        with patch("smtplib.SMTP",return_value=server):
            ok,receipt,_ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertFalse(ok)
        self.assertEqual(receipt.status,"failed")
        server.send.assert_not_called()
        server.rset.assert_called_once()
        self.assertEqual(self.state_repo.get_reported_items(self.task.id),[])

    def test_post_data_disconnect_is_uncertain_and_never_retried(self):
        handoff = self.publish()
        server = self.server()
        server.getreply.side_effect = [(354, b"send body"), smtplib.SMTPServerDisconnected("Lost reply")]
        with patch("smtplib.SMTP",return_value=server) as connect:
            ok,receipt,_ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
            self.assertFalse(ok)
            self.assertEqual(receipt.status,"uncertain")
            self.assertEqual(self.run_repo.get_run(self.run.run_id).status,"needs_attention")
            self.assertEqual(self.dispatcher.dispatch_all_pending(),[])
            self.dispatcher.dispatch_handoff(handoff.handoff_id)
            connect.assert_called_once()
        with self.assertRaises(DeliveryError):
            self.publisher.republish_handoff(handoff.handoff_id)

    def test_explicit_data_rejection_is_failed_not_uncertain(self):
        handoff = self.publish()
        server = self.server()
        server.getreply.side_effect = [(354, b"send body"), (550, b"reject")]
        with patch("smtplib.SMTP",return_value=server):
            ok,receipt,_ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertFalse(ok)
        self.assertEqual(receipt.status,"failed")

    def test_kill_switch_and_config_or_approval_change_block_network(self):
        handoff = self.publish()
        self.dispatcher.enqueue_handoff(handoff.handoff_id)
        with patch("smtplib.SMTP") as connect:
            self.settings.delivery.global_handoff_kill_switch = True
            self.assertFalse(self.dispatcher.dispatch_handoff(handoff.handoff_id)[0])
            self.settings.delivery.global_handoff_kill_switch = False
            self.config.recipient_groups["test-team"] = ["changed@example.test"]
            save_delivery_config(self.config,self.settings.paths.delivery_config_file)
            self.assertFalse(self.dispatcher.dispatch_handoff(handoff.handoff_id)[0])
            connect.assert_not_called()

    def test_tampered_package_or_symlink_is_blocked_before_network(self):
        handoff = self.publish()
        package = self.settings.paths.delivery_outbox_dir/handoff.handoff_id
        (package/"email.html").write_bytes(b"changed")
        with patch("smtplib.SMTP") as connect:
            self.assertFalse(self.dispatcher.dispatch_handoff(handoff.handoff_id)[0])
            (package/"email.html").unlink()
            (package/"email.html").symlink_to(self.stage/"email.html")
            self.assertFalse(self.dispatcher.dispatch_handoff(handoff.handoff_id)[0])
            connect.assert_not_called()

    def test_running_or_missing_archive_handoff_is_not_ready(self):
        handoff = self.publish()
        manifest = self.settings.paths.run_archive_dir/self.task.id/self.run.run_id/"run-manifest.json"
        manifest.unlink()
        with patch("smtplib.SMTP") as connect:
            self.assertFalse(self.dispatcher.dispatch_handoff(handoff.handoff_id)[0])
            connect.assert_not_called()
        self.assertIsNone(self.dispatcher.queue.get(handoff.handoff_id))

    def test_test_email_queues_same_transport_and_obeys_global_switch(self):
        with patch("smtplib.SMTP",return_value=self.server()) as connect:
            ok,message = self.dispatcher.send_test_email("explicit@example.test")
            self.assertTrue(ok,message)
            connect.assert_not_called()
            self.settings.delivery.global_handoff_kill_switch = True
            self.assertFalse(self.dispatcher.send_test_email("explicit@example.test")[0])
            results = self.dispatcher.dispatch_all_pending()
            self.assertTrue(all(not r[1] for r in results))
            connect.assert_not_called()
            self.settings.delivery.global_handoff_kill_switch = False
            results = self.dispatcher.dispatch_all_pending()
            self.assertTrue(results[0][1],results)
            connect.assert_called_once()

    def test_queue_single_claim_and_recovery_never_resend(self):
        handoff = self.publish()
        self.dispatcher.enqueue_handoff(handoff.handoff_id)
        token = self.dispatcher.queue.claim(handoff.handoff_id)
        self.assertTrue(token)
        self.assertIsNone(self.dispatcher.queue.claim(handoff.handoff_id))
        self.dispatcher.queue.start_data(handoff.handoff_id,token)
        with patch("os.kill",side_effect=ProcessLookupError):
            recovered = self.dispatcher.queue.recover_interrupted()
        self.assertEqual(recovered,[handoff.handoff_id])
        self.assertEqual(self.dispatcher.queue.get(handoff.handoff_id)["status"],"uncertain")
        self.assertEqual(self.dispatcher.queue.pending(),[])

    def test_connection_diagnostic_is_queued_and_does_not_send_mail(self):
        self.settings.delivery.global_handoff_kill_switch = True
        server = self.server()
        server.noop.return_value = (250,b"ok")
        with patch("smtplib.SMTP",return_value=server) as connect:
            ok,message = self.dispatcher.test_connection()
            self.assertTrue(ok,message)
            connect.assert_not_called()
            jobs = self.dispatcher.queue.pending()
            self.assertEqual(len(jobs),1)
            results = self.dispatcher.dispatch_all_pending()
            self.assertTrue(results[0][1],results)
            self.assertEqual(self.dispatcher.queue.get(jobs[0]["job_id"])["status"],"connection_ok")
            server.mail.assert_not_called()
            server.send.assert_not_called()

    def test_approval_revoked_during_recipient_exchange_blocks_data(self):
        handoff = self.publish()
        server = self.server()
        def recipient(_):
            self.task_repo.set_delivery_approved(self.task.id,False)
            return 250,b"ok"
        server.rcpt.side_effect = recipient
        with patch("smtplib.SMTP",return_value=server):
            ok,receipt,_ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertFalse(ok)
        self.assertEqual(receipt.status,"failed")
        server.send.assert_not_called()

    def test_archive_identity_or_request_hash_change_blocks_smtp(self):
        handoff = self.publish()
        archive = self.settings.paths.run_archive_dir/self.task.id/self.run.run_id
        manifest = json.loads((archive/"run-manifest.json").read_text())
        manifest["task_id"] = "different-task"
        (archive/"run-manifest.json").write_text(json.dumps(manifest))
        with patch("smtplib.SMTP") as connect:
            self.assertFalse(self.dispatcher.dispatch_handoff(handoff.handoff_id)[0])
            manifest["task_id"] = self.task.id
            (archive/"run-manifest.json").write_text(json.dumps(manifest))
            (archive/"delivery-request.json").write_text("{}")
            self.assertFalse(self.dispatcher.dispatch_handoff(handoff.handoff_id)[0])
            connect.assert_not_called()

    def test_queued_cancellation_is_terminal_without_smtp(self):
        handoff = self.publish()
        self.dispatcher.enqueue_handoff(handoff.handoff_id)
        self.run_repo.request_cancel(self.run.run_id)
        self.assertTrue(self.dispatcher.queue.cancel_pending_for_run(self.run.run_id))
        with patch("smtplib.SMTP") as connect:
            self.assertEqual(self.dispatcher.dispatch_all_pending(),[])
            connect.assert_not_called()
        self.assertEqual(self.run_repo.get_run(self.run.run_id).status,"cancelled")
        self.assertEqual(self.dispatcher.queue.get(handoff.handoff_id)["status"],"failed")

    def test_cancel_race_at_data_boundary_is_checked_transactionally(self):
        handoff = self.publish()
        server = self.server()
        original = self.dispatcher.queue.start_data
        def race(job_id,token):
            self.run_repo.request_cancel(self.run.run_id)
            return original(job_id,token)
        with patch("smtplib.SMTP",return_value=server),patch.object(self.dispatcher.queue,"start_data",side_effect=race):
            ok,receipt,_ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertFalse(ok)
        server.send.assert_not_called()
        self.assertEqual(self.run_repo.get_run(self.run.run_id).status,"cancelled")

    def test_cancel_after_data_started_preserves_actual_smtp_acceptance(self):
        handoff = self.publish()
        server = self.server()
        def after_data(_):
            self.run_repo.request_cancel(self.run.run_id)
            self.assertFalse(self.dispatcher.queue.cancel_pending_for_run(self.run.run_id))
        server.send.side_effect = after_data
        with patch("smtplib.SMTP",return_value=server):
            ok,receipt,_ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok)
        self.assertEqual(receipt.status,"smtp_accepted")
        self.assertEqual(self.run_repo.get_run(self.run.run_id).status,"succeeded")

    def test_malformed_final_reply_cannot_establish_acceptance(self):
        handoff = self.publish()
        server = self.server()
        server.getreply.side_effect = [(354, b"send body"), (250.0, b"invalid numeric type")]
        with patch("smtplib.SMTP",return_value=server):
            ok,receipt,_ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertFalse(ok)
        self.assertEqual(receipt.status,"uncertain")
        self.assertEqual(self.state_repo.get_reported_items(self.task.id),[])

    def test_reconciliation_failure_after_smtp_acceptance_never_resends(self):
        handoff = self.publish()
        server = self.server()
        with patch("smtplib.SMTP",return_value=server) as connect,patch.object(self.dispatcher.queue,"_commit_reported",side_effect=RuntimeError("injected DB evidence failure")):
            ok,receipt,_ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
            self.assertFalse(ok)
            self.assertEqual(receipt.status,"uncertain")
            self.assertEqual(self.dispatcher.dispatch_all_pending(),[])
            connect.assert_called_once()
        self.assertEqual(self.state_repo.get_reported_items(self.task.id),[])

    def test_dead_dispatcher_before_data_is_failed_without_retry(self):
        handoff = self.publish()
        self.dispatcher.enqueue_handoff(handoff.handoff_id)
        self.assertTrue(self.dispatcher.queue.claim(handoff.handoff_id))
        with patch("os.kill",side_effect=ProcessLookupError):
            self.dispatcher.queue.recover_interrupted()
        self.assertEqual(self.dispatcher.queue.get(handoff.handoff_id)["status"],"failed")
        self.assertEqual(self.dispatcher.queue.pending(),[])

    def test_encoded_mime_size_limit_is_enforced_before_network(self):
        handoff = self.publish()
        self.settings.delivery.max_message_bytes = 100
        with patch("smtplib.SMTP") as connect:
            self.assertFalse(self.dispatcher.dispatch_handoff(handoff.handoff_id)[0])
            connect.assert_not_called()

    def test_alert_escapes_reason_and_obeys_same_live_gate(self):
        manager = SystemAlertManager(self.settings,self.delivery_repo,self.state_repo)
        handoff = manager.emit_alert(self.task,self.run,"failed","<img src=x onerror=alert(1)>")
        self.assertIsNotNone(handoff)
        html = (self.settings.paths.delivery_outbox_dir/handoff.handoff_id/"alert.html").read_text()
        self.assertIn("&lt;img",html)
        self.assertNotIn("<img",html)
        self.settings.delivery.global_handoff_kill_switch = True
        different_run = replace(self.run,run_id="another-run")
        self.assertIsNone(manager.emit_alert(self.task,different_run,"failed","oops"))

    def test_mime_boundary_rfc2046_compliance_and_no_rfc2231_folding(self):
        handoff = self.publish()
        self.dispatcher.enqueue_handoff(handoff.handoff_id)
        snapshot = self.dispatcher.queue.get(handoff.handoff_id)
        raw_mime = snapshot["mime_bytes"]

        self.assertNotIn(b"boundary*0*", raw_mime)
        self.assertNotIn(b"boundary*1*", raw_mime)
        self.assertIn(b'Content-Type: multipart/mixed; boundary="ro-mix-', raw_mime)
        self.assertIn(b'Content-Type: multipart/related; boundary="ro-rel-', raw_mime)
        self.assertIn(b'Content-Type: multipart/alternative; boundary="ro-alt-', raw_mime)

        parsed = BytesParser(policy=policy.default).parsebytes(raw_mime)
        for part in parsed.walk():
            if part.is_multipart():
                b = part.get_boundary()
                self.assertIsNotNone(b)
                self.assertLessEqual(len(b), 70, f"Boundary exceeds RFC 2046 70-char limit: {b}")
                self.assertLessEqual(len(b), 30, f"Boundary unexpectedly long: {b}")

        config = self.dispatcher.get_config()
        sender_profile_id = "default"
        smtp = config.get_sender(sender_profile_id)
        recipients = config.recipient_groups[handoff.recipient_group_id]
        request, files = self.dispatcher._check_handoff(handoff, config)
        recreated = self.dispatcher._mime(request, files, smtp, recipients, snapshot["message_id"])
        self.assertEqual(recreated, raw_mime)


if __name__ == "__main__":
    unittest.main()
