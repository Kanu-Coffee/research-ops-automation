"""Fenced, two-stage execution with immutable application-owned evidence."""
import hashlib
import copy
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from researchops.config import Settings
from researchops.domain.models import CompositionInput, ScheduledRun, TaskDefinition
from researchops.errors import ConcurrencyError, HardGateError, RunnerError, ValidationError, WorkspaceError
from researchops.engine.archive import RunArchive, canonical_json
from researchops.engine.technical_gate import TechnicalGateValidator
from researchops.engine.dedupe import ContentDeduplicator
from researchops.engine.composition_input import CompositionInputBuilder
from researchops.engine.run_timeline import RunTimeline
from researchops.engine.execution_plan import resolve_run_plan
from researchops.engine.message_validator import MessageValidator, validate_media
from researchops.runners.base import BaseRunner, RunnerInvocationContext
from researchops.runners.fake import FakeRunner
from researchops.runners.codex import CodexRunner
from researchops.runners.antigravity import AntigravityRunner
from researchops.workspace.security import read_safe_bytes
from researchops.delivery.handoff import HandoffPublisher
from researchops.delivery.system_alert import SystemAlertManager

logger = logging.getLogger(__name__)


class PolicyStop(Exception):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason
        super().__init__(reason)


class Orchestrator:
    def __init__(self, settings, task_repo, run_repo, delivery_repo, state_repo, workspace_mgr,
                 custom_runner=None, smtp_dispatcher=None):
        self.settings, self.task_repo, self.run_repo = settings, task_repo, run_repo
        self.delivery_repo, self.state_repo = delivery_repo, state_repo
        self.workspace_mgr, self.custom_runner = workspace_mgr, custom_runner
        self.technical_gate = TechnicalGateValidator(settings.paths.schemas_dir)
        self.deduplicator = ContentDeduplicator(state_repo)
        self.comp_input_builder = CompositionInputBuilder(settings.paths.schemas_dir)
        self.message_validator = MessageValidator(settings.paths.schemas_dir, settings.delivery.max_message_bytes)
        self.handoff_publisher = HandoffPublisher(settings, delivery_repo, state_repo)
        self.alert_mgr = SystemAlertManager(settings, delivery_repo, state_repo)

    def resolve_runner(self, task_def, stage_settings=None):
        if self.custom_runner is not None:
            return self.custom_runner
        kind = (stage_settings or task_def.runner).get("type", "fake")
        if kind == "fake":
            from researchops.config import bundled_path
            return FakeRunner(fixtures_dir=bundled_path(self.settings.paths.repo_root, "examples/tasks/software-releases/fixtures"))
        if kind == "codex_exec":
            return CodexRunner(codex_binary=self.settings.runner.codex_binary,
                               cgroup_root=self.settings.runner.cgroup_root)
        if kind == "antigravity_exec":
            return AntigravityRunner(antigravity_binary=self.settings.runner.antigravity_binary,
                                     cgroup_root=self.settings.runner.cgroup_root)
        raise RunnerError(f"Unsupported runner: {kind}")

    def execute_run(self, run_id, force_dry_run=None, *, fencing_token=None, cancel_event=None):
        self_claimed = fencing_token is None
        if self_claimed:
            claim = self.run_repo.claim_next_run("inline-worker", lease_seconds=3600,
                max_running=self.settings.runner.global_concurrency, run_id=run_id)
            if not claim:
                raise ConcurrencyError("Run is not queued or task execution is already claimed")
            run, lease = claim
            fencing_token = lease.fencing_token
        run = self.run_repo.get_run(run_id)
        timeline = RunTimeline(self.run_repo, run_id, run.attempt if run else 1, fencing_token)
        try:
            timeline.transition("preflight", coarse_phase="preflight", allow_cancel=True)
            return self._execute_owned_run(run_id,force_dry_run,fencing_token=fencing_token,
                cancel_event=cancel_event,timeline=timeline)
        except Exception as exc:
            # Includes version/archive errors before phase initialization.
            run=self.run_repo.get_run(run_id)
            if run and run.status=="running":
                try:
                    controls = self.run_repo.get_execution_controls(run_id)
                    state = "cancelled" if controls.get("cancel_requested") else "needs_attention" if isinstance(exc, ConcurrencyError) else "failed"
                    try:
                        timeline.finish(state)
                    except Exception:
                        logger.warning("Execution step completion could not be recorded for %s", run_id)
                    self.run_repo.update_run_status(run_id,state,"finalize",fencing_token=fencing_token,
                        finished_at=datetime.now(timezone.utc).isoformat(),error_message=f"Execution setup/finalization failed: {type(exc).__name__}")
                except ConcurrencyError:
                    self.run_repo.mark_stale_attention(run_id,fencing_token)
            raise
        finally:
            if self_claimed:
                run=self.run_repo.get_run(run_id)
                locked,_=self.workspace_mgr.is_locked(run.task_id)
                if not locked:
                    self.run_repo.mark_cleanup_verified(run_id,fencing_token)
                    self.run_repo.release_lease(run_id,fencing_token)

    def _execute_owned_run(self,run_id,force_dry_run=None,*,fencing_token,cancel_event=None,timeline):
        run = self.run_repo.get_run(run_id)
        if not run:
            raise ValidationError("Run no longer exists")
        self.run_repo.assert_run_owner(run_id,fencing_token,allow_cancel=True)
        version = self.task_repo.get_version(run.task_version_hash)
        if not version or version.task_id != run.task_id or version.definition.id != run.task_id:
            raise ValidationError("Run/task/version identity mismatch")
        task = version.definition
        controls = self.run_repo.get_execution_controls(run_id)
        execution_plan = resolve_run_plan(run, task, self.run_repo)
        delivery_only = execution_plan["scope"] == "delivery_only"
        compose_only = execution_plan["scope"] in {"compose_only", "delivery_only"}
        dry_run = bool(force_dry_run or controls.get("force_dry_run") or run.trigger_type == "candidate_dry_run")
        archive = RunArchive(self.settings, run)
        archive.json("execution-plan.json", execution_plan)
        delivery_authorization = self.run_repo.get_delivery_authorization(run_id)
        if delivery_authorization is not None:
            archive.json("delivery-authorization.json", delivery_authorization)
        archive.json("task-snapshot/definition.json", task.to_dict())
        for name, content in version.package_files.items():
            archive.write("task-snapshot/" + name, content.encode("utf-8"))

        research = comp_input = comp_result = handoff = None
        warnings, executions, validation_errors = [], [], []
        output_dirs = []
        cleanup_verified, locked = True, False
        final_status, failure = "failed", None
        artifact_report = None
        research_context = delivery_history = None
        prepared_message = None
        research_acquisition = None

        def guard():
            if cancel_event is not None and cancel_event.is_set():
                raise ConcurrencyError("Execution cancellation or lease loss")
            self.run_repo.assert_run_owner(run_id, fencing_token)

        def cancelled():
            try:
                guard()
                return False
            except ConcurrencyError:
                return True

        def phase(value, *, coarse=True):
            guard()
            timeline.transition(value, coarse_phase=value if coarse else None)

        def task_schema(key):
            name = task.output.get(key)
            return json.loads(version.package_files[name]) if name in version.package_files else None

        def retain_verified_files(report, root, *, parent=False):
            """Retain completed acquisition evidence even if a later file fails."""
            for item in (report or {}).get("entries", []):
                if not item.get("sha256") or item.get("size_bytes") is None:
                    continue
                relative = item["path"]
                archive_name = relative if item["include_in_compose"] else "research-artifacts/" + relative
                source_name = archive_name if parent else relative
                payload = read_safe_bytes(root / source_name, root, 20_000_000)
                if len(payload) != item["size_bytes"] or hashlib.sha256(payload).hexdigest() != item["sha256"]:
                    raise HardGateError("Confirmed artifact changed before archive preservation")
                archive.write(archive_name, payload)

        def stage_inputs(stage, input_dir):
            for name in task.instructions.get(stage + "_files", ["task.md"]):
                relative = PurePosixPath(name)
                if relative.is_absolute() or ".." in relative.parts or "\\" in name:
                    raise HardGateError("Unsafe task instruction path")
                if name not in version.package_files:
                    raise HardGateError(f"Missing sealed instruction: {name}")
                dest = input_dir / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(version.package_files[name], encoding="utf-8")
            for key in ("research_schema", "composition_schema", "composition_record_schema"):
                name = task.output.get(key)
                if name and name in version.package_files:
                    relative = PurePosixPath(name)
                    if relative.is_absolute() or ".." in relative.parts or "\\" in name:
                        raise HardGateError("Unsafe task schema path")
                    dest = input_dir / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(version.package_files[name], encoding="utf-8")
            for name, value in (("research-context.json", research_context),
                                ("delivery-history.json", delivery_history)):
                if value is not None:
                    (input_dir / name).write_bytes(canonical_json(value))

        def invoke(stage, dirs):
            nonlocal cleanup_verified
            input_dir, tmp_dir, output_dir = dirs
            stage_settings = execution_plan["stages"][stage]
            output_dirs.append((stage, output_dir))
            context = RunnerInvocationContext(task_id=task.id, run_id=run_id, attempt=run.attempt,
                invocation_stage=stage, model=stage_settings["model"],
                reasoning_effort=stage_settings["reasoning_effort"],
                timeout_seconds=task.runner.get("timeout_seconds", self.settings.runner.default_timeout_seconds),
                network_profile=task.runner.get("network_profile", "none"),
                resource_profile=task.runner.get("resource_profile", "standard-research"),
                fencing_token=fencing_token, cancellation_check=cancelled,
                timezone=run.timezone, local_date=run.local_date,
                local_date_display=run.local_date_display, scheduled_for=run.scheduled_for,
                trace_log_dir=archive.staging / "logs",
                trace_max_bytes=self.settings.runner.trace_max_bytes,
                trace_max_event_bytes=self.settings.runner.trace_max_event_bytes,
                trace_max_events=self.settings.runner.trace_max_events,
                trace_preview_bytes=self.settings.runner.trace_preview_bytes,
                artifact_connection_ids=sorted(self.settings.media.providers)
                    if stage == "research" and task.runner.get("network_profile") == "public-research" else [],
                acquisition_session=research_acquisition if stage == "research" else None)
            context.trace_log_dir.mkdir(mode=0o700, exist_ok=True)
            archive.capture(input_dir, f"inputs/{stage}")
            cleanup_verified = False
            runner = self.resolve_runner(task, stage_settings)
            acquisition_cleanup = True
            try:
                result = getattr(runner, "execute_" + stage)(
                    input_dir=input_dir, tmp_dir=tmp_dir, output_dir=output_dir,
                    project_dir=self.workspace_mgr.get_task_workspace_dir(task.id) / "project", context=context)
            finally:
                if context.acquisition_session is not None:
                    acquisition_cleanup = context.acquisition_session.close()
                    if not acquisition_cleanup:
                        cleanup_verified = False
            cleanup_verified = result.cleanup_verified is True and acquisition_cleanup
            timeline.summarize(exit_code=result.exit_code, cleanup_verified=int(cleanup_verified),
                stdout_bytes=result.log_sizes.get("stdout", len(result.stdout.encode("utf-8"))),
                stderr_bytes=result.log_sizes.get("stderr", len(result.stderr.encode("utf-8"))))
            # RunnerExecutionResult.events is an audit wrapper, not the native
            # event stream. Report a count only when the collector observed it.
            parsed_events = result.isolation.get("trace_diagnostics", {}).get("parsed_events")
            if type(parsed_events) is int and 0 <= parsed_events < 2**63:
                timeline.summarize(event_count=parsed_events)
            from researchops.runners.base import safe_response_diagnostic
            executions.append({"stage": stage, "exit_code": result.exit_code,
                               "execution_settings": stage_settings,
                               "error_message": result.error_message,
                               "cleanup_verified": cleanup_verified,
                               "isolation": getattr(result, "isolation", {}),
                               "response_diagnostic": safe_response_diagnostic(
                                   getattr(result, "response_diagnostic", None))})
            for stream_name in ("stdout", "stderr"):
                name = f"logs/{stage}.{stream_name}"
                source = result.log_files.get(stream_name)
                if source is not None:
                    archive.capture_log(name, source, self.settings.runner.trace_max_bytes)
                else:
                    archive.write(name, getattr(result, stream_name).encode("utf-8"))
            archive.json(f"logs/{stage}.events.json", result.events)
            if not cleanup_verified:
                local = result.isolation.get('control_process_cleanup_verified')
                detail = ('Local process cleanup verified; remote completion unverified'
                          if local is True and result.isolation.get('remote_turn_completion_unverified')
                          else 'Child process cleanup could not be verified')
                raise PolicyStop("needs_attention", f"{stage}: {result.error_message}; {detail}"
                                 if result.error_message else detail)
            guard()
            if not result.success:
                code = result.exit_code
                status = "timed_out" if code == 124 else "needs_attention" if code in (77, 78, 126) else "failed"
                raise PolicyStop(status, result.error_message or f"{stage} worker failed ({code})")

        try:
            guard()
            if not dry_run and task.delivery.get("mode") == "handoff":
                from researchops.delivery.authorization import require_run_delivery_authorization
                require_run_delivery_authorization(self.settings, self.run_repo.db, run)
            self.workspace_mgr.acquire_workspace_lock(task.id, run_id, fencing_token)
            locked = True
            from researchops.engine.research_context import build_research_context, validate_reused_context
            research_context = build_research_context(task, run)
            if research_context is not None and not compose_only and run.trigger_type == "retry" and run.parent_run_id:
                parent_archive = self.settings.paths.run_archive_dir / task.id / run.parent_run_id
                parent_path = parent_archive / "research-context.json"
                if parent_path.exists():
                    from researchops.strict_json import strict_json_loads
                    parent_context = strict_json_loads(read_safe_bytes(parent_path, parent_archive, 64_000))
                    research_context = validate_reused_context(parent_context, task, run)
                    from researchops.engine.research_context import validate_retry_origin
                    validate_retry_origin(research_context, run, self.run_repo, self.settings.paths.run_archive_dir)
                    parent_record = self.run_repo.get_composition_input_record(run.parent_run_id)
                    if parent_record is not None:
                        raw_parent = canonical_json(parent_record["input"])
                        if (parent_record["input_sha256"] != hashlib.sha256(raw_parent).hexdigest()
                                or parent_record["input"].get("research_context") != research_context):
                            raise HardGateError("Retry context differs from the parent's stored composition")
            if research_context is not None and not compose_only:
                if (task.state or {}).get("research_context", {}).get("delivery_history", False):
                    from researchops.engine.delivery_history import build_delivery_history
                    delivery_history = build_delivery_history(self.settings, self.task_repo, self.run_repo,
                        self.delivery_repo, task, run, research_context)
                    from researchops.engine.delivery_history import attach_ledger_reconciliation
                    project_root = self.workspace_mgr.get_task_workspace_dir(task.id) / "project"
                    ledger_path = project_root / "state" / "test-series" / research_context["series_id"] / "sent-products.json"
                    ledger_bytes = None
                    if ledger_path.exists() or ledger_path.is_symlink():
                        try:
                            ledger_bytes = read_safe_bytes(ledger_path, project_root, 2_000_000)
                        except (OSError, WorkspaceError, ValidationError):
                            ledger_bytes = b""
                    delivery_history = attach_ledger_reconciliation(delivery_history, ledger_bytes)
                archive.json("research-context.json", research_context)
                if delivery_history is not None:
                    archive.json("delivery-history.json", delivery_history)
            artifact_sources = {}
            if compose_only:
                phase("prepare_compose", coarse=False)
                source_ref = execution_plan["source_composition"]
                if source_ref is None:
                    raise HardGateError("Immutable composition source is unavailable")
                source_run_id = source_ref["run_id"]
                if delivery_only:
                    from researchops.services.prepared_delivery import load_prepared_message
                    prepared_message = load_prepared_message(self.settings, self.task_repo, self.run_repo,
                        self.delivery_repo, source_run_id, expected_source=execution_plan["source_message"])
                    archive.json("prepared-message-source.json", prepared_message.source_message)
                ancestor, seen = run, {run_id}
                for _ in range(128):
                    if not ancestor.parent_run_id or ancestor.parent_run_id in seen:
                        raise HardGateError("Composition source is not in the Run ancestry")
                    ancestor = self.run_repo.get_run(ancestor.parent_run_id)
                    if (ancestor is None or ancestor.task_id != task.id
                            or ancestor.task_version_hash != run.task_version_hash):
                        raise HardGateError("Composition source ancestry identity mismatch")
                    if ancestor.run_id == source_run_id:
                        break
                    seen.add(ancestor.run_id)
                else:
                    raise HardGateError("Composition source ancestry exceeds the verification limit")
                source_run = self.run_repo.get_run(source_run_id)
                if (source_run is None or source_run.task_id != task.id
                        or source_run.task_version_hash != run.task_version_hash
                        or source_run.local_date != run.local_date
                        or source_run.scheduled_for != run.scheduled_for):
                    raise HardGateError("Immutable composition source identity mismatch")
                source_record = self.run_repo.get_composition_input_record(source_run_id, source_ref["revision"])
                source = source_record["input"] if source_record else None
                research = self.run_repo.get_research_result(source_run_id)
                if not source or not research or source["task_version_hash"] != run.task_version_hash:
                    raise HardGateError("Immutable parent composition input/research is unavailable")
                source = copy.deepcopy(source)
                parent_archive = self.settings.paths.run_archive_dir / task.id / source_run_id
                source_bytes = canonical_json(source)
                if (source_record["task_id"] != task.id or source["task_id"] != task.id
                        or source["run_id"] != source_run_id
                        or source["composition_revision"] != source_ref["revision"]
                        or source_record["input_sha256"] != source_ref["input_sha256"]
                        or source_record["input_sha256"] != hashlib.sha256(source_bytes).hexdigest()
                        or read_safe_bytes(parent_archive / "composition-input.json", parent_archive,
                                           20_000_000) != source_bytes):
                    raise HardGateError("Parent composition input identity/hash mismatch")
                from researchops.engine.acquisition_evidence import retain_acquisition_evidence
                acquisition_origin = (source.get("artifact_report") or {}).get("acquisition_evidence")
                if acquisition_origin is not None:
                    ancestor, seen = source_run, set()
                    while ancestor and ancestor.run_id not in seen and len(seen) < 256:
                        seen.add(ancestor.run_id)
                        if (ancestor.task_id != task.id or ancestor.task_version_hash != run.task_version_hash):
                            raise HardGateError("Inherited acquisition producer belongs to another task version")
                        if ancestor.run_id == acquisition_origin["run_id"]:
                            if ancestor.attempt != acquisition_origin["attempt"]:
                                raise HardGateError("Inherited acquisition producer attempt changed")
                            break
                        ancestor = self.run_repo.get_run(ancestor.parent_run_id) if ancestor.parent_run_id else None
                    else:
                        raise HardGateError("Inherited acquisition producer is outside the source ancestry")
                retain_acquisition_evidence(archive, parent_archive, source)
                if research_context is not None:
                    research_context = validate_reused_context(source.get("research_context"), task, run)
                    delivery_history = source.get("delivery_history")
                    for name, value in (("research-context.json", research_context),
                                        ("delivery-history.json", delivery_history)):
                        if value is not None:
                            raw = canonical_json(value)
                            if read_safe_bytes(parent_archive / name, parent_archive, 2_000_000) != raw:
                                raise HardGateError("Parent research sidecar differs from immutable composition")
                            archive.write(name, raw)
                if source.get("schema_version") == 4:
                    from jsonschema import Draft202012Validator, FormatChecker
                    Draft202012Validator(self.comp_input_builder.schema, format_checker=FormatChecker()).validate(source)
                    # The parent snapshot is immutable. Derive a new identity for
                    # this revision without fetching or reinterpreting old files.
                    parent_report = source["artifact_report"]
                    if read_safe_bytes(parent_archive / "artifact-report.json", parent_archive,
                                       2_000_000) != canonical_json(parent_report):
                        raise HardGateError("Parent artifact report does not match the stored input")
                    from researchops.delivery.artifact_integrity import validate_artifact_contract
                    validate_artifact_contract(CompositionInput(**source))
                    retain_verified_files(parent_report, parent_archive, parent=True)
                    artifact_report = copy.deepcopy(parent_report)
                    artifact_report["derived_from"] = {"run_id": source_run_id,
                        "composition_revision": source["composition_revision"],
                        "artifact_report_sha256": hashlib.sha256(canonical_json(parent_report)).hexdigest()}
                    artifact_report["run_id"] = run_id
                    artifact_report["composition_revision"] = controls["composition_revision"]
                    source["artifact_report"] = artifact_report
                source["run_id"] = run_id
                source["composition_revision"] = controls["composition_revision"]
                comp_input = CompositionInput(**source)
                self.run_repo.save_research_result(run_id, task.id, research, fencing_token=fencing_token)
                for artifact in comp_input.inline_artifacts + comp_input.attachments:
                    artifact_sources[artifact["path"]] = (prepared_message.files[artifact["path"]] if delivery_only else
                        read_safe_bytes(parent_archive / artifact["path"], parent_archive, self.settings.delivery.max_message_bytes))
                warnings = list(research.warnings)
            else:
                phase("research")
                res_dirs = self.workspace_mgr.prepare_attempt_staging(task.id, run_id, run.attempt, "research")
                if task.runner.get("network_profile") == "public-research":
                    from researchops.engine.research_acquisition import ResearchAcquisitionSession
                    research_acquisition = ResearchAcquisitionSession(self.settings,
                        run_id=run_id, task_id=task.id, attempt=run.attempt,
                        fencing_token=fencing_token, storage_root=archive.staging / "research-acquisition",
                        cancellation_check=cancelled)
                stage_inputs("research", res_dirs[0])
                invoke("research", res_dirs)
                phase("validate")
                raw = read_safe_bytes(res_dirs[2] / "result.json", res_dirs[2], 20_000_000)
                archive.write("result.json", raw)
                research, warnings = self.technical_gate.validate_research_output(
                    raw.decode("utf-8"), task, task_schema("research_schema"))
                # Operational file diagnostics belong to the Run. Do not mutate
                # research.warnings and thereby force them into worker mail prose.
                warnings = list(warnings)
                timeline.summarize(record_count=len(research.records), warning_count=len(warnings))
                self.run_repo.save_research_result(run_id, task.id, research, fencing_token=fencing_token)
                if research.status == "needs_attention":
                    raise PolicyStop("needs_attention", "Research requested operator attention")
                if research.status == "partial":
                    policy = task.delivery.get("partial_policy", "send_with_warning")
                    if policy in ("hold", "fail"):
                        raise PolicyStop("needs_attention" if policy == "hold" else "failed", f"Partial result policy: {policy}")
                phase("dedupe")
                reportable, excluded, dedupe_warnings = self.deduplicator.process_records(task, research.records)
                warnings.extend(dedupe_warnings)
                timeline.summarize(record_count=len(research.records), reportable_count=len(reportable),
                    excluded_count=len(excluded), warning_count=len(dedupe_warnings))
                archive.json("dedupe-report.json", {"enabled": (task.state or {}).get("dedupe", {}).get("enabled", False),
                    "raw_count": len(research.records), "reportable_count": len(reportable),
                    "excluded_records": excluded, "warnings": dedupe_warnings})
                phase("artifact_acquisition", coarse=False)
                from researchops.engine.artifact_acquirer import ArtifactAcquirer
                acquirer = ArtifactAcquirer(self.settings)
                try:
                    acquired = acquirer.acquire(research.artifacts, research.records, reportable, res_dirs[2],
                        run_id=run_id, task_id=task.id, task_version_hash=run.task_version_hash,
                        composition_revision=controls.get("composition_revision", 1),
                        dedupe_enabled=bool((task.state or {}).get("dedupe", {}).get("enabled", False)),
                        network_allowed=task.runner.get("network_profile") == "public-research",
                        cancellation_check=cancelled, acquisition_session=research_acquisition)
                except Exception as acquisition_error:
                    if getattr(acquisition_error, "code", None) == "worker_cleanup_failed":
                        cleanup_verified = False
                        raise PolicyStop("needs_attention", "Artifact acquisition process cleanup could not be verified") from acquisition_error
                    raise
                finally:
                    if acquirer.last_report and acquirer.last_report.get("entries"):
                        artifact_report = acquirer.last_report
                        entries = artifact_report["entries"]
                        timeline.summarize(artifact_count=len(entries),
                            available_count=sum(item["status"] == "available" for item in entries),
                            failed_count=sum(item["status"] == "failed" for item in entries),
                            excluded_artifact_count=sum(item["status"] == "excluded" for item in entries))
                        primary_error = sys.exc_info()[1]
                        try:
                            retain_verified_files(artifact_report, res_dirs[2])
                        except Exception as evidence_error:
                            if primary_error is not None:
                                raise HardGateError(f"{primary_error}; artifact preservation failed: "
                                                    f"{type(evidence_error).__name__}") from primary_error
                            raise
                inline, attachments = acquired.inline_artifacts, acquired.attachments
                selected_paths = {item["path"] for item in inline + attachments}
                for path, source_path in acquired.file_paths.items():
                    payload = read_safe_bytes(source_path, res_dirs[2], 20_000_000)
                    if path in selected_paths:
                        artifact_sources[path] = payload
                for entry in (artifact_report or {}).get("entries", []):
                    if entry["status"] == "failed":
                        warnings.append("Requested artifact is unavailable; see artifact-report.json: "
                            + str(entry.get("reason_code") or "acquisition_failed"))
                if acquired.hold:
                    raise PolicyStop("needs_attention", "Task requires a requested file that could not be acquired")
                phase("prepare_compose", coarse=False)
                if not reportable and not task.delivery.get("send_on_empty", True):
                    raise PolicyStop("succeeded", "Empty report skipped by task policy")
                recipient_groups = None
                if task.delivery.get("recipient_routing_mode", "legacy_ids") == "catalog_name":
                    from researchops.delivery.recipient_routing import catalog_recipient_snapshot
                    from researchops.delivery.smtp_config import load_delivery_config
                    recipient_groups = catalog_recipient_snapshot(self.run_repo.db,
                        load_delivery_config(self.settings.paths.delivery_config_file), task_id=task.id)
                comp_input = self.comp_input_builder.build_composition_input(
                    task, run, research, reportable, inline, attachments,
                    composition_revision=controls.get("composition_revision", 1),
                    record_schema=task_schema("composition_record_schema"),
                    recipient_groups=recipient_groups, artifact_report=artifact_report,
                    research_context=research_context, delivery_history=delivery_history)
            if not (archive.staging / "result.json").exists():
                archive.write("result.json", research.raw_json.encode("utf-8"))
            comp_bytes = canonical_json(comp_input.to_dict())
            archive.write("composition-input.json", comp_bytes)
            guard()
            self.run_repo.save_composition_input(comp_input, hashlib.sha256(comp_bytes).hexdigest(), fencing_token=fencing_token)
            timeline.summarize(reportable_count=len(comp_input.reportable_records),
                attachment_count=len(comp_input.attachments), inline_count=len(comp_input.inline_artifacts),
                composition_revision=comp_input.composition_revision)
            if not delivery_only:
                phase("compose")
            cmp_dirs = self.workspace_mgr.prepare_attempt_staging(task.id, run_id, run.attempt, "compose")
            if not delivery_only:
                stage_inputs("compose", cmp_dirs[0])
                (cmp_dirs[0] / "composition-input.json").write_bytes(comp_bytes)
            for artifact in comp_input.inline_artifacts + comp_input.attachments:
                dest = cmp_dirs[2] / artifact["path"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(artifact_sources[artifact["path"]])
            if delivery_only:
                for name in ("composition-result.json", prepared_message.composition_result.html_path,
                             prepared_message.composition_result.text_path):
                    destination = cmp_dirs[2] / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(prepared_message.files[name])
            else:
                invoke("compose", cmp_dirs)
            phase("validate_message")
            comp_result, _, _, hashes = self.message_validator.validate_composition(
                cmp_dirs[2], comp_input, task, task_schema("composition_schema"))
            if delivery_only and (comp_result.to_dict() != prepared_message.composition_result.to_dict() or
                    any(hashes[key] != prepared_message.hashes[key] for key in ("html", "text", "composition_result"))):
                raise HardGateError("Prepared email changed while rebinding the delivery-only revision")
            timeline.summarize(attachment_count=len(comp_input.attachments), inline_count=len(comp_input.inline_artifacts),
                composition_revision=comp_input.composition_revision)
            guard()
            self.run_repo.save_composition_result(run_id, task.id, comp_result,
                revision=comp_input.composition_revision, fencing_token=fencing_token,
                recipient_resolution=hashes.get("recipient_resolution"),
                composition_binding=hashes.get("composition_binding"))
            if hashes.get("composition_binding"):
                archive.json("composition-binding.json", hashes["composition_binding"])
            if hashes.get("recipient_resolution"):
                archive.json("recipient-resolution.json", hashes["recipient_resolution"])
            for name in ("composition-result.json", comp_result.html_path, comp_result.text_path):
                archive.write(name, read_safe_bytes(cmp_dirs[2] / name, cmp_dirs[2], self.settings.delivery.max_message_bytes))
            for artifact in comp_input.inline_artifacts + comp_input.attachments:
                if not (archive.staging / artifact["path"]).exists():
                    archive.write(artifact["path"], artifact_sources[artifact["path"]])
            phase("handoff")
            handoff = self.handoff_publisher.create_and_publish_handoff(
                task_def=task, run=run, comp_input=comp_input, comp_result=comp_result,
                compose_output_dir=cmp_dirs[2], archive_run_dir=archive.staging,
                file_hashes=hashes, force_mode="dry_run" if dry_run else None)
            # Preserve exactly the bytes hashed by the publisher, not a fresh serialization.
            if not (archive.staging / "delivery-request.json").exists():
                raw = json.dumps(handoff.delivery_request, indent=2, sort_keys=True).encode("utf-8")
                if hashlib.sha256(raw).hexdigest() != handoff.delivery_request_sha256:
                    raw = canonical_json(handoff.delivery_request)
                if hashlib.sha256(raw).hexdigest() != handoff.delivery_request_sha256:
                    raise HardGateError("Delivery request serialization/hash mismatch")
                archive.write("delivery-request.json", raw)
            final_status = "awaiting_receipt" if handoff.mode == "handoff" and handoff.status == "published" else "succeeded"
        except PolicyStop as exc:
            final_status, failure = exc.status, exc.reason
            warnings.append(exc.reason)
        except Exception as exc:
            controls = self.run_repo.get_execution_controls(run_id)
            final_status = "cancelled" if controls.get("cancel_requested") else "needs_attention" if isinstance(exc, ConcurrencyError) else "failed"
            failure = str(exc)
            validation_errors = list(getattr(exc, "errors", []))
            warnings.extend(getattr(exc, "warnings", []))
        finally:
            if research_acquisition is not None:
                try:
                    acquisition_cleanup = research_acquisition.close()
                except Exception:
                    acquisition_cleanup = False
                if not acquisition_cleanup:
                    cleanup_verified = False
                    final_status = "needs_attention"
                    failure = failure or "Research file acquisition cleanup could not be verified"
            self._finalize_run(run,task,archive,research,comp_input,comp_result,handoff,
                warnings,executions,validation_errors,output_dirs,cleanup_verified,locked,
                final_status,failure,fencing_token,cancel_event,artifact_report=artifact_report,timeline=timeline,
                acquisition_cleanup_verified=research_acquisition is None or research_acquisition.cleanup_verified)
        return self.run_repo.get_run(run_id)

    def _finalize_run(self,run,task,archive,research,comp_input,comp_result,handoff,
                      warnings,executions,validation_errors,output_dirs,cleanup_verified,locked,
                      final_status,failure,fencing_token,cancel_event,*,artifact_report=None,timeline,
                      acquisition_cleanup_verified=True):
        """Publish evidence and terminal state under one fenced finalization lock."""
        run_id=run.run_id
        failure_step_recorded = False
        try:
            if not acquisition_cleanup_verified:
                # An uncollected broker could still be writing acquisition
                # evidence. Keep staging pending; never publish it as immutable.
                raise HardGateError("Research acquisition cleanup is unverified; pending evidence retained")
            step_state = ("skipped" if final_status == "succeeded" and failure else "succeeded"
                          if final_status in ("succeeded", "awaiting_receipt") else final_status)
            timeline.finish(step_state, summary={"validation_error_count": len(validation_errors)})
            timeline.transition("finalize", coarse_phase="finalize", allow_cancel=True)
            timeline.summarize(warning_count=len(warnings), cleanup_verified=int(cleanup_verified))
            if artifact_report is not None:
                archive.json("artifact-report.json", artifact_report)
            # Publication has not happened yet. The database records its actual
            # completion after archive.finish; this immutable snapshot stays honest.
            archive.json("timeline.json", timeline.snapshot())
            with self.run_repo.db.transaction() as conn:
                def commit_guard():
                    now=datetime.now(timezone.utc).isoformat()
                    owner=conn.execute("""SELECT r.status FROM scheduled_runs r
                        JOIN run_leases l ON l.run_id=r.run_id JOIN task_claims c ON c.run_id=r.run_id
                        WHERE r.run_id=? AND r.status='running' AND l.fencing_token=?
                          AND c.fencing_token=? AND l.lease_expires_at>?""",
                        (run_id,fencing_token,fencing_token,now)).fetchone()
                    if not owner:
                        raise ConcurrencyError("Archive finalization rejected: execution lease is no longer current")
                commit_guard()
                control=conn.execute("SELECT cancel_requested FROM execution_controls WHERE run_id=?",(run_id,)).fetchone()
                if control and control[0]:
                    final_status,failure="cancelled","Cancelled before finalization"
                elif cancel_event is not None and cancel_event.is_set():
                    final_status,failure="needs_attention","Execution stopped before finalization"
                if not cleanup_verified:
                    final_status="needs_attention"
                    failure=failure or "Child process cleanup could not be verified"
                if handoff and handoff.mode=="handoff" and final_status!="awaiting_receipt":
                    conn.execute("UPDATE delivery_handoffs SET status='failed',decision_reason=? WHERE handoff_id=? AND status IN ('prepared','published')",(failure or "Run did not complete delivery preparation",handoff.handoff_id))
                    handoff.status="failed"
                for stage,directory in output_dirs:
                    archive.capture(directory,f"outputs/{stage}")
                archive.json("validation-report.json",{"errors":validation_errors,"warnings":warnings,
                    "failure":failure,"executions":executions,"cleanup_verified":cleanup_verified})
                composition={"status":"not_produced"}
                if comp_result:
                    composition={"status":"validated","revision":comp_input.composition_revision,**comp_result.to_dict()}
                delivery={"mode":"disabled","status":"not_requested"}
                if handoff:
                    delivery={"mode":handoff.mode,"status":handoff.status}
                    if handoff.status in ("prepared","published"):
                        delivery.update({"message_type":handoff.message_type,"recipient_group_id":handoff.recipient_group_id,
                            "message_revision":handoff.message_revision,"handoff_id":handoff.handoff_id,
                            "idempotency_key":handoff.idempotency_key,"delivery_request_path":"delivery-request.json",
                            "delivery_request_sha256":handoff.delivery_request_sha256})
                        if handoff.published_at:
                            delivery["published_at"]=handoff.published_at
                finished=datetime.now(timezone.utc).isoformat() if final_status!="awaiting_receipt" else None
                manifest={"run_id":run_id,"task_id":task.id,"task_hash":run.task_version_hash,
                    "status":final_status,"phase":"finalize","attempt":run.attempt,
                    "trigger_type":run.trigger_type,"scheduled_for":run.scheduled_for,
                    "started_at":run.started_at,"finished_at":finished,
                    "result_outcome":{"status":research.status if research else "not_produced",
                        "record_count":len(research.records) if research else 0,
                        "reportable_record_count":len(comp_input.reportable_records) if comp_input else 0},
                    "composition":composition,"handoff":delivery,"warnings":list(warnings),
                    "failure":{"message":failure,"errors":validation_errors} if failure else None,
                    "workspace":{"task_workspace_id":task.id,"generation":run.workspace_generation,
                        "attempt_root":f".runs/{run_id}/attempt-{run.attempt}","fencing_token":fencing_token,
                        "child_group_ids":[str(x["isolation"]["cgroup"]) for x in executions if x.get("isolation",{}).get("cgroup")]}}
                archive.finish(manifest,before_commit=commit_guard)
                final_step = timeline.finish("succeeded", conn=conn)
                # The manifest is a pre-publication snapshot. The authoritative
                # run clock ends after publication, together with this step.
                completed_at = final_step["finished_at"] if final_status != "awaiting_receipt" else None
                conn.execute("""UPDATE scheduled_runs SET status=?,phase='finalize',finished_at=?,error_message=?,last_progress_at=?
                    WHERE run_id=? AND status='running'""",
                    (final_status,completed_at,failure if final_status not in ("succeeded","awaiting_receipt") else None,
                     datetime.now(timezone.utc).isoformat(),run_id))
            timeline.acknowledge_finished()
        except Exception as exc:
            # No ready archive + no awaiting_receipt state means the dispatcher
            # cannot consume a package, even if it was already in the outbox.
            with self.run_repo.db.transaction() as conn:
                live=conn.execute("""SELECT 1 FROM run_leases l JOIN task_claims c ON c.run_id=l.run_id
                    WHERE l.run_id=? AND l.fencing_token=? AND c.fencing_token=? AND l.lease_expires_at>?""",
                    (run_id,fencing_token,fencing_token,datetime.now(timezone.utc).isoformat())).fetchone()
                if live:
                    try:
                        timeline.finish("needs_attention", conn=conn, allow_terminal=True)
                        failure_step_recorded = True
                    except Exception:
                        # A failed diagnostic writer must not prevent the
                        # authoritative failure/cleanup path or mask its cause.
                        logger.warning("Finalization step completion could not be recorded for %s", run_id)
                    message=((failure + "; ") if failure else "") + f"Archive finalization failed: {type(exc).__name__}; pending evidence retained"
                    conn.execute("UPDATE scheduled_runs SET status='needs_attention',phase='finalize',finished_at=?,error_message=? WHERE run_id=? AND status='running'",
                        (datetime.now(timezone.utc).isoformat(),message,run_id))
                    conn.execute("UPDATE delivery_handoffs SET status='failed',decision_reason=? WHERE run_id=? AND status IN ('prepared','published')",(message,run_id))
            if failure_step_recorded:
                timeline.acknowledge_finished()
            raise
        finally:
            if locked and cleanup_verified:
                try:
                    self.workspace_mgr.release_workspace_lock(task.id,run_id,fencing_token,child_cleanup_verified=True)
                    self.run_repo.mark_cleanup_verified(run_id,fencing_token)
                except ConcurrencyError:
                    logger.warning("Workspace ownership changed during cleanup of %s",run_id)
        if final_status in ("failed","timed_out","needs_attention"):
            try:
                self.alert_mgr.emit_alert(task,run,final_status,failure or "Execution failed")
            except Exception:
                logger.exception("Could not queue operator alert; local evidence retained")
