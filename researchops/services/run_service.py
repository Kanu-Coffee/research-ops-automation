"""Run lifecycle management and execution service."""

import json
import hashlib
import uuid
import os
import stat
import re
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional
from jsonschema import Draft202012Validator, FormatChecker

from researchops.config import Settings
from researchops.domain.models import ScheduledRun
from researchops.domain.events import AuditEvent
from researchops.errors import NotFoundError, ValidationError, WorkspaceError, ResearchOpsError, DeliveryError
from researchops.engine.orchestrator import Orchestrator
from researchops.strict_json import strict_json_loads
from researchops.storage.repositories import (
    DeliveryRepository, RunRepository, StateRepository, TaskRepository
)
from researchops.workspace.security import assert_path_contained, read_safe_bytes, safe_file_info
from researchops.engine.execution_plan import build_execution_plan, resolve_task_stages, resolve_run_plan, normalize_stage


def _utc_timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            return None
        return parsed.isoformat()
    except (ValueError, OverflowError):
        return None


def _elapsed_ms(start, end):
    if not start or not end:
        return None
    elapsed = int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000)
    return elapsed if elapsed >= 0 else None


def _run_timing(run, now=None):
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    created, started, finished = (_utc_timestamp(getattr(run, key))
                                  for key in ("created_at", "started_at", "finished_at"))
    active = run.status in {"queued", "running", "awaiting_receipt"} and finished is None
    queue_active = active and run.status == "queued" and not started
    endpoint = finished or (observed if active else None)
    return {"created_at": created, "started_at": started, "finished_at": finished,
        "observed_at": observed, "active": active, "queue_active": queue_active,
        "end_missing": not active and not finished,
        "queue_duration_ms": _elapsed_ms(created, started or (observed if queue_active else None)),
        "duration_ms": _elapsed_ms(started, endpoint),
        "total_duration_ms": _elapsed_ms(created, endpoint)}


def _public_audit_event(event):
    """Only fixed event names and numeric counters belong in routine Run details."""
    from researchops.engine.run_timeline import SAFE_SUMMARY_KEYS
    event_types = {"run_enqueued", "run_cancel_requested", "run_retried", "compose_only_enqueued",
        "worker_claimed_lease", "stale_lease_blocked", "run_completed", "run_delivery_queued",
        "run_delivery_data_started"}
    details = event.get("details")
    safe = {}
    if isinstance(details, dict):
        for key, value in details.items():
            if (key in SAFE_SUMMARY_KEYS and type(value) is int and -(2**63) <= value < 2**63 and
                    (key == "exit_code" or value >= 0) and (key != "cleanup_verified" or value in (0, 1))):
                safe[key] = value
    event_type = event.get("event_type")
    return {"event_type": event_type if isinstance(event_type, str) and event_type in event_types else "other",
        "occurred_at": _utc_timestamp(event.get("occurred_at")), "details": safe}


