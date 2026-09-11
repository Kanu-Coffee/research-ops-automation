"""Run-scoped authorization for explicitly replayed, delivery-compatible tasks."""

import hashlib
import json
import re

from researchops.errors import DeliveryError
from researchops.strict_json import strict_json_loads


REPLAY_TRIGGERS = {"retry", "compose_only", "delivery_only"}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _delivery(conn, task_id, version_hash):
    row = conn.execute("SELECT task_id,definition_json FROM task_versions WHERE version_hash=?",
                       (version_hash,)).fetchone()
    if row is None or row["task_id"] != task_id:
        raise DeliveryError("Invalid delivery task version")
    value = strict_json_loads(row["definition_json"])
    delivery = value.get("delivery") if isinstance(value, dict) else None
    if not isinstance(delivery, dict) or delivery.get("mode") != "handoff":
        raise DeliveryError("Task version is not in live handoff mode")
    return delivery


def _status(conn, task_id):
    row = conn.execute("SELECT active_version_hash,delivery_mode FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None or not row["active_version_hash"]:
        raise DeliveryError("Delivery task has no active version")
    deleted = conn.execute("SELECT deleted_at FROM entity_catalog WHERE kind='task' AND legacy_key=?",
                           (task_id,)).fetchone()
    if deleted and deleted["deleted_at"] is not None:
        raise DeliveryError("Delivery task is deleted")
    if row["delivery_mode"] != "handoff":
        raise DeliveryError("Live mode is not enabled for this task")
    return row


def encode_delivery_authorization(value):
    keys = {"schema_version", "task_id", "source_task_version_hash", "active_task_version_hash",
            "delivery_contract_sha256"}
    if (not isinstance(value, dict) or set(value) != keys or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or not isinstance(value["task_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value["task_id"])
            or any(not isinstance(value[key], str) or not re.fullmatch(r"[a-f0-9]{64}", value[key])
                   for key in keys - {"schema_version", "task_id"})):
        raise DeliveryError("Invalid run delivery authorization")
    raw = _canonical(value)
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_delivery_authorization(conn, run, authorization):
    """Validate under the caller's transaction, including the active-version pin."""
    encode_delivery_authorization(authorization)
    if (run.trigger_type not in REPLAY_TRIGGERS or run.task_id != authorization["task_id"]
            or run.task_version_hash != authorization["source_task_version_hash"]):
        raise DeliveryError("Run does not match its delivery authorization")
    parent = conn.execute("SELECT task_id,task_version_hash FROM scheduled_runs WHERE run_id=?",
                          (run.parent_run_id,)).fetchone()
    if (parent is None or parent["task_id"] != run.task_id or parent["task_version_hash"] != run.task_version_hash):
        raise DeliveryError("Run delivery authorization requires an immutable replay parent")
    status = _status(conn, run.task_id)
    if status["active_version_hash"] != authorization["active_task_version_hash"]:
        raise DeliveryError("Task changed after this run's delivery authorization; review the current settings")
    source = _delivery(conn, run.task_id, run.task_version_hash)
    current = _delivery(conn, run.task_id, status["active_version_hash"])
    if (source != current or hashlib.sha256(_canonical(source).encode("utf-8")).hexdigest()
            != authorization["delivery_contract_sha256"]):
        raise DeliveryError("Original and current Task delivery settings differ; review delivery before retrying")


def build_delivery_authorization(settings, db, *, task_id, source_version_hash, trigger_type):
    """Build a pin without storing it; create_run stores it in its own transaction."""
    from researchops.delivery.smtp_config import load_delivery_config
    from researchops.delivery.policy import require_live
    conn = db.get_connection()
    try:
        status = _status(conn, task_id)
        source = _delivery(conn, task_id, source_version_hash)
        current = _delivery(conn, task_id, status["active_version_hash"])
        config = load_delivery_config(settings.paths.delivery_config_file)
        sender = source.get("sender_profile_id", "default")
        require_live(settings, config, sender)
        deleted = conn.execute("SELECT deleted_at FROM entity_catalog WHERE kind='sender' AND legacy_key=?",
                               (sender,)).fetchone()
        if deleted and deleted["deleted_at"] is not None:
            raise DeliveryError("Delivery sender is deleted")
        if status["active_version_hash"] == source_version_hash:
            return None
        if settings.environment != "production" or trigger_type not in REPLAY_TRIGGERS:
            raise DeliveryError("Delivery task version is no longer active")
        if source != current:
            raise DeliveryError("Original and current Task delivery settings differ; review delivery before retrying")
        return {"schema_version": 1, "task_id": task_id,
            "source_task_version_hash": source_version_hash,
            "active_task_version_hash": status["active_version_hash"],
            "delivery_contract_sha256": hashlib.sha256(_canonical(source).encode("utf-8")).hexdigest()}
    finally:
        conn.close()


def read_delivery_authorization(conn, run_id):
    row = conn.execute("SELECT authorization_json,authorization_sha256 FROM run_delivery_authorizations WHERE run_id=?",
                       (run_id,)).fetchone()
    if row is None:
        return None
    value = strict_json_loads(row["authorization_json"])
    raw, digest = encode_delivery_authorization(value)
    if raw != row["authorization_json"] or digest != row["authorization_sha256"]:
        raise DeliveryError("Run delivery authorization integrity mismatch")
    return value


def assert_delivery_version(conn, task_id, version_hash, *, run_id=None, production=False, system_alert=False):
    """One version gate for publication, worker preflight, and SMTP DATA."""
    status = _status(conn, task_id)
    if system_alert:
        # A research-message authorization does not authorize potentially changed
        # alert recipients or policies. Keep the original alert version rule.
        if status["active_version_hash"] != version_hash:
            raise DeliveryError("Delivery task version is no longer active")
        return
    authorization = read_delivery_authorization(conn, run_id) if run_id else None
    if authorization is None:
        if status["active_version_hash"] != version_hash:
            raise DeliveryError("Delivery task version is no longer active")
        return
    if not production:
        raise DeliveryError("Run-scoped delivery authorization requires production")
    row = conn.execute("""SELECT r.*,e.cancel_requested,e.force_dry_run FROM scheduled_runs r
        JOIN execution_controls e ON e.run_id=r.run_id WHERE r.run_id=?""", (run_id,)).fetchone()
    if (row is None or row["task_id"] != task_id or row["task_version_hash"] != version_hash
            or row["force_dry_run"] or row["cancel_requested"]):
        raise DeliveryError("This run is not authorized for live delivery")
    from types import SimpleNamespace
    validate_delivery_authorization(conn, SimpleNamespace(**dict(row)), authorization)


def require_run_delivery_authorization(settings, db, run):
    """Fail before a model call without requiring a not-yet-selected recipient."""
    from researchops.delivery.smtp_config import load_delivery_config
    from researchops.delivery.policy import require_live
    conn = db.get_connection()
    try:
        assert_delivery_version(conn, run.task_id, run.task_version_hash, run_id=run.run_id,
                                production=settings.environment == "production")
        controls = conn.execute("SELECT force_dry_run,cancel_requested FROM execution_controls WHERE run_id=?",
                                (run.run_id,)).fetchone()
        if controls is None or controls["force_dry_run"] or controls["cancel_requested"] or run.trigger_type == "candidate_dry_run":
            raise DeliveryError("This run is not authorized for live delivery")
        source = _delivery(conn, run.task_id, run.task_version_hash)
        sender = source.get("sender_profile_id", "default")
        deleted = conn.execute("SELECT deleted_at FROM entity_catalog WHERE kind='sender' AND legacy_key=?",
                               (sender,)).fetchone()
        if deleted and deleted["deleted_at"] is not None:
            raise DeliveryError("Delivery sender is deleted")
        require_live(settings, load_delivery_config(settings.paths.delivery_config_file), sender)
    finally:
        conn.close()
