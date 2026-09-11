"""Data access repositories for SQLite storage."""

import json
from datetime import datetime, timedelta, timezone
import sqlite3
import uuid
from typing import Any, Dict, List, Optional
from researchops.domain.models import (
    TaskDefinition, TaskVersion, ScheduledRun, RunLease,
    ResearchResult, CompositionInput, CompositionResult,
    DeliveryHandoff, DeliveryReceipt, ReportedItem
)
from researchops.domain.events import AuditEvent
from researchops.storage.db import Database
from researchops.errors import ConcurrencyError, ValidationError


def require_catalog_active(conn, kind: str, key: str) -> None:
    """Guard a mutation in its existing transaction; legacy missing entries are allowed."""
    row = conn.execute("SELECT deleted_at FROM entity_catalog WHERE kind=? AND legacy_key=?",
                       (kind, key)).fetchone()
    if row and row["deleted_at"] is not None:
        raise ValidationError("This item is deleted; restore it before using it")


def require_catalog_references(conn, definition: Dict[str, Any]) -> None:
    from researchops.services.ownership import require_task_delivery_ownership
    require_task_delivery_ownership(conn, definition)
    delivery = definition.get("delivery", {})
    require_catalog_active(conn, "sender", delivery.get("sender_profile_id", "default"))
    for group in delivery.get("allowed_recipient_group_ids", []):
        require_catalog_active(conn, "recipient_group", group)


def _ensure_task_catalog(conn, task_id: str, name: str, instant: str) -> None:
    require_catalog_active(conn, "task", task_id)
    if conn.execute("SELECT 1 FROM entity_catalog WHERE kind='task' AND legacy_key=?", (task_id,)).fetchone():
        return
    from researchops.services.ownership import resolve_creation_owner
    conn.execute("""INSERT INTO entity_catalog(kind,legacy_key,display_name,created_at,updated_at,owner_user_id)
        VALUES('task',?,?,?,?,?) ON CONFLICT(kind,legacy_key) DO NOTHING""",
        (task_id, name, instant, instant, resolve_creation_owner(conn)))


def _rename_task_catalog(conn, task_id: str, name: str, instant: str) -> None:
    _ensure_task_catalog(conn, task_id, name, instant)
    conn.execute("UPDATE entity_catalog SET display_name=?,updated_at=? WHERE kind='task' AND legacy_key=?",
                 (name, instant, task_id))


