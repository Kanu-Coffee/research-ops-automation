"""Strict response contract observed with the installed Antigravity CLI.

The CLI emits ``event`` (not ``type``) envelopes and places validated JSON in
``result.structured_output``. Its human response can contain multiple JSON
objects and completion-tool metadata, so it is never used as an import source.

This parser is for explicit, trusted, no-action development validation only.
Rejecting an unexpected action in a completed transcript is *not* a guarantee
that the action was prevented; it is not an arbitrary-task security boundary.
"""

from dataclasses import dataclass
import math

from researchops.runners.streams import JsonLineEventCollector, RunnerStreamError


class AntigravityResponseError(RunnerStreamError):
    """A response cannot be accepted without revealing its untrusted contents."""


@dataclass(frozen=True)
class AntigravityResponse:
    structured_output: dict
    usage: dict[str, int]
    duration_seconds: float | None
    num_turns: int | None
    event_count: int
    step_types: tuple[str, ...]
    model: str | None = None


def _nonnegative_integer(value, label: str) -> int:
    if type(value) is not int or value < 0:
        raise AntigravityResponseError(f"Antigravity {label} must be a nonnegative integer")
    return value


def parse_antigravity_response(raw: bytes, *, max_total_bytes: int = 1048576) -> AntigravityResponse:
    """Extract only a terminal successful structured output from bounded JSONL.

    Callers must separately verify process exit/cleanup, the supplied JSON
    schema, output file safety, and application research/composition contracts.
    No success is inferred from ordinary assistant text or token usage.
    """
    collector = JsonLineEventCollector("antigravity", max_total_bytes=max_total_bytes)
    collector.feed(raw)
    events = [entry["raw"] for entry in collector.finish()]
    if len(events) < 2 or events[0].get("event") != "init":
        raise AntigravityResponseError("Antigravity response is missing its initial event")
    if not isinstance(events[0].get("init"), dict):
        raise AntigravityResponseError("Antigravity initial event must contain an object")
    if events[-1].get("event") != "result":
        raise AntigravityResponseError("Antigravity response is missing its terminal result")

    step_types = []
    for event in events[1:-1]:
        if event.get("event") != "step_update" or not isinstance(event.get("step_update"), dict):
            raise AntigravityResponseError("Antigravity response contains an unsupported lifecycle event")
        step_type = event["step_update"].get("step_type")
        if not isinstance(step_type, str) or step_type not in {"user_input", "agent_response", "finish"}:
            raise AntigravityResponseError("Antigravity response contains an unexpected action step")
        step_types.append(step_type)

    result = events[-1].get("result")
    if not isinstance(result, dict) or result.get("status") != "SUCCESS":
        raise AntigravityResponseError("Antigravity did not return a successful terminal result")
    if result.get("denied_actions") or result.get("error"):
        raise AntigravityResponseError("Antigravity reported denied actions or a terminal error")
    structured_output = result.get("structured_output")
    if not isinstance(structured_output, dict):
        raise AntigravityResponseError("Antigravity successful result has no structured output object")

    # Metadata is optional evidence, never a substitute for terminal success.
    raw_usage = result.get("usage", {})
    if not isinstance(raw_usage, dict):
        raise AntigravityResponseError("Antigravity usage must be an object")
    usage = {key: _nonnegative_integer(value, "usage value") for key, value in raw_usage.items()}
    duration = result.get("duration_seconds")
    if duration is not None:
        if type(duration) not in {int, float}:
            raise AntigravityResponseError("Antigravity duration must be a finite nonnegative number")
        try:
            duration = float(duration)
        except OverflowError as exc:
            raise AntigravityResponseError("Antigravity duration exceeds the supported range") from exc
        if not math.isfinite(duration) or duration < 0:
            raise AntigravityResponseError("Antigravity duration must be a finite nonnegative number")
    turns = result.get("num_turns")
    if turns is not None:
        turns = _nonnegative_integer(turns, "turn count")

    # The observed CLI did not report a model identity. Do not guess the
    # authenticated daemon's configured/default model from a marketing name.
    return AntigravityResponse(structured_output=structured_output, usage=usage,
                              duration_seconds=duration, num_turns=turns,
                              event_count=len(events), step_types=tuple(step_types))
