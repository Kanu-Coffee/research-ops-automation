"""Legacy receipt import is audit-only; SMTP success comes from durable local attempts."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from researchops.domain.models import DeliveryReceipt
from researchops.domain.events import AuditEvent
from researchops.errors import ReceiptError, ValidationError


class ReceiptConsumer:
    def __init__(self, settings, delivery_repo, run_repo, state_repo, task_repo=None):
        self.settings, self.delivery_repo, self.run_repo = settings, delivery_repo, run_repo
        self.state_repo, self.task_repo = state_repo, task_repo

    @property
    def schema(self):
        return json.loads((self.settings.paths.schemas_dir / "delivery-receipt.schema.json").read_text())

    def import_and_verify_receipt(self, receipt_data_or_file):
        """Preserve external claims without granting them authority over operational state."""
        if isinstance(receipt_data_or_file, Path):
            raw = receipt_data_or_file.read_text(encoding="utf-8")
            data = json.loads(raw)
        elif isinstance(receipt_data_or_file, str):
            if receipt_data_or_file.lstrip().startswith("{"):
                raw = receipt_data_or_file
            else:
                raw = Path(receipt_data_or_file).read_text(encoding="utf-8")
            data = json.loads(raw)
        else:
            data = receipt_data_or_file
            raw = json.dumps(data, sort_keys=True)
        errors = list(Draft202012Validator(self.schema, format_checker=FormatChecker()).iter_errors(data))
        if errors:
            raise ValidationError("Delivery receipt schema validation failed", errors=[e.json_path for e in errors])
        if data["proof"]["type"] == "local_smtp_attempt" or data["external_receipt_id"].startswith("smtp-"):
            raise ReceiptError("Local SMTP evidence cannot be imported")
        receipt = DeliveryReceipt(**data)
        handoff = self.delivery_repo.get_handoff(receipt.handoff_id)
        if not handoff or handoff.idempotency_key != receipt.idempotency_key or handoff.delivery_request_sha256 != receipt.delivery_request_sha256:
            raise ReceiptError("Imported receipt does not match a durable handoff")
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.delivery_repo.db.transaction() as conn:
            existing = conn.execute("SELECT receipt_sha256 FROM delivery_receipts WHERE external_receipt_id=?",
                                    (receipt.external_receipt_id,)).fetchone()
            if existing:
                if existing[0] != digest:
                    raise ReceiptError("Receipt identity already exists with different immutable evidence")
            else:
                conn.execute("""INSERT INTO delivery_receipts(external_receipt_id,handoff_id,idempotency_key,
                    delivery_request_sha256,status,occurred_at,external_message_ref,error_json,proof_json,
                    receipt_json,receipt_sha256,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (receipt.external_receipt_id,receipt.handoff_id,receipt.idempotency_key,
                     receipt.delivery_request_sha256,receipt.status,receipt.occurred_at,receipt.external_message_ref,
                     json.dumps(receipt.error) if receipt.error else None,json.dumps(receipt.proof),raw,digest,
                     datetime.now(timezone.utc).isoformat()))
        self.state_repo.save_audit_event(AuditEvent(entity_type="receipt", entity_id=receipt.external_receipt_id,
            event_type="legacy_receipt_imported_untrusted", details={"handoff_id":handoff.handoff_id,"operational_state_changed":False}))
        return receipt, handoff
