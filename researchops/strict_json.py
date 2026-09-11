"""Bounded JSON decoding for model output and other untrusted messages.

This is a syntax/interchange boundary, not a business-content filter. The
caller owns its object/schema requirements and retains the original bytes.
"""

import json
import math
from typing import Any


DEFAULT_MAX_JSON_BYTES = 1_000_000
MAX_JSON_INTEGER_DIGITS = 4300
MAX_JSON_NESTING = 128


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-finite JSON number is forbidden")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number is forbidden")
    return number


def _bounded_integer(value):
    if len(value.lstrip("-")) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError("JSON integer exceeds the digit limit")
    try:
        return int(value)
    except ValueError as exc:
        # Python can impose a stricter interpreter-level digit limit. Do not
        # change that process-wide setting or include model data in the error.
        raise ValueError("JSON integer exceeds the interpreter digit limit") from exc


def strict_json_loads(text: str | bytes, *, max_bytes: int = DEFAULT_MAX_JSON_BYTES) -> Any:
    """Decode exactly one UTF-8 JSON value without ambiguous or lossy fields.

    Reject duplicate keys, non-finite numbers (including exponent overflow),
    oversized integers, invalid Unicode and excessive parser nesting. The byte
    limit counts UTF-8, not characters. All rejected input raises ValueError;
    arrays/scalars are allowed here and may be rejected by a caller's schema.
    """
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("JSON max_bytes must be a nonnegative integer")
    if not isinstance(text, (str, bytes)):
        raise ValueError("JSON input must be str or bytes")
    try:
        if isinstance(text, bytes):
            if len(text) > max_bytes:
                raise ValueError("JSON input exceeds the byte limit")
            source = text.decode("utf-8", errors="strict")
        else:
            # ASCII is the minimum encoded size, so avoid copying oversized
            # strings just to calculate their final UTF-8 length.
            if len(text) > max_bytes or len(text.encode("utf-8")) > max_bytes:
                raise ValueError("JSON input exceeds the byte limit")
            source = text
    except UnicodeError as exc:
        raise ValueError("JSON input must be valid UTF-8") from exc
    try:
        value = json.loads(source, object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant, parse_float=_finite_float,
                           parse_int=_bounded_integer)
    except RecursionError as exc:
        raise ValueError("JSON input exceeds the parser nesting limit") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON syntax at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc

    # A valid UTF-8 source can still spell an unpaired surrogate with a JSON
    # escape. Such values cannot safely be re-encoded into downstream archives.
    # Interpreter parser recursion thresholds differ between Python versions.
    # Enforce our own stable interchange depth instead of relying on them.
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, (dict, list)) and depth > MAX_JSON_NESTING:
            raise ValueError("JSON input exceeds the nesting limit")
        if isinstance(item, str):
            try:
                item.encode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise ValueError("JSON strings must contain valid Unicode scalar values") from exc
        elif isinstance(item, dict):
            pending.extend((key, depth + 1) for key in item.keys())
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value
