"""Bind approved artifact bytes and record associations to one composition revision."""

import hashlib
import re
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator, FormatChecker

from researchops.domain.models import CompositionInput
from researchops.engine.archive import canonical_json
from researchops.errors import DeliveryError, ValidationError
from researchops.strict_json import strict_json_loads


def _identity(comp_input):
    return {key: getattr(comp_input, key) for key in (
        "task_id", "run_id", "task_version_hash", "composition_revision")}


def _digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def validate_acquisition_references(references):
    if (not isinstance(references, list) or not 1 <= len(references) <= 64
            or any(not isinstance(reference, dict) or set(reference) != {"acquisition_id", "source_sha256"}
                or not isinstance(reference["acquisition_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", reference["acquisition_id"])
                or not isinstance(reference["source_sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", reference["source_sha256"]) for reference in references)
            or len({reference["acquisition_id"] for reference in references}) != len(references)):
        raise ValidationError("Artifact acquisition provenance is invalid")


def validate_artifact_contract(comp_input):
    """Validate controller decisions; never infer product association from filenames."""
    if comp_input.schema_version != 4:
        return
    report = comp_input.artifact_report
    if not isinstance(report, dict) or any(report.get(k) != v for k, v in _identity(comp_input).items()):
        raise ValidationError("Artifact report belongs to another run or composition revision")
    if "acquisition_evidence" in report:
        evidence = report["acquisition_evidence"]
        if (not isinstance(evidence, dict) or set(evidence) != {"run_id", "attempt", "ledger_sha256"}
                or not isinstance(evidence["run_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}", evidence["run_id"])
                or type(evidence["attempt"]) is not int or evidence["attempt"] < 1
                or not isinstance(evidence["ledger_sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", evidence["ledger_sha256"])):
            raise ValidationError("Artifact acquisition ledger binding is invalid")
    known = {record["record_id"] for record in comp_input.reportable_records}
    entries, paths, selected = {}, set(), {}
    for entry in report["entries"]:
        artifact_id, path = entry["artifact_id"], entry["path"]
        parts = PurePosixPath(path)
        if (parts.is_absolute() or "\\" in path or any(p in ("", ".", "..") for p in path.split("/")) or
                any(ord(c) < 32 or ord(c) == 127 for c in path)):
            raise ValidationError("Unsafe artifact report path")
        if artifact_id in entries or path in paths:
            raise ValidationError("Duplicate artifact report identity or path")
        entries[artifact_id] = entry
        paths.add(path)
        references = entry.get("derived_from")
        if references is not None:
            validate_acquisition_references(references)
        requested, remaining = entry["requested_record_ids"], entry["record_ids"]
        if entry["scope"] == "record":
            if not requested or remaining != [rid for rid in requested if rid in known]:
                raise ValidationError("Artifact record associations differ from reportable records")
        elif requested or remaining:
            raise ValidationError("Run or legacy artifact scope cannot carry product associations")
        available = entry["status"] == "available"
        if available and (entry["sha256"] is None or entry["size_bytes"] is None):
            raise ValidationError("Available artifact has no verified hash or size")
        source = entry["source"] or {}
        if (source and entry["scope"] != "record") or (
                entry["scope"] == "run" and entry["role"] not in ("attachment", "evidence")):
            raise ValidationError("Source artifacts require record scope; run artifacts must be local attachments or evidence")
        if source.get("kind") == "cardrag_pdf" and entry["role"] != "attachment":
            raise ValidationError("Source PDF must be an attachment")
        if available and source.get("kind") == "cardrag_pdf" and any(
                entry[key] != source[key] for key in ("sha256", "size_bytes")):
            raise ValidationError("Available PDF differs from the requested source descriptor")
        if entry["include_in_compose"]:
            if (not available or entry["role"] not in ("attachment", "inline_image") or
                    (entry["scope"] == "record" and not remaining)):
                raise ValidationError("Unavailable or excluded artifact cannot enter composition")
            selected[artifact_id] = entry
    observed = set()
    for role, artifacts in (("inline_image", comp_input.inline_artifacts), ("attachment", comp_input.attachments)):
        for artifact in artifacts:
            aid = artifact["artifact_id"]
            entry = selected.get(aid)
            if aid in observed or not entry or entry["role"] != role:
                raise ValidationError("Composition media do not match approved artifact decisions")
            observed.add(aid)
            if (any(artifact[key] != entry[key] for key in ("path", "sha256", "size_bytes", "record_ids")) or
                    artifact.get("source") != entry["source"] or artifact.get("derived_from") != entry.get("derived_from")):
                raise ValidationError("Composition artifact metadata or associations changed")
            source = entry["source"] or {}
            if source.get("kind") == "cardrag_pdf" and artifact["mime_type"] != "application/pdf":
                raise ValidationError("Source PDF MIME differs from its sealed descriptor")
    if observed != set(selected):
        raise ValidationError("Composition omits an approved artifact")


def composition_binding(comp_input, result_bytes, file_evidence):
    """Original result stays untouched; this controller record authenticates its files."""
    return {"schema_version": 1, **_identity(comp_input),
        "composition_input_sha256": _digest(comp_input.to_dict()),
        "composition_result_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "artifact_report_sha256": _digest(comp_input.artifact_report), "files": file_evidence}


def require_stored_composition(db, task_def, run_id, version_hash, revision, group_id, *,
                               schemas_dir, comp_input=None, comp_result=None, raw_result=None,
                               file_root=None, archive_root=None, request=None):
    """Require v4 evidence in both routing modes; v2/v3 retain their original contract."""
    from researchops.delivery.package import read_file
    from researchops.delivery.recipient_routing import resolve_recipient
    from researchops.engine.composition_input import CompositionInputBuilder
    from researchops.storage.repositories import RunRepository
    repo = RunRepository(db)
    record = repo.get_composition_input_record(run_id, revision)
    result_record = repo.get_composition_result_record(run_id, revision)
    required = bool(request and request.get("composition_binding_sha256")) or bool(
        result_record and result_record.get("composition_binding"))
    if comp_input is None and not required and (not record or record["input"].get("schema_version") != 4):
        return None
    if comp_input is not None and comp_input.schema_version != 4:
        if required or (record and record["input"].get("schema_version") == 4):
            raise DeliveryError("Cannot substitute a legacy input for a stored v4 composition")
        return None
    run = repo.get_run(run_id)
    result = repo.get_composition_result(run_id, revision)
    if (type(revision) is not int or revision < 1 or not run or not record or not result or not result_record or
            run.task_id != task_def.id or run.task_version_hash != version_hash):
        raise DeliveryError("Artifact composition requires an exact stored run and revision")
    value = record["input"]
    schema = CompositionInputBuilder(schemas_dir).schema
    if not isinstance(value, dict) or any(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value)):
        raise DeliveryError("Stored artifact composition input schema is invalid")
    saved_input = CompositionInput(**value)
    identity = {"task_id": task_def.id, "run_id": run_id,
        "task_version_hash": version_hash, "composition_revision": revision}
    if (saved_input.schema_version != 4 or _identity(saved_input) != identity or
            record["task_id"] != task_def.id or record["task_version_hash"] != version_hash or
            result_record["task_id"] != task_def.id or result_record["revision"] != revision or
            result.recipient_group_id != group_id or _digest(value) != record["input_sha256"] or
            (comp_input is not None and comp_input.to_dict() != value) or
            (comp_result is not None and comp_result.to_dict() != result.to_dict())):
        raise DeliveryError("Artifact composition identity, input hash or result revision mismatch")
    try:
        validate_artifact_contract(saved_input)
    except (KeyError, TypeError, ValidationError) as exc:
        raise DeliveryError("Stored artifact associations are invalid") from exc
    parent = saved_input.artifact_report.get("derived_from")
    if parent:
        previous = repo.get_composition_input_record(parent["run_id"], parent["composition_revision"])
        previous_report = previous and previous["input"].get("artifact_report")
        if (not previous or previous["task_id"] != task_def.id or previous["task_version_hash"] != version_hash or
                _digest(previous["input"]) != previous["input_sha256"] or not isinstance(previous_report, dict) or
                _digest(previous_report) != parent["artifact_report_sha256"] or
                previous_report.get("entries") != saved_input.artifact_report["entries"]):
            raise DeliveryError("Compose-only artifact evidence does not match its stored parent revision")
    binding = result_record.get("composition_binding")
    if not isinstance(binding, dict) or any(binding.get(k) != v for k, v in identity.items()):
        raise DeliveryError("Stored artifact composition binding is missing or mismatched")
    if raw_result is None:
        original_root = archive_root if archive_root is not None else file_root
        if original_root is None:
            raise DeliveryError("Original composition result is required for artifact verification")
        raw_result = read_file(original_root, "composition-result.json", 1_000_000)
    try:
        data = strict_json_loads(raw_result)
        selected = resolve_recipient(data, saved_input, task_def)
        normalized = {key: val for key, val in data.items() if key != "recipient_group_name"}
        normalized["recipient_group_id"] = selected
    except (ValueError, TypeError, ValidationError) as exc:
        raise DeliveryError("Original artifact composition result is invalid") from exc
    if selected != group_id or normalized != result.to_dict():
        raise DeliveryError("Original composition result differs from its stored normalized result")
    artifacts = saved_input.inline_artifacts + saved_input.attachments
    expected_paths = [result.html_path, result.text_path, *(a["path"] for a in artifacts)]
    files = binding.get("files")
    if not isinstance(files, dict) or len(expected_paths) != len(set(expected_paths)) or set(files) != set(expected_paths):
        raise DeliveryError("Stored composition file inventory is incomplete")
    for artifact in artifacts:
        if files[artifact["path"]] != {k: artifact[k] for k in ("sha256", "size_bytes")}:
            raise DeliveryError("Artifact byte evidence differs from the sealed input")
    if binding != composition_binding(saved_input, raw_result, files):
        raise DeliveryError("Stored composition binding hashes do not match")
    if request is not None and request.get("composition_binding_sha256") != _digest(binding):
        raise DeliveryError("Delivery request is not bound to this artifact composition")
    if archive_root is not None:
        for name, expected in (("composition-input.json", value), ("artifact-report.json", saved_input.artifact_report),
                                ("composition-binding.json", binding)):
            if read_file(archive_root, name) != canonical_json(expected):
                raise DeliveryError("Archived artifact evidence differs from the stored revision")
    roots = [Path(root) for root in (file_root, archive_root) if root is not None]
    if not roots:
        raise DeliveryError("Composition artifact bytes are required for verification")
    for root in dict.fromkeys(roots):
        for path, evidence in files.items():
            content = read_file(root, path)
            if evidence != {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}:
                raise DeliveryError("Composition body or artifact bytes changed after validation")
    if request is not None:
        if (request["subject"] != result.subject or request["body"]["html"]["path"] != result.html_path or
                request["body"]["text"]["path"] != result.text_path):
            raise DeliveryError("Delivery body differs from the stored composition result")
        for info in request["body"].values():
            if {k: info[k] for k in ("sha256", "size_bytes")} != files[info["path"]]:
                raise DeliveryError("Delivery body hash differs from the validated composition")
        if request["attachments"] != delivery_artifacts(saved_input):
            raise DeliveryError("Delivery attachments differ from the approved artifact inventory")
    return binding


def delivery_artifacts(comp_input):
    """Project the sealed media inventory onto the existing SMTP package contract."""
    result = []
    for disposition, artifacts in (("inline", comp_input.inline_artifacts), ("attachment", comp_input.attachments)):
        for artifact in artifacts:
            result.append({"path": artifact["path"], "filename": artifact.get("filename") or Path(artifact["path"]).name,
                "media_type": artifact.get("mime_type", artifact.get("media_type", "application/octet-stream")),
                "size_bytes": artifact["size_bytes"], "sha256": artifact["sha256"], "disposition": disposition,
                **({"content_id": artifact["cid"]} if disposition == "inline" else {})})
    return result
