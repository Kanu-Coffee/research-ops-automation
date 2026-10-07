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

    def test_recipient_visibility_bcc_hides_addresses_in_headers_and_sends_to_all_recipients(self):
        from researchops.delivery.policy import recipient_visibility
        from researchops.domain.models import TaskDefinition, TaskVersion
        from researchops.delivery.smtp_config import delivery_revision

        task_bcc = TaskDefinition(version=2, id="market-digest-bcc", name="Market Digest BCC",
            enabled=True, schedule={"cron": "0 9 * * *"},
            workspace={"mode": "persistent_task"}, runner={"type": "fake"}, instructions={}, output={},
            delivery={"mode": "handoff", "allowed_recipient_group_ids": ["test-team"],
                      "recipient_visibility": "bcc"},
            state={"dedupe": {"enabled": False}},
            alerting={"events": ["failed"], "recipient_group_id": "researchops-admins"})
        version_bcc = TaskVersion(task_id=task_bcc.id, version_hash="c" * 64, sealed_at="2026-09-04T00:00:00Z",
            definition=task_bcc, package_files={})
        self.task_repo.save_version(version_bcc)
        self.task_repo.set_active_version(task_bcc.id, version_bcc.version_hash)
        from researchops.domain.models import ScheduledRun
        dry = ScheduledRun(run_id="dry-bcc", task_id=task_bcc.id, task_version_hash=version_bcc.version_hash,
            scheduled_for="2026-09-04T15:30:00Z", timezone="Asia/Seoul", local_date="2026-09-05",
            local_date_display="2026.09.05", trigger_type="candidate_dry_run", status="succeeded", phase="finalize", attempt=1)
        self.run_repo.create_run(dry)
        dry_input = self.make_input(dry)
        dry_input = replace(dry_input, task_id=task_bcc.id, task_version_hash=version_bcc.version_hash)
        self.publisher.create_and_publish_handoff(task_bcc, dry, dry_input, self.result, self.stage,
            self.root / "unused", self.hashes, force_mode="dry_run")
        self.task_repo.set_delivery_approved(task_bcc.id, True, delivery_revision(self.config))

        run = ScheduledRun(run_id="live-bcc", task_id=task_bcc.id, task_version_hash=version_bcc.version_hash,
            scheduled_for="2026-09-04T15:30:00Z", timezone="Asia/Seoul", local_date="2026-09-05",
            local_date_display="2026.09.05", trigger_type="manual", status="awaiting_receipt", phase="finalize", attempt=1)
        self.run_repo.create_run(run)
        comp_input = self.make_input(run)
        comp_input = replace(comp_input, task_id=task_bcc.id, task_version_hash=version_bcc.version_hash)
        self.run_repo.save_composition_input(comp_input, "d" * 64)

        handoff = self._publish_custom(task_bcc, run, comp_input)

        server = self.server()
        with patch("smtplib.SMTP", return_value=server):
            ok, receipt, message = self.dispatcher.dispatch_handoff(handoff.handoff_id)
            self.assertTrue(ok, message)
            self.assertEqual(receipt.status, "smtp_accepted")

            # (a) server.rcpt called for all group addresses
            server.rcpt.assert_any_call("first@example.test")
            server.rcpt.assert_any_call("second@example.test")
            self.assertEqual(server.rcpt.call_count, 2)

            # (b) Raw SMTP DATA bytes contain NO recipient addresses
            raw_sent = smtp_message_bytes(server)
            self.assertNotIn(b"first@example.test", raw_sent)
            self.assertNotIn(b"second@example.test", raw_sent)

            # (c) To: undisclosed-recipients:; and NO Bcc header
            parsed = BytesParser(policy=policy.default).parsebytes(raw_sent)
            self.assertEqual(parsed["To"], "undisclosed-recipients:;")
            self.assertIsNone(parsed["Bcc"])
            self.assertIn(b"To: undisclosed-recipients:;", raw_sent)

            # (d) HTML/text body bytes and Date header timezone +0900
            self.assertIn("+0900", parsed["Date"])
            self.assertEqual(parsed["Subject"], "Seoul digest")
            self.assertIn("서울 보고서", parsed.get_body(preferencelist=("html",)).get_content())
            self.assertIn("서울 보고서", parsed.get_body(preferencelist=("plain",)).get_content())

            # (e) attempt and receipt files have no addresses
            evidence = self.settings.paths.receipts_dir / handoff.handoff_id
            self.assertTrue((evidence / "receipt.json").exists())
            self.assertNotIn("first@example.test", (evidence / "attempt.json").read_text())
            self.assertNotIn("second@example.test", (evidence / "attempt.json").read_text())

    def _publish_custom(self, task, run, comp_input):
        archive = self.settings.paths.run_archive_dir / task.id / run.run_id
        archive.mkdir(parents=True, exist_ok=True)
        (archive / "run-manifest.json").write_text(json.dumps({
            "run_id": run.run_id, "task_id": task.id, "status": "awaiting_receipt", "phase": "finalize", "attempt": 1,
            "result_outcome": {"status": "success"},
            "composition": {"status": "validated", "revision": 1, "recipient_group_id": "test-team",
                            "recipient_group_reason": "Test", "subject": "Seoul digest", "html_path": "email.html",
                            "text_path": "email.txt", "included_record_ids": ["c1"]},
            "handoff": {"status": "pending", "mode": "handoff"},
            "workspace": {"task_workspace_id": task.id, "generation": 1, "attempt_root": f"staging/{run.run_id}/attempt-1",
                          "fencing_token": "fixture-test-fence", "child_group_ids": []}}))
        handoff = self.publisher.create_and_publish_handoff(task, run, comp_input, self.result, self.stage,
            self.root / "unused", self.hashes, force_mode="handoff")
        manifest = json.loads((archive / "run-manifest.json").read_text())
        manifest["handoff"] = {"status": "published", "mode": "handoff", "message_type": "market_digest",
            "recipient_group_id": handoff.recipient_group_id, "message_revision": 1, "handoff_id": handoff.handoff_id,
            "idempotency_key": handoff.idempotency_key, "delivery_request_path": "delivery-request.json",
            "delivery_request_sha256": handoff.delivery_request_sha256, "published_at": handoff.published_at}
        (archive / "run-manifest.json").write_text(json.dumps(manifest))
        (archive / "delivery-request.json").write_bytes(
            (self.settings.paths.delivery_outbox_dir / handoff.handoff_id / "delivery-request.json").read_bytes())
        return handoff

    def test_recipient_visibility_snapshot_and_retry_consistency(self):
        from researchops.domain.models import TaskDefinition, TaskVersion, ScheduledRun
        from researchops.delivery.smtp_config import delivery_revision

        task_bcc = TaskDefinition(version=2, id="market-digest-bcc2", name="Market Digest BCC 2",
            enabled=True, schedule={"cron": "0 9 * * *"},
            workspace={"mode": "persistent_task"}, runner={"type": "fake"}, instructions={}, output={},
            delivery={"mode": "handoff", "allowed_recipient_group_ids": ["test-team"],
                      "recipient_visibility": "bcc"},
            state={"dedupe": {"enabled": False}},
            alerting={"events": ["failed"], "recipient_group_id": "researchops-admins"})
        version_bcc = TaskVersion(task_id=task_bcc.id, version_hash="e" * 64, sealed_at="2026-09-04T00:00:00Z",
            definition=task_bcc, package_files={})
        self.task_repo.save_version(version_bcc)
        self.task_repo.set_active_version(task_bcc.id, version_bcc.version_hash)
        dry = ScheduledRun(run_id="dry-bcc2", task_id=task_bcc.id, task_version_hash=version_bcc.version_hash,
            scheduled_for="2026-09-04T15:30:00Z", timezone="Asia/Seoul", local_date="2026-09-05",
            local_date_display="2026.09.05", trigger_type="candidate_dry_run", status="succeeded", phase="finalize", attempt=1)
        self.run_repo.create_run(dry)
        dry_input = self.make_input(dry)
        dry_input = replace(dry_input, task_id=task_bcc.id, task_version_hash=version_bcc.version_hash)
        self.publisher.create_and_publish_handoff(task_bcc, dry, dry_input, self.result, self.stage,
            self.root / "unused", self.hashes, force_mode="dry_run")
        self.task_repo.set_delivery_approved(task_bcc.id, True, delivery_revision(self.config))

        run = ScheduledRun(run_id="live-bcc2", task_id=task_bcc.id, task_version_hash=version_bcc.version_hash,
            scheduled_for="2026-09-04T15:30:00Z", timezone="Asia/Seoul", local_date="2026-09-05",
            local_date_display="2026.09.05", trigger_type="manual", status="awaiting_receipt", phase="finalize", attempt=1)
        self.run_repo.create_run(run)
        comp_input = self.make_input(run)
        comp_input = replace(comp_input, task_id=task_bcc.id, task_version_hash=version_bcc.version_hash)
        self.run_repo.save_composition_input(comp_input, "f" * 64)

        handoff = self._publish_custom(task_bcc, run, comp_input)

        # Enqueue handoff
        self.dispatcher.enqueue_handoff(handoff.handoff_id)
        job = self.dispatcher.queue.get(handoff.handoff_id)
        self.assertIsNotNone(job)

        # _prepare_job validation passes with matching snapshot
        cfg, smtp, envelope, diagnostic = self.dispatcher._prepare_job(job)
        self.assertEqual(envelope["recipients"], ["first@example.test", "second@example.test"])

        # If recipient mapping in config changes, snapshot comparison fails
        bad_config = self.dispatcher.get_config()
        bad_config.recipient_groups["test-team"] = ["changed@example.test"]
        with patch.object(self.dispatcher, "get_config", return_value=bad_config):
            with self.assertRaises(DeliveryError):
                self.dispatcher._prepare_job(job)

    def test_recipient_visibility_fail_closed_validation(self):
        from researchops.delivery.policy import recipient_visibility, recipient_visibility_for_task_version
        from researchops.domain.models import TaskDefinition, TaskVersion

        for bad in ("cc", "true", "BCC", 123, None):
            with self.subTest(bad=bad):
                with self.assertRaises(DeliveryError):
                    recipient_visibility({"delivery": {"recipient_visibility": bad}})

        task_bad = TaskDefinition(version=2, id="market-digest-bad", name="Market Digest Bad",
            enabled=True, schedule={"cron": "0 9 * * *"},
            workspace={"mode": "persistent_task"}, runner={"type": "fake"}, instructions={}, output={},
            delivery={"mode": "handoff", "allowed_recipient_group_ids": ["test-team"],
                      "recipient_visibility": "cc"},
            state={"dedupe": {"enabled": False}},
            alerting={"events": ["failed"], "recipient_group_id": "researchops-admins"})
        version_bad = TaskVersion(task_id=task_bad.id, version_hash="9" * 64, sealed_at="2026-09-04T00:00:00Z",
            definition=task_bad, package_files={})
        self.task_repo.save_version(version_bad)

        with self.assertRaises(DeliveryError):
            recipient_visibility_for_task_version(self.task_repo.db, task_bad.id, version_bad.version_hash)

    def test_idle_polling_does_not_query_mime_blob_or_reverify_completed_sidecars(self):
        # 1. Publish and dispatch a handoff so we have a completed attempt with evidence
        handoff = self.publish()
        server = self.server()
        with patch("smtplib.SMTP", return_value=server):
            ok, receipt, _ = self.dispatcher.dispatch_handoff(handoff.handoff_id)
            self.assertTrue(ok)

        # Initial dispatch_all_pending establishes the reconciliation timestamp
        self.dispatcher.dispatch_all_pending()

        # In subsequent idle polling ticks (within _reconcile_interval):
        # - SmtpQueue.get (which selects mime_bytes) must NOT be called for completed attempts
        # - SmtpQueue.completed must NOT be called
        # - publish_package must NOT be called
        with patch.object(self.dispatcher.queue, "get", wraps=self.dispatcher.queue.get) as spy_get, \
             patch.object(self.dispatcher.queue, "completed", wraps=self.dispatcher.queue.completed) as spy_completed, \
             patch("researchops.delivery.smtp_dispatcher.publish_package") as mock_publish:
            for _ in range(5):
                results = self.dispatcher.dispatch_all_pending()
                self.assertEqual(results, [])

            spy_get.assert_not_called()
            spy_completed.assert_not_called()
            mock_publish.assert_not_called()

    def test_reconciliation_reads_metadata_only_without_mime_bytes(self):
        # Enqueue and finish a job with a distinct large mime_bytes payload
        huge_mime = b"X" * 120_000
        job_id = "test-meta-only-job"
        self.dispatcher.queue.enqueue(job_id, None, "<test-meta@local>", huge_mime,
                                      {"connection_test": True}, "rev1")
        token = self.dispatcher.queue.claim(job_id)
        self.dispatcher.queue.finish(job_id, token, "connection_ok")

        # Verify completed() method returns metadata without mime_bytes
        completed_rows = self.dispatcher.queue.completed()
        matching = [r for r in completed_rows if r["job_id"] == job_id]
        self.assertEqual(len(matching), 1)
        self.assertNotIn("mime_bytes", matching[0])
        self.assertNotIn("envelope_json", matching[0])
        self.assertEqual(matching[0]["mime_sha256"], hashlib.sha256(huge_mime).hexdigest())

        # Verify get_metadata() also excludes mime_bytes
        meta = self.dispatcher.queue.get_metadata(job_id)
        self.assertIsNotNone(meta)
        self.assertNotIn("mime_bytes", meta)
        self.assertNotIn("envelope_json", meta)
        self.assertEqual(meta["mime_sha256"], hashlib.sha256(huge_mime).hexdigest())

        # Trigger full reconciliation and ensure sidecar is created with SHA-256
        reconciled = self.dispatcher.reconcile_completed_attempts(force=True, full=True)
        self.assertGreaterEqual(reconciled, 1)

        sidecar = self.settings.paths.receipts_dir / job_id
        self.assertTrue((sidecar / "attempt-manifest.json").exists())
        attempt_data = json.loads((sidecar / "attempt.json").read_text())
        self.assertEqual(attempt_data["mime_sha256"], hashlib.sha256(huge_mime).hexdigest())

    def test_reconciliation_cursor_pagination_and_wrap_around(self):
        # Create 5 completed jobs
        for i in range(5):
            jid = f"job-cursor-{i}"
            self.dispatcher.queue.enqueue(jid, None, f"<c{i}@local>", b"data", {"connection_test": True}, "rev")
            tok = self.dispatcher.queue.claim(jid)
            self.dispatcher.queue.finish(jid, tok, "connection_ok")

        # Set small batch size of 2
        self.dispatcher._reconcile_batch_size = 2
        self.dispatcher._last_reconciled_at = None

        # Cycle 1: batch 1 (2 jobs)
        c1 = self.dispatcher.reconcile_completed_attempts(force=True)
        self.assertEqual(c1, 2)
        self.assertIsNotNone(self.dispatcher._reconcile_cursor)

        # Cycle 2: batch 2 (2 jobs)
        c2 = self.dispatcher.reconcile_completed_attempts(force=True)
        self.assertEqual(c2, 2)
        self.assertIsNotNone(self.dispatcher._reconcile_cursor)

        # Cycle 3: batch 3 (remaining jobs, wraps around)
        c3 = self.dispatcher.reconcile_completed_attempts(force=True)
        self.assertGreaterEqual(c3, 1)
        self.assertIsNone(self.dispatcher._reconcile_cursor)

    def test_recovery_of_missing_sidecar_without_smtp_transmission(self):
        # Publish handoff
        handoff = self.publish()
        server = self.server()
        evidence = self.settings.paths.receipts_dir / handoff.handoff_id

        # 1. Dispatch handoff where initial publish_package fails with OSError
        with patch("smtplib.SMTP", return_value=server) as connect, \
             patch("researchops.delivery.smtp_dispatcher.publish_package", side_effect=OSError("Disk full")):
            ok, receipt, message = self.dispatcher.dispatch_handoff(handoff.handoff_id)
            self.assertTrue(ok)
            self.assertIn("protected attempt audit export is pending", message)
            self.assertEqual(receipt.status, "smtp_accepted")
            connect.assert_called_once()

        # Sidecar was not created due to publish error
        self.assertFalse(evidence.exists())

        # Record original database record, counts, and receipt hash
        conn = self.db.get_connection()
        try:
            original_attempt_row = dict(conn.execute("SELECT * FROM smtp_attempts WHERE job_id=?", (handoff.handoff_id,)).fetchone())
            original_receipt_row = dict(conn.execute("SELECT * FROM delivery_receipts WHERE external_receipt_id=?", ("smtp-" + handoff.handoff_id,)).fetchone())
            orig_attempt_count = conn.execute("SELECT count(*) FROM smtp_attempts").fetchone()[0]
            orig_receipt_count = conn.execute("SELECT count(*) FROM delivery_receipts").fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(original_attempt_row["status"], "smtp_accepted")
        orig_mime_sha256 = original_attempt_row["mime_sha256"]
        orig_mime_bytes = original_attempt_row["mime_bytes"]
        self.assertEqual(hashlib.sha256(orig_mime_bytes).hexdigest(), orig_mime_sha256)

        # 2. Simulate process restart by creating a brand new SmtpDispatcher instance
        from researchops.delivery.smtp_dispatcher import SmtpDispatcher
        new_dispatcher = SmtpDispatcher(self.settings, self.consumer, self.delivery_repo, self.state_repo)

        # 3. First run of dispatch_all_pending / reconciliation on the new dispatcher recovers the sidecar
        with patch("smtplib.SMTP") as mock_smtp:
            # First tick of dispatch_all_pending runs reconciliation on startup
            results = new_dispatcher.dispatch_all_pending()
            self.assertEqual(results, [])
            mock_smtp.assert_not_called()

        # Sidecar is regenerated cleanly without any SMTP connection
        self.assertTrue((evidence / "attempt-manifest.json").exists())
        self.assertTrue((evidence / "receipt.json").exists())
        self.assertTrue((evidence / "attempt.json").exists())

        attempt_data = json.loads((evidence / "attempt.json").read_text())
        self.assertEqual(attempt_data["job_id"], handoff.handoff_id)
        self.assertEqual(attempt_data["status"], "smtp_accepted")
        self.assertEqual(attempt_data["mime_sha256"], orig_mime_sha256)
        self.assertEqual(attempt_data["message_id"], original_attempt_row["message_id"])

        receipt_file_bytes = (evidence / "receipt.json").read_bytes()
        self.assertEqual(receipt_file_bytes, original_receipt_row["receipt_json"].encode("utf-8"))

        # Assert DB rows and counts are 100% byte-for-byte and field-for-field immutable
        conn = self.db.get_connection()
        try:
            post_attempt_row = dict(conn.execute("SELECT * FROM smtp_attempts WHERE job_id=?", (handoff.handoff_id,)).fetchone())
            post_receipt_row = dict(conn.execute("SELECT * FROM delivery_receipts WHERE external_receipt_id=?", ("smtp-" + handoff.handoff_id,)).fetchone())
            post_attempt_count = conn.execute("SELECT count(*) FROM smtp_attempts").fetchone()[0]
            post_receipt_count = conn.execute("SELECT count(*) FROM delivery_receipts").fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(post_attempt_row, original_attempt_row)
        self.assertEqual(post_receipt_row, original_receipt_row)
        self.assertEqual(post_attempt_count, orig_attempt_count)
        self.assertEqual(post_receipt_count, orig_receipt_count)

    def test_recover_interrupted_immediately_archives_dead_jobs(self):
        # Simulate a job stuck in sending state with a non-existent PID
        jid = "dead-process-job"
        self.dispatcher.queue.enqueue(jid, None, "<dead@local>", b"data", {"connection_test": True}, "rev")
        tok = self.dispatcher.queue.claim(jid)
        with self.db.transaction() as conn:
            conn.execute("UPDATE smtp_attempts SET claim_pid=99999999 WHERE job_id=?", (jid,))

        sidecar = self.settings.paths.receipts_dir / jid
        self.assertFalse(sidecar.exists())

        # Calling dispatch_all_pending() recovers the job and archives it immediately
        self.dispatcher.dispatch_all_pending()

        # Job is recovered to failed
        recovered_job = self.dispatcher.queue.get_metadata(jid)
        self.assertEqual(recovered_job["status"], "failed")

        # Sidecar was exported immediately
        self.assertTrue((sidecar / "attempt-manifest.json").exists())
        attempt_data = json.loads((sidecar / "attempt.json").read_text())
        self.assertEqual(attempt_data["status"], "failed")

    def test_tampered_sidecar_is_not_overwritten_and_archive_attempt_fails(self):
        jid = "tampered-job"
        self.dispatcher.queue.enqueue(jid, None, "<tampered@local>", b"data", {"connection_test": True}, "rev")
        tok = self.dispatcher.queue.claim(jid)
        self.dispatcher.queue.finish(jid, tok, "connection_ok")

        self.assertTrue(self.dispatcher.archive_attempt(jid))
        sidecar = self.settings.paths.receipts_dir / jid
        attempt_file = sidecar / "attempt.json"
        self.assertTrue(attempt_file.exists())

        # Tamper with the sidecar file
        tampered_bytes = b'{"tampered": true}'
        attempt_file.write_bytes(tampered_bytes)

        # Archiving must fail and must NOT overwrite the tampered file
        self.assertFalse(self.dispatcher.archive_attempt(jid))
        self.assertEqual(attempt_file.read_bytes(), tampered_bytes)

        # Reconciling also does not overwrite and does not crash
        self.dispatcher.reconcile_completed_attempts(force=True, full=True)
        self.assertEqual(attempt_file.read_bytes(), tampered_bytes)


if __name__ == "__main__":
    unittest.main()
