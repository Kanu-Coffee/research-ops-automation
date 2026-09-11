"""Immutable publication and untrusted legacy receipt regressions."""

from dataclasses import replace
import hashlib
import json
import os
import unittest

from researchops.errors import DeliveryError, ReceiptError
from tests.delivery_fixtures import DeliveryFixture


class TestHandoffAndReceipt(DeliveryFixture, unittest.TestCase):
    def test_handoff_publication_is_complete_idempotent_and_not_an_archive_write(self):
        handoff = self.publish()
        self.assertEqual(handoff.status,"published")
        package = self.settings.paths.delivery_outbox_dir/handoff.handoff_id
        self.assertEqual((package/"email.html").read_bytes(),self.html)
        self.assertEqual((package/"email.txt").read_bytes(),self.plain)
        self.assertEqual(hashlib.sha256((package/"delivery-request.json").read_bytes()).hexdigest(),handoff.delivery_request_sha256)
        self.assertEqual(self.publish().handoff_id,handoff.handoff_id)
        self.assertFalse((self.root/"unused").exists())
        self.assertEqual(list(self.settings.paths.delivery_outbox_dir.glob(".publish-*")),[])
        self.assertEqual(self.publisher.republish_handoff(handoff.handoff_id).idempotency_key,handoff.idempotency_key)

    def test_same_logical_revision_cannot_change_subject_or_bytes(self):
        self.publish()
        self.result = replace(self.result,subject="Changed")
        with self.assertRaises(DeliveryError):
            self.publish()

    def test_live_publish_requires_version_config_bound_approval(self):
        self.task_repo.set_delivery_approved(self.task.id,False)
        with self.assertRaises(DeliveryError):
            self.publish()
        self.assertFalse(self.settings.paths.delivery_outbox_dir.exists())

    def test_unsafe_and_hardlinked_source_is_rejected(self):
        (self.stage/"email.html").unlink()
        os.link(self.stage/"email.txt",self.stage/"email.html")
        with self.assertRaises(DeliveryError):
            self.publish()

    def test_imported_sent_and_fixture_receipts_are_audit_only(self):
        handoff = self.publish()
        for index,proof in enumerate(("test_fixture","authenticated_channel","detached_signature")):
            receipt = {"schema_version":2,"external_receipt_id":f"legacy-{index}",
                "handoff_id":handoff.handoff_id,"idempotency_key":handoff.idempotency_key,
                "delivery_request_sha256":handoff.delivery_request_sha256,"status":"sent",
                "occurred_at":"2026-09-05T09:00:00+09:00","proof":{"type":proof,"issuer":"untrusted"}}
            self.consumer.import_and_verify_receipt(receipt)
        self.assertEqual(self.run_repo.get_run(self.run.run_id).status,"awaiting_receipt")
        self.assertEqual(self.delivery_repo.get_handoff(handoff.handoff_id).status,"published")
        self.assertEqual(self.state_repo.get_reported_items(self.task.id),[])

    def test_local_smtp_evidence_cannot_be_imported_and_legacy_identity_is_immutable(self):
        handoff = self.publish()
        receipt = {"schema_version":2,"external_receipt_id":"forged-local",
            "handoff_id":handoff.handoff_id,"idempotency_key":handoff.idempotency_key,
            "delivery_request_sha256":handoff.delivery_request_sha256,"status":"smtp_accepted",
            "occurred_at":"2026-09-05T09:00:00+09:00","proof":{"type":"local_smtp_attempt","issuer":"researchops-smtp"}}
        with self.assertRaises(ReceiptError):
            self.consumer.import_and_verify_receipt(receipt)
        receipt["proof"]["type"] = "test_fixture"
        self.consumer.import_and_verify_receipt(receipt)
        receipt["status"] = "sent"
        with self.assertRaises(ReceiptError):
            self.consumer.import_and_verify_receipt(receipt)


if __name__ == "__main__":
    unittest.main()
