"""A real, topic-independent research package built from operator input.

This is not a fixture: its instructions contain only the operator's requested
work, and its contracts deliberately avoid industry-specific business fields.
"""

import json
from typing import Dict, Optional

import yaml

from researchops.errors import ValidationError


def build_production_package(*, task_id: str, name: str, instructions: str,
                             runner_type: str, recipient_group_id: str,
                             cron: str, schedule_enabled: bool,
                             model: Optional[str] = None,
                             task_md: Optional[str] = None,
                             email_spec_md: Optional[str] = None,
                             sender_profile_id: str = "default",
                             recipient_routing_mode: str = "legacy_ids",
                             stage_settings: Optional[dict] = None) -> Dict[str, str]:
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise ValidationError("Task name must contain 1–200 characters")
    if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 100_000:
        raise ValidationError("Research instructions must contain 1–100000 characters")
    for label, text in (("task.md", task_md), ("email_spec.md", email_spec_md)):
        if text is not None and (not isinstance(text, str) or not text.strip() or len(text) > 100_000):
            raise ValidationError(f"{label} must contain 1–100000 characters")
    if runner_type not in {"codex_exec", "antigravity_exec"}:
        raise ValidationError("Production tasks require Codex or Antigravity")
    if recipient_routing_mode not in {"legacy_ids", "catalog_name"}:
        raise ValidationError("Unknown recipient routing mode")
    if type(schedule_enabled) is not bool:
        raise ValidationError("Schedule enablement must be a boolean")
    if model is not None and (not isinstance(model, str) or len(model) > 200 or
                              any(c in model for c in "\r\n\x00")):
        raise ValidationError("Invalid runner model")
    runner = {"type": runner_type, "session_mode": "fresh", "timeout_seconds": 7200,
              "max_attempts": 2, "concurrency": 1, "sandbox": "task-workspace",
              "network_profile": "public-research", "resource_profile": "standard-research",
              "model": model or None, "reasoning_effort": None}
    if stage_settings is not None:
        if not isinstance(stage_settings, dict) or set(stage_settings) != {"research", "compose"}:
            raise ValidationError("Research and Compose settings are required")
        runner["stages"] = {stage: dict(value) for stage, value in stage_settings.items()}
        runner.update(runner["stages"]["research"])
    config = {
        "version": 2, "id": task_id, "name": name.strip(), "enabled": schedule_enabled,
        "schedule": {"cron": cron, "timezone": "Asia/Seoul", "misfire_policy": "enqueue_once"},
        "workspace": {"mode": "persistent_task", "access": "read_write_execute"},
        "runner": runner,
        "instructions": {"research_files": ["task.md"], "compose_files": ["task.md", "email_spec.md"]},
        "output": {"research_schema": "output.schema.json",
                   "composition_record_schema": "composition-record.schema.json",
                   "composition_schema": "composition.schema.json",
                   "html_path": "email.html", "text_path": "email.txt"},
        "delivery": {"message_type": "research_digest", "mode": "handoff",
                     "sender_profile_id": sender_profile_id,
                     "allowed_recipient_group_ids": [recipient_group_id], "send_on_empty": True,
                     "partial_policy": "send_with_warning"},
        "state": {"dedupe": {"enabled": False}},
        "retention": {"run_days": 180, "artifact_days": 365, "log_days": 180},
    }
    schema_base = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}
    research_schema = {
        **schema_base, "required": ["status", "summary", "records"],
        "properties": {"status": {"enum": ["success", "no_updates", "partial", "needs_attention"]},
                       "summary": {"type": "string"},
                       "records": {"type": "array", "items": {"type": "object"}}},
        "additionalProperties": True,
    }
    record_schema = {**schema_base, "required": ["record_id"],
                     "properties": {"record_id": {"type": "string", "minLength": 1}},
                     "additionalProperties": True}
    composition_schema = {
        **schema_base,
        "required": ["recipient_group_id", "recipient_group_reason", "subject", "html_path",
                     "text_path", "included_record_ids"],
        "properties": {
            "recipient_group_id": {"enum": [recipient_group_id]},
            "recipient_group_reason": {"type": "string", "minLength": 1, "maxLength": 2000},
            "subject": {"type": "string", "minLength": 1, "maxLength": 998, "pattern": "^[^\\r\\n]+$"},
            "html_path": {"const": "email.html"}, "text_path": {"const": "email.txt"},
            "included_record_ids": {"type": "array", "uniqueItems": True,
                                    "items": {"type": "string", "minLength": 1}},
        },
        "additionalProperties": False,
    }
    if recipient_routing_mode == "catalog_name":
        config["delivery"].pop("allowed_recipient_group_ids")
        config["delivery"]["recipient_routing_mode"] = "catalog_name"
        composition_schema["required"][0] = "recipient_group_name"
        del composition_schema["properties"]["recipient_group_id"]
        composition_schema["properties"]["recipient_group_name"] = {
            "type": "string", "minLength": 1, "maxLength": 200}
    default_task_md = """# Operator research task

Follow `invocation_stage` from the application context. The application supplies
the logical date and timezone; use Asia/Seoul and do not replace a retry's date.

## Requested work

""" + instructions.strip() + """

## Research phase contract

When invocation_stage is research, carry out the requested work using available
web/search, MCP, file and code tools where needed. Prefer original public sources;
record actual source URLs, publication dates and retrieval dates when available.
Do not invent facts, citations, tool executions, or unavailable source content.
Keep all relevant records, identify uncertainty, and distinguish a genuine absence
of updates from inaccessible or incomplete research. Do not send email or access
SMTP credentials, recipient addresses, unrelated tasks, or application state.

Return result.json matching output.schema.json. Its status is success,
no_updates, partial, or needs_attention; summary is readable text; records is an
array of objects. Prefer each record to have title, summary, sources, and any
task-specific detail. Include coverage as {complete: boolean,
expected_target_count: integer, completed_target_count: integer, issues: array}.
Each coverage issue has target, status (blocked or unavailable), and notes.
Include warnings as an array of strings and artifacts as an array (empty when no
files are being attached). Do not include recipient_group_id or recipient_group_name in research output.

## Compose phase contract

When invocation_stage is compose, do not repeat research. Use the immutable
composition-input.json and email_spec.md. Preserve its reportable records and
their application-assigned record_id values. Generate the final subject, HTML
and plain text yourself; the application forwards these bodies unchanged.
Select exactly the opaque recipient group in allowed_recipient_group_ids, never
an address. Produce composition-result.json, email.html and email.txt as required
by the application output contract and composition.schema.json.
"""
    email_spec = """# Email presentation

Write a useful, polished research digest in the language of the operator's
instructions (Korean when not specified). The subject should describe the actual
findings and include the run's logical date. Start with an executive summary,
then present every reportable record with meaningful detail and verified source
links. Preserve warnings, uncertainty and incomplete coverage visibly.

Use `run.local_date_display` from composition-input.json in both HTML and text.
Create balanced <html><head>...</head><body>...</body></html>. Add exactly one
canonical date marker on body: data-local-date="YYYY-MM-DD", using run.local_date
(the hyphenated date, not the dotted display date). Do not add a second date marker.
Every included record must appear exactly once in HTML as data-record-id="THE_RECORD_ID" and
in plain text with its exact record_id. included_record_ids must contain exactly
all reportable record IDs. When no records exist, explain the actual research
outcome without fabricating findings and still write a complete email.

Use self-contained email-compatible HTML with escaped source text, inline styles
and ordinary https source links. No scripts, forms, iframes, tracking resources,
remote CSS/fonts/images, or external image loads. Do not invent inline artifacts
or attachments; use only those explicitly included in composition-input.json.
Produce an equivalent useful plain-text body, not merely a link to the HTML.
"""
    if recipient_routing_mode == "catalog_name":
        default_task_md = default_task_md.replace(
            "Select exactly the opaque recipient group in allowed_recipient_group_ids, never\nan address.",
            "Follow the recipient selection rules in task.md. Select exactly one display_name\n"
            "from recipient_groups in composition-input.json and return recipient_group_name\n"
            "and recipient_group_reason. Never return an address or invent a group name.")
    return {
        "task.yaml": yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        "task.md": task_md if task_md is not None else default_task_md,
        "email_spec.md": email_spec_md if email_spec_md is not None else email_spec,
        "output.schema.json": json.dumps(research_schema, ensure_ascii=False, indent=2),
        "composition-record.schema.json": json.dumps(record_schema, ensure_ascii=False, indent=2),
        "composition.schema.json": json.dumps(composition_schema, ensure_ascii=False, indent=2),
    }
