"""Read and authenticate already validated mail without invoking an AI worker."""

from dataclasses import dataclass
import hashlib

from researchops.domain.models import CompositionInput
from researchops.engine.archive import canonical_json
from researchops.engine.message_validator import MessageValidator
from researchops.errors import DeliveryError, ValidationError, WorkspaceError
from researchops.strict_json import strict_json_loads
from researchops.workspace.security import read_safe_bytes


@dataclass(frozen=True)
class PreparedMessage:
    source_run: object
    source_composition: dict
    source_message: dict
    composition_input: CompositionInput
    composition_result: object
    hashes: dict
    files: dict
    research: object


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def load_prepared_message(settings, task_repo, run_repo, delivery_repo, run_id, *, expected_source=None):
    try:
        return _load_prepared_message(settings, task_repo, run_repo, delivery_repo, run_id, expected_source=expected_source)
    except (ValidationError, DeliveryError):
        raise
    except (KeyError, TypeError, ValueError, AttributeError, OSError, WorkspaceError):
        raise ValidationError("PREPARED_MESSAGE_ARCHIVE_INVALID") from None


def _load_prepared_message(settings, task_repo, run_repo, delivery_repo, run_id, *, expected_source=None):
    """Validate DB, archive index, original contracts and exact message bytes.

    A delivery-only retry always follows its original message pin, including a
    child that failed before saving its own composition input. Family/live
    dispatch guards and current operational authorization belong to enqueue.
    """
    requested = run_repo.get_run(run_id)
    if requested is None:
        raise ValidationError("PREPARED_MESSAGE_RUN_UNAVAILABLE")
    plan = run_repo.get_execution_plan(run_id)
    source_ref = expected_source
    if plan and plan["scope"] == "delivery_only":
        if source_ref is not None and source_ref != plan["source_message"]:
            raise ValidationError("PREPARED_MESSAGE_SOURCE_CHANGED")
        source_ref = plan["source_message"]
    source_id = source_ref["run_id"] if source_ref is not None else run_id
    if source_id != run_id:
        ancestor, seen = requested, {run_id}
        for _ in range(128):
            if not ancestor.parent_run_id or ancestor.parent_run_id in seen:
                raise ValidationError("PREPARED_MESSAGE_SOURCE_NOT_ANCESTOR")
            ancestor = run_repo.get_run(ancestor.parent_run_id)
            if ancestor is None or ancestor.task_id != requested.task_id or ancestor.task_version_hash != requested.task_version_hash:
                raise ValidationError("PREPARED_MESSAGE_SOURCE_IDENTITY_MISMATCH")
            if ancestor.run_id == source_id:
                break
            seen.add(ancestor.run_id)
        else:
            raise ValidationError("PREPARED_MESSAGE_SOURCE_ANCESTRY_LIMIT")
    source_run = run_repo.get_run(source_id)
    if (source_run is None or source_run.task_id != requested.task_id or
            source_run.task_version_hash != requested.task_version_hash or
            source_run.local_date != requested.local_date or source_run.scheduled_for != requested.scheduled_for or
            source_run.timezone != requested.timezone or source_run.local_date_display != requested.local_date_display):
        raise ValidationError("PREPARED_MESSAGE_SOURCE_IDENTITY_MISMATCH")
    if source_run.status not in {"failed", "timed_out"}:
        raise ValidationError("PREPARED_MESSAGE_SOURCE_NOT_FAILED")
    controls = run_repo.get_execution_controls(source_id)
    if controls.get("cancel_requested") or controls.get("force_dry_run") or not controls.get("child_cleanup_verified"):
        raise ValidationError("PREPARED_MESSAGE_SOURCE_NOT_RECOVERABLE")
    if delivery_repo.get_handoff_for_run(source_id) is not None:
        raise ValidationError("PREPARED_MESSAGE_HANDOFF_ALREADY_EXISTS")
    version = task_repo.get_version(source_run.task_version_hash)
    if not version or version.task_id != source_run.task_id:
        raise ValidationError("PREPARED_MESSAGE_TASK_UNAVAILABLE")
    revision = source_ref["revision"] if source_ref else controls["composition_revision"]
    input_record = run_repo.get_composition_input_record(source_id, revision)
    result_record = run_repo.get_composition_result_record(source_id, revision)
    result = run_repo.get_composition_result(source_id, revision)
    research = run_repo.get_research_result(source_id)
    if (not input_record or not result_record or not result or not research or
            input_record["task_id"] != source_run.task_id or result_record["task_id"] != source_run.task_id or
            input_record["task_version_hash"] != source_run.task_version_hash):
        raise ValidationError("PREPARED_MESSAGE_VALIDATED_RESULT_UNAVAILABLE")
    root = settings.paths.run_archive_dir / source_run.task_id / source_id
    index_bytes = read_safe_bytes(root / "artifact-manifest.json", root, 2_000_000)
    index = strict_json_loads(index_bytes, max_bytes=2_000_000)
    manifest_bytes = read_safe_bytes(root / "run-manifest.json", root, 2_000_000)
    manifest = strict_json_loads(manifest_bytes, max_bytes=2_000_000)
    if (index.get("task_id") != source_run.task_id or index.get("run_id") != source_id or
            index.get("run_manifest") != {"relative_path": "run-manifest.json", "sha256": _digest(manifest_bytes)} or
            manifest.get("task_id") != source_run.task_id or manifest.get("run_id") != source_id or
            manifest.get("task_hash") != source_run.task_version_hash or
            manifest.get("scheduled_for") != source_run.scheduled_for or
            manifest.get("composition") != {"status": "validated", "revision": revision, **result.to_dict()}):
        raise ValidationError("PREPARED_MESSAGE_ARCHIVE_IDENTITY_MISMATCH")
    entries = index.get("artifacts")
    if not isinstance(entries, list) or len(entries) > 4096 or any(not isinstance(e, dict) for e in entries):
        raise ValidationError("PREPARED_MESSAGE_ARCHIVE_INDEX_INVALID")
    inventory = {entry.get("relative_path"): entry for entry in entries}
    if len(inventory) != len(entries):
        raise ValidationError("PREPARED_MESSAGE_ARCHIVE_INDEX_INVALID")
    files = {}

    def read(name, limit=None):
        entry = inventory.get(name)
        if entry is None:
            raise ValidationError("PREPARED_MESSAGE_FILE_NOT_INDEXED")
        raw = read_safe_bytes(root / name, root, limit or settings.delivery.max_message_bytes)
        if entry.get("size_bytes") != len(raw) or entry.get("sha256") != _digest(raw):
            raise ValidationError("PREPARED_MESSAGE_FILE_HASH_MISMATCH")
        files[name] = raw
        return raw

    validation = strict_json_loads(read("validation-report.json", 2_000_000), max_bytes=2_000_000)
    if validation.get("cleanup_verified") is not True:
        raise ValidationError("PREPARED_MESSAGE_CLEANUP_UNVERIFIED")
    input_bytes = read("composition-input.json", 20_000_000)
    if input_bytes != canonical_json(input_record["input"]) or _digest(input_bytes) != input_record["input_sha256"]:
        raise ValidationError("PREPARED_MESSAGE_INPUT_HASH_MISMATCH")
    comp_input = CompositionInput(**input_record["input"])
    if (comp_input.run_id != source_id or comp_input.task_id != source_run.task_id or
            comp_input.task_version_hash != source_run.task_version_hash or comp_input.composition_revision != revision):
        raise ValidationError("PREPARED_MESSAGE_INPUT_IDENTITY_MISMATCH")
    read("composition-result.json", 1_000_000)
    read(result.html_path)
    read(result.text_path)
    for artifact in comp_input.inline_artifacts + comp_input.attachments:
        read(artifact["path"])
    if read("result.json", 20_000_000) != research.raw_json.encode("utf-8"):
        raise ValidationError("PREPARED_MESSAGE_RESEARCH_HASH_MISMATCH")
    for name, value in (("artifact-report.json", comp_input.artifact_report if comp_input.schema_version == 4 else None),
                        ("composition-binding.json", result_record.get("composition_binding")),
                        ("recipient-resolution.json", result_record.get("recipient_resolution")),
                        ("research-context.json", getattr(comp_input, "research_context", None)),
                        ("delivery-history.json", getattr(comp_input, "delivery_history", None))):
        if value is not None and read(name, 2_000_000) != canonical_json(value):
            raise ValidationError("PREPARED_MESSAGE_SIDECAR_HASH_MISMATCH")
    schema_name = version.definition.output.get("composition_schema")
    task_schema = strict_json_loads(version.package_files[schema_name]) if schema_name in version.package_files else None
    validated, _, _, hashes = MessageValidator(settings.paths.schemas_dir, settings.delivery.max_message_bytes).validate_composition(
        root, comp_input, version.definition, task_schema)
    if validated.to_dict() != result.to_dict():
        raise ValidationError("PREPARED_MESSAGE_RESULT_CHANGED")
    for key in ("composition_binding", "recipient_resolution"):
        if hashes.get(key) != result_record.get(key):
            raise ValidationError("PREPARED_MESSAGE_STORED_BINDING_CHANGED")
    from researchops.delivery.artifact_integrity import require_stored_composition
    require_stored_composition(run_repo.db, version.definition, source_id, source_run.task_version_hash,
        revision, result.recipient_group_id, schemas_dir=settings.paths.schemas_dir,
        comp_input=comp_input, comp_result=result, file_root=root, archive_root=root)
    # Validation may read the files a second time. Both views must authenticate
    # the exact bytes returned for the child; never mix two archive versions.
    for key, name in (("composition_result", "composition-result.json"), ("html", result.html_path), ("text", result.text_path)):
        if hashes[key] != _digest(files[name]):
            raise ValidationError("PREPARED_MESSAGE_CHANGED_DURING_VALIDATION")
    source_composition = {"run_id": source_id, "revision": revision, "input_sha256": input_record["input_sha256"]}
    source_message = {**source_composition, "result_sha256": hashes["composition_result"],
        "html_sha256": hashes["html"], "text_sha256": hashes["text"],
        "files_sha256": {name: _digest(raw) for name, raw in sorted(files.items())},
        "artifact_manifest_sha256": _digest(index_bytes)}
    if source_ref is not None and source_ref != source_message:
        raise ValidationError("PREPARED_MESSAGE_SOURCE_CHANGED")
    return PreparedMessage(source_run, source_composition, source_message, comp_input, result, hashes, files, research)
