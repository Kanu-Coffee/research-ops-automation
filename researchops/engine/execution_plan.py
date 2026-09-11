"""Immutable runner choices, separate from sealed task and research inputs."""

import copy
import hashlib
import json
import re

from researchops.errors import ValidationError


STAGES = ("research", "compose")


def normalize_stage(value):
    if not isinstance(value, dict) or set(value) - {"type", "model", "reasoning_effort"}:
        raise ValidationError("Invalid execution stage settings")
    kind = value.get("type")
    if kind not in {"codex_exec", "antigravity_exec", "fake"}:
        raise ValidationError("Unsupported execution provider")
    result = {"type": kind}
    for name in ("model", "reasoning_effort"):
        item = value.get(name)
        if item == "":
            item = None
        if item is not None and (not isinstance(item, str) or not 1 <= len(item) <= 200
                                 or any(ord(c) < 32 or ord(c) == 127 for c in item)):
            raise ValidationError("Invalid execution model or effort")
        result[name] = item
    return result


def resolve_task_stages(task_def):
    runner = task_def.runner if hasattr(task_def, "runner") else task_def["runner"]
    base = {key: runner.get(key) for key in ("type", "model", "reasoning_effort")}
    configured = runner.get("stages", {})
    if not isinstance(configured, dict) or set(configured) - set(STAGES):
        raise ValidationError("Invalid task execution stages")
    return {stage: normalize_stage(configured.get(stage, base)) for stage in STAGES}


def build_execution_plan(stages, *, scope="full", source_composition=None, selection_source=None, source_message=None):
    if scope not in {"full", "compose_only", "delivery_only"} or not isinstance(stages, dict) or set(stages) != set(STAGES):
        raise ValidationError("Invalid execution scope or stages")
    source = copy.deepcopy(source_composition)
    if source is not None:
        if (not isinstance(source, dict) or set(source) != {"run_id", "revision", "input_sha256"}
                or not isinstance(source["run_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", source["run_id"])
                or type(source["revision"]) is not int or source["revision"] < 1
                or not isinstance(source["input_sha256"], str)
                or len(source["input_sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in source["input_sha256"])):
            raise ValidationError("Invalid immutable composition source")
    if scope == "full" and source is not None:
        raise ValidationError("A full execution cannot reuse composition input")
    message = None
    if scope == "delivery_only":
        message = copy.deepcopy(source_message)
        fields = {"run_id", "revision", "input_sha256", "result_sha256", "html_sha256", "text_sha256",
                  "files_sha256", "artifact_manifest_sha256"}
        if (source is None or not isinstance(message, dict) or set(message) != fields or
                any(message.get(key) != source[key] for key in ("run_id", "revision", "input_sha256"))):
            raise ValidationError("Invalid immutable prepared message source")
        for key in ("input_sha256", "result_sha256", "html_sha256", "text_sha256", "artifact_manifest_sha256"):
            if not isinstance(message[key], str) or not re.fullmatch(r"[0-9a-f]{64}", message[key]):
                raise ValidationError("Invalid prepared message hash")
        files = message["files_sha256"]
        from pathlib import PurePosixPath
        if not isinstance(files, dict) or not 3 <= len(files) <= 256:
            raise ValidationError("Invalid prepared message file inventory")
        for name, digest in files.items():
            if (not isinstance(name, str) or not name or len(name) > 1024 or "\\" in name or
                    any(ord(c) < 32 for c in name) or PurePosixPath(name).is_absolute() or
                    ".." in PurePosixPath(name).parts or PurePosixPath(name).as_posix() != name or
                    not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise ValidationError("Invalid prepared message file hash")
        if files.get("composition-result.json") != message["result_sha256"]:
            raise ValidationError("Prepared message result hash mismatch")
    elif source_message is not None:
        raise ValidationError("Only delivery-only executions can reuse a prepared message")
    provenance = copy.deepcopy(selection_source or {"kind": "task_version"})
    if (not isinstance(provenance, dict) or set(provenance) - {"kind", "run_id", "task_version_hash"}
            or provenance.get("kind") not in {"task_version", "parent_run", "manual", "legacy"}
            or any(not isinstance(value, str) or not 1 <= len(value) <= 200
                   or any(ord(c) < 32 or ord(c) == 127 for c in value)
                   for value in provenance.values())):
        raise ValidationError("Invalid execution selection source")
    result = {"schema_version": 1, "scope": scope,
            "stages": {stage: normalize_stage(stages[stage]) for stage in STAGES},
            "source_composition": source, "selection_source": provenance}
    if scope == "delivery_only":
        result["source_message"] = message
    return result


def validate_execution_plan(value):
    fields = {"schema_version", "scope", "stages", "source_composition", "selection_source"}
    if isinstance(value, dict) and value.get("scope") == "delivery_only":
        fields.add("source_message")
    if (not isinstance(value, dict) or set(value) != fields or type(value["schema_version"]) is not int
            or value["schema_version"] != 1):
        raise ValidationError("Invalid execution plan")
    return build_execution_plan(value["stages"], scope=value["scope"],
        source_composition=value["source_composition"], selection_source=value["selection_source"],
        source_message=value.get("source_message"))


def encode_execution_plan(value):
    raw = json.dumps(validate_execution_plan(value), sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False)
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resolve_run_plan(run, task_def, run_repo):
    saved = run_repo.get_execution_plan(run.run_id)
    if saved is not None:
        return saved
    if run.trigger_type == "delivery_only":
        raise ValidationError("Delivery-only execution requires its immutable source plan")
    scope = "compose_only" if run.trigger_type == "compose_only" else "full"
    source = None
    if scope == "compose_only" and run.parent_run_id:
        revision = run_repo.get_execution_controls(run.run_id)["composition_revision"] - 1
        record = run_repo.get_composition_input_record(run.parent_run_id, revision)
        if record:
            source = {"run_id": record["run_id"], "revision": record["revision"],
                      "input_sha256": record["input_sha256"]}
    return build_execution_plan(resolve_task_stages(task_def), scope=scope,
        source_composition=source, selection_source={"kind": "legacy", "task_version_hash": run.task_version_hash})
