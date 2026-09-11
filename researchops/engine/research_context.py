"""Operator-owned research dates; immutable replay inputs without DB migrations."""

from datetime import date, timedelta
import re

from researchops.errors import HardGateError, ValidationError

ISSUERS = ("woori", "shinhan", "kb", "samsung", "hyundai", "hana", "lotte", "bc")
SERIES_PATTERN = r"[a-z0-9][a-z0-9_-]{0,63}"


def validate_context_config(config):
    if config is None:
        return
    if not isinstance(config, dict) or type(config.get("enabled")) is not bool:
        raise ValidationError("research_context.enabled must be a boolean")
    allowed = {"enabled", "active_issuers", "reference_date", "lookback_days", "series_id", "delivery_history"}
    if set(config) - allowed:
        raise ValidationError("Unknown research_context setting")
    if not config["enabled"]:
        return
    issuers = config.get("active_issuers")
    if (not isinstance(issuers, list) or not issuers or len(issuers) > 8
            or any(not isinstance(x, str) or x not in ISSUERS for x in issuers)
            or len(issuers) != len(set(issuers))):
        raise ValidationError("research_context requires unique canonical active_issuers")
    days = config.get("lookback_days")
    if type(days) is not int or not 1 <= days <= 3660:
        raise ValidationError("research_context.lookback_days must be an integer from 1 through 3660")
    if not isinstance(config.get("series_id"), str) or not re.fullmatch(SERIES_PATTERN, config["series_id"]):
        raise ValidationError("research_context.series_id is invalid")
    reference = config.get("reference_date")
    if reference is not None:
        try:
            if not isinstance(reference, str) or date.fromisoformat(reference).isoformat() != reference:
                raise ValueError
            date.fromisoformat(reference) - timedelta(days=days - 1)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValidationError("research_context.reference_date must be a valid ISO calendar date") from exc
    if type(config.get("delivery_history", False)) is not bool:
        raise ValidationError("research_context.delivery_history must be a boolean")


def build_research_context(task, run):
    config = (task.state or {}).get("research_context")
    validate_context_config(config)
    if not config or not config["enabled"]:
        return None
    if task.id != run.task_id or run.timezone != "Asia/Seoul":
        raise HardGateError("Research context task/timezone mismatch")
    try:
        reference = date.fromisoformat(config.get("reference_date") or run.local_date)
        start = reference - timedelta(days=config["lookback_days"] - 1)
    except (ValueError, TypeError, OverflowError) as exc:
        raise HardGateError("Research context date range is invalid") from exc
    return {"schema_version": 1, "task_id": task.id, "task_version_hash": run.task_version_hash,
        "origin_run_id": run.run_id, "parent_run_id": run.parent_run_id,
        "logical_date": run.local_date, "timezone": "Asia/Seoul",
        "active_issuers": list(config["active_issuers"]), "series_id": config["series_id"],
        "reference_date": reference.isoformat(),
        "reference_date_source": "task_override" if config.get("reference_date") else "logical_date",
        "lookback_days": config["lookback_days"], "start_date": start.isoformat(),
        "end_date": reference.isoformat(), "bounds": "inclusive",
        "data_basis": "current_snapshot_filtered_by_launch_date"}


def validate_reused_context(value, task, run):
    """Retry/Compose-only retain dates and origin, never the current wall clock."""
    expected = build_research_context(task, run)
    if expected is None or not isinstance(value, dict):
        raise HardGateError("Parent research context is unavailable")
    mutable_identity = {"origin_run_id", "parent_run_id"}
    if {k: v for k, v in value.items() if k not in mutable_identity} != {
            k: v for k, v in expected.items() if k not in mutable_identity}:
        raise HardGateError("Parent research context conflicts with the sealed task or logical date")
    return value


def validate_retry_origin(value, run, run_repo, archive_root):
    """A pre-Compose retry has no stored composition yet; bind its actual ancestry."""
    current = run
    seen = {run.run_id}
    origin = None
    for _ in range(128):
        if not current.parent_run_id:
            break
        parent = run_repo.get_run(current.parent_run_id)
        if (not parent or parent.run_id in seen or parent.task_id != run.task_id
                or parent.task_version_hash != run.task_version_hash or parent.local_date != run.local_date
                or parent.scheduled_for != run.scheduled_for):
            raise HardGateError("Retry context parent identity mismatch")
        seen.add(parent.run_id)
        if (archive_root / run.task_id / parent.run_id / "research-context.json").exists():
            origin = parent
        current = parent
    else:
        raise HardGateError("Retry context ancestry exceeds the verification limit")
    if origin is None or value.get("origin_run_id") != origin.run_id or value.get("parent_run_id") != origin.parent_run_id:
        raise HardGateError("Retry context origin does not match its actual parent ancestry")
