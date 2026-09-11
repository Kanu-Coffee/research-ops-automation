"""Domain and operational exceptions for ResearchOps."""

from typing import Any, Dict, List, Optional


class ResearchOpsError(Exception):
    """Base exception for all ResearchOps errors."""
    pass


class ConfigError(ResearchOpsError):
    """Configuration error."""
    pass


class ValidationError(ResearchOpsError):
    """Validation failure error."""
    def __init__(self, message: str, errors: Optional[List[str]] = None, warnings: Optional[List[str]] = None):
        super().__init__(message)
        self.errors = errors or []
        self.warnings = warnings or []


class HardGateError(ValidationError):
    """Hard technical gate failure. Candidate must not proceed."""
    pass


class WorkspaceError(ResearchOpsError):
    """Workspace isolation, path or lock failure."""
    pass


class RunnerError(ResearchOpsError):
    """AI Worker runner execution failure."""
    def __init__(self, message: str, exit_code: Optional[int] = None, stderr: Optional[str] = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class DeliveryError(ResearchOpsError):
    """Delivery handoff publication or formatting failure."""
    pass


class ReceiptError(ResearchOpsError):
    """Delivery receipt verification failure."""
    pass


class ConcurrencyError(ResearchOpsError):
    """Workspace or task concurrency fencing conflict."""
    pass


class NotFoundError(ResearchOpsError):
    """Requested resource not found."""
    pass
