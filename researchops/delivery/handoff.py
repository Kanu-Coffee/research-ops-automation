"""Immutable, approved handoff publication to the built-in SMTP queue boundary."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from researchops.delivery.package import publish_package, read_file, validated_files, validate_outbox
from researchops.delivery.policy import require_approval
from researchops.delivery.smtp_config import load_delivery_config
from researchops.delivery.recipient_routing import require_current_recipient, require_stored_selection, routing_mode
from researchops.domain.events import AuditEvent
from researchops.domain.models import DeliveryHandoff
from researchops.errors import DeliveryError


class HandoffPublisher:
    def __init__(self, settings, delivery_repo, state_repo):
        self.settings, self.delivery_repo, self.state_repo = settings, delivery_repo, state_repo

    def create_and_publish_handoff(self, task_def, run, comp_input, comp_result,
                                   compose_output_dir, archive_run_dir, file_hashes, force_mode=None):
        mode = force_mode or task_def.delivery.get("mode", "dry_run")
        if mode not in ("disabled", "dry_run", "handoff"):
            raise DeliveryError("Unknown delivery mode")
        if (task_def.id != run.task_id or comp_input.task_id != run.task_id or
                comp_input.run_id != run.run_id or comp_input.task_version_hash != run.task_version_hash):
            raise DeliveryError("Composition/run identity mismatch")
        from researchops.services.ownership import require_task_delivery_ownership
        require_task_delivery_ownership(self.delivery_repo.db, task_def)
        if comp_result.recipient_group_id not in comp_input.allowed_recipient_group_ids:
            raise DeliveryError("Composition selected an unapproved recipient group")
        revision = comp_input.composition_revision
        from researchops.delivery.artifact_integrity import require_stored_composition
        binding = require_stored_composition(self.delivery_repo.db, task_def, run.run_id, run.task_version_hash,
            revision, comp_result.recipient_group_id, schemas_dir=self.settings.paths.schemas_dir,
            comp_input=comp_input, comp_result=comp_result, file_root=compose_output_dir)
        if routing_mode(task_def) == "catalog_name":
            raw_result = read_file(compose_output_dir, "composition-result.json", 1_000_000)
            if hashlib.sha256(raw_result).hexdigest() != file_hashes.get("composition_result"):
                raise DeliveryError("Validated composition result changed before publication")
            require_stored_selection(self.delivery_repo.db, task_def, run.run_id, run.task_version_hash,
                revision, comp_result.recipient_group_id, schemas_dir=self.settings.paths.schemas_dir, comp_input=comp_input,
                comp_result=comp_result, raw_result=raw_result)
            require_current_recipient(self.delivery_repo.db,
                load_delivery_config(self.settings.paths.delivery_config_file),
                comp_result.recipient_group_id, catalog_required=True, task_id=run.task_id)
        key = f"ro-{run.task_id}-{run.run_id}-r{revision}"
        handoff_id = "handoff-" + hashlib.sha256(key.encode()).hexdigest()[:32]
        existing = self.delivery_repo.get_handoff_by_idempotency_key(key)
        now = datetime.now(timezone.utc).isoformat()
        bodies = {}
        for kind, path, media in (("html", comp_result.html_path, "text/html"),
                                  ("text", comp_result.text_path, "text/plain")):
            content = read_file(compose_output_dir, path)
            if hashlib.sha256(content).hexdigest() != file_hashes[kind]:
                raise DeliveryError("Validated composition body changed before publication")
            bodies[kind] = {"path": path, "sha256": file_hashes[kind], "media_type": media,
                            "size_bytes": len(content)}
        from researchops.delivery.artifact_integrity import delivery_artifacts
        attachments = delivery_artifacts(comp_input)
        request = {"schema_version": 2, "handoff_id": handoff_id, "idempotency_key": key,
            "task_id": run.task_id, "run_id": run.run_id, "task_version_hash": run.task_version_hash,
            "message_revision": revision, "message_type": task_def.delivery.get("message_type", "market_digest"),
            "recipient_group_id": comp_result.recipient_group_id, "subject": comp_result.subject,
            "body": bodies, "attachments": attachments,
            "created_at": existing.delivery_request["created_at"] if existing else now}
        if binding is not None:
            from researchops.engine.archive import canonical_json
            request["composition_binding_sha256"] = hashlib.sha256(canonical_json(binding)).hexdigest()
        files = validated_files(compose_output_dir, request, self.settings.paths.schemas_dir)
        raw = json.dumps(request, indent=2, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        if existing:
            if existing.delivery_request_sha256 != digest or existing.mode != mode:
                raise DeliveryError("Logical message revision is immutable; create a new composition revision")
            if existing.status == "prepared" and mode == "handoff":
                return self._publish(existing, raw, files)
            return existing
        handoff = DeliveryHandoff(handoff_id=handoff_id, idempotency_key=key,
            run_id=run.run_id, task_id=run.task_id, task_version_hash=run.task_version_hash,
            message_revision=revision, message_type=request["message_type"],
            recipient_group_id=comp_result.recipient_group_id, mode=mode,
            status="not_requested" if mode == "disabled" else "prepared",
            delivery_request=request, delivery_request_sha256=digest)
        if mode == "handoff":
            self._approve(handoff)
        self.delivery_repo.save_handoff(handoff)
        if mode == "handoff":
            self._publish(handoff, raw, files)
        self.state_repo.save_audit_event(AuditEvent(entity_type="handoff", entity_id=handoff_id,
            event_type="handoff_" + handoff.status,
            details={"mode":mode, "idempotency_key":key, "recipient_group_id":handoff.recipient_group_id}))
        # The orchestrator owns final run archive publication after this method returns.
        return handoff

    def _approve(self, handoff):
        config = load_delivery_config(self.settings.paths.delivery_config_file)
        require_approval(self.settings, self.delivery_repo.db, config, handoff.task_id,
            handoff.task_version_hash, handoff.recipient_group_id,
            system_alert=handoff.message_type == "system_alert", run_id=handoff.run_id,
            message_revision=handoff.message_revision)

    def _publish(self, handoff, raw, files):
        self._approve(handoff)
        publish_package(self.settings.paths.delivery_outbox_dir, handoff.handoff_id, raw, files)
        handoff.status = "published"
        handoff.published_at = datetime.now(timezone.utc).isoformat()
        self.delivery_repo.save_handoff(handoff)
        return handoff

    def republish_handoff(self, handoff_id):
        """Validate an existing complete package; never reset sent/uncertain dispatch state."""
        handoff = self.delivery_repo.get_handoff(handoff_id)
        if not handoff or handoff.mode != "handoff" or handoff.status != "published":
            raise DeliveryError("Only a published, undispatched live handoff can be republished")
        self._approve(handoff)
        from researchops.delivery.queue import SmtpQueue
        if SmtpQueue(self.delivery_repo.db).get(handoff_id):
            raise DeliveryError("SMTP job already exists; republish cannot reset its delivery state")
        validate_outbox(self.settings, handoff)
        from researchops.delivery.artifact_integrity import require_stored_composition
        from researchops.storage.repositories import TaskRepository
        task = TaskRepository(self.delivery_repo.db).get_version(handoff.task_version_hash).definition
        require_stored_composition(self.delivery_repo.db, task, handoff.run_id, handoff.task_version_hash,
            handoff.message_revision, handoff.recipient_group_id, schemas_dir=self.settings.paths.schemas_dir,
            archive_root=self.settings.paths.run_archive_dir / handoff.task_id / handoff.run_id,
            file_root=self.settings.paths.delivery_outbox_dir / handoff.handoff_id, request=handoff.delivery_request)
        self.state_repo.save_audit_event(AuditEvent(entity_type="handoff", entity_id=handoff_id,
            event_type="handoff_republish_verified", details={"idempotency_key":handoff.idempotency_key}))
        return handoff
