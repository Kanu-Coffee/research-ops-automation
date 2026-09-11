"""Minimum technical gate validator for Research results."""

import json
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from jsonschema import Draft202012Validator, FormatChecker

from researchops.domain.models import ResearchResult, TaskDefinition
from researchops.errors import HardGateError, ValidationError
from researchops.strict_json import strict_json_loads


class TechnicalGateValidator:
    def __init__(self, schemas_dir: Path):
        self.schemas_dir = schemas_dir
        self._generic_schema: Optional[Dict[str, Any]] = None

    @property
    def generic_schema(self) -> Dict[str, Any]:
        if self._generic_schema is None:
            schema_file = self.schemas_dir / "generic-result.schema.json"
            with open(schema_file, "r", encoding="utf-8") as f:
                self._generic_schema = json.load(f)
        return self._generic_schema

    def validate_research_output(
        self,
        raw_result_text: str,
        task_def: TaskDefinition,
        task_research_schema: Optional[Dict[str, Any]] = None
    ) -> Tuple[ResearchResult, List[str]]:
        """Validate research JSON against minimum technical envelope and task schema.

        Returns:
            (ResearchResult, warnings_list)
        Raises:
            HardGateError: if the candidate envelope or required fields are fundamentally unreadable.
        """
        try:
            data = strict_json_loads(raw_result_text, max_bytes=20_000_000)
        except ValueError as jde:
            raise HardGateError(f"Research output is not valid JSON: {jde}")

        if not isinstance(data, dict):
            raise HardGateError("Research output root must be a JSON object")

        # 1. Validate against generic-result.schema.json (minimum envelope)
        validator = Draft202012Validator(self.generic_schema, format_checker=FormatChecker())
        envelope_errors = list(validator.iter_errors(data))
        if envelope_errors:
            error_msgs = [f"{e.json_path}: {e.message}" for e in envelope_errors]
            raise HardGateError("Generic result envelope validation failed", errors=error_msgs)

        status = data.get("status")
        summary = data.get("summary", "")
        raw_records = data.get("records", [])
        raw_coverage = data.get("coverage")
        raw_warnings = data.get("warnings") or []
        raw_artifacts = data.get("artifacts") or []

        warnings: List[str] = []
        if isinstance(raw_warnings, list):
            warnings.extend([str(w) for w in raw_warnings])
        else:
            warnings.append("Malformed warnings metadata preserved in raw result.")

        # 2. Check records structure
        if not isinstance(raw_records, list):
            raise HardGateError("'records' field must be an array")

        # Ensure every record is a dictionary and has a stable record_id or identifier
        records: List[Dict[str, Any]] = []
        seen_ids = set()
        for idx, rec in enumerate(raw_records):
            if not isinstance(rec, dict):
                raise HardGateError(
                    f"Record at index {idx} is not an object. Whole candidate invalid; no silent drops."
                )
            # Normalize or check record_id
            rec_copy = dict(rec)
            if "record_id" not in rec_copy:
                canonical = json.dumps(rec, sort_keys=True, ensure_ascii=False, allow_nan=False)
                digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
                rec_copy["record_id"] = f"record-{idx + 1}-{digest}"
            record_id = rec_copy["record_id"]
            if not isinstance(record_id, str) or not record_id.strip() or record_id in seen_ids:
                raise HardGateError("Record IDs must be nonempty strings and unique; candidate preserved")
            seen_ids.add(record_id)
            records.append(rec_copy)

        # 3. Task research schemas describe content quality; readable records are
        # preserved even when optional/business rules disagree. The generic
        # envelope above and the composition-record contract remain hard gates.
        if task_research_schema:
            task_val = Draft202012Validator(task_research_schema, format_checker=FormatChecker())
            task_errors = list(task_val.iter_errors(data))
            warnings.extend(f"Lint warning: {err.json_path}: {err.message}" for err in task_errors)

        # 4. Coverage check
        if isinstance(raw_coverage, list):
            targets = [x for x in raw_coverage if isinstance(x, dict)]
            completed = sum(x.get("status") == "checked" for x in targets)
            raw_coverage = {
                "complete": bool(targets) and completed == len(raw_coverage),
                "expected_target_count": len(raw_coverage),
                "completed_target_count": completed,
                "issues": [{"target": str(x.get("target") or "unknown"),
                            "status": "blocked" if x.get("status") == "blocked" else "unavailable",
                            "notes": str(x.get("notes") or "Not checked")}
                           for x in targets if x.get("status") != "checked"],
            }
        if not isinstance(raw_coverage, dict):
            raw_coverage = {
                "complete": False,
                "expected_target_count": len(records),
                "completed_target_count": len(records),
                "issues": [{"target": "unknown", "status": "unavailable", "notes": "Malformed coverage object"}]
            }
            warnings.append("Research output provided malformed coverage object; normalized.")
        else:
            if not raw_coverage.get("complete", False):
                warnings.append("Coverage report indicates incomplete target research.")

        # Normalize coverage dictionary for composition input
        valid_coverage = type(raw_coverage.get("complete")) is bool
        def count_value(name):
            nonlocal valid_coverage
            value = raw_coverage.get(name)
            if type(value) is not int or value < 0:
                valid_coverage = False
                warnings.append(f"Coverage {name} missing or invalid; marked incomplete.")
                return 0
            return value
        expected = count_value("expected_target_count")
        completed = count_value("completed_target_count")
        issues = raw_coverage.get("issues", [])
        if not isinstance(issues, list):
            valid_coverage = False
            issues = []
            warnings.append("Malformed coverage issues preserved in raw result.")
        normalized_issues = []
        for issue in issues:
            if not isinstance(issue, dict):
                valid_coverage = False
                warnings.append("Malformed coverage issue preserved in raw result.")
                continue
            normalized_issues.append({"target": str(issue.get("target") or "unknown"),
                "status": "blocked" if issue.get("status") == "blocked" else "unavailable",
                "notes": None if issue.get("notes") is None else str(issue["notes"])})
        coverage = {
            "complete": valid_coverage and raw_coverage.get("complete") is True and completed == expected and not normalized_issues,
            "expected_target_count": expected, "completed_target_count": completed,
            "issues": normalized_issues,
        }

        # Normalize artifacts
        artifacts: List[Dict[str, Any]] = []
        if isinstance(raw_artifacts, list):
            artifacts = raw_artifacts
        else:
            warnings.append("Malformed optional artifact metadata preserved in raw result.")

        result = ResearchResult(
            status=status,
            summary=summary,
            records=records,
            coverage=coverage,
            warnings=warnings,
            raw_json=raw_result_text,
            artifacts=artifacts
        )
        return result, warnings
