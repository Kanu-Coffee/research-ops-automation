"""Core domain models and dataclasses for ResearchOps."""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import json


@dataclass(frozen=True)
class TaskDefinition:
    id: str
    name: str
    enabled: bool
    workspace: Dict[str, Any]
    runner: Dict[str, Any]
    instructions: Dict[str, List[str]]
    output: Dict[str, str]
    delivery: Dict[str, Any]
    description: Optional[str] = None
    schedule: Optional[Dict[str, Any]] = None
    state: Optional[Dict[str, Any]] = None
    retention: Optional[Dict[str, Any]] = None
    alerting: Optional[Dict[str, Any]] = None
    version: int = 2

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class TaskVersion:
    task_id: str
    version_hash: str
    sealed_at: str
    definition: TaskDefinition
    package_files: Dict[str, str]  # relative_path -> content
    is_active: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "version_hash": self.version_hash,
            "sealed_at": self.sealed_at,
            "definition": self.definition.to_dict(),
            "package_files": self.package_files,
            "is_active": self.is_active
        }


@dataclass
class ScheduledRun:
    run_id: str
    task_id: str
    task_version_hash: str
    scheduled_for: str
    timezone: str
    local_date: str
    local_date_display: str
    trigger_type: str  # schedule, manual, retry, test, candidate_dry_run
    status: str = "queued"  # queued, running, awaiting_receipt, succeeded, failed, timed_out, cancelled, needs_attention
    phase: str = "queued"   # queued, preflight, research, validate, dedupe, compose, validate_message, handoff, finalize
    attempt: int = 0
    workspace_generation: int = 1
    parent_run_id: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_heartbeat_at: Optional[str] = None
    last_progress_at: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RunLease:
    run_id: str
    worker_id: str
    attempt: int
    fencing_token: str
    claimed_at: str
    heartbeat_at: str
    lease_expires_at: str


@dataclass
class ResearchResult:
    status: str  # success, no_updates, partial, needs_attention
    summary: str
    records: List[Dict[str, Any]]
    coverage: Dict[str, Any]
    warnings: List[str]
    raw_json: str
    artifacts: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class CompositionInput:
    task_id: str
    run_id: str
    task_version_hash: str
    composition_revision: int
    run: Dict[str, str]
    result: Dict[str, Any]
    coverage: Dict[str, Any]
    allowed_recipient_group_ids: List[str]
    reportable_records: List[Dict[str, Any]]
    inline_artifacts: List[Dict[str, Any]]
    attachments: List[Dict[str, Any]]
    schema_version: int = 2
    recipient_routing_mode: str = "legacy_ids"
    recipient_groups: List[Dict[str, str]] = field(default_factory=list)
    artifact_report: Optional[Dict[str, Any]] = None
    research_context: Optional[Dict[str, Any]] = None
    delivery_history: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        if self.schema_version == 2:
            value.pop("recipient_routing_mode")
            value.pop("recipient_groups")
        if self.schema_version in (2, 3):
            value.pop("artifact_report")
        for key in ("research_context", "delivery_history"):
            if value[key] is None:
                value.pop(key)
        return value


@dataclass(frozen=True)
class CompositionResult:
    recipient_group_id: str
    recipient_group_reason: str
    subject: str
    html_path: str
    text_path: str
    included_record_ids: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DeliveryHandoff:
    handoff_id: str
    idempotency_key: str
    run_id: str
    task_id: str
    task_version_hash: str
    message_revision: int
    message_type: str
    recipient_group_id: str
    mode: str  # disabled, dry_run, handoff
    status: str  # not_requested, pending, prepared, published, acknowledged, failed, uncertain, skipped
    delivery_request: Dict[str, Any]
    delivery_request_sha256: str
    published_at: Optional[str] = None
    external_receipt_id: Optional[str] = None
    acknowledged_at: Optional[str] = None
    external_delivery_status: Optional[str] = None  # accepted, sent, failed, uncertain
    receipt_sha256: Optional[str] = None
    receipt_trust_status: Optional[str] = None  # verified, rejected
    decision_reason: Optional[str] = None


@dataclass(frozen=True)
class DeliveryReceipt:
    external_receipt_id: str
    handoff_id: str
    idempotency_key: str
    delivery_request_sha256: str
    status: str  # accepted, sent, failed, uncertain
    occurred_at: str
    proof: Dict[str, Any]
    schema_version: int = 2
    external_message_ref: Optional[str] = None
    error: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class ReportedItem:
    task_id: str
    entity_key: str
    content_fingerprint: str
    run_id: str
    handoff_id: str
    reported_at: str


@dataclass(frozen=True)
class Artifact:
    relative_path: str
    role: str
    sha256: str
    size_bytes: int
    mime_type: str
    logical_name: Optional[str] = None
