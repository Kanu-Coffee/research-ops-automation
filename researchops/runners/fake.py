"""Fake runner for local test double execution without external LLM/CLI."""

import json
import html
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from researchops.runners.base import (
    BaseRunner, RunnerInvocationContext, RunnerExecutionResult
)


class FakeRunner(BaseRunner):
    def __init__(
        self,
        fixtures_dir: Optional[Path] = None,
        simulate_failure: bool = False,
        custom_research_result: Optional[Dict[str, Any]] = None,
        custom_composition_result: Optional[Dict[str, Any]] = None,
        custom_html: Optional[str] = None,
        custom_text: Optional[str] = None
    ):
        self.fixtures_dir = fixtures_dir
        self.simulate_failure = simulate_failure
        self.custom_research_result = custom_research_result
        self.custom_composition_result = custom_composition_result
        self.custom_html = custom_html
        self.custom_text = custom_text

    def execute_research(
        self,
        input_dir: Path,
        tmp_dir: Path,
        output_dir: Path,
        project_dir: Path,
        context: RunnerInvocationContext
    ) -> RunnerExecutionResult:
        if self.simulate_failure:
            return RunnerExecutionResult(
                success=False,
                exit_code=1,
                stdout="",
                stderr="Simulated research failure in FakeRunner",
                error_message="Simulated research failure", cleanup_verified=True
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        result_file = output_dir / "result.json"

        if self.custom_research_result is not None:
            result_file.write_text(json.dumps(self.custom_research_result, indent=2), encoding="utf-8")
        elif self.fixtures_dir and (self.fixtures_dir / "sample-result.json").exists():
            shutil.copy2(self.fixtures_dir / "sample-result.json", result_file)
        else:
            # Minimal fallback research result
            default_res = {
                "status": "success",
                "summary": "Fake research completed successfully.",
                "records": [
                    {
                        "record_id": "rec-1",
                        "title": "Mock Record",
                        "content": "Mock research content"
                    }
                ],
                "coverage": {
                    "complete": True,
                    "expected_target_count": 1,
                    "completed_target_count": 1,
                    "issues": []
                },
                "warnings": []
            }
            result_file.write_text(json.dumps(default_res, indent=2), encoding="utf-8")

        return RunnerExecutionResult(
            success=True,
            exit_code=0,
            stdout="Fake research finished.",
            stderr="",
            cleanup_verified=True,
            output_files={"result": result_file}
        )

    def execute_compose(
        self,
        input_dir: Path,
        tmp_dir: Path,
        output_dir: Path,
        project_dir: Path,
        context: RunnerInvocationContext
    ) -> RunnerExecutionResult:
        if self.simulate_failure:
            return RunnerExecutionResult(
                success=False,
                exit_code=1,
                stdout="",
                stderr="Simulated compose failure in FakeRunner",
                error_message="Simulated compose failure", cleanup_verified=True
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        comp_file = output_dir / "composition-result.json"
        html_file = output_dir / "email.html"
        text_file = output_dir / "email.txt"

        # Read input composition-input.json if available
        comp_input_path = input_dir / "composition-input.json"
        reportable_record_ids = None
        cdata = {}
        if comp_input_path.exists():
            try:
                cdata = json.loads(comp_input_path.read_text(encoding="utf-8"))
                reportable_record_ids = [r["record_id"] for r in cdata.get("reportable_records", [])]
            except Exception:
                pass

        if self.custom_composition_result is not None:
            comp_data = dict(self.custom_composition_result)
        elif self.fixtures_dir and (self.fixtures_dir / "sample-composition-result.json").exists():
            comp_data = json.loads((self.fixtures_dir / "sample-composition-result.json").read_text(encoding="utf-8"))
        else:
            allowed_groups = cdata.get("allowed_recipient_group_ids", [])
            comp_data = {
                "recipient_group_id": allowed_groups[0] if allowed_groups else "release-team",
                "recipient_group_reason": "Mock default composition reason",
                "subject": "Mock digest subject",
                "html_path": "email.html",
                "text_path": "email.txt",
                "included_record_ids": ["rec-1"]
            }

        if reportable_record_ids is not None and self.custom_composition_result is None:
            if cdata.get("recipient_routing_mode") == "catalog_name":
                groups = cdata.get("recipient_groups", [])
                selected = next((group for group in groups
                                 if group["recipient_group_id"] == comp_data.get("recipient_group_id")),
                                groups[0] if groups else {})
                comp_data.pop("recipient_group_id", None)
                comp_data["recipient_group_name"] = selected.get("display_name", "")
            comp_data["included_record_ids"] = reportable_record_ids
            if not reportable_record_ids and not (self.fixtures_dir and
                    (self.fixtures_dir / "sample-composition-result.json").is_file()):
                comp_data["subject"] = "[조사 결과] 신규 보고 내역 없음"

        comp_file.write_text(json.dumps(comp_data, indent=2, ensure_ascii=False), encoding="utf-8")

        requested_notices = ["요청한 파일을 확보하지 못했습니다: " + entry["path"]
            for entry in (cdata.get("artifact_report") or {}).get("entries", [])
            if entry.get("announce_missing") and entry.get("status") in ("failed", "excluded")]

        if self.custom_html is not None:
            html_file.write_text(self.custom_html, encoding="utf-8")
        else:
            local_date = html.escape(str(cdata.get("run", {}).get("local_date", "")), quote=True)
            images_by_record = {}
            common_images = []
            for item in cdata.get("inline_artifacts", []):
                if not item.get("cid"):
                    continue
                tag = f'<img src="cid:{html.escape(str(item["cid"]), quote=True)}" alt="Research evidence">'
                linked = item.get("record_ids", [])
                if linked:
                    images_by_record.setdefault(linked[0], []).append(tag)
                else:
                    common_images.append(tag)
            rows = "".join(
                f'<div data-record-id="{html.escape(str(record_id), quote=True)}">'
                f'{html.escape(str(record_id))}{"".join(images_by_record.get(record_id, []))}</div>'
                for record_id in reportable_record_ids or []
            )
            if not rows:
                rows = "<p>조사 기간 동안 신규 보고 내역이 없습니다.</p>"
            images = "".join(common_images)
            html_file.write_text(
                f'<!DOCTYPE html><html><head><meta name="researchops-local-date" '
                f'content="{local_date}"></head><body>{rows}{images}'
                + "".join(f"<p>{html.escape(notice)}</p>" for notice in requested_notices) + '</body></html>',
                encoding="utf-8"
            )

        if self.custom_text is not None:
            text_file.write_text(self.custom_text, encoding="utf-8")
        elif self.fixtures_dir and (self.fixtures_dir / "sample-email-preview.txt").exists():
            shutil.copy2(self.fixtures_dir / "sample-email-preview.txt", text_file)
        elif reportable_record_ids == []:
            text_file.write_text("조사 기간 동안 신규 보고 내역이 없습니다.", encoding="utf-8")
        else:
            records = cdata.get("reportable_records", [])
            text_file.write_text("Mock email plain text body\n" + "\n".join(
                f"Record: {record['record_id']} - {record.get('title', '')}" for record in records
            ) + ("\n" + "\n".join(requested_notices) if requested_notices else ""), encoding="utf-8")

        return RunnerExecutionResult(
            success=True,
            exit_code=0,
            stdout="Fake compose finished.",
            stderr="",
            cleanup_verified=True,
            output_files={
                "composition_result": comp_file,
                "html": html_file,
                "text": text_file
            }
        )
