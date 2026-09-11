"""Shared live delivery gates for publication and dispatch."""

from researchops.delivery.smtp_config import delivery_revision
from researchops.errors import DeliveryError, ValidationError
from researchops.storage.repositories import TaskRepository


def sender_for_task_version(db, task_id, version_hash):
    """Resolve controller-owned delivery routing from the immutable task version."""
    version = TaskRepository(db).get_version(version_hash)
    if not version or version.task_id != task_id:
        raise DeliveryError("Invalid delivery task version")
    return version.definition.delivery.get("sender_profile_id", "default")


def require_live(settings, config, sender_profile_id="default"):
    if settings.delivery.global_handoff_kill_switch is not False:
        raise DeliveryError("Global delivery kill switch is active")
    if config.enabled is not True:
        raise DeliveryError("Built-in SMTP delivery is disabled")
    config.validate()
    config.get_sender(sender_profile_id).validate(sending=True)


def require_approval(settings, db, config, task_id, version_hash, recipient_group_id, *, system_alert=False, run_id=None, message_revision=None):
    from researchops.services.ownership import require_task_delivery_ownership
    sender_profile_id = sender_for_task_version(db, task_id, version_hash)
    try:
        require_task_delivery_ownership(db, TaskRepository(db).get_version(version_hash).definition)
    except ValidationError as error:
        raise DeliveryError(str(error)) from error
    require_live(settings, config, sender_profile_id)
    repo = TaskRepository(db)
    status = repo.get_task_status(task_id) or {}
    conn = db.get_connection()
    try:
        from researchops.delivery.authorization import assert_delivery_version
        assert_delivery_version(conn, task_id, version_hash, run_id=run_id,
                                production=settings.environment == "production", system_alert=system_alert)
        row = conn.execute("SELECT deleted_at FROM entity_catalog WHERE kind='task' AND legacy_key=?", (task_id,)).fetchone()
        if row and row["deleted_at"] is not None:
            raise DeliveryError("Delivery task is deleted")
    finally:
        conn.close()
    # Publishing a production handoff task and saving its recipient mapping are
    # the operator's delivery instructions. No trial run or second approval is
    # needed. Development retains its explicitly approved diagnostic workflow.
    if settings.environment != "production" and (not status.get("delivery_approved") or
            status.get("approved_version_hash") != version_hash or
            status.get("approved_delivery_revision") != delivery_revision(config, sender_profile_id, db=db) or
            not status.get("approval_dry_run_id")):
        raise DeliveryError("Development live delivery requires version, recipient and sender approval with dry-run evidence")
    version = repo.get_version(version_hash)
    if not version or version.task_id != task_id:
        raise DeliveryError("Invalid delivery task version")
    if version.definition.delivery.get("mode", "dry_run") != "handoff":
        raise DeliveryError("Task version is not in live handoff mode")
    if status.get("delivery_mode") != "handoff":
        raise DeliveryError("Live mode is not enabled for this task")
    if run_id:
        from researchops.storage.repositories import RunRepository
        runs = RunRepository(db)
        run = runs.get_run(run_id)
        controls = runs.get_execution_controls(run_id)
        if (not run or run.task_id != task_id or run.task_version_hash != version_hash or
                run.trigger_type == "candidate_dry_run" or controls.get("force_dry_run") or
                (controls.get("cancel_requested") and not system_alert)):
            raise DeliveryError("This run is not authorized for live delivery")
    from researchops.delivery.recipient_routing import require_stored_selection, require_current_recipient, routing_mode
    mode = routing_mode(version.definition)
    allowed = version.definition.delivery.get("allowed_recipient_group_ids", [])
    if system_alert:
        allowed = [(version.definition.alerting or {}).get("recipient_group_id", "researchops-admins")]
    elif mode == "catalog_name":
        require_stored_selection(db, version.definition, run_id, version_hash, message_revision, recipient_group_id,
            schemas_dir=settings.paths.schemas_dir)
        allowed = [recipient_group_id]
    if recipient_group_id not in allowed:
        raise DeliveryError("Recipient group is not approved or has no mailbox mapping")
    require_current_recipient(db, config, recipient_group_id,
        catalog_required=mode == "catalog_name" and not system_alert, task_id=task_id)
