"""Audit and domain events for ResearchOps."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass
class AuditEvent:
    entity_type: str  # task, draft, version, run, handoff, receipt, workspace
    entity_id: str
    event_type: str
    details: Dict[str, Any]
    actor: str = "single-operator"
    occurred_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    event_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
