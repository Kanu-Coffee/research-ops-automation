"""Safe SMTP timing, committed with the existing queue ownership transitions.

The worker archive is immutable before SMTP starts. Delivery timing therefore
lives in the run audit ledger; it never rewrites the research archive or stores
addresses, message bodies, SMTP replies, or credentials.
"""

from datetime import datetime
import hashlib
import json


def record_delivery_event(conn, job_id, event_type, now, *, state=None):
    row = conn.execute("""SELECT h.run_id,r.attempt,length(a.mime_bytes) AS size
        FROM smtp_attempts a JOIN delivery_handoffs h ON h.handoff_id=a.handoff_id
        JOIN scheduled_runs r ON r.run_id=h.run_id
        WHERE a.job_id=? AND h.message_type!='system_alert'""", (job_id,)).fetchone()
    if row is None:
        # Connection checks, test emails and alerts are not business run steps.
        return
    step_id = "smtp-" + hashlib.sha256(job_id.encode()).hexdigest()[:32]
    details = {"schema_version": 1, "step_id": step_id, "phase": "smtp",
               "label": "smtp", "attempt": row["attempt"],
               "summary": {"mime_bytes": row["size"]}}
    if event_type == "run_step_started":
        details.update(state="running", started_at=now, finished_at=None, duration_ms=None)
    elif event_type == "run_step_finished":
        started_at = None
        # This may be a dispatcher recovered after a release upgrade. Missing
        # historical starts stay unknown instead of becoming queue-created time.
        for item in conn.execute("""SELECT details_json FROM audit_events
            WHERE entity_type='run' AND entity_id=? AND event_type='run_step_started'
            ORDER BY event_id DESC""", (row["run_id"],)):
            try:
                previous = json.loads(item[0])
                if previous.get("step_id") == step_id:
                    started_at = previous.get("started_at")
                    break
            except (ValueError, AttributeError):
                continue
        duration_ms = None
        if isinstance(started_at, str):
            try:
                elapsed = (datetime.fromisoformat(now) - datetime.fromisoformat(started_at)).total_seconds()
                if elapsed >= 0:
                    duration_ms = round(elapsed * 1000)
            except (ValueError, TypeError, OverflowError):
                pass
        details.update(state=state, started_at=started_at, finished_at=now, duration_ms=duration_ms)
    conn.execute("""INSERT INTO audit_events(entity_type,entity_id,event_type,details_json,actor,occurred_at)
        VALUES('run',?,?,?,'smtp-dispatcher',?)""",
        (row["run_id"], event_type, json.dumps(details, sort_keys=True), now))
