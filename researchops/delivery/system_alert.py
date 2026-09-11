"""Escaped system alerts published through the same approved SMTP queue."""

from datetime import datetime, timezone
import hashlib
from html import escape
import json

from researchops.delivery.handoff import HandoffPublisher
from researchops.domain.models import DeliveryHandoff
from researchops.domain.events import AuditEvent
from researchops.errors import DeliveryError


class SystemAlertManager:
    def __init__(self, settings, delivery_repo, state_repo):
        self.settings, self.delivery_repo, self.state_repo = settings, delivery_repo, state_repo

    def emit_alert(self, task_def, run, event_type, reason):
        config = task_def.alerting or {}
        if event_type not in config.get("events", []):
            return None
        key = f"alert-{task_def.id}-{run.run_id}-{event_type}"
        existing = self.delivery_repo.get_handoff_by_idempotency_key(key)
        if existing:
            return existing
        handoff_id = "alert-" + hashlib.sha256(key.encode()).hexdigest()[:32]
        now = datetime.now(timezone.utc).isoformat()
        safe_event = " ".join(str(event_type).split())[:100]
        subject = f"[ResearchOps] {safe_event}: {task_def.id}"
        text = f"System alert: {safe_event}\nTask: {task_def.id}\nRun: {run.run_id}\nReason: {reason}\n"
        html = "<html><body><h2>ResearchOps system alert</h2><pre>" + escape(text) + "</pre></body></html>"
        files = {"alert.html":html.encode("utf-8"), "alert.txt":text.encode("utf-8")}
        bodies = {}
        for kind, path, media in (("html","alert.html","text/html"),("text","alert.txt","text/plain")):
            bodies[kind] = {"path":path,"media_type":media,"size_bytes":len(files[path]),
                            "sha256":hashlib.sha256(files[path]).hexdigest()}
        request = {"schema_version":2,"handoff_id":handoff_id,"idempotency_key":key,
            "task_id":task_def.id,"run_id":run.run_id,"task_version_hash":run.task_version_hash,
            "message_revision":1,"message_type":"system_alert",
            "recipient_group_id":config.get("recipient_group_id","researchops-admins"),
            "subject":subject,"body":bodies,"attachments":[],"created_at":now}
        raw = json.dumps(request, indent=2, sort_keys=True).encode()
        handoff = DeliveryHandoff(handoff_id=handoff_id,idempotency_key=key,run_id=run.run_id,
            task_id=task_def.id,task_version_hash=run.task_version_hash,message_revision=1,
            message_type="system_alert",recipient_group_id=request["recipient_group_id"],mode="handoff",
            status="prepared",delivery_request=request,delivery_request_sha256=hashlib.sha256(raw).hexdigest())
        publisher = HandoffPublisher(self.settings,self.delivery_repo,self.state_repo)
        try:
            publisher._approve(handoff)
            self.delivery_repo.save_handoff(handoff)
            publisher._publish(handoff,raw,files)
        except DeliveryError:
            # A blocked alert is local audit evidence, never a recursive alert or bypass.
            self.state_repo.save_audit_event(AuditEvent(entity_type="run",entity_id=run.run_id,
                event_type="system_alert_blocked",details={"event":safe_event}))
            return None
        self.state_repo.save_audit_event(AuditEvent(entity_type="handoff",entity_id=handoff_id,
            event_type="system_alert_queued",details={"event":safe_event}))
        return handoff
