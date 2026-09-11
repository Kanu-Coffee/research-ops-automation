"""Fenced execution milestones with bounded, content-free audit details."""

from datetime import datetime, timezone
import time
import uuid

from researchops.errors import ValidationError


STEP_PHASES = frozenset({"preflight", "research", "validate", "dedupe", "artifact_acquisition",
    "prepare_compose", "compose", "validate_message", "handoff", "finalize",
    "smtp"})
STEP_STATES = frozenset({"running", "succeeded", "failed", "cancelled", "timed_out", "needs_attention",
    "skipped", "uncertain"})
SAFE_SUMMARY_KEYS = frozenset({"record_count", "reportable_count", "excluded_count", "artifact_count",
    "available_count", "failed_count", "excluded_artifact_count", "attachment_count", "inline_count",
    "warning_count", "validation_error_count", "stdout_bytes", "stderr_bytes", "event_count", "exit_code",
    "cleanup_verified", "composition_revision", "file_count", "total_bytes", "recipient_count",
    "attempt_count", "smtp_reply_code", "queued_count", "accepted_count", "mime_bytes"})
STEP_EVENT_TYPES = frozenset({"run_step_started", "run_step_finished"})


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_summary(summary):
    if not isinstance(summary, dict) or set(summary) - SAFE_SUMMARY_KEYS:
        raise ValidationError("Invalid execution step summary")
    if any(type(value) is not int or not -(2**63) <= value < 2**63 or
           (key != "exit_code" and value < 0) or (key == "cleanup_verified" and value not in (0, 1))
           for key, value in summary.items()):
        raise ValidationError("Execution step summaries require bounded numeric counters")
    return dict(summary)


def validate_step_event(event_type, details):
    fields = {"schema_version", "step_id", "phase", "label", "state", "started_at", "finished_at",
              "duration_ms", "attempt", "summary"}
    if (not isinstance(event_type, str) or event_type not in STEP_EVENT_TYPES or not isinstance(details, dict) or set(details) != fields or
            type(details["schema_version"]) is not int or details["schema_version"] != 1 or
            not isinstance(details["phase"], str) or details["phase"] not in STEP_PHASES or details["label"] != details["phase"] or
            not isinstance(details["state"], str) or details["state"] not in STEP_STATES or type(details["attempt"]) is not int or details["attempt"] < 1 or
            not isinstance(details["step_id"], str) or not 1 <= len(details["step_id"]) <= 255 or
            any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in details["step_id"])):
        raise ValidationError("Invalid execution step audit contract")
    missing_smtp_start = (details["phase"] == "smtp" and event_type == "run_step_finished"
                          and details["started_at"] is None and details["duration_ms"] is None)
    unknown_smtp_duration = details["phase"] == "smtp" and details["duration_ms"] is None
    for field in ("started_at", "finished_at"):
        value = details[field]
        if value is None and (field == "finished_at" or missing_smtp_start):
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValidationError("Execution step timestamp must be UTC") from None
    if event_type == "run_step_started":
        if details["state"] != "running" or details["finished_at"] is not None or details["duration_ms"] is not None:
            raise ValidationError("A started step cannot claim a completion")
    elif (details["state"] == "running" or details["finished_at"] is None or
          (not unknown_smtp_duration and (type(details["duration_ms"]) is not int or not 0 <= details["duration_ms"] < 2**63))):
        raise ValidationError("A completed step requires its observed duration and terminal state")
    safe_summary(details["summary"])


class RunTimeline:
    def __init__(self, run_repo, run_id, attempt, fencing_token):
        self.run_repo, self.run_id = run_repo, run_id
        self.attempt, self.fencing_token = attempt, fencing_token
        self.active = None
        self._started_ns = None
        self._summary = {}

    def summarize(self, **values):
        if self.active is not None:
            self._summary.update(safe_summary(values))

    def _completion(self, state, summary=None):
        counters = {**self._summary, **safe_summary(summary or {})}
        return {**self.active, "state": state, "finished_at": utc_now(),
                "duration_ms": max(0, (time.monotonic_ns() - self._started_ns) // 1_000_000), "summary": counters}

    def transition(self, phase, *, coarse_phase=None, summary=None, allow_cancel=False):
        events = []
        if self.active is not None:
            events.append(("run_step_finished", self._completion("succeeded", summary)))
        started_ns = time.monotonic_ns()
        details = {"schema_version": 1, "step_id": f"a{self.attempt}-{phase}-{uuid.uuid4().hex[:12]}",
            "phase": phase, "label": phase, "state": "running", "started_at": utc_now(),
            "finished_at": None, "duration_ms": None, "attempt": self.attempt, "summary": {}}
        events.append(("run_step_started", details))
        self.run_repo.append_run_step_events(self.run_id, self.fencing_token, self.attempt, events,
            coarse_phase=coarse_phase, allow_cancel=allow_cancel)
        self.active, self._started_ns, self._summary = details, started_ns, {}

    def finish(self, state, *, summary=None, conn=None, allow_terminal=False):
        """With an existing transaction, acknowledge_finished only after commit."""
        if self.active is None:
            return None
        details = self._completion(state, summary)
        self.run_repo.append_run_step_events(self.run_id, self.fencing_token, self.attempt,
            [("run_step_finished", details)], allow_cancel=True, allow_terminal=allow_terminal, conn=conn)
        if conn is None:
            self.acknowledge_finished()
        return details

    def acknowledge_finished(self):
        self.active, self._started_ns, self._summary = None, None, {}

    def snapshot(self):
        result = self.run_repo.get_run_event_snapshot(self.run_id)
        return {"schema_version": 1, "run_id": self.run_id, "attempt": self.attempt,
                "captured_at": utc_now(), "snapshot_scope": "before_archive_publication",
                "events": result["step_events"], "event_count": result["step_total_count"],
                "truncated": result["step_total_count"] > len(result["step_events"])}
