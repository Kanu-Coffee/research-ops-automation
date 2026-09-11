"""Controller-owned recipient catalog snapshots and exact name resolution.

Only public group identity and display names cross the worker boundary. Mapping
and catalog status are checked again against current controller configuration.
"""

import hashlib
import re

from researchops.domain.models import CompositionInput
from researchops.engine.archive import canonical_json
from researchops.errors import DeliveryError, ValidationError
from researchops.services.ownership import entity_owner, installation_owner

_DEFAULT_OWNER = object()


def routing_mode(task_def):
    mode = task_def.delivery.get("recipient_routing_mode", "legacy_ids")
    if mode not in ("legacy_ids", "catalog_name"):
        raise ValidationError("Unknown recipient routing mode")
    return mode


def catalog_recipient_snapshot(db, config, *, task_id=None, owner_user_id=_DEFAULT_OWNER):
    """Return one Task owner's active mapped groups, without mailbox addresses."""
    owner = (entity_owner(db, "task", task_id) if task_id is not None else
        installation_owner(db) if owner_user_id is _DEFAULT_OWNER else owner_user_id)
    from researchops.delivery.smtp_config import validate_address
    conn = db.get_connection()
    try:
        rows = conn.execute("""SELECT legacy_key,display_name FROM entity_catalog
            WHERE kind='recipient_group' AND deleted_at IS NULL AND owner_user_id IS ?
            ORDER BY entity_id""", (owner,)).fetchall()
    finally:
        conn.close()
    groups = []
    for row in rows:
        addresses = config.recipient_groups.get(row["legacy_key"])
        if not isinstance(addresses, list) or not addresses:
            continue
        try:
            if len(set(addresses)) != len(addresses):
                continue
            for address in addresses:
                validate_address(address)
        except (DeliveryError, TypeError):
            continue
        groups.append({"recipient_group_id": row["legacy_key"], "display_name": row["display_name"]})
    return groups


def validate_snapshot(groups):
    if not isinstance(groups, list) or not groups:
        raise ValidationError("Catalog recipient routing requires an active mapped recipient group")
    ids = []
    for group in groups:
        if (not isinstance(group, dict) or set(group) != {"recipient_group_id", "display_name"} or
                not isinstance(group["recipient_group_id"], str) or
                not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", group["recipient_group_id"]) or
                not isinstance(group["display_name"], str) or not group["display_name"].strip() or
                any(ord(char) < 32 or ord(char) == 127 for char in group["display_name"]) or
                group["display_name"] != group["display_name"].strip() or len(group["display_name"]) > 200):
            raise ValidationError("Invalid public recipient catalog snapshot")
        ids.append(group["recipient_group_id"])
    if len(set(ids)) != len(ids):
        raise ValidationError("Recipient catalog snapshot contains duplicate group IDs")


def resolve_recipient(data, comp_input, task_def):
    """Resolve one worker selection strictly within the immutable input."""
    if not isinstance(data, dict):
        raise ValidationError("Compose recipient selection must be an object")
    mode = routing_mode(task_def)
    if mode == "legacy_ids":
        allowed = task_def.delivery.get("allowed_recipient_group_ids", [])
        if (comp_input.schema_version not in (2, 4) or comp_input.recipient_routing_mode != "legacy_ids" or
                "recipient_group_name" in data or data.get("recipient_group_id") not in allowed or
                allowed != comp_input.allowed_recipient_group_ids):
            raise ValidationError("Recipient group does not match the sealed task allowlist")
        return data["recipient_group_id"]
    if (comp_input.schema_version not in (3, 4) or comp_input.recipient_routing_mode != mode or
            "recipient_group_id" in data):
        raise ValidationError("Recipient routing contract does not match the sealed task")
    validate_snapshot(comp_input.recipient_groups)
    if comp_input.allowed_recipient_group_ids != [g["recipient_group_id"] for g in comp_input.recipient_groups]:
        raise ValidationError("Recipient IDs do not match the immutable catalog snapshot")
    name = data.get("recipient_group_name")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("Compose must select exactly one recipient group name")
    matches = [g for g in comp_input.recipient_groups if g["display_name"] == name.strip()]
    if not matches:
        raise ValidationError("Selected recipient group name is not in the immutable catalog snapshot")
    if len(matches) != 1:
        raise ValidationError("Selected recipient group name is ambiguous in the immutable catalog snapshot")
    return matches[0]["recipient_group_id"]


def recipient_resolution(data, comp_input, task_def, raw_result):
    group_id = resolve_recipient(data, comp_input, task_def)
    return {"schema_version": 1, "task_id": comp_input.task_id, "run_id": comp_input.run_id,
        "task_version_hash": comp_input.task_version_hash,
        "composition_revision": comp_input.composition_revision,
        "recipient_group_name": data["recipient_group_name"], "recipient_group_id": group_id,
        "composition_input_sha256": hashlib.sha256(canonical_json(comp_input.to_dict())).hexdigest(),
        "composition_result_sha256": hashlib.sha256(raw_result).hexdigest()}