class TaskRepository:
    def __init__(self, db: Database):
        self.db = db

    def save_version(self, version: TaskVersion) -> None:
        if version.task_id != version.definition.id:
            raise ValidationError("Task version identity does not match definition.id")
        with self.db.transaction() as conn:
            _ensure_task_catalog(conn, version.task_id, version.definition.name, version.sealed_at)
            conn.execute("INSERT OR IGNORE INTO tasks(task_id,updated_at) VALUES(?,?)", (version.task_id,version.sealed_at))
            existing = conn.execute("SELECT * FROM task_versions WHERE version_hash=?", (version.version_hash,)).fetchone()
            if existing:
                if (existing["task_id"] != version.task_id or json.loads(existing["definition_json"]) != version.definition.to_dict() or json.loads(existing["package_files_json"]) != version.package_files):
                    raise ValidationError("Sealed task version is immutable")
                return
            conn.execute(
                """
                INSERT INTO task_versions (
                    version_hash, task_id, definition_json, package_files_json, sealed_at, is_active
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    version.version_hash,
                    version.task_id,
                    json.dumps(version.definition.to_dict()),
                    json.dumps(version.package_files),
                    version.sealed_at,
                    0
                )
            )

    def get_version(self, version_hash: str) -> Optional[TaskVersion]:
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT * FROM task_versions WHERE version_hash = ?", (version_hash,)).fetchone()
            if not row:
                return None
            def_dict = json.loads(row["definition_json"])
            task_def = TaskDefinition(**def_dict)
            return TaskVersion(
                task_id=row["task_id"],
                version_hash=row["version_hash"],
                sealed_at=row["sealed_at"],
                definition=task_def,
                package_files=json.loads(row["package_files_json"]),
                is_active=bool(row["is_active"])
            )
        finally:
            conn.close()

    def list_versions(self, task_id: str) -> List[TaskVersion]:
        conn = self.db.get_connection()
        try:
            rows = conn.execute("SELECT * FROM task_versions WHERE task_id = ? ORDER BY sealed_at DESC", (task_id,)).fetchall()
            versions = []
            for row in rows:
                def_dict = json.loads(row["definition_json"])
                task_def = TaskDefinition(**def_dict)
                versions.append(
                    TaskVersion(
                        task_id=row["task_id"],
                        version_hash=row["version_hash"],
                        sealed_at=row["sealed_at"],
                        definition=task_def,
                        package_files=json.loads(row["package_files_json"]),
                        is_active=bool(row["is_active"])
                    )
                )
            return versions
        finally:
            conn.close()

    def set_active_version(self, task_id: str, version_hash: str, *,
                           delivery_mode: str = "dry_run", schedule_enabled: bool = False,
                           require_idle: bool = False,
                           expected_active_version_hash: Optional[str] = None,
                           expected_updated_at: Optional[str] = None) -> None:
        if delivery_mode not in {"disabled", "dry_run", "handoff"} or type(schedule_enabled) is not bool:
            raise ValidationError("Invalid task publication state")
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            require_catalog_active(conn, "task", task_id)
            if expected_active_version_hash is not None or expected_updated_at is not None:
                current = conn.execute("SELECT active_version_hash,updated_at FROM tasks WHERE task_id=?",
                                       (task_id,)).fetchone()
                if (not current or
                        (expected_active_version_hash is not None and
                         (current["active_version_hash"] or "") != expected_active_version_hash) or
                        (expected_updated_at is not None and current["updated_at"] != expected_updated_at)):
                    raise ValidationError("Task changed before publication; reopen Edit to review the current task")
            if require_idle:
                active = conn.execute("SELECT active_version_hash FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if active and active["active_version_hash"] != version_hash:
                    self._require_idle_for_edit(conn, task_id)
            version = conn.execute("SELECT task_id,definition_json FROM task_versions WHERE version_hash=?",(version_hash,)).fetchone()
            if not version or version["task_id"] != task_id:
                raise ValidationError("Candidate does not belong to this task")
            definition = json.loads(version["definition_json"])
            require_catalog_references(conn, definition)
            if delivery_mode == "handoff" and json.loads(version["definition_json"]).get("delivery", {}).get("mode") != "handoff":
                raise ValidationError("Live publication requires a sealed handoff task")
            # Unset active for all versions of this task
            conn.execute("UPDATE task_versions SET is_active = 0 WHERE task_id = ?", (task_id,))
            conn.execute("UPDATE task_versions SET is_active = 1 WHERE version_hash = ?", (version_hash,))
            conn.execute(
                """
                INSERT INTO tasks (task_id, active_version_hash, enabled, delivery_mode, delivery_approved, updated_at)
                VALUES (?, ?, ?, ?, 0, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    active_version_hash = excluded.active_version_hash,
                    enabled = excluded.enabled,
                    delivery_approved = 0,
                    delivery_mode = excluded.delivery_mode,
                    approved_version_hash = NULL,
                    approved_delivery_revision = NULL,
                    approval_dry_run_id = NULL,
                    approved_at = NULL,
                    updated_at = excluded.updated_at;
                """,
                (task_id, version_hash, int(schedule_enabled), delivery_mode, now)
            )
            _rename_task_catalog(conn, task_id, definition["name"], now)

    def publish_new_production_task(self, version: TaskVersion, *, schedule_enabled: bool = False) -> None:
        """Atomically create a real task, or accept an exact duplicate submission.

        A conflicting ID never overwrites another task. Duplicate requests do
        not re-enable a schedule an operator subsequently disabled.
        """
        if (version.task_id != version.definition.id or
                version.definition.delivery.get("mode") != "handoff" or
                type(schedule_enabled) is not bool):
            raise ValidationError("Invalid production task publication")
        with self.db.transaction() as conn:
            require_catalog_active(conn, "task", version.task_id)
            require_catalog_references(conn, version.definition.to_dict())
            existing = conn.execute("SELECT active_version_hash FROM tasks WHERE task_id=?", (version.task_id,)).fetchone()
            if existing:
                prior = conn.execute("SELECT package_files_json FROM task_versions WHERE version_hash=?",
                                     (existing["active_version_hash"],)).fetchone()
                if (existing["active_version_hash"] == version.version_hash and prior and
                        json.loads(prior["package_files_json"]) == version.package_files):
                    return
                raise ValidationError("Task ID already exists; choose another ID or edit the existing task")
            conn.execute("""INSERT INTO tasks(task_id,active_version_hash,enabled,delivery_mode,updated_at)
                VALUES(?,?,?,'handoff',?)""",
                (version.task_id, version.version_hash, int(schedule_enabled), version.sealed_at))
            conn.execute("""INSERT INTO task_versions(version_hash,task_id,definition_json,
                package_files_json,sealed_at,is_active) VALUES(?,?,?,?,?,1)""",
                (version.version_hash, version.task_id, json.dumps(version.definition.to_dict()),
                 json.dumps(version.package_files), version.sealed_at))
            _rename_task_catalog(conn, version.task_id, version.definition.name, version.sealed_at)
            conn.execute("""INSERT INTO audit_events(entity_type,entity_id,event_type,details_json,occurred_at)
                VALUES('task',?,'production_task_published',?,?)""",
                (version.task_id, json.dumps({"version_hash": version.version_hash,
                    "schedule_enabled": schedule_enabled, "delivery_mode": "handoff"}), version.sealed_at))

    @staticmethod
    def _require_idle_for_edit(conn, task_id: str) -> None:
        busy = conn.execute("""SELECT run_id FROM scheduled_runs WHERE task_id=?
            AND status IN ('queued','running','awaiting_receipt') LIMIT 1""", (task_id,)).fetchone()
        claim = conn.execute("SELECT run_id FROM task_claims WHERE task_id=?", (task_id,)).fetchone()
        handoff = conn.execute("""SELECT handoff_id FROM delivery_handoffs WHERE task_id=?
            AND mode='handoff' AND status IN ('prepared','published') LIMIT 1""", (task_id,)).fetchone()
        if busy or claim or handoff:
            raise ValidationError("Task has a queued/running execution or pending email. Wait for it to finish "
                                  "(or cancel it in Runs) and save again; existing runs will not be changed.")

    def publish_production_update(self, version: TaskVersion, *, expected_version_hash: str,
                                  schedule_enabled: bool, expected_delivery_mode: str,
                                  expected_updated_at: Optional[str] = None) -> None:
        """CAS publication keeps history/workspace and never changes old runs.

        The current delivery-mode control is deliberately preserved, rather than
        re-enabling a task whose sender was disabled after it was first created.
        All checks share the scheduler/worker's SQLite write transaction.
        """
        if (version.task_id != version.definition.id or type(schedule_enabled) is not bool or
                not isinstance(expected_version_hash, str) or not expected_version_hash):
            raise ValidationError("Invalid task edit publication")
        with self.db.transaction() as conn:
            require_catalog_active(conn, "task", version.task_id)
            require_catalog_references(conn, version.definition.to_dict())
            current = conn.execute("SELECT * FROM tasks WHERE task_id=?", (version.task_id,)).fetchone()
            if not current:
                raise ValidationError("Task no longer exists; reload before saving")
            existing = conn.execute("SELECT * FROM task_versions WHERE version_hash=?", (version.version_hash,)).fetchone()
            if existing and (existing["task_id"] != version.task_id or
                    json.loads(existing["definition_json"]) != version.definition.to_dict() or
                    json.loads(existing["package_files_json"]) != version.package_files):
                raise ValidationError("Sealed task version is immutable")
            # A duplicate completed save is harmless and must not re-enable a
            # schedule or sender subsequently disabled by another action.
            if (current["active_version_hash"] == version.version_hash and existing and
                    expected_version_hash != version.version_hash):
                return
            if (current["active_version_hash"] != expected_version_hash or
                    current["delivery_mode"] != expected_delivery_mode or
                    (expected_updated_at is not None and current["updated_at"] != expected_updated_at)):
                raise ValidationError("Task changed since this editor was opened; reload before saving")
            if current["active_version_hash"] == version.version_hash and bool(current["enabled"]) == schedule_enabled:
                return
            self._require_idle_for_edit(conn, version.task_id)
            if not existing:
                conn.execute("""INSERT INTO task_versions(version_hash,task_id,definition_json,
                    package_files_json,sealed_at,is_active) VALUES(?,?,?,?,?,0)""",
                    (version.version_hash, version.task_id, json.dumps(version.definition.to_dict()),
                     json.dumps(version.package_files), version.sealed_at))
            conn.execute("UPDATE task_versions SET is_active=0 WHERE task_id=?", (version.task_id,))
            conn.execute("UPDATE task_versions SET is_active=1 WHERE version_hash=?", (version.version_hash,))
            conn.execute("""UPDATE tasks SET active_version_hash=?,enabled=?,updated_at=?,
                delivery_approved=0,approved_version_hash=NULL,approved_delivery_revision=NULL,
                approval_dry_run_id=NULL,approved_at=NULL WHERE task_id=?""",
                (version.version_hash, int(schedule_enabled), version.sealed_at, version.task_id))
            # New cron rules must not replay pre-edit occurrences under the new
            # instructions after a long-disabled schedule is enabled.
            conn.execute("""INSERT INTO scheduler_watermarks(task_id,evaluated_through) VALUES(?,?)
                ON CONFLICT(task_id) DO UPDATE SET evaluated_through=excluded.evaluated_through""",
                (version.task_id, version.sealed_at))
            _rename_task_catalog(conn, version.task_id, version.definition.name, version.sealed_at)
            conn.execute("""INSERT INTO audit_events(entity_type,entity_id,event_type,details_json,occurred_at)
                VALUES('task',?,'production_task_edited',?,?)""",
                (version.task_id, json.dumps({"previous_version_hash": expected_version_hash,
                    "version_hash": version.version_hash, "schedule_enabled": schedule_enabled,
                    "delivery_mode": current["delivery_mode"]}), version.sealed_at))

    def get_active_version(self, task_id: str) -> Optional[TaskVersion]:
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT active_version_hash FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row or not row["active_version_hash"]:
                return None
            return self.get_version(row["active_version_hash"])
        finally:
            conn.close()

    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                return None
            return dict(row)
        finally:
            conn.close()

    def set_task_enabled(self, task_id: str, enabled: bool) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            require_catalog_active(conn, "task", task_id)
            if enabled:
                active = conn.execute("""SELECT v.definition_json FROM tasks t JOIN task_versions v
                    ON v.version_hash=t.active_version_hash WHERE t.task_id=?""", (task_id,)).fetchone()
                if active:
                    require_catalog_references(conn, json.loads(active["definition_json"]))
            conn.execute(
                "UPDATE tasks SET enabled = ?, updated_at = ? WHERE task_id = ?",
                (1 if enabled else 0, now, task_id)
            )

    def set_delivery_approved(self, task_id: str, approved: bool, delivery_revision: Optional[str] = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            require_catalog_active(conn, "task", task_id)
            task = conn.execute("SELECT active_version_hash FROM tasks WHERE task_id=?",(task_id,)).fetchone()
            evidence = None
            if approved:
                if not task or not task[0] or not delivery_revision:
                    raise ValidationError("Delivery approval requires active version and delivery configuration revision")
                definition=conn.execute("SELECT definition_json FROM task_versions WHERE version_hash=?",(task[0],)).fetchone()
                if not definition or json.loads(definition[0]).get("delivery",{}).get("mode")!="handoff":
                    raise ValidationError("The sealed task version must explicitly request handoff mode before live approval")
                require_catalog_references(conn, json.loads(definition[0]))
                evidence = conn.execute("""SELECT r.run_id FROM scheduled_runs r JOIN delivery_handoffs h ON h.run_id=r.run_id
                    WHERE r.task_id=? AND r.task_version_hash=? AND r.status='succeeded'
                    AND h.mode='dry_run' ORDER BY r.created_at DESC LIMIT 1""", (task_id,task[0])).fetchone()
                if not evidence:
                    raise ValidationError("Delivery approval requires a successful dry-run of the same version")
            conn.execute(
                """UPDATE tasks SET delivery_approved=?, approved_version_hash=?, approved_delivery_revision=?,
                   approval_dry_run_id=?, approved_at=?, delivery_mode=?, updated_at=? WHERE task_id=?""",
                (int(approved),task[0] if approved else None,delivery_revision if approved else None,
                 evidence[0] if evidence else None,now if approved else None,'handoff' if approved else 'dry_run',now,task_id)
            )

    def set_production_delivery_enabled(self, task_id: str, version_hash: str, enabled: bool) -> None:
        """Change current task delivery without changing its schedule or legacy evidence."""
        if type(enabled) is not bool:
            raise ValidationError("Delivery enablement must be a boolean")
        with self.db.transaction() as conn:
            require_catalog_active(conn, "task", task_id)
            version = conn.execute("SELECT task_id,definition_json FROM task_versions WHERE version_hash=?",
                                   (version_hash,)).fetchone()
            if not version or version["task_id"] != task_id:
                raise ValidationError("Invalid active task version")
            if enabled:
                require_catalog_references(conn, json.loads(version["definition_json"]))
            if enabled and json.loads(version["definition_json"]).get("delivery", {}).get("mode") != "handoff":
                raise ValidationError("Enable delivery by publishing a task with handoff mode")
            changed = conn.execute("""UPDATE tasks SET delivery_mode=?,updated_at=?
                WHERE task_id=? AND active_version_hash=?""",
                ("handoff" if enabled else "disabled", datetime.now(timezone.utc).isoformat(),
                 task_id, version_hash)).rowcount
            if changed != 1:
                raise ValidationError("Active task version changed; reload before changing delivery")

    def has_dry_run_evidence(self, task_id: str, version_hash: str) -> bool:
        conn = self.db.get_connection()
        try:
            return conn.execute("""SELECT 1 FROM scheduled_runs r JOIN delivery_handoffs h ON h.run_id=r.run_id
                WHERE r.task_id=? AND r.task_version_hash=? AND r.status='succeeded' AND h.mode='dry_run' LIMIT 1""", (task_id,version_hash)).fetchone() is not None
        finally:
            conn.close()

    def list_tasks(self, enabled_only: bool = False, *, include_deleted: bool = False) -> List[Dict[str, Any]]:
        conn = self.db.get_connection()
        try:
            query = "SELECT t.* FROM tasks t WHERE 1=1"
            if enabled_only:
                query += " AND t.enabled=1"
            if not include_deleted:
                query += " AND NOT EXISTS(SELECT 1 FROM entity_catalog c WHERE c.kind='task' AND c.legacy_key=t.task_id AND c.deleted_at IS NOT NULL)"
            rows = conn.execute(query + " ORDER BY t.task_id ASC").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()



class RunRepository:
    def __init__(self, db: Database):
        self.db = db

    def append_run_step_events(self, run_id, fencing_token, attempt, events, *, coarse_phase=None,
                               allow_cancel=False, allow_terminal=False, conn=None):
        """Append step transitions and an optional coarse phase under one fence."""
        from researchops.engine.run_timeline import validate_step_event
        if not isinstance(events, list) or not 1 <= len(events) <= 2:
            raise ValidationError("Invalid execution step transition")
        for event_type, details in events:
            validate_step_event(event_type, details)
            if details["attempt"] != attempt:
                raise ValidationError("Execution step attempt mismatch")
        if coarse_phase is not None and coarse_phase not in {
                "preflight", "research", "validate", "dedupe", "compose", "validate_message", "handoff", "finalize"}:
            raise ValidationError("Invalid coarse execution phase")

        def append(connection):
            now = datetime.now(timezone.utc).isoformat()
            row = connection.execute("""SELECT r.status,r.attempt,l.lease_expires_at,e.cancel_requested
                FROM scheduled_runs r JOIN run_leases l ON l.run_id=r.run_id
                JOIN task_claims c ON c.run_id=r.run_id LEFT JOIN execution_controls e ON e.run_id=r.run_id
                WHERE r.run_id=? AND l.fencing_token=? AND c.fencing_token=?""",
                (run_id, fencing_token, fencing_token)).fetchone()
            if (not row or row["lease_expires_at"] <= now or row["attempt"] != attempt or
                    (row["status"] != "running" and not allow_terminal)):
                raise ConcurrencyError("Execution step write requires the current run claim and lease")
            if row["cancel_requested"] and not allow_cancel:
                raise ConcurrencyError("Run cancellation requested")
            previous = connection.execute("""SELECT event_type,details_json FROM audit_events
                WHERE entity_type='run' AND entity_id=? AND event_type IN ('run_step_started','run_step_finished')
                ORDER BY event_id LIMIT 2001""", (run_id,)).fetchall()
            if len(previous) + len(events) > 2000:
                raise ValidationError("Execution timeline exceeds its event budget")
            started, finished = {}, set()
            for item in previous:
                detail = json.loads(item["details_json"])
                if item["event_type"] == "run_step_started":
                    started[detail["step_id"]] = detail
                else:
                    finished.add(detail["step_id"])
            for event_type, details in events:
                step_id = details["step_id"]
                if event_type == "run_step_started":
                    if step_id in started or step_id in finished:
                        raise ConcurrencyError("Execution step has already started")
                    started[step_id] = details
                else:
                    original = started.get(step_id)
                    if (not original or step_id in finished or any(original[key] != details[key]
                            for key in ("attempt", "phase", "label", "started_at"))):
                        raise ConcurrencyError("Execution step completion has no matching unfinished start")
                    finished.add(step_id)
                occurred_at = details["started_at"] if event_type == "run_step_started" else details["finished_at"]
                connection.execute("""INSERT INTO audit_events(entity_type,entity_id,event_type,details_json,actor,occurred_at)
                    VALUES('run',?,?,?,'run-worker',?)""",
                    (run_id, event_type, json.dumps(details, ensure_ascii=False, allow_nan=False), occurred_at))
            if coarse_phase is not None:
                connection.execute("UPDATE scheduled_runs SET phase=?,last_progress_at=? WHERE run_id=? AND status='running'",
                                   (coarse_phase, now, run_id))
        if conn is not None:
            append(conn)
        else:
            with self.db.transaction() as connection:
                append(connection)

    def get_run_event_snapshot(self, run_id, *, step_limit=2000, audit_limit=50):
        """Read a bounded run timeline plus the separate management event history."""
        if (type(step_limit) is not int or not 0 <= step_limit <= 2000 or
                type(audit_limit) is not int or not 0 <= audit_limit <= 500):
            raise ValidationError("Invalid run event query limit")
        conn = self.db.get_connection()
        try:
            conn.execute("BEGIN")
            counts = conn.execute("""SELECT COUNT(*) AS total,COALESCE(MAX(event_id),0) AS revision,
                COALESCE(SUM(CASE WHEN event_type IN ('run_step_started','run_step_finished') THEN 1 ELSE 0 END),0) AS steps
                FROM audit_events WHERE entity_type='run' AND entity_id=?""", (run_id,)).fetchone()
            def query(step, limit):
                operator, order = ("IN", "ASC") if step else ("NOT IN", "DESC")
                rows = conn.execute(f"""SELECT * FROM audit_events WHERE entity_type='run' AND entity_id=?
                    AND event_type {operator} ('run_step_started','run_step_finished') ORDER BY event_id {order} LIMIT ?""",
                    (run_id, limit)).fetchall()
                return [AuditEvent(entity_type=row["entity_type"], entity_id=row["entity_id"],
                    event_type=row["event_type"], details=json.loads(row["details_json"]), actor=row["actor"],
                    occurred_at=row["occurred_at"], event_id=row["event_id"]).to_dict() for row in rows]
            return {"step_events": query(True, step_limit), "step_total_count": counts["steps"],
                    "audit_events": query(False, audit_limit), "audit_total_count": counts["total"] - counts["steps"],
                    "revision": counts["revision"]}
        finally:
            conn.close()

    def create_run(self, run: ScheduledRun, *, force_dry_run: bool = False, composition_revision: int = 1,
                   request_key: Optional[str] = None, request_payload: Optional[Dict[str,Any]] = None,
                   expected_active_version_hash: Optional[str] = None,
                   execution_plan: Optional[Dict[str,Any]] = None,
                   delivery_authorization: Optional[Dict[str,Any]] = None) -> ScheduledRun:
        instant = datetime.fromisoformat(run.scheduled_for.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValidationError("Repository requires an explicit scheduled_for offset")
        run.scheduled_for = instant.astimezone(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            require_catalog_active(conn, "task", run.task_id)
            version = conn.execute("SELECT definition_json FROM task_versions WHERE version_hash=?", (run.task_version_hash,)).fetchone()
            if version:
                require_catalog_references(conn, json.loads(version["definition_json"]))
            payload_json = json.dumps(request_payload or {},sort_keys=True,separators=(",",":"))
            if request_key:
                prior = conn.execute("SELECT * FROM run_commands WHERE task_id=? AND request_key=?",(run.task_id,request_key)).fetchone()
                if prior:
                    if prior["request_json"] != payload_json:
                        raise ValidationError("Request key was already used with a different command")
                    return ScheduledRun(**dict(conn.execute("SELECT * FROM scheduled_runs WHERE run_id=?",(prior["run_id"],)).fetchone()))
            if expected_active_version_hash is not None:
                active = conn.execute("SELECT active_version_hash FROM tasks WHERE task_id=?", (run.task_id,)).fetchone()
                if (expected_active_version_hash != run.task_version_hash or not active or
                        active["active_version_hash"] != expected_active_version_hash):
                    raise ValidationError("Task changed before this run was queued; reload and run the current version")
            if run.parent_run_id and not force_dry_run and run.trigger_type in ('retry','compose_only','delivery_only'):
                from researchops.delivery.retry_guard import require_retry_family_clear
                require_retry_family_clear(conn,run.parent_run_id,prepared_email=run.trigger_type == 'delivery_only')
            if run.trigger_type == 'delivery_only':
                parent = conn.execute("""SELECT r.task_id,r.task_version_hash,r.status,e.cancel_requested,e.force_dry_run,
                    e.child_cleanup_verified
                    FROM scheduled_runs r JOIN execution_controls e ON e.run_id=r.run_id WHERE r.run_id=?""",
                    (run.parent_run_id,)).fetchone()
                if (not parent or parent['task_id'] != run.task_id or parent['task_version_hash'] != run.task_version_hash
                        or parent['status'] not in ('failed','timed_out') or parent['cancel_requested']
                        or not parent['child_cleanup_verified']
                        or parent['force_dry_run'] or force_dry_run
                        or conn.execute('SELECT 1 FROM task_claims WHERE task_id=?', (run.task_id,)).fetchone()
                        or conn.execute("SELECT 1 FROM delivery_handoffs WHERE run_id=? AND message_type!='system_alert'",
                                        (run.parent_run_id,)).fetchone()):
                    raise ValidationError('Stored mail source changed or is not ready for delivery')
            conn.execute(
                """
                INSERT INTO scheduled_runs (
                    run_id, task_id, task_version_hash, scheduled_for, timezone,
                    local_date, local_date_display, trigger_type, status, phase,
                    attempt, workspace_generation, parent_run_id, created_at,
                    started_at, finished_at, last_heartbeat_at, last_progress_at, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id, run.task_id, run.task_version_hash, run.scheduled_for, run.timezone,
                    run.local_date, run.local_date_display, run.trigger_type, run.status, run.phase,
                    run.attempt, run.workspace_generation, run.parent_run_id, run.created_at,
                    run.started_at, run.finished_at, run.last_heartbeat_at, run.last_progress_at, run.error_message
                )
            )
            conn.execute("INSERT INTO execution_controls(run_id,force_dry_run,composition_revision) VALUES(?,?,?)", (run.run_id,int(force_dry_run or run.trigger_type == "candidate_dry_run"),composition_revision))
            if execution_plan is not None:
                self.insert_execution_plan(conn, run.run_id, execution_plan)
            if delivery_authorization is not None:
                if force_dry_run or run.trigger_type == "candidate_dry_run":
                    raise ValidationError("Dry-run execution cannot hold live delivery authorization")
                from researchops.delivery.authorization import validate_delivery_authorization, encode_delivery_authorization
                validate_delivery_authorization(conn, run, delivery_authorization)
                raw, digest = encode_delivery_authorization(delivery_authorization)
                conn.execute("INSERT INTO run_delivery_authorizations(run_id,authorization_json,authorization_sha256) VALUES(?,?,?)",
                             (run.run_id, raw, digest))
            if request_key:
                conn.execute("INSERT INTO run_commands VALUES(?,?,?,?)",(run.task_id,request_key,run.run_id,payload_json))
            return run

    def get_delivery_authorization(self, run_id):
        from researchops.delivery.authorization import read_delivery_authorization
        conn = self.db.get_connection()
        try:
            return read_delivery_authorization(conn, run_id)
        finally:
            conn.close()

    @staticmethod
    def insert_execution_plan(conn, run_id, execution_plan):
        from researchops.engine.execution_plan import encode_execution_plan
        raw, digest = encode_execution_plan(execution_plan)
        conn.execute("INSERT INTO run_execution_plans(run_id,plan_json,plan_sha256) VALUES(?,?,?)",
                     (run_id, raw, digest))

    def get_execution_plan(self, run_id):
        from researchops.engine.execution_plan import encode_execution_plan
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT plan_json,plan_sha256 FROM run_execution_plans WHERE run_id=?",
                               (run_id,)).fetchone()
            if row is None:
                return None
            from researchops.strict_json import strict_json_loads
            value = strict_json_loads(row["plan_json"])
            raw, digest = encode_execution_plan(value)
            if raw != row["plan_json"] or digest != row["plan_sha256"]:
                raise ValidationError("Execution plan integrity mismatch")
            return value
        finally:
            conn.close()

    def get_run_command(self, task_id, request_key):
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT run_id,request_json FROM run_commands WHERE task_id=? AND request_key=?",
                               (task_id, request_key)).fetchone()
            if row is None:
                return None
            from researchops.strict_json import strict_json_loads
            return {"run_id": row["run_id"], "request": strict_json_loads(row["request_json"])}
        finally:
            conn.close()

    def get_run(self, run_id: str) -> Optional[ScheduledRun]:
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT * FROM scheduled_runs WHERE run_id = ?", (run_id,)).fetchone()
            if not row:
                return None
            return ScheduledRun(**dict(row))
        finally:
            conn.close()

    def list_runs(self, task_id: Optional[str] = None, status: Optional[str] = None, limit: int = 50) -> List[ScheduledRun]:
        conn = self.db.get_connection()
        try:
            query = "SELECT * FROM scheduled_runs"
            params = []
            conditions = []
            if task_id:
                conditions.append("task_id = ?")
                params.append(task_id)
            if status:
                conditions.append("status = ?")
                params.append(status)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [ScheduledRun(**dict(row)) for row in rows]
        finally:
            conn.close()

    def update_run_status(
        self,
        run_id: str,
        status: str,
        phase: str,
        attempt: Optional[int] = None,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        error_message: Optional[str] = None,
        fencing_token: Optional[str] = None,
        expected_status: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            current = conn.execute("SELECT status FROM scheduled_runs WHERE run_id=?",(run_id,)).fetchone()
            if not current:
                raise ValidationError("Run does not exist")
            if expected_status is not None and current[0] != expected_status:
                raise ConcurrencyError("Run state changed")
            transitions={"queued":{"queued","running","cancelled","failed","needs_attention"},
                         "running":{"running","awaiting_receipt","succeeded","failed","timed_out","cancelled","needs_attention"},
                         "awaiting_receipt":{"awaiting_receipt","succeeded","failed","needs_attention"}}
            if status not in transitions.get(current[0],{current[0]}):
                raise ConcurrencyError(f"Invalid run state transition: {current[0]} -> {status}")
            lease = conn.execute("SELECT * FROM run_leases WHERE run_id=?",(run_id,)).fetchone()
            if lease and (fencing_token != lease["fencing_token"] or lease["lease_expires_at"] <= now):
                raise ConcurrencyError("Run update requires the current live fencing token")
            if fencing_token and not lease:
                raise ConcurrencyError("Run lease no longer exists")
            if current[0] in {"succeeded","failed","timed_out","cancelled","needs_attention"} and status != current[0]:
                raise ConcurrencyError("Terminal run cannot be reopened; enqueue a new attempt")
            updates = ["status = ?", "phase = ?", "last_progress_at = ?"]
            params: List[Any] = [status, phase, now]
            if attempt is not None:
                updates.append("attempt = ?")
                params.append(attempt)
            if started_at:
                updates.append("started_at = ?")
                params.append(started_at)
            if finished_at:
                updates.append("finished_at = ?")
                params.append(finished_at)
            if error_message is not None:
                updates.append("error_message = ?")
                params.append(error_message)

            params.append(run_id)
            conn.execute(f"UPDATE scheduled_runs SET {', '.join(updates)} WHERE run_id = ?", params)

    def claim_next_run(self, worker_id: str, lease_seconds: int = 60, max_running: int = 2, run_id: Optional[str] = None):
        """One transaction owns both a run and its task; stale claims are never stolen."""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        with self.db.transaction() as conn:
            if conn.execute("SELECT count(*) FROM task_claims").fetchone()[0] >= max_running:
                return None
            query = """SELECT r.* FROM scheduled_runs r LEFT JOIN task_claims c ON c.task_id=r.task_id
                WHERE r.status='queued' AND c.task_id IS NULL AND r.scheduled_for<=?"""
            params = [now_iso]
            if run_id:
                query += " AND r.run_id=?"
                params.append(run_id)
            query += " ORDER BY r.created_at,r.run_id LIMIT 1"
            row = conn.execute(query,params).fetchone()
            if not row:
                return None
            run = ScheduledRun(**dict(row))
            token = "fence-" + uuid.uuid4().hex
            lease = RunLease(run.run_id,worker_id,run.attempt+1,token,now_iso,now_iso,(now+timedelta(seconds=lease_seconds)).isoformat())
            conn.execute("INSERT INTO task_claims VALUES(?,?,?,?)",(run.task_id,run.run_id,token,now_iso))
            conn.execute("INSERT INTO run_leases VALUES(?,?,?,?,?,?,?)",tuple(vars(lease).values()))
            conn.execute("UPDATE scheduled_runs SET status='running',phase='preflight',attempt=?,started_at=?,last_heartbeat_at=? WHERE run_id=? AND status='queued'",(lease.attempt,now_iso,now_iso,run.run_id))
            conn.execute("INSERT OR IGNORE INTO execution_controls(run_id) VALUES(?)",(run.run_id,))
            run.status,run.phase,run.attempt,run.started_at = "running","preflight",lease.attempt,now_iso
            return run,lease

    def assert_run_owner(self, run_id: str, fencing_token: str, *, allow_cancel: bool = False) -> None:
        conn = self.db.get_connection()
        try:
            row = conn.execute("""SELECT r.status,l.lease_expires_at,e.cancel_requested FROM scheduled_runs r
                JOIN run_leases l ON l.run_id=r.run_id JOIN task_claims c ON c.run_id=r.run_id
                LEFT JOIN execution_controls e ON e.run_id=r.run_id
                WHERE r.run_id=? AND l.fencing_token=? AND c.fencing_token=?""",(run_id,fencing_token,fencing_token)).fetchone()
            if not row or row["status"] != "running" or row["lease_expires_at"] <= datetime.now(timezone.utc).isoformat():
                raise ConcurrencyError("Execution no longer owns a live task lease")
            if row["cancel_requested"] and not allow_cancel:
                raise ConcurrencyError("Run cancellation requested")
        finally:
            conn.close()

    def get_execution_controls(self, run_id: str) -> Dict[str, Any]:
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT * FROM execution_controls WHERE run_id=?",(run_id,)).fetchone()
            return dict(row) if row else {"cancel_requested":0,"force_dry_run":0,"composition_revision":1,"child_cleanup_verified":0}
        finally:
            conn.close()

    def set_force_dry_run(self, run_id: str, value: bool) -> None:
        with self.db.transaction() as conn:
            changed = conn.execute("UPDATE execution_controls SET force_dry_run=? WHERE run_id=? AND EXISTS(SELECT 1 FROM scheduled_runs WHERE run_id=? AND status='queued')",(int(value),run_id,run_id)).rowcount
            if not changed:
                raise ConcurrencyError("Execution mode can only change before a run is claimed")

    def request_cancel(self, run_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            conn.execute("UPDATE execution_controls SET cancel_requested=1 WHERE run_id=?",(run_id,))
            conn.execute("UPDATE scheduled_runs SET status='cancelled',phase='finalize',finished_at=?,error_message='Cancelled before execution' WHERE run_id=? AND status='queued'",(now,run_id))

    def mark_cleanup_verified(self, run_id: str, fencing_token: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE execution_controls SET child_cleanup_verified=1 WHERE run_id=? AND EXISTS(SELECT 1 FROM task_claims WHERE run_id=? AND fencing_token=?)",(run_id,run_id,fencing_token))

    def mark_stale_attention(self, run_id: str, fencing_token: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            conn.execute("""UPDATE scheduled_runs SET status='needs_attention',phase='finalize',error_message='Stale worker lease expired; process cleanup not verified',finished_at=?
                WHERE run_id=? AND status='running' AND EXISTS(SELECT 1 FROM run_leases WHERE run_id=? AND fencing_token=? AND lease_expires_at<?)""",(now,run_id,run_id,fencing_token,now))

    def acquire_lease(self, lease: RunLease) -> bool:
        with self.db.transaction() as conn:
            # Check existing lease
            existing = conn.execute("SELECT * FROM run_leases WHERE run_id = ?", (lease.run_id,)).fetchone()
            if existing:
                return False
            run = conn.execute("SELECT task_id FROM scheduled_runs WHERE run_id=?",(lease.run_id,)).fetchone()
            if not run:
                return False
            claim = conn.execute("SELECT fencing_token FROM task_claims WHERE task_id=?",(run[0],)).fetchone()
            if claim:
                return False
            conn.execute("INSERT INTO task_claims VALUES(?,?,?,?)",(run[0],lease.run_id,lease.fencing_token,lease.claimed_at))
            conn.execute(
                """
                INSERT INTO run_leases (
                    run_id, worker_id, attempt, fencing_token, claimed_at, heartbeat_at, lease_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease.run_id, lease.worker_id, lease.attempt, lease.fencing_token,
                    lease.claimed_at, lease.heartbeat_at, lease.lease_expires_at
                )
            )
            return True

    def heartbeat_lease(self, run_id: str, fencing_token: str, new_expires_at: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            res = conn.execute(
                "UPDATE run_leases SET heartbeat_at = ?, lease_expires_at = ? WHERE run_id = ? AND fencing_token = ? AND lease_expires_at > ?",
                (now, new_expires_at, run_id, fencing_token, now)
            )
            if res.rowcount:
                conn.execute("UPDATE scheduled_runs SET last_heartbeat_at=? WHERE run_id=?",(now,run_id))
            return res.rowcount > 0

    def release_lease(self, run_id: str, fencing_token: str) -> None:
        with self.db.transaction() as conn:
            run = conn.execute("SELECT status FROM scheduled_runs WHERE run_id=?",(run_id,)).fetchone()
            control = conn.execute("SELECT child_cleanup_verified FROM execution_controls WHERE run_id=?",(run_id,)).fetchone()
            if run and run[0] != "queued" and (not control or not control[0]):
                raise ConcurrencyError("Cannot release task ownership until child cleanup is verified")
            conn.execute("DELETE FROM run_leases WHERE run_id = ? AND fencing_token = ?", (run_id, fencing_token))
            conn.execute("DELETE FROM task_claims WHERE run_id=? AND fencing_token=?",(run_id,fencing_token))

    def get_lease(self, run_id: str) -> Optional[RunLease]:
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT * FROM run_leases WHERE run_id = ?", (run_id,)).fetchone()
            if not row:
                return None
            return RunLease(**dict(row))
        finally:
            conn.close()

    def list_expired_leases(self, now_iso: str) -> List[RunLease]:
        conn = self.db.get_connection()
        try:
            rows = conn.execute("SELECT * FROM run_leases WHERE lease_expires_at < ?", (now_iso,)).fetchall()
            return [RunLease(**dict(row)) for row in rows]
        finally:
            conn.close()

    def get_active_run_for_task(self, task_id: str) -> Optional[ScheduledRun]:
        conn = self.db.get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM scheduled_runs WHERE task_id = ? AND status IN ('queued', 'running') ORDER BY created_at DESC LIMIT 1",
                (task_id,)
            ).fetchone()
            if not row:
                return None
            return ScheduledRun(**dict(row))
        finally:
            conn.close()

    def get_run_by_scheduled_time(self, task_id: str, scheduled_for: str) -> Optional[ScheduledRun]:
        conn = self.db.get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM scheduled_runs WHERE task_id = ? AND scheduled_for = ? LIMIT 1",
                (task_id, scheduled_for)
            ).fetchone()
            if not row:
                return None
            return ScheduledRun(**dict(row))
        finally:
            conn.close()


    def _guard_owned_write(self, conn, run_id: str, fencing_token: Optional[str]) -> None:
        row = conn.execute("SELECT fencing_token,lease_expires_at FROM run_leases WHERE run_id=?",(run_id,)).fetchone()
        if row and (row[0] != fencing_token or row[1] <= datetime.now(timezone.utc).isoformat()):
            raise ConcurrencyError("Result write rejected: stale or missing fencing token")
        if fencing_token and not row:
            raise ConcurrencyError("Result write rejected: lease no longer exists")

    def save_research_result(self, run_id: str, task_id: str, result: ResearchResult, *, fencing_token: Optional[str] = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            self._guard_owned_write(conn,run_id,fencing_token)
            conn.execute(
                """
                INSERT INTO research_results (
                    run_id, task_id, status, summary, records_json, coverage_json,
                    warnings_json, raw_json, artifacts_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, task_id, result.status, result.summary, json.dumps(result.records),
                    json.dumps(result.coverage), json.dumps(result.warnings), result.raw_json,
                    json.dumps(result.artifacts), now
                )
            )

    def get_research_result(self, run_id: str) -> Optional[ResearchResult]:
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT * FROM research_results WHERE run_id = ?", (run_id,)).fetchone()
            if not row:
                return None
            return ResearchResult(
                status=row["status"],
                summary=row["summary"],
                records=json.loads(row["records_json"]),
                coverage=json.loads(row["coverage_json"]),
                warnings=json.loads(row["warnings_json"]),
                raw_json=row["raw_json"],
                artifacts=json.loads(row["artifacts_json"])
            )
        finally:
            conn.close()

    def save_composition_input(self, comp_input: CompositionInput, sha256_hash: str, *, fencing_token: Optional[str] = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            self._guard_owned_write(conn,comp_input.run_id,fencing_token)
            conn.execute(
                """
                INSERT INTO composition_inputs (
                    run_id, task_id, task_version_hash, revision, input_json, input_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    comp_input.run_id, comp_input.task_id, comp_input.task_version_hash,
                    comp_input.composition_revision, json.dumps(comp_input.to_dict()),
                    sha256_hash, now
                )
            )

    def get_composition_input(self, run_id: str, revision: Optional[int] = None) -> Optional[Dict[str, Any]]:
        record = self.get_composition_input_record(run_id, revision)
        return record["input"] if record else None

    def get_composition_input_record(self, run_id: str, revision: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Read immutable input with its stored identity and original canonical hash."""
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT * FROM composition_inputs WHERE run_id = ? AND (? IS NULL OR revision=?) ORDER BY revision DESC LIMIT 1", (run_id,revision,revision)).fetchone()
            if not row:
                return None
            return {"run_id": row["run_id"], "task_id": row["task_id"],
                    "task_version_hash": row["task_version_hash"], "revision": row["revision"],
                    "input": json.loads(row["input_json"]), "input_sha256": row["input_sha256"]}
        finally:
            conn.close()

    def save_composition_result(self, run_id: str, task_id: str, comp_result: CompositionResult, *, revision: Optional[int] = None, fencing_token: Optional[str] = None, recipient_resolution: Optional[Dict[str, Any]] = None, composition_binding: Optional[Dict[str, Any]] = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload = comp_result.to_dict()
        if recipient_resolution is not None:
            payload["recipient_resolution"] = recipient_resolution
        if composition_binding is not None:
            payload["composition_binding"] = composition_binding
        with self.db.transaction() as conn:
            self._guard_owned_write(conn,run_id,fencing_token)
            if revision is None:
                control = conn.execute("SELECT composition_revision FROM execution_controls WHERE run_id=?",(run_id,)).fetchone()
                revision = control[0] if control else 1
            conn.execute(
                """
                INSERT INTO composition_results (
                    run_id, task_id, recipient_group_id, recipient_group_reason,
                    subject, html_path, text_path, included_record_ids_json, result_json, created_at, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, task_id, comp_result.recipient_group_id, comp_result.recipient_group_reason,
                    comp_result.subject, comp_result.html_path, comp_result.text_path,
                    json.dumps(comp_result.included_record_ids), json.dumps(payload), now, revision
                )
            )

    def get_composition_result_record(self, run_id: str, revision: Optional[int] = None) -> Optional[Dict[str, Any]]:
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT * FROM composition_results WHERE run_id=? AND (? IS NULL OR revision=?) ORDER BY revision DESC LIMIT 1",
                               (run_id, revision, revision)).fetchone()
            if not row:
                return None
            return {"run_id": row["run_id"], "task_id": row["task_id"], "revision": row["revision"],
                    "recipient_resolution": json.loads(row["result_json"]).get("recipient_resolution"),
                    "composition_binding": json.loads(row["result_json"]).get("composition_binding")}
        finally:
            conn.close()

    def get_composition_result(self, run_id: str, revision: Optional[int] = None) -> Optional[CompositionResult]:
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT * FROM composition_results WHERE run_id=? AND (? IS NULL OR revision=?) ORDER BY revision DESC LIMIT 1",
                               (run_id, revision, revision)).fetchone()
            if not row:
                return None
            return CompositionResult(
                recipient_group_id=row["recipient_group_id"],
                recipient_group_reason=row["recipient_group_reason"],
                subject=row["subject"],
                html_path=row["html_path"],
                text_path=row["text_path"],
                included_record_ids=json.loads(row["included_record_ids_json"])
            )
        finally:
            conn.close()


class DeliveryRepository:
    def __init__(self, db: Database):
        self.db = db

    def save_handoff(self, handoff: DeliveryHandoff) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM delivery_handoffs WHERE handoff_id=? OR idempotency_key=?",(handoff.handoff_id,handoff.idempotency_key)).fetchone()
            if existing:
                immutable = ("handoff_id","idempotency_key","run_id","task_id","task_version_hash","message_revision","message_type","recipient_group_id","mode","delivery_request_sha256")
                if any(existing[key] != getattr(handoff,key) for key in immutable) or json.loads(existing["delivery_request_json"]) != handoff.delivery_request:
                    raise ValidationError("Delivery package identity and content are immutable")
                if existing["status"] == "prepared" and handoff.status == "published":
                    conn.execute("UPDATE delivery_handoffs SET status='published',published_at=? WHERE handoff_id=? AND status='prepared'",(handoff.published_at,handoff.handoff_id))
                elif existing["status"] != handoff.status:
                    raise ConcurrencyError("Handoff status cannot be reset through save_handoff")
                return
            conn.execute(
                """
                INSERT INTO delivery_handoffs (
                    handoff_id, idempotency_key, run_id, task_id, task_version_hash,
                    message_revision, message_type, recipient_group_id, mode, status,
                    delivery_request_json, delivery_request_sha256, published_at,
                    external_receipt_id, acknowledged_at, external_delivery_status,
                    receipt_sha256, receipt_trust_status, decision_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handoff.handoff_id, handoff.idempotency_key, handoff.run_id, handoff.task_id,
                    handoff.task_version_hash, handoff.message_revision, handoff.message_type,
                    handoff.recipient_group_id, handoff.mode, handoff.status,
                    json.dumps(handoff.delivery_request), handoff.delivery_request_sha256,
                    handoff.published_at, handoff.external_receipt_id, handoff.acknowledged_at,
                    handoff.external_delivery_status, handoff.receipt_sha256,
                    handoff.receipt_trust_status, handoff.decision_reason, now
                )
            )

    def get_handoff(self, handoff_id: str) -> Optional[DeliveryHandoff]:
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT * FROM delivery_handoffs WHERE handoff_id = ?", (handoff_id,)).fetchone()
            if not row:
                return None
            return self._row_to_handoff(row)
        finally:
            conn.close()

    def get_handoff_by_idempotency_key(self, idempotency_key: str) -> Optional[DeliveryHandoff]:
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT * FROM delivery_handoffs WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if not row:
                return None
            return self._row_to_handoff(row)
        finally:
            conn.close()

    def get_handoff_for_run(self, run_id: str) -> Optional[DeliveryHandoff]:
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT * FROM delivery_handoffs WHERE run_id = ? ORDER BY created_at DESC LIMIT 1", (run_id,)).fetchone()
            if not row:
                return None
            return self._row_to_handoff(row)
        finally:
            conn.close()

    def list_handoffs(self, task_id: Optional[str] = None, status: Optional[str] = None) -> List[DeliveryHandoff]:
        conn = self.db.get_connection()
        try:
            query = "SELECT * FROM delivery_handoffs"
            params = []
            conditions = []
            if task_id:
                conditions.append("task_id = ?")
                params.append(task_id)
            if status:
                conditions.append("status = ?")
                params.append(status)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY created_at DESC"

            rows = conn.execute(query, params).fetchall()
            return [self._row_to_handoff(row) for row in rows]
        finally:
            conn.close()

    def save_receipt(self, receipt: DeliveryReceipt, receipt_raw: str, receipt_sha256: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT receipt_sha256,receipt_json FROM delivery_receipts WHERE external_receipt_id=?",(receipt.external_receipt_id,)).fetchone()
            if existing:
                if existing[0] != receipt_sha256 or existing[1] != receipt_raw:
                    raise ValidationError("Receipt evidence is immutable")
                return
            conn.execute(
                """
                INSERT INTO delivery_receipts (
                    external_receipt_id, handoff_id, idempotency_key, delivery_request_sha256,
                    status, occurred_at, external_message_ref, error_json, proof_json,
                    receipt_json, receipt_sha256, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.external_receipt_id, receipt.handoff_id, receipt.idempotency_key,
                    receipt.delivery_request_sha256, receipt.status, receipt.occurred_at,
                    receipt.external_message_ref, json.dumps(receipt.error) if receipt.error else None,
                    json.dumps(receipt.proof), receipt_raw, receipt_sha256, now
                )
            )

    def get_receipt(self, external_receipt_id: str) -> Optional[DeliveryReceipt]:
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT * FROM delivery_receipts WHERE external_receipt_id = ?", (external_receipt_id,)).fetchone()
            if not row:
                return None
            return DeliveryReceipt(
                external_receipt_id=row["external_receipt_id"],
                handoff_id=row["handoff_id"],
                idempotency_key=row["idempotency_key"],
                delivery_request_sha256=row["delivery_request_sha256"],
                status=row["status"],
                occurred_at=row["occurred_at"],
                proof=json.loads(row["proof_json"]),
                external_message_ref=row["external_message_ref"],
                error=json.loads(row["error_json"]) if row["error_json"] else None
            )
        finally:
            conn.close()

    def get_receipt_for_handoff(self, handoff_id: str) -> Optional[DeliveryReceipt]:
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT * FROM delivery_receipts WHERE handoff_id = ? ORDER BY imported_at DESC LIMIT 1", (handoff_id,)).fetchone()
            if not row:
                return None
            return DeliveryReceipt(
                external_receipt_id=row["external_receipt_id"],
                handoff_id=row["handoff_id"],
                idempotency_key=row["idempotency_key"],
                delivery_request_sha256=row["delivery_request_sha256"],
                status=row["status"],
                occurred_at=row["occurred_at"],
                proof=json.loads(row["proof_json"]),
                external_message_ref=row["external_message_ref"],
                error=json.loads(row["error_json"]) if row["error_json"] else None
            )
        finally:
            conn.close()

    def _row_to_handoff(self, row: Any) -> DeliveryHandoff:
        return DeliveryHandoff(
            handoff_id=row["handoff_id"],
            idempotency_key=row["idempotency_key"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            task_version_hash=row["task_version_hash"],
            message_revision=row["message_revision"],
            message_type=row["message_type"],
            recipient_group_id=row["recipient_group_id"],
            mode=row["mode"],
            status=row["status"],
            delivery_request=json.loads(row["delivery_request_json"]),
            delivery_request_sha256=row["delivery_request_sha256"],
            published_at=row["published_at"],
            external_receipt_id=row["external_receipt_id"],
            acknowledged_at=row["acknowledged_at"],
            external_delivery_status=row["external_delivery_status"],
            receipt_sha256=row["receipt_sha256"],
            receipt_trust_status=row["receipt_trust_status"],
            decision_reason=row["decision_reason"]
        )


class StateRepository:
    def __init__(self, db: Database):
        self.db = db

    def save_reported_item(self, item: ReportedItem) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO reported_items (
                    task_id, entity_key, content_fingerprint, run_id, handoff_id, reported_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (item.task_id, item.entity_key, item.content_fingerprint, item.run_id, item.handoff_id, item.reported_at)
            )

    def is_reported_item_unchanged(self, task_id: str, entity_key: str, content_fingerprint: str) -> bool:
        conn = self.db.get_connection()
        try:
            row = conn.execute(
                """
                SELECT 1 FROM reported_items r JOIN delivery_handoffs h ON h.handoff_id=r.handoff_id
                WHERE r.task_id = ? AND r.entity_key = ? AND r.content_fingerprint = ?
                  AND h.status='smtp_accepted' AND h.receipt_trust_status='verified_local_smtp'
                """,
                (task_id, entity_key, content_fingerprint)
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def get_reported_items(self, task_id: str) -> List[ReportedItem]:
        conn = self.db.get_connection()
        try:
            rows = conn.execute("SELECT * FROM reported_items WHERE task_id = ? ORDER BY reported_at DESC", (task_id,)).fetchall()
            return [
                ReportedItem(
                    task_id=row["task_id"],
                    entity_key=row["entity_key"],
                    content_fingerprint=row["content_fingerprint"],
                    run_id=row["run_id"],
                    handoff_id=row["handoff_id"],
                    reported_at=row["reported_at"]
                )
                for row in rows
            ]
        finally:
            conn.close()

    def save_audit_event(self, event: AuditEvent) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (
                    entity_type, entity_id, event_type, details_json, actor, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event.entity_type, event.entity_id, event.event_type, json.dumps(event.details), event.actor, event.occurred_at)
            )

    def list_audit_events(self, entity_type: Optional[str] = None, entity_id: Optional[str] = None, limit: int = 100) -> List[AuditEvent]:
        conn = self.db.get_connection()
        try:
            query = "SELECT * FROM audit_events"
            params = []
            conditions = []
            if entity_type:
                conditions.append("entity_type = ?")
                params.append(entity_type)
            if entity_id:
                conditions.append("entity_id = ?")
                params.append(entity_id)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY event_id DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [
                AuditEvent(
                    event_id=row["event_id"],
                    entity_type=row["entity_type"],
                    entity_id=row["entity_id"],
                    event_type=row["event_type"],
                    details=json.loads(row["details_json"]),
                    actor=row["actor"],
                    occurred_at=row["occurred_at"]
                )
                for row in rows
            ]
        finally:
            conn.close()
