"""Email-only retries against isolated data and a synthetic SMTP protocol."""
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
import smtplib
import sqlite3
import unittest
from unittest.mock import patch

from researchops.delivery.queue import add_delivery_schema
from researchops.delivery.smtp_config import save_delivery_config
from researchops.errors import DeliveryError
from tests.delivery_fixtures import DeliveryFixture, smtp_server


class EmailRetryTests(DeliveryFixture, unittest.TestCase):
    def make_failure(self, *, code=550, uncertain=False):
        handoff = self.publish()
        server = smtp_server()
        server.getreply.side_effect = [(354,b'continue'),
            smtplib.SMTPServerDisconnected('private mailbox text') if uncertain else (code,b'private reply')]
        with patch('smtplib.SMTP',return_value=server):
            self.dispatcher.dispatch_handoff(handoff.handoff_id)
        return handoff

    def test_manual_reuses_exact_message_and_preserves_original_proof(self):
        h = self.make_failure()
        original = self.dispatcher.queue.get(h.handoff_id)
        receipt_path = self.settings.paths.receipts_dir/h.handoff_id/'receipt.json'
        original_receipt = receipt_path.read_bytes()
        created = self.dispatcher.retry_email(h.handoff_id,request_key='manual-one')
        repeated = self.dispatcher.retry_email(h.handoff_id,request_key='manual-one')
        self.assertEqual(created,repeated)
        self.assertNotEqual(created['job_id'],h.handoff_id)
        second = self.dispatcher.queue.get(created['job_id'])
        for field in ('mime_bytes','mime_sha256','message_id','envelope_json'):
            self.assertEqual(original[field],second[field])
        self.assertEqual(self.dispatcher.queue.get(h.handoff_id),original)
        with patch('smtplib.SMTP',return_value=smtp_server()) as connect:
            self.assertTrue(self.dispatcher.dispatch_all_pending()[0][1])
            self.assertEqual(self.dispatcher.dispatch_all_pending(),[])
            connect.assert_called_once()
        self.assertEqual(receipt_path.read_bytes(),original_receipt)
        self.assertEqual(self.dispatcher.queue.get(h.handoff_id),original)
        self.assertEqual(self.run_repo.get_run(self.run.run_id).status,'succeeded')
        self.assertEqual(len(self.state_repo.get_reported_items(self.task.id)),1)
        self.assertFalse(self.dispatcher.retry_status(h.handoff_id)['eligible'])

    def test_auto_retry_is_durable_delayed_and_can_be_expedited_once(self):
        h = self.make_failure(code=451)
        status = self.dispatcher.retry_status(h.handoff_id)
        self.assertEqual(len(status['attempts']),2)
        self.assertTrue(status['eligible'])
        self.assertEqual(self.run_repo.get_run(self.run.run_id).status,'awaiting_receipt')
        self.assertEqual(self.dispatcher.queue.pending(),[])
        self.db.init_schema()
        self.assertEqual(self.dispatcher.queue.pending(),[])
        latest = self.dispatcher.queue.latest(h.handoff_id)
        self.assertGreater(latest['next_attempt_at'],datetime.now(timezone.utc).isoformat())
        result = self.dispatcher.retry_email(h.handoff_id,request_key='expedite')
        self.assertEqual(result['job_id'],latest['job_id'])
        self.assertEqual(self.dispatcher.retry_email(h.handoff_id,request_key='expedite'),result)
        self.assertEqual(len(self.dispatcher.queue.pending()),1)
        with self.assertRaises(DeliveryError):
            self.dispatcher.retry_email(h.handoff_id,request_key='different-click')

    def test_uncertain_is_never_auto_retried_and_requires_reasoned_ack(self):
        h = self.make_failure(uncertain=True)
        status = self.dispatcher.retry_status(h.handoff_id)
        self.assertTrue(status['uncertain'])
        self.assertTrue(status['can_retry_uncertain'])
        self.assertFalse(status['eligible'])
        with self.assertRaises(DeliveryError):
            self.dispatcher.retry_email(h.handoff_id,request_key='no-ack')
        with self.assertRaises(DeliveryError):
            self.dispatcher.retry_email(h.handoff_id,request_key='no-reason',allow_uncertain=True)
        with patch('smtplib.SMTP') as connect:
            self.assertEqual(self.dispatcher.dispatch_all_pending(),[])
            connect.assert_not_called()
        self.dispatcher.retry_email(h.handoff_id,request_key='ack',allow_uncertain=True,
            reason='Operator elects one resend after checking delivery; duplicate is possible')
        server = smtp_server()
        server.mail.return_value = (451,b'temporary failure')
        with patch('smtplib.SMTP',return_value=server):
            self.dispatcher.dispatch_all_pending()
        self.assertEqual(len(self.dispatcher.queue.history(h.handoff_id)),2)
        self.assertEqual(self.dispatcher.queue.pending(),[])
        self.assertEqual(self.delivery_repo.get_handoff(h.handoff_id).status,'uncertain')
        self.assertEqual(self.run_repo.get_run(self.run.run_id).status,'needs_attention')
        self.assertTrue(self.dispatcher.retry_status(h.handoff_id)['uncertain'])
        with self.assertRaises(DeliveryError):
            self.dispatcher.retry_email(h.handoff_id,request_key='third')

    def test_changed_recipients_or_tampered_mime_block_retry_before_network(self):
        h = self.make_failure()
        self.config.recipient_groups['test-team']=['changed@example.test']
        save_delivery_config(self.config,self.settings.paths.delivery_config_file)
        with patch('smtplib.SMTP') as connect:
            with self.assertRaises(DeliveryError):
                self.dispatcher.retry_email(h.handoff_id,request_key='changed')
            connect.assert_not_called()
        self.config.recipient_groups['test-team']=['first@example.test','second@example.test']
        save_delivery_config(self.config,self.settings.paths.delivery_config_file)
        with self.db.transaction() as conn:
            conn.execute("UPDATE smtp_attempts SET mime_bytes=? WHERE job_id=?",(b'changed',h.handoff_id))
        self.assertFalse(self.dispatcher.retry_status(h.handoff_id)['eligible'])

    def test_retry_key_binds_acknowledgement_and_reason(self):
        h = self.make_failure()
        self.dispatcher.retry_email(h.handoff_id,request_key='same',reason='first')
        with self.assertRaises(DeliveryError):
            self.dispatcher.retry_email(h.handoff_id,request_key='same',reason='different')

    def test_retry_policy_limit_does_not_enqueue(self):
        self.settings.delivery.smtp_max_attempts=1
        h = self.make_failure(code=451)
        self.assertEqual(len(self.dispatcher.queue.history(h.handoff_id)),1)
        self.assertEqual(self.run_repo.get_run(self.run.run_id).status,'failed')

    def test_auto_schedule_conflict_preserves_known_failure_receipt(self):
        with patch.object(self.dispatcher.queue,'_insert_retry',side_effect=DeliveryError('related run won race')):
            h = self.make_failure(code=451)
        self.assertEqual(self.dispatcher.queue.get(h.handoff_id)['status'],'failed')
        self.assertEqual(self.run_repo.get_run(self.run.run_id).status,'failed')
        self.assertEqual(self.delivery_repo.get_handoff(h.handoff_id).external_delivery_status,'failed')
        self.assertTrue((self.settings.paths.receipts_dir/h.handoff_id/'receipt.json').exists())

    def test_expired_auto_retry_is_finished_without_network(self):
        h = self.make_failure(code=451)
        latest = self.dispatcher.queue.latest(h.handoff_id)
        old=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
        with self.db.transaction() as conn:
            conn.execute('UPDATE smtp_attempts SET next_attempt_at=?,expires_at=? WHERE job_id=?',(old,old,latest['job_id']))
        with patch('smtplib.SMTP') as connect:
            self.dispatcher.dispatch_all_pending()
            connect.assert_not_called()
        ended=self.dispatcher.queue.get(latest['job_id'])
        self.assertEqual(ended['status'],'failed')
        self.assertEqual(ended['error_code'],'smtp_retry_expired')
        self.assertEqual(len(self.dispatcher.queue.history(h.handoff_id)),2)

    def test_expiry_is_rechecked_in_durable_body_transaction(self):
        h=self.make_failure(code=451)
        latest=self.dispatcher.queue.latest(h.handoff_id)
        now=datetime.now(timezone.utc)
        expiry=now+timedelta(seconds=30)
        with self.db.transaction() as conn:
            conn.execute('UPDATE smtp_attempts SET next_attempt_at=?,expires_at=? WHERE job_id=?',
                ((now-timedelta(seconds=1)).isoformat(),expiry.isoformat(),latest['job_id']))
        server=smtp_server()
        with patch('smtplib.SMTP',return_value=server),patch('researchops.delivery.queue._now',return_value=(expiry+timedelta(seconds=1)).isoformat()):
            self.dispatcher.dispatch_all_pending()
        server.send.assert_not_called()
        self.assertEqual(self.dispatcher.queue.get(latest['job_id'])['status'],'failed')

    def test_timeout_change_retires_queued_snapshot_then_manual_retry(self):
        self.settings.environment='production'
        h=self.make_failure(code=451)
        old=self.dispatcher.queue.latest(h.handoff_id)
        self.config.smtp.final_reply_timeout_seconds=900
        save_delivery_config(self.config,self.settings.paths.delivery_config_file)
        self.assertTrue(self.dispatcher.retry_status(h.handoff_id)['eligible'])
        with patch('smtplib.SMTP') as connect:
            new=self.dispatcher.retry_email(h.handoff_id,request_key='changed-timeout')
            connect.assert_not_called()
        self.assertNotEqual(new['job_id'],old['job_id'])
        retired=self.dispatcher.queue.get(old['job_id'])
        self.assertEqual(retired['mime_bytes'],old['mime_bytes'])
        self.assertEqual(retired['config_revision'],old['config_revision'])
        self.assertEqual(retired['status'],'failed')
        with patch('smtplib.SMTP',return_value=smtp_server()):
            self.assertTrue(self.dispatcher.dispatch_all_pending()[0][1])

    def test_concurrent_manual_requests_create_one_child(self):
        from concurrent.futures import ThreadPoolExecutor
        h=self.make_failure()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _: self.dispatcher.retry_email(h.handoff_id,request_key='parallel'),range(2)))
        self.assertEqual(results[0],results[1])
        self.assertEqual(len(self.dispatcher.queue.history(h.handoff_id)),2)

    def test_cancelling_manual_retry_preserves_earlier_uncertainty(self):
        h=self.make_failure(uncertain=True)
        receipt=self.delivery_repo.get_handoff(h.handoff_id).external_receipt_id
        child=self.dispatcher.retry_email(h.handoff_id,request_key='manual',allow_uncertain=True,reason='Explicit duplicate risk accepted')
        self.run_repo.request_cancel(self.run.run_id)
        self.assertTrue(self.dispatcher.queue.cancel_pending_for_run(self.run.run_id))
        self.assertEqual(self.dispatcher.queue.get(child['job_id'])['status'],'failed')
        self.assertEqual(self.run_repo.get_run(self.run.run_id).status,'needs_attention')
        current=self.delivery_repo.get_handoff(h.handoff_id)
        self.assertEqual(current.status,'uncertain')
        self.assertEqual(current.external_receipt_id,receipt)
        with patch('smtplib.SMTP') as connect:
            self.dispatcher.dispatch_all_pending()
            connect.assert_not_called()

    def test_child_run_cannot_bypass_uncertain_or_pending_email(self):
        h = self.make_failure(uncertain=True)
        for trigger in ('retry','compose_only'):
            child=replace(self.run,run_id='child-'+trigger,parent_run_id=self.run.run_id,
                trigger_type=trigger,status='queued')
            with self.assertRaises(DeliveryError):
                self.run_repo.create_run(child)
        self.dispatcher.retry_email(h.handoff_id,request_key='ack',allow_uncertain=True,reason='Explicit duplicate risk accepted')
        child=replace(self.run,run_id='child-pending',parent_run_id=self.run.run_id,trigger_type='compose_only',status='queued')
        with self.assertRaises(DeliveryError):
            self.run_repo.create_run(child)