def require_stored_selection(db, task_def, run_id, version_hash, revision, group_id,
                             *, schemas_dir, comp_input=None, comp_result=None, raw_result=None):
    """Bind catalog routing to this run/version/revision, never latest-result lookup."""
    from researchops.storage.repositories import RunRepository
    if routing_mode(task_def) != "catalog_name":
        return None
    if not run_id or type(revision) is not int or revision < 1:
        raise DeliveryError("Catalog delivery requires an exact run and composition revision")
    repo = RunRepository(db)
    run = repo.get_run(run_id)
    if not run or run.task_id != task_def.id or run.task_version_hash != version_hash:
        raise DeliveryError("Recipient selection does not belong to this stored run")
    record = repo.get_composition_input_record(run_id, revision)
    saved_result = repo.get_composition_result(run_id, revision)
    result_record = repo.get_composition_result_record(run_id, revision)
    if not record or not saved_result or not result_record:
        raise DeliveryError("Stored recipient input/result is unavailable for this revision")
    snapshot = record["input"]
    from jsonschema import Draft202012Validator, FormatChecker
    from researchops.engine.composition_input import CompositionInputBuilder
    validator = Draft202012Validator(CompositionInputBuilder(schemas_dir).schema, format_checker=FormatChecker())
    if not isinstance(snapshot, dict) or any(validator.iter_errors(snapshot)):
        raise DeliveryError("Stored composition input schema is invalid")
    if (record["task_id"] != task_def.id or record["task_version_hash"] != version_hash or
            result_record["task_id"] != task_def.id or snapshot.get("task_id") != task_def.id or
            snapshot.get("run_id") != run_id or snapshot.get("task_version_hash") != version_hash or
            snapshot.get("composition_revision") != revision or saved_result.recipient_group_id != group_id):
        raise DeliveryError("Stored recipient selection has a different run, task version or revision")
    digest = hashlib.sha256(canonical_json(snapshot)).hexdigest()
    resolution = result_record.get("recipient_resolution")
    if not isinstance(resolution, dict) or not resolution or digest != record["input_sha256"]:
        raise DeliveryError("Stored recipient selection hash or resolution evidence is missing")
    if (comp_input is not None and comp_input.to_dict() != snapshot) or (
            comp_result is not None and comp_result.to_dict() != saved_result.to_dict()):
        raise DeliveryError("Composition does not match the exact stored input/result revision")
    try:
        selected = resolve_recipient({"recipient_group_name": resolution.get("recipient_group_name")},
                                     CompositionInput(**snapshot), task_def)
    except (TypeError, ValidationError) as exc:
        raise DeliveryError("Stored recipient resolution does not match the immutable snapshot") from exc
    expected = {"schema_version": 1, "task_id": task_def.id, "run_id": run_id,
        "task_version_hash": version_hash, "composition_revision": revision,
        "recipient_group_name": resolution.get("recipient_group_name"), "recipient_group_id": selected,
        "composition_input_sha256": digest,
        "composition_result_sha256": resolution.get("composition_result_sha256")}
    if (resolution != expected or selected != group_id or
            not isinstance(resolution.get("composition_result_sha256"), str) or
            not re.fullmatch(r"[a-f0-9]{64}", resolution["composition_result_sha256"])):
        raise DeliveryError("Stored recipient resolution identity/hash mismatch")
    if raw_result is not None:
        from researchops.strict_json import strict_json_loads
        try:
            data = strict_json_loads(raw_result)
            raw_id = resolve_recipient(data, CompositionInput(**snapshot), task_def)
            normalized = {key: value for key, value in data.items() if key != "recipient_group_name"}
            normalized["recipient_group_id"] = raw_id
        except (ValueError, TypeError, ValidationError) as exc:
            raise DeliveryError("Original composition result cannot verify the stored selection") from exc
        if (hashlib.sha256(raw_result).hexdigest() != resolution["composition_result_sha256"] or
                normalized != saved_result.to_dict()):
            raise DeliveryError("Original composition result differs from the stored selection")
    return resolution


def require_current_recipient(db, config, group_id, *, catalog_required=False, task_id=None):
    from researchops.delivery.smtp_config import validate_address
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT deleted_at,owner_user_id FROM entity_catalog WHERE kind='recipient_group' AND legacy_key=?",
                           (group_id,)).fetchone()
        if task_id is not None:
            owner = entity_owner(conn, "task", task_id)
            if (row and row["owner_user_id"] != owner) or (row is None and owner is not None):
                raise DeliveryError("Selected recipient group does not belong to this Task owner")
    finally:
        conn.close()
    if (catalog_required and row is None) or (row and row["deleted_at"] is not None):
        raise DeliveryError("Selected recipient group is missing or deleted")
    addresses = config.recipient_groups.get(group_id)
    if not isinstance(addresses, list) or not addresses:
        raise DeliveryError("Selected recipient group has no mailbox mapping")
    for address in addresses:
        validate_address(address)