class RunService:
    def __init__(
        self,
        settings: Settings,
        task_repo: TaskRepository,
        run_repo: RunRepository,
        delivery_repo: DeliveryRepository,
        state_repo: StateRepository,
        orchestrator: Orchestrator,
        model_catalog=None,
    ):
        self.settings = settings
        self.task_repo = task_repo
        self.run_repo = run_repo
        self.delivery_repo = delivery_repo
        self.state_repo = state_repo
        self.orchestrator = orchestrator
        self.model_catalog = model_catalog
        self._artifact_report_validator = None

    def enqueue_run(
        self,
        task_id: str,
        trigger_type: str = "manual",
        candidate_version_hash: Optional[str] = None,
        scheduled_for: Optional[str] = None,
        force_dry_run: Optional[bool] = None,
        request_key: Optional[str] = None,
    ) -> ScheduledRun:
        if candidate_version_hash:
            version = self.task_repo.get_version(candidate_version_hash)
            if not version or version.task_id != task_id:
                raise NotFoundError(f"Candidate version '{candidate_version_hash}' not found")
            version_hash = candidate_version_hash
        else:
            active_version = self.task_repo.get_active_version(task_id)
            if not active_version:
                raise NotFoundError(f"Task '{task_id}' has no active version to run")
            version = active_version
            version_hash = active_version.version_hash

        now = datetime.now(timezone.utc)
        run_id = f"run-{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"

        tz_str = version.definition.schedule.get("timezone", "Asia/Seoul") if version.definition.schedule else "Asia/Seoul"
        if tz_str != "Asia/Seoul":
            raise ValidationError("Create and validate a new Asia/Seoul task candidate before running")
        try:
            scheduled_at = datetime.fromisoformat(scheduled_for.replace("Z","+00:00")) if scheduled_for else now
        except ValueError as exc:
            raise ValidationError("scheduled_for must be an ISO 8601 timestamp") from exc
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=ZoneInfo("Asia/Seoul"))
        business_time = scheduled_at.astimezone(ZoneInfo("Asia/Seoul"))
        local_date = business_time.strftime("%Y-%m-%d")
        local_date_display = business_time.strftime("%Y.%m.%d")

        run = ScheduledRun(
            run_id=run_id,
            task_id=task_id,
            task_version_hash=version_hash,
            scheduled_for=scheduled_at.astimezone(timezone.utc).isoformat(),
            timezone=tz_str,

            local_date=local_date,
            local_date_display=local_date_display,
            trigger_type=trigger_type,
            status="queued",
            phase="queued",
            attempt=0,
            workspace_generation=1,
            created_at=now.isoformat()
        )

        self._validate_request_key(request_key)
        execution_plan = build_execution_plan(resolve_task_stages(version.definition),
            selection_source={"kind": "task_version", "task_version_hash": version_hash})
        saved = self.run_repo.create_run(run,force_dry_run=bool(force_dry_run or candidate_version_hash),
            request_key=request_key,request_payload={"task_id":task_id,"version_hash":version_hash,
                "trigger_type":trigger_type,"scheduled_for":scheduled_for,
                "force_dry_run":bool(force_dry_run or candidate_version_hash)},
            expected_active_version_hash=version_hash if not candidate_version_hash else None,
            execution_plan=execution_plan)
        if saved.run_id != run.run_id:
            return saved
        self.state_repo.save_audit_event(
            AuditEvent(
                entity_type="run",
                entity_id=run_id,
                event_type="run_enqueued",
                details={"task_id": task_id, "trigger": trigger_type, "version_hash": version_hash}
            )
        )
        return run

    def execute_run(self, run_id: str, force_dry_run: Optional[bool] = None) -> ScheduledRun:
        from researchops.services.worker import WorkerService
        return WorkerService(self.settings,self.task_repo,self.run_repo,self,self.state_repo,self.orchestrator.workspace_mgr).execute_run_id(run_id,force_dry_run=force_dry_run)

    def list_runs(self, task_id: Optional[str] = None, status: Optional[str] = None, limit: int = 50) -> List[ScheduledRun]:
        return self.run_repo.list_runs(task_id=task_id, status=status, limit=limit)

    def show_run(self, run_id: str) -> Dict[str, Any]:
        run = self.run_repo.get_run(run_id)
        if not run:
            raise NotFoundError(f"Run '{run_id}' not found")

        res_result = self.run_repo.get_research_result(run_id)
        comp_result = self.run_repo.get_composition_result(run_id)
        comp_input = self.run_repo.get_composition_input(run_id)
        handoff = self.delivery_repo.get_handoff_for_run(run_id)

        receipt = None
        if handoff and handoff.external_receipt_id:
            receipt = self.delivery_repo.get_receipt(handoff.external_receipt_id)
        snapshot = self.run_repo.get_run_event_snapshot(run_id)
        timeline = self._run_timeline(run, snapshot)
        execution_plan = self.get_execution_plan(run_id)
        composition_source = self._composition_source(run, execution_plan)
        default_scope = ("compose_only" if composition_source and
            (execution_plan["scope"] == "compose_only" or
             (run.status in {"failed", "timed_out", "needs_attention"} and comp_result is None)) else "full")
        if execution_plan["scope"] == "delivery_only":
            default_scope = "delivery_only"

        return {
            "run": {
                "run_id": run.run_id,
                "task_id": run.task_id,
                "status": run.status,
                "phase": run.phase,
                "attempt": run.attempt,
                "version_hash": run.task_version_hash,
                "local_date": run.local_date,
                "scheduled_for": run.scheduled_for,
                "created_at": run.created_at,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "error_message": run.error_message
            },
            "research": {
                "status": res_result.status,
                "summary": res_result.summary,
                "record_count": len(res_result.records),
                "warnings": res_result.warnings
            } if res_result else None,
            "composition_input": {
                "reportable_record_count": len(comp_input.get("reportable_records", []))
            } if comp_input else None,
            "composition": comp_result.to_dict() if comp_result else None,
            "handoff": {
                "handoff_id": handoff.handoff_id,
                "mode": handoff.mode,
                "status": handoff.status,
                "idempotency_key": handoff.idempotency_key,
                "recipient_group_id": handoff.recipient_group_id,
                "external_delivery_status": handoff.external_delivery_status,
                "published_at": handoff.published_at,
                "acknowledged_at": handoff.acknowledged_at
            } if handoff else None,
            "receipt": receipt.to_dict() if receipt else None,
            "execution_plan": execution_plan,
            "execution_settings": execution_plan["stages"],
            "retry_settings": {"default_scope": default_scope,
                "compose_available": composition_source is not None,
                "execution_settings": execution_plan["stages"], "source_composition": composition_source},
            "timeline": timeline,
            "audit_events": [_public_audit_event(event) for event in snapshot["audit_events"]],
            "audit_events_meta": {"total_count": snapshot["audit_total_count"],
                "shown_count": len(snapshot["audit_events"]),
                "omitted_count": max(0, snapshot["audit_total_count"] - len(snapshot["audit_events"]))},
            "mcp_audit": self.get_run_mcp_audit(run_id),
            "artifact_report": self.get_run_artifact_report(run_id),
            "response_diagnostics": self.get_run_response_diagnostics(run_id)
        }

    def get_run_status(self, run_id: str) -> Dict[str, Any]:
        """Poll the database without opening or hashing archived model artifacts."""
        run = self.run_repo.get_run(run_id)
        if run is None:
            raise NotFoundError("Run not found")
        snapshot = self.run_repo.get_run_event_snapshot(run_id, step_limit=0, audit_limit=0)
        return {"run_id": run.run_id, "task_id": run.task_id, "status": run.status,
            "phase": run.phase, "attempt": run.attempt, "version_hash": run.task_version_hash,
            "local_date": run.local_date, "scheduled_for": run.scheduled_for,
            "created_at": run.created_at, "started_at": run.started_at, "finished_at": run.finished_at,
            "error_message": run.error_message, "timing": _run_timing(run),
            "timeline_revision": self._timeline_revision(run, snapshot)}

    @staticmethod
    def _timeline_revision(run, snapshot):
        values = [snapshot["revision"], run.status, run.phase, run.attempt, run.started_at, run.finished_at]
        return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()[:24]

    def get_run_timeline(self, run_id: str, *, now=None) -> Dict[str, Any]:
        run = self.run_repo.get_run(run_id)
        if run is None:
            raise NotFoundError("Run not found")
        return self._run_timeline(run, self.run_repo.get_run_event_snapshot(run_id), now=now)

    def _run_timeline(self, run, snapshot, *, now=None):
        from researchops.engine.run_timeline import validate_step_event
        timing = _run_timing(run, now)
        steps, invalid = {}, 0
        for sequence, event in enumerate(snapshot["step_events"]):
            details = event.get("details")
            try:
                validate_step_event(event.get("event_type"), details)
            except (ValueError, TypeError, KeyError, ValidationError):
                invalid += 1
                continue
            step_id = details["step_id"]
            prior = steps.get(step_id)
            finished = event["event_type"] == "run_step_finished"
            if prior and (prior["completion_recorded"] or not finished or
                    any(prior[key] != details[key] for key in ("phase", "attempt", "started_at"))):
                invalid += 1
                continue
            steps[step_id] = {key: details[key] for key in
                ("step_id", "phase", "state", "started_at", "finished_at", "duration_ms", "attempt", "summary")}
            steps[step_id].update({"completion_recorded": finished,
                "active": not finished and timing["active"] and details["attempt"] == run.attempt,
                "observed_at": timing["observed_at"],
                "_sequence": prior["_sequence"] if prior else sequence,
                "_order": _utc_timestamp(details["started_at"]) or _utc_timestamp(event.get("occurred_at")) or ""})
        ordered = sorted(steps.values(), key=lambda step: (step["_order"], step["step_id"]))
        latest_step = max(ordered, key=lambda step: step["_sequence"]) if ordered else None
        omitted = max(0, snapshot["step_total_count"] - len(snapshot["step_events"]))
        for step in ordered:
            step.pop("_order")
            step.pop("_sequence")
            # A later milestone is evidence that an earlier unfinished record
            # cannot be treated as today's still-running work.
            if step is not latest_step or omitted:
                step["active"] = False
        return {"status": "partial" if invalid or omitted else "recorded" if ordered else "not_recorded",
            "revision": self._timeline_revision(run, snapshot), "run": timing, "steps": ordered,
            "event_count": snapshot["step_total_count"], "omitted_event_count": omitted,
            "invalid_event_count": invalid}

    def get_run_response_diagnostics(self, run_id: str) -> Dict[str, Any]:
        """Read response errors independently of MCP, without returning model text."""
        from researchops.runners.base import safe_response_diagnostic
        if not self.run_repo.get_run(run_id):
            raise NotFoundError("Run not found")
        unavailable = {"status": "unavailable", "errors": []}
        try:
            report = strict_json_loads(self.read_run_archive_file(run_id, "validation-report.json",
                max_bytes=4_000_000), max_bytes=4_000_000)
        except NotFoundError:
            return {"status": "not_recorded", "errors": []}
        except (OSError, ValueError, ValidationError, WorkspaceError):
            return unavailable
        if (not isinstance(report, dict) or not isinstance(report.get("executions", []), list) or
                len(report.get("executions", [])) > 100):
            return unavailable
        errors = []
        for execution in report.get("executions", []):
            if not isinstance(execution, dict):
                return unavailable
            raw = execution.get("response_diagnostic")
            if raw is None:
                continue
            diagnostic = safe_response_diagnostic(raw)
            if diagnostic is None:
                return unavailable
            invocation_stage = execution.get("stage")
            errors.append({"invocation_stage": invocation_stage if invocation_stage in ("research", "compose")
                else "unknown", **diagnostic})
        return {"status": "recorded" if errors else "not_recorded", "errors": errors}

    def get_run_artifact_report(self, run_id: str) -> Dict[str, Any]:
        """Verify controller evidence and expose only requested-file diagnostics.

        The recorded acquisition status and today's file availability are separate.
        No source URL, response body, arbitrary metadata, or raw error is returned.
        """
        run = self.run_repo.get_run(run_id)
        if not run:
            raise NotFoundError("Run not found")
        unavailable = {"status": "unavailable", "entries": []}
        try:
            raw = self.read_run_archive_file(run_id, "artifact-report.json", max_bytes=2_000_000)
        except NotFoundError:
            return {"status": "not_recorded", "entries": []}
        except (OSError, ValueError, ValidationError, WorkspaceError):
            return unavailable
        try:
            report = strict_json_loads(raw, max_bytes=2_000_000)
            if self._artifact_report_validator is None:
                schema = json.loads((self.settings.paths.schemas_dir / "composition-input.schema.json").read_text())
                self._artifact_report_validator = Draft202012Validator(
                    {"$ref": "#/$defs/artifactReport", "$defs": schema["$defs"]}, format_checker=FormatChecker())
            if next(self._artifact_report_validator.iter_errors(report), None) is not None:
                return unavailable
            revision = self.run_repo.get_execution_controls(run_id)["composition_revision"]
            if (not isinstance(report, dict) or type(report.get("schema_version")) is not int or
                    report["schema_version"] != 1 or report.get("task_id") != run.task_id or
                    report.get("run_id") != run_id or report.get("task_version_hash") != run.task_version_hash or
                    type(report.get("composition_revision")) is not int or report["composition_revision"] != revision or
                    not isinstance(report.get("entries"), list) or len(report["entries"]) > 64):
                return unavailable
            manifest_raw = self.read_run_archive_file(run_id, "run-manifest.json", max_bytes=2_000_000)
            manifest = strict_json_loads(manifest_raw, max_bytes=2_000_000)
            index = strict_json_loads(self.read_run_archive_file(run_id, "artifact-manifest.json", max_bytes=2_000_000), max_bytes=2_000_000)
            if (not isinstance(manifest, dict) or manifest.get("run_id") != run_id or
                    manifest.get("task_id") != run.task_id or manifest.get("task_hash") != run.task_version_hash or
                    not isinstance(manifest.get("composition"), dict) or
                    manifest["composition"].get("revision", revision) != revision or
                    not isinstance(index, dict) or index.get("run_id") != run_id or index.get("task_id") != run.task_id or
                    index.get("run_manifest") != {"relative_path": "run-manifest.json", "sha256": hashlib.sha256(manifest_raw).hexdigest()} or
                    not isinstance(index.get("artifacts"), list) or len(index["artifacts"]) > 2000):
                return unavailable
            indexed = {}
            for item in index["artifacts"]:
                if (not isinstance(item, dict) or not isinstance(item.get("relative_path"), str) or
                        item["relative_path"] in indexed):
                    return unavailable
                indexed[item["relative_path"]] = item
            recorded_report = indexed.get("artifact-report.json", {})
            if (recorded_report.get("sha256") != hashlib.sha256(raw).hexdigest() or
                    recorded_report.get("size_bytes") != len(raw)):
                return unavailable
            safe_entries, seen, seen_paths = [], set(), set()
            for entry in report["entries"]:
                if (not self._valid_artifact_report_entry(entry) or entry["artifact_id"] in seen or
                        entry["path"] in seen_paths):
                    return unavailable
                seen.add(entry["artifact_id"])
                seen_paths.add(entry["path"])
                selected = {key: entry.get(key) for key in ("role", "scope", "status", "include_in_compose", "size_bytes")}
                if selected["role"] not in {"attachment", "inline_image", "evidence"}:
                    selected["role"] = "unknown"
                if "requested_scope" in entry:
                    requested_scope = entry["requested_scope"]
                    selected["requested_scope"] = (requested_scope if requested_scope in {"record", "run", "legacy"}
                        else None if requested_scope is None else "unknown")
                identifier = lambda value: value if re.fullmatch(r"[\w.:-]{1,200}", value) else "[비공개 식별자]"
                selected.update(artifact_id=identifier(entry["artifact_id"]),
                    requested_record_ids=[identifier(value) for value in entry["requested_record_ids"]],
                    record_ids=[identifier(value) for value in entry["record_ids"]],
                    declared_status=entry["declared_status"] if entry["declared_status"] in {
                        "ready", "pending", "unavailable", "failed", "error", "not_found", "unspecified"} else "unknown")
                selected["reason_code"] = self._artifact_reason_code(entry.get("reason_code"))
                selected.update(self._artifact_download_state(run, entry, indexed))
                safe_entries.append(selected)
            return {"status": "recorded", "entries": safe_entries}
        except (KeyError, TypeError, OSError, ValueError, NotFoundError, ValidationError, WorkspaceError):
            return unavailable

    @staticmethod
    def _valid_artifact_report_entry(entry) -> bool:
        if not isinstance(entry, dict):
            return False
        for key in ("artifact_id", "path", "role", "declared_status"):
            if not isinstance(entry.get(key), str) or not entry[key] or len(entry[key]) > 4096:
                return False
        path = PurePosixPath(entry["path"])
        if (path.is_absolute() or ".." in path.parts or path.as_posix() != entry["path"] or
                entry["path"] == "." or "\\" in entry["path"] or
                any(ord(char) < 32 or ord(char) == 127 for char in entry["path"])):
            return False
        if (entry.get("scope") not in {"record", "run", "legacy"} or
                entry.get("status") not in {"available", "failed", "excluded"} or
                type(entry.get("include_in_compose")) is not bool or type(entry.get("announce_missing")) is not bool or
                (entry.get("reason_code") is not None and not isinstance(entry["reason_code"], str))):
            return False
        if "requested_scope" in entry and entry["requested_scope"] is not None:
            requested_scope = entry["requested_scope"]
            if (not isinstance(requested_scope, str) or len(requested_scope) > 64 or
                    any(ord(char) < 32 or ord(char) == 127 for char in requested_scope)):
                return False
        for key in ("requested_record_ids", "record_ids"):
            values = entry.get(key)
            if (not isinstance(values, list) or len(values) > 1000 or
                    any(not isinstance(value, str) or not value or len(value) > 4096 for value in values) or
                    len(values) != len(set(values))):
                return False
        digest, size = entry.get("sha256"), entry.get("size_bytes")
        if digest is None and size is None:
            return entry["status"] != "available" and not entry["include_in_compose"]
        return (isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest) is not None and
                type(size) is int and 0 <= size <= 20_000_000 and
                (not entry["include_in_compose"] or entry["status"] == "available"))

    @staticmethod
    def _artifact_reason_code(value):
        # Only controller-defined codes can cross the ordinary diagnostic UI.
        allowed = {"available", "evidence_only", "records_excluded", "legacy_unscoped_dedupe",
            "network_disabled", "provider_unavailable", "local_file_missing", "invalid_media",
            "mime_type_mismatch", "source_hash_mismatch", "source_size_mismatch", "source_metadata_mismatch",
            "metadata_response_invalid", "source_unauthorized",
            "source_not_found", "source_http_error", "total_bytes_limit", "message_size_limit",
            "image_size_limit", "phase_timeout", "acquisition_interrupted", "cancelled", "unsafe_artifact",
            "malformed_artifact", "invalid_artifact_role", "invalid_artifact_scope", "invalid_artifact_role_scope",
            "artifact_limit_exceeded", "artifact_write_failed", "timeout",
            "destination_denied", "host_denied", "https_required", "port_denied", "invalid_url", "dns_failed",
            "connection_failed", "peer_mismatch", "tls_verification_failed", "transport_failed", "redirect_denied",
            "invalid_redirect", "too_many_redirects", "redirect_loop", "https_downgrade_denied", "body_too_large",
            "headers_too_large", "incomplete_response", "invalid_response", "invalid_framing",
            "unsupported_transfer_encoding", "unsupported_content_encoding", "too_many_chunks", "too_many_headers",
            "credential_unavailable", "provider_configuration_invalid", "worker_start_failed", "worker_failed",
            "worker_cleanup_failed"}
        return value if value in allowed else "unknown" if value else None

    def _artifact_download_state(self, run, entry, indexed):
        if entry.get("sha256") is None:
            return {"download_status": "not_archived", "download_path": None}
        outcome = "not_archived"
        root = self.settings.paths.run_archive_dir / run.task_id / run.run_id
        for name in (entry["path"], "research-artifacts/" + entry["path"]):
            index_entry = indexed.get(name)
            if not index_entry:
                continue
            if (index_entry.get("sha256") != entry["sha256"] or
                    index_entry.get("size_bytes") != entry["size_bytes"]):
                outcome = "changed"
                continue
            try:
                target = self.get_run_archive_file(run.run_id, name)
                if target is None:
                    outcome = "missing"
                    continue
                digest, size = safe_file_info(target, root, 20_000_000)
                if digest != entry["sha256"] or size != entry["size_bytes"]:
                    outcome = "changed"
                    continue
                return {"download_status": "verified", "download_path": name}
            except (OSError, ValueError, ValidationError, WorkspaceError):
                outcome = "unavailable"
        return {"download_status": outcome, "download_path": None}

    def get_run_mcp_audit(self, run_id: str) -> Dict[str, Any]:
        """Read only the safe MCP summary from the archived validation report.

        Old runs remain readable. No native log parsing, auth probing, or raw
        server arguments/output are added to the routine run query response.
        """
        try:
            report = strict_json_loads(self.read_run_archive_file(run_id, "validation-report.json",
                max_bytes=4_000_000), max_bytes=4_000_000)
        except NotFoundError:
            return {"status": "not_recorded", "phases": []}
        except (OSError, ValueError, ValidationError, WorkspaceError):
            return {"status": "unavailable", "phases": []}
        if not isinstance(report, dict) or not isinstance(report.get("executions", []), list):
            return {"status": "unavailable", "phases": []}

        def identifier(value):
            return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) else "[redacted]"

        phases = []
        for execution in report.get("executions", [])[:100]:
            if not isinstance(execution, dict) or not isinstance(execution.get("isolation"), dict):
                continue
            isolation = execution["isolation"]
            mcp = isolation.get("mcp")
            if not isinstance(mcp, dict):
                continue
            tools = isolation.get("mcp_tools", [])
            if not isinstance(tools, list):
                return {"status": "unavailable", "phases": []}
            summaries = []
            for tool in tools[:500]:
                if not isinstance(tool, dict):
                    continue
                status = tool.get("status")
                if not isinstance(status, str) or status not in {"succeeded", "failed", "denied", "unverified"}:
                    status = "unverified"
                output_verified = tool.get("output_verified") is True
                success = tool.get("success") is True
                if status == "succeeded" and not (output_verified and success):
                    status = "unverified"
                error_code = tool.get("error_code")
                if not isinstance(error_code, str) or error_code not in {"MCP_TOOL_ERROR", "MCP_PROTOCOL_ERROR", "MCP_PERMISSION_DENIED",
                                      "MCP_OUTPUT_UNVERIFIED", "MCP_EVIDENCE_INVALID"}:
                    error_code = None
                summaries.append({"server": identifier(tool.get("server")), "tool": identifier(tool.get("tool")),
                    "status": status, "error_code": error_code, "success": status == "succeeded",
                    "provider_reported_success": tool.get("provider_reported_success") is True,
                    "output_verified": output_verified})
                if type(tool.get("event_bytes")) is int and tool["event_bytes"] >= 0:
                    summaries[-1]["event_bytes"] = tool["event_bytes"]
                if tool.get("content_truncated") is True:
                    summaries[-1]["content_truncated"] = True
                from researchops.runners.mcp_audit import safe_tool_timing
                summaries[-1].update(safe_tool_timing(tool))
            provider = mcp.get("provider")
            stage = execution.get("stage")
            raw_diagnostics = isolation.get("trace_diagnostics")
            diagnostics = None
            if isinstance(raw_diagnostics, dict):
                diagnostics = {key: raw_diagnostics.get(key) is True for key in
                    ("complete", "terminal_observed", "model_activity_observed")}
                for key in ("parsed_events", "parsed_bytes", "received_stdout_bytes", "unparsed_bytes",
                            "largest_event_bytes", "completed_tool_count", "completed_mcp_count", "pending_tool_count"):
                    value = raw_diagnostics.get(key)
                    if type(value) is int and value >= 0:
                        diagnostics[key] = value
                reason = raw_diagnostics.get("termination_reason")
                diagnostics["termination_reason"] = identifier(reason) if reason else None
            elif isolation.get("trace_validation_error"):
                diagnostics = {"complete": False, "terminal_observed": False,
                               "model_activity_observed": False, "legacy_diagnostics_missing": True}
            count = isolation.get("mcp_tool_count", len(tools))
            count = count if type(count) is int and count >= len(tools) else len(tools)
            phases.append({"provider": provider if isinstance(provider, str) and provider in {"codex_exec", "antigravity_exec"} else "unknown",
                "stage": stage if isinstance(stage, str) and stage in {"research", "compose"} else "unknown",
                "tools": summaries, "omitted_tool_count": max(0, count - len(summaries)),
                **({"trace_diagnostics": diagnostics} if diagnostics is not None else {})})
        return {"status": "recorded" if phases else "not_recorded", "phases": phases}

    def get_run_artifacts(self, run_id: str) -> List[Dict[str, Any]]:
        run = self.run_repo.get_run(run_id)
        if not run:
            raise NotFoundError(f"Run '{run_id}' not found")

        archive_dir = self.settings.paths.run_archive_dir / run.task_id / run.run_id
        assert_path_contained(archive_dir, self.settings.paths.run_archive_dir)
        if not archive_dir.exists():
            return []

        artifacts = []
        for directory,dirs,files in os.walk(archive_dir,followlinks=False):
            dirs.sort()
            for name in dirs:
                assert_path_contained(Path(directory)/name,archive_dir)
            for name in sorted(files):
                item=Path(directory)/name
                relative=item.relative_to(archive_dir).as_posix()
                safe=self.get_run_archive_file(run_id,relative)
                if safe is None:
                    continue
                artifacts.append({
                    "filename": relative,
                    "path": str(safe),
                    "size_bytes": safe.stat().st_size
                })
                if len(artifacts)>1000:
                    raise ValidationError("Archive artifact count exceeds listing limit")
        return artifacts

    def cancel_run(self, run_id: str) -> ScheduledRun:
        run = self.run_repo.get_run(run_id)
        if not run:
            raise NotFoundError(f"Run '{run_id}' not found")
        if run.status in ("succeeded", "failed", "cancelled"):
            return run

        self.run_repo.request_cancel(run_id)
        from researchops.delivery.queue import SmtpQueue
        SmtpQueue(self.run_repo.db).cancel_pending_for_run(run_id)
        self.state_repo.save_audit_event(
            AuditEvent(
                entity_type="run",
                entity_id=run_id,
                event_type="run_cancel_requested",
                details={"reason": "Operator cancel"}
            )
        )
        return self.run_repo.get_run(run_id)  # type: ignore

    def get_run_logs(self, run_id: str) -> str:
        run = self.run_repo.get_run(run_id)
        if not run:
            raise NotFoundError(f"Run '{run_id}' not found")
        names=["run.log"]+[f"logs/{phase}.{stream}" for phase in ("research","compose") for stream in ("stdout","stderr","events.json")]
        files=[(name,self.get_run_archive_file(run_id,name)) for name in names]
        available=[(name,path) for name,path in files if path is not None]
        if not available:
            return f"Status: {run.status}\nPhase: {run.phase}\nError: {run.error_message or 'none'}\n"
        remaining=2_000_000
        chunks=[]
        for name,path in available:
            prefix="" if len(available)==1 and name=="run.log" else f"\n[{name}]\n"
            remaining-=len(prefix.encode("utf-8"))
            if remaining<=0:
                break
            preview = min(remaining, self.settings.runner.trace_preview_bytes)
            if path.stat().st_size > preview:
                from researchops.workspace.security import open_safe_file
                with open_safe_file(path, path.parent, path.stat().st_size) as (stream, size):
                    first = stream.read(preview // 2)
                    stream.seek(max(0, size - preview // 2))
                    last = stream.read(preview // 2)
                content=(first.decode("utf-8", errors="replace") +
                    "\n[Log omitted in preview; download the archived file for complete captured bytes.]\n" +
                    last.decode("utf-8", errors="replace"))
            else:
                content=self.read_run_archive_file(run_id,name,max_bytes=remaining).decode("utf-8",errors="replace")
            bounded=content.encode("utf-8")[:remaining].decode("utf-8",errors="ignore")
            chunks.append(prefix+bounded)
            remaining-=len(bounded.encode("utf-8"))
        return "".join(chunks)

    def get_execution_plan(self, run_id: str) -> Dict[str, Any]:
        run = self.run_repo.get_run(run_id)
        if run is None:
            raise NotFoundError(f"Run '{run_id}' not found")
        version = self.task_repo.get_version(run.task_version_hash)
        if version is None or version.task_id != run.task_id:
            raise ValidationError("Run task version is unavailable")
        return resolve_run_plan(run, version.definition, self.run_repo)

    @staticmethod
    def _source_reference(record):
        return {"run_id": record["run_id"], "revision": record["revision"],
                "input_sha256": record["input_sha256"]}

    def _composition_source(self, run, plan):
        record = self.run_repo.get_composition_input_record(run.run_id)
        if record is not None:
            return self._source_reference(record)
        return plan["source_composition"] if plan["scope"] in {"compose_only", "delivery_only"} else None

    def _selected_stages(self, run, parent_plan, scope, execution_settings, selection_source):
        import copy
        stages = copy.deepcopy(parent_plan["stages"])
        supplied = execution_settings if execution_settings is not None else {}
        if not isinstance(supplied, dict) or set(supplied) - {"research", "compose"}:
            raise ValidationError("Invalid execution stage selection")
        if scope == "compose_only" and "research" in supplied and normalize_stage(supplied["research"]) != stages["research"]:
            raise ValidationError("A compose-only retry cannot change research settings")
        if selection_source is None:
            selection_source = {"kind": "manual"} if supplied else {"kind": "parent_run", "run_id": run.run_id}
        # Structural validation precedes catalog access and rejects arbitrary metadata.
        build_execution_plan(stages, selection_source=selection_source)
        legacy_stages = stages
        if selection_source.get("kind") == "task_version":
            version_hash = selection_source.get("task_version_hash")
            version = self.task_repo.get_version(version_hash) if version_hash else None
            if version is None or version.task_id != run.task_id:
                raise ValidationError("Selected Task settings snapshot is unavailable")
            legacy_stages = resolve_task_stages(version.definition)
        for stage, selected in supplied.items():
            selected = normalize_stage(selected)
            if selected == stages[stage]:
                continue
            if selected["type"] == "fake":
                raise ValidationError("Test runners cannot be selected for a retry")
            catalog = self.model_catalog
            if catalog is None:
                from researchops.services.model_catalog import ModelCatalogService
                catalog = ModelCatalogService(self.settings)
            try:
                stages[stage] = normalize_stage(catalog.validate_stage(selected, legacy=legacy_stages[stage]))
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
        return stages, selection_source

    def retry_run(self, run_id: str, *, request_key: Optional[str] = None, scope=None,
                  execution_settings=None, selection_source=None) -> ScheduledRun:
        if scope == "delivery_only" or (scope is None and self.get_execution_plan(run_id)["scope"] == "delivery_only"):
            if execution_settings or selection_source:
                raise ValidationError("이메일 전송 전용 실행에는 AI 설정을 변경할 수 없습니다.")
            return self.send_prepared_email(run_id, request_key=request_key)
        return self._enqueue_retry(run_id, request_key=request_key, scope=scope,
            execution_settings=execution_settings, selection_source=selection_source, operation="retry")

    def compose_only(self, run_id: str, *, request_key: Optional[str] = None,
                     execution_settings=None, selection_source=None) -> ScheduledRun:
        return self._enqueue_retry(run_id, request_key=request_key, scope="compose_only",
            execution_settings=execution_settings, selection_source=selection_source, operation="compose_only")

    def _enqueue_retry(self, run_id, *, request_key, scope, execution_settings, selection_source, operation):
        self._validate_request_key(request_key)
        old_run = self.run_repo.get_run(run_id)
        if old_run is None:
            raise NotFoundError(f"Run '{run_id}' not found")
        if old_run.status in {"queued", "running", "awaiting_receipt"}:
            raise ValidationError("A pending execution or delivery cannot be retried")
        parent_plan = self.get_execution_plan(run_id)
        scope = parent_plan["scope"] if scope is None else scope
        if scope not in {"full", "compose_only"}:
            raise ValidationError("Choose a full run or compose-only retry")
        supplied = execution_settings if execution_settings is not None else {}
        if not isinstance(supplied, dict) or set(supplied) - {"research", "compose"}:
            raise ValidationError("Invalid execution stage selection")
        requested_stages = {stage: normalize_stage(value) for stage, value in supplied.items()}
        requested_source = (selection_source if selection_source is not None else
                            {"kind": "manual"} if supplied else {"kind": "parent_run", "run_id": run_id})
        request_shape = {"operation": operation, "parent_run_id": run_id, "scope": scope,
            "execution_settings": requested_stages,
            "selection_source": build_execution_plan(parent_plan["stages"],
                selection_source=requested_source)["selection_source"]}
        if request_key:
            previous = self.run_repo.get_run_command(old_run.task_id, request_key)
            if previous and "execution_request" in previous["request"]:
                if previous["request"]["execution_request"] != request_shape:
                    raise ValidationError("Request key was already used with a different command")
                # Metadata refresh must not make an already accepted command
                # unreplayable or change the choices frozen in its original Run.
                return self.run_repo.get_run(previous["run_id"])
        source = self._composition_source(old_run, parent_plan) if scope == "compose_only" else None
        if scope == "compose_only" and source is None:
            raise ValidationError(f"Run '{run_id}' has no immutable composition input for compose-only")
        stages, provenance = self._selected_stages(old_run, parent_plan, scope, execution_settings, selection_source)
        plan = build_execution_plan(stages, scope=scope, source_composition=source, selection_source=provenance)
        controls = self.run_repo.get_execution_controls(run_id)
        revision = max(controls["composition_revision"], source["revision"]) + 1 if source else 1
        now = datetime.now(timezone.utc)
        new_run = ScheduledRun(run_id=f"run-{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:12]}",
            task_id=old_run.task_id, task_version_hash=old_run.task_version_hash,
            scheduled_for=old_run.scheduled_for, timezone=old_run.timezone,
            local_date=old_run.local_date, local_date_display=old_run.local_date_display,
            trigger_type="compose_only" if scope == "compose_only" else "retry",
            workspace_generation=old_run.workspace_generation, parent_run_id=run_id, created_at=now.isoformat())
        force_dry_run = bool(controls["force_dry_run"])
        authorization = self._delivery_authorization(new_run, force_dry_run=force_dry_run)
        payload = {"operation": operation, "parent_run_id": run_id,
            "task_version_hash": old_run.task_version_hash, "force_dry_run": force_dry_run,
            "execution_plan": plan, "composition_revision": revision, "execution_request": request_shape,
            "delivery_authorization": authorization}
        saved = self.run_repo.create_run(new_run, force_dry_run=force_dry_run, composition_revision=revision,
            request_key=request_key, request_payload=payload, execution_plan=plan,
            delivery_authorization=authorization)
        if saved.run_id != new_run.run_id:
            return saved
        self.state_repo.save_audit_event(AuditEvent(entity_type="run", entity_id=new_run.run_id,
            event_type="compose_only_enqueued" if scope == "compose_only" else "run_retried",
            details={"parent_run_id": run_id, "composition_revision": revision,
                     "execution_scope": scope, "execution_settings": stages, "selection_source": provenance}))
        return new_run

    def _delivery_authorization(self, run, *, force_dry_run=False):
        version = self.task_repo.get_version(run.task_version_hash)
        if force_dry_run or not version or version.definition.delivery.get("mode") != "handoff":
            return None
        from researchops.delivery.authorization import build_delivery_authorization
        try:
            return build_delivery_authorization(self.settings, self.run_repo.db, task_id=run.task_id,
                source_version_hash=run.task_version_hash, trigger_type=run.trigger_type)
        except DeliveryError as exc:
            raise ValidationError(str(exc)) from exc

    def _prepared_email(self, run_id):
        from researchops.services.prepared_delivery import load_prepared_message
        return load_prepared_message(self.settings, self.task_repo, self.run_repo, self.delivery_repo, run_id)

    def _check_prepared_delivery(self, run, prepared, *, check_family=True):
        from researchops.delivery.retry_guard import require_retry_family_clear
        from researchops.delivery.recipient_routing import require_current_recipient, routing_mode
        from researchops.delivery.smtp_config import load_delivery_config
        from dataclasses import replace
        controls = self.run_repo.get_execution_controls(run.run_id)
        if controls.get("cancel_requested") or controls.get("force_dry_run"):
            raise ValidationError("취소되었거나 시험 실행인 메일은 이 기능으로 발송할 수 없습니다.")
        if check_family:
            conn = self.run_repo.db.get_connection()
            try:
                require_retry_family_clear(conn, run.run_id, prepared_email=True)
            finally:
                conn.close()
        authorization = self._delivery_authorization(replace(run, trigger_type="delivery_only"))
        version = self.task_repo.get_version(run.task_version_hash)
        if version is None or version.definition.delivery.get("mode") != "handoff":
            raise ValidationError("원본 Task가 실제 이메일 발송 모드인 경우에만 사용할 수 있습니다.")
        config = load_delivery_config(self.settings.paths.delivery_config_file)
        require_current_recipient(self.run_repo.db, config, prepared.composition_result.recipient_group_id,
            catalog_required=routing_mode(version.definition) == "catalog_name", task_id=run.task_id)
        return authorization

    def prepared_email_status(self, run_id: str) -> Dict[str, Any]:
        """Read-only eligibility for validated messages that never reached SMTP."""
        run = self.run_repo.get_run(run_id)
        if run is None:
            raise NotFoundError(f"Run '{run_id}' not found")
        status = {"eligible": False, "block_reason": None, "source_run_id": None, "source_revision": None}
        if self.delivery_repo.get_handoff_for_run(run_id):
            status["block_reason"] = "이메일 전달 정보가 있습니다. 아래 전송 상태와 재시도 기능을 이용하세요."
            return status
        if run.status not in {"failed", "timed_out"}:
            status["block_reason"] = "실행 완료와 메일 검증 이후, 발송 전에 실패한 경우에 사용할 수 있습니다."
            return status
        try:
            prepared = self._prepared_email(run_id)
        except (ResearchOpsError, OSError, ValueError, KeyError):
            status["block_reason"] = "검증 완료된 미발송 메일이 없거나 보존된 원문을 확인할 수 없습니다."
            return status
        status.update(source_run_id=prepared.source_run.run_id,
                      source_revision=prepared.source_composition["revision"])
        try:
            self._check_prepared_delivery(run, prepared)
        except (ResearchOpsError, OSError, ValueError, KeyError):
            status["block_reason"] = "현재 발송 설정 또는 관련 실행의 전송 상태 때문에 발송할 수 없습니다. 발신 계정·수신자·실행 상태를 확인하세요."
            return status
        status["eligible"] = True
        return status

    def send_prepared_email(self, run_id: str, *, request_key: Optional[str] = None) -> ScheduledRun:
        """Queue a new delivery-only child with immutable source message evidence."""
        self._validate_request_key(request_key)
        old = self.run_repo.get_run(run_id)
        if old is None:
            raise NotFoundError(f"Run '{run_id}' not found")
        request = {"operation": "send_prepared_email", "parent_run_id": run_id}
        if request_key:
            previous = self.run_repo.get_run_command(old.task_id, request_key)
            if previous:
                if previous["request"].get("execution_request") != request:
                    raise ValidationError("Request key was already used with a different command")
                return self.run_repo.get_run(previous["run_id"])
        if old.status not in {"failed", "timed_out"}:
            raise ValidationError("발송 전에 실패한 실행의 작성 완료 메일만 보낼 수 있습니다.")
        prepared = self._prepared_email(run_id)
        # create_run checks the family after its idempotency lookup under the
        # same write transaction, so simultaneous identical clicks can replay.
        authorization = self._check_prepared_delivery(old, prepared, check_family=False)
        parent_plan = self.get_execution_plan(run_id)
        plan = build_execution_plan(parent_plan["stages"], scope="delivery_only",
            source_composition=prepared.source_composition, source_message=prepared.source_message,
            selection_source={"kind": "parent_run", "run_id": run_id})
        controls = self.run_repo.get_execution_controls(run_id)
        revision = max(controls["composition_revision"], prepared.source_composition["revision"]) + 1
        now = datetime.now(timezone.utc)
        child = ScheduledRun(run_id=f"run-{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:12]}",
            task_id=old.task_id, task_version_hash=old.task_version_hash,
            scheduled_for=old.scheduled_for, timezone=old.timezone,
            local_date=old.local_date, local_date_display=old.local_date_display,
            trigger_type="delivery_only", workspace_generation=old.workspace_generation,
            parent_run_id=run_id, created_at=now.isoformat())
        saved = self.run_repo.create_run(child, composition_revision=revision, request_key=request_key,
            request_payload={"execution_request": request, "execution_plan": plan,
                "composition_revision": revision, "delivery_authorization": authorization},
            execution_plan=plan, delivery_authorization=authorization)
        if saved.run_id == child.run_id:
            self.state_repo.save_audit_event(AuditEvent(entity_type="run", entity_id=child.run_id,
                event_type="prepared_email_enqueued", details={"parent_run_id": run_id,
                    "source_run_id": prepared.source_run.run_id, "composition_revision": revision,
                    "source_revision": prepared.source_composition["revision"], "model_calls": 0}))
        return saved

    @staticmethod
    def _validate_request_key(request_key):
        if request_key is not None and (not isinstance(request_key,str) or not 1<=len(request_key)<=128):
            raise ValidationError("request_key must contain 1-128 characters")

    def get_run_archive_file(self, run_id: str, filename: str) -> Optional[Path]:
        if not isinstance(filename,str) or not filename or "\\" in filename or any(ord(c)<32 or ord(c)==127 for c in filename):
            raise ValidationError(f"Invalid filename: {filename}")
        relative=PurePosixPath(filename)
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix()!=filename or filename==".":
            raise ValidationError(f"Invalid filename: {filename}")
        run = self.run_repo.get_run(run_id)
        if not run:
            raise NotFoundError(f"Run '{run_id}' not found")
        arch_dir = self.settings.paths.run_archive_dir / run.task_id / run.run_id
        assert_path_contained(arch_dir,self.settings.paths.run_archive_dir)
        target = assert_path_contained(arch_dir / filename, arch_dir)
        try:
            info=target.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
            raise ValidationError("Archive artifact must be a singly linked regular file")
        return target

    def read_run_archive_file(self,run_id: str,filename: str,*,max_bytes: int = 20_000_000) -> bytes:
        target=self.get_run_archive_file(run_id,filename)
        if target is None:
            raise NotFoundError(f"Archive file '{filename}' not found for run '{run_id}'")
        run=self.run_repo.get_run(run_id)
        root=self.settings.paths.run_archive_dir/run.task_id/run.run_id
        return read_safe_bytes(target,root,max_bytes)

    def get_run_events(self, run_id: str) -> List[AuditEvent]:
        snapshot = self.run_repo.get_run_event_snapshot(run_id, step_limit=0)
        return [AuditEvent(**event) for event in snapshot["audit_events"]]