class RetryMigrationTests(unittest.TestCase):
    def test_legacy_row_and_constraints_migrate_without_rewriting_evidence(self):
        c=sqlite3.connect(':memory:');self.addCleanup(c.close);c.row_factory=sqlite3.Row
        c.execute('CREATE TABLE delivery_handoffs(handoff_id TEXT PRIMARY KEY)')
        c.execute("INSERT INTO delivery_handoffs VALUES('h')")
        c.execute('''CREATE TABLE smtp_attempts(job_id TEXT PRIMARY KEY,handoff_id TEXT UNIQUE,
            status TEXT,phase TEXT,claim_token TEXT,claim_pid INTEGER,message_id TEXT UNIQUE,
            mime_bytes BLOB,mime_sha256 TEXT,envelope_json TEXT,config_revision TEXT,
            created_at TEXT,updated_at TEXT,error TEXT,server_reply TEXT)''')
        c.execute("INSERT INTO smtp_attempts VALUES('old','h','uncertain','finished',NULL,NULL,'message',?, 'hash','{}','revision','before','after','original',NULL)",(b'original MIME',))
        original=dict(c.execute('SELECT * FROM smtp_attempts').fetchone())
        add_delivery_schema(c);add_delivery_schema(c)
        row=dict(c.execute("SELECT * FROM smtp_attempts WHERE job_id='old'").fetchone())
        self.assertEqual({k:row[k] for k in original},original)
        self.assertEqual(row['retryable'],0)
        self.assertIsNone(row['diagnostics_json'])
        self.assertEqual(list(c.execute('PRAGMA foreign_key_check')),[])
