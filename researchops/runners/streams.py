"""Bounded strict JSONL collection, independent of provider/process success.

Codex event names follow its documented non-interactive lifecycle. Antigravity
uses a separate `event` envelope, interpreted by the provider-specific parsers;
this transport collector retains those objects without semantic classification.
Neither a parsed stream nor a turn.completed event authorizes
result import, cleanup, delivery, or a successful run.
"""

import json
import math


class RunnerStreamError(ValueError):
    pass


def _reject_constant(value):
    raise RunnerStreamError("Non-finite JSON values are not valid event data")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise RunnerStreamError("Non-finite JSON values are not valid event data")
    return number


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RunnerStreamError("Duplicate JSON object key in event stream")
        result[key] = value
    return result


class JsonLineEventCollector:
    def __init__(self, provider: str, *, max_total_bytes: int = 1048576,
                 max_line_bytes: int = 262144, max_events: int = 10000):
        if provider not in {"codex", "antigravity"}:
            raise ValueError("Unsupported event provider")
        if any(type(value) is not int or value <= 0 for value in
               (max_total_bytes, max_line_bytes, max_events)):
            raise ValueError("Event stream limits must be positive integers")
        self.provider = provider
        self.max_total_bytes, self.max_line_bytes, self.max_events = max_total_bytes, max_line_bytes, max_events
        self._buffer = bytearray()
        self._events = []
        self._total = 0
        self._closed = False

    def feed(self, chunk: bytes):
        if self._closed:
            raise RunnerStreamError("Event stream is already closed")
        if not isinstance(chunk, bytes):
            raise TypeError("Event stream chunks must be bytes")
        if self._total + len(chunk) > self.max_total_bytes:
            self._closed = True
            raise RunnerStreamError("Event stream exceeded total byte limit")
        self._total += len(chunk)
        self._buffer.extend(chunk)
        try:
            while (newline := self._buffer.find(b"\n")) >= 0:
                raw = bytes(self._buffer[:newline])
                del self._buffer[:newline + 1]
                self._line(raw)
            if len(self._buffer) > self.max_line_bytes:
                raise RunnerStreamError("Event stream exceeded line byte limit")
        except RunnerStreamError:
            self._closed = True
            raise

    def _line(self, raw: bytes):
        if len(raw) > self.max_line_bytes:
            raise RunnerStreamError("Event stream exceeded line byte limit")
        if not raw.strip():
            return
        if len(self._events) >= self.max_events:
            raise RunnerStreamError("Event stream exceeded event count limit")
        try:
            event = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant, parse_float=_finite_float,
                               object_pairs_hook=_unique_object)
        except RunnerStreamError:
            raise  # Preserve specific duplicate-key and non-finite-value diagnostics.
        except (ValueError, RecursionError) as exc:
            # Includes UTF-8/JSON decoding and Python's integer digit-limit errors.
            raise RunnerStreamError("Invalid UTF-8 or JSON event data") from exc
        if not isinstance(event, dict):
            raise RunnerStreamError("Event stream records must be JSON objects")
        kind = event.get("type")
        if kind is not None and (not isinstance(kind, str) or not kind):
            raise RunnerStreamError("Event type must be nonempty text when present")
        category = "unknown"
        if self.provider == "codex":
            category = {"thread.started": "session", "turn.started": "progress",
                        "turn.completed": "completed", "turn.failed": "error", "error": "error",
                        "item.started": "progress", "item.updated": "progress",
                        "item.completed": "progress"}.get(kind, "unknown")
        self._events.append({"provider": self.provider, "sequence": len(self._events) + 1,
                             "type": kind, "category": category, "raw": event})

    def finish(self) -> list[dict]:
        if self._closed:
            raise RunnerStreamError("Event stream is already closed")
        self._closed = True
        self._line(bytes(self._buffer))
        self._buffer.clear()
        return self._events
