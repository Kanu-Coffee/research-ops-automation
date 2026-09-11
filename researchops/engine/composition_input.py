"""Immutable composition input builder and schema validator."""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from jsonschema import Draft202012Validator, FormatChecker

from researchops.domain.models import CompositionInput, ResearchResult, ScheduledRun, TaskDefinition
from researchops.errors import HardGateError


class CompositionInputBuilder:
    def __init__(self, schemas_dir: Path):
        self.schemas_dir = schemas_dir
        self._schema: Optional[Dict[str, Any]] = None

    @property
    def schema(self) -> Dict[str, Any]:
        if self._schema is None:
            schema_file = self.schemas_dir / "composition-input.schema.json"
            with open(schema_file, "r", encoding="utf-8") as f:
                self._schema = json.load(f)
        return self._schema

    def build_composition_input(
        self,
        task_def: TaskDefinition,
        run: ScheduledRun,
        research_result: ResearchResult,
        reportable_records: List[Dict[str, Any]],
        inline_artifacts: Optional[List[Dict[str, Any]]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        composition_revision: int = 1,
        record_schema: Optional[Dict[str, Any]] = None,
        recipient_groups: Optional[List[Dict[str, str]]] = None,
        artifact_report: Optional[Dict[str, Any]] = None,
        research_context: Optional[Dict[str, Any]] = None,
        delivery_history: Optional[Dict[str, Any]] = None,
    ) -> CompositionInput:
        """Build and validate immutable CompositionInput against composition-input.schema.json."""
        from researchops.delivery.recipient_routing import routing_mode, validate_snapshot
        mode = routing_mode(task_def)
        groups = []
        if mode == "catalog_name":
            groups = [dict(group) for group in recipient_groups or []]
            validate_snapshot(groups)
            allowed_groups = [group["recipient_group_id"] for group in groups]
        else:
            allowed_groups = list(task_def.delivery.get("allowed_recipient_group_ids", []))

        # Map status to allowed enum in schema: success | no_updates | partial
        res_status = research_result.status
        if res_status not in ("success", "no_updates", "partial"):
            raise HardGateError(f"Research status {res_status!r} cannot enter composition")
        if task_def.id != run.task_id or run.timezone != "Asia/Seoul":
            raise HardGateError("Composition task identity/timezone mismatch")
        if research_context is not None:
            from researchops.engine.research_context import validate_reused_context
            validate_reused_context(research_context, task_def, run)
        if delivery_history is not None and (research_context is None
                or delivery_history.get("task_id") != task_def.id
                or delivery_history.get("series_id") != research_context["series_id"]):
            raise HardGateError("Delivery history does not match the research context")

        # Format local_date_display if not present or malformed
        date_display = run.local_date_display
        if not date_display or len(date_display) != 10:
            date_display = run.local_date.replace("-", ".")

        run_info = {
            "scheduled_for": run.scheduled_for,
            "timezone": run.timezone,
            "local_date": run.local_date,
            "local_date_display": date_display
        }

        result_info = {
            "status": res_status,
            "summary": research_result.summary,
            "warnings": research_result.warnings
        }

        coverage_info = research_result.coverage

        clean_records = []
        for r in reportable_records:
            clean_r = dict(r)
            clean_records.append(clean_r)

        ids = [r.get("record_id") for r in clean_records]
        if any(not isinstance(rid, str) or not rid for rid in ids) or len(set(ids)) != len(ids):
            raise HardGateError("Composition record IDs must be nonempty and unique")
        if record_schema:
            record_validator = Draft202012Validator(record_schema, format_checker=FormatChecker())
            errors = [f"Record {i}: {error.message}" for i, record in enumerate(clean_records)
                      for error in record_validator.iter_errors(record)]
            if errors:
                raise HardGateError("Task composition-record contract failed", errors=errors)
        cids = [a.get("cid") for a in inline_artifacts or []]
        if len(cids) != len(set(cids)):
            raise HardGateError("Duplicate inline artifact CID")

        comp_input = CompositionInput(
            schema_version=4 if artifact_report is not None else (3 if mode == "catalog_name" else 2),
            recipient_routing_mode=mode,
            recipient_groups=groups,
            task_id=task_def.id,
            run_id=run.run_id,
            task_version_hash=run.task_version_hash,
            composition_revision=composition_revision,
            run=run_info,
            result=result_info,
            coverage=coverage_info,
            allowed_recipient_group_ids=allowed_groups,
            reportable_records=clean_records,
            inline_artifacts=inline_artifacts or [],
            attachments=attachments or [],
            artifact_report=artifact_report,
            research_context=research_context,
            delivery_history=delivery_history,
        )

        input_dict = comp_input.to_dict()

        # Validate against composition-input.schema.json
        validator = Draft202012Validator(self.schema, format_checker=FormatChecker())
        errors = list(validator.iter_errors(input_dict))
        if errors:
            err_msgs = [f"{e.json_path}: {e.message}" for e in errors]
            raise HardGateError("Generated composition input failed schema validation", errors=err_msgs)

        if comp_input.schema_version == 4:
            from researchops.delivery.artifact_integrity import validate_artifact_contract
            validate_artifact_contract(comp_input)

        return comp_input
