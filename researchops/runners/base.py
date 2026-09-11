"""Base runner interface and execution results."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


@dataclass
class RunnerInvocationContext:
    task_id: str
    run_id: str
    attempt: int
    invocation_stage: str  # research or compose
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    timeout_seconds: int = 7200
    network_profile: str = "none"
    resource_profile: str = "standard-research"
    fencing_token: Optional[str] = None
    cancellation_check: Optional[Callable[[], bool]] = field(default=None, repr=False)
    timezone: str = "Asia/Seoul"
    local_date: Optional[str] = None
    local_date_display: Optional[str] = None
    scheduled_for: Optional[str] = None
    # Application-owned evidence; never included in worker prompts/grants.
    trace_log_dir: Optional[Path] = field(default=None, repr=False)
    trace_max_bytes: int = 64 * 1024 * 1024
    trace_max_event_bytes: int = 8 * 1024 * 1024
    trace_max_events: int = 10_000
    trace_preview_bytes: int = 64 * 1024
    # Public acquisition capabilities only; never endpoints or credential references.
    artifact_connection_ids: List[str] = field(default_factory=list)
    # The controller owns this capability and its credentials. Only the staged
    # credential-free file helper is described to the Research worker.
    acquisition_session: Optional[Any] = field(default=None, repr=False)


@dataclass
class RunnerExecutionResult:
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    events: List[Dict[str, Any]] = field(default_factory=list)
    output_files: Dict[str, Path] = field(default_factory=dict)
    error_message: Optional[str] = None
    cleanup_verified: bool = False
    isolation: Dict[str, Any] = field(default_factory=dict)
    log_files: Dict[str, Path] = field(default_factory=dict)
    log_sizes: Dict[str, int] = field(default_factory=dict)
    response_diagnostic: Optional[Dict[str, Any]] = None


def safe_response_diagnostic(value: Any) -> Optional[Dict[str, Any]]:
    """Keep only fixed response-boundary codes and bounded numeric positions."""
    from researchops.runners.response_transport import RESPONSE_DIAGNOSTIC_CODES, RESPONSE_DIAGNOSTIC_STAGES
    if (not isinstance(value, dict) or not isinstance(value.get("code"), str) or
            value["code"] not in RESPONSE_DIAGNOSTIC_CODES or not isinstance(value.get("stage"), str) or
            value["stage"] not in RESPONSE_DIAGNOSTIC_STAGES):
        return None
    result = {"code": value["code"], "stage": value["stage"]}
    for key in ("line", "column", "offset"):
        position = value.get(key)
        minimum = 0 if key == "offset" else 1
        result[key] = position if type(position) is int and minimum <= position <= 2 ** 31 - 1 else None
    return result


class BaseRunner(ABC):
    @abstractmethod
    def execute_research(
        self,
        input_dir: Path,
        tmp_dir: Path,
        output_dir: Path,
        project_dir: Path,
        context: RunnerInvocationContext
    ) -> RunnerExecutionResult:
        """Execute Stage 1 Research invocation."""
        pass

    @abstractmethod
    def execute_compose(
        self,
        input_dir: Path,
        tmp_dir: Path,
        output_dir: Path,
        project_dir: Path,
        context: RunnerInvocationContext
    ) -> RunnerExecutionResult:
        """Execute Stage 2 Compose invocation."""
        pass
