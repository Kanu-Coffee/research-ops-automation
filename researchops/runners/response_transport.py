"""Strict response transport with content-free, stage-specific diagnostics.

Locations refer to the JSON source at the failing stage: line and column are
one-based, and offset is a zero-based Unicode character offset. A position is
omitted when the strict decoder cannot identify one reliably. No model content,
JSON keys, decoder messages or source-bearing exception chains are retained.
"""

import json
import re
from dataclasses import dataclass

from researchops.strict_json import DEFAULT_MAX_JSON_BYTES, strict_json_loads


RESPONSE_DIAGNOSTIC_CODES = frozenset({
    "response_missing", "outer_json_invalid", "outer_size_exceeded",
    "envelope_shape_invalid", "response_json_type_invalid", "inner_json_invalid",
    "inner_size_exceeded", "inner_object_required",
    "file_reference_invalid", "import_size_exceeded", "submission_shape_invalid",
    "submission_file_unsafe", "submission_file_changed", "submission_size_exceeded",
    "submission_json_invalid", "submission_object_required",
})
RESPONSE_DIAGNOSTIC_STAGES = frozenset({"outer_json", "envelope", "inner_json", "inner_type", "import", "submission"})


@dataclass(frozen=True)
class FileResponseReference:
    """An unconsumed reference, never a research/compose result object."""

    response_file: str
    sha256: str
    size_bytes: int
    transport_version: int = 2

    def to_dict(self):
        return {"transport_version": self.transport_version, "response_file": self.response_file,
                "sha256": self.sha256, "size_bytes": self.size_bytes}


class ResponseTransportError(ValueError):
    """Only fixed categories and numeric source positions may cross this boundary."""

    def __init__(self, code, stage, *, line=None, column=None, offset=None):
        if code not in RESPONSE_DIAGNOSTIC_CODES or stage not in RESPONSE_DIAGNOSTIC_STAGES:
            raise ValueError("Invalid response diagnostic category")
        for value, minimum in ((line, 1), (column, 1), (offset, 0)):
            if value is not None and (type(value) is not int or value < minimum):
                raise ValueError("Invalid response diagnostic position")
        self.code, self.stage = code, stage
        self.line, self.column, self.offset = line, column, offset
        fields = ["stage=" + stage]
        fields.extend(f"{key}={value}" for key, value in (("line", line), ("column", column), ("offset", offset))
                      if value is not None)
        super().__init__(code + " (" + ", ".join(fields) + ")")

    def to_dict(self):
        return {"code": self.code, "stage": self.stage, "line": self.line,
                "column": self.column, "offset": self.offset}


def _parse_json(source, stage, max_bytes):
    # Delegate all interchange policy to the existing strict decoder. In
    # particular, do not unescape, strip code fences, repair or retry the input.
    failure = None
    try:
        return strict_json_loads(source, max_bytes=max_bytes)
    except ValueError as exc:
        size_error = str(exc) == "JSON input exceeds the byte limit"
        prefix = {"outer_json": "outer", "inner_json": "inner", "submission": "submission"}[stage]
        code = prefix + ("_size_exceeded" if size_error else "_json_invalid")
        cause = exc.__cause__
        position = {}
        if isinstance(cause, json.JSONDecodeError):
            position = {"line": cause.lineno, "column": cause.colno, "offset": cause.pos}
        failure = ResponseTransportError(code, stage, **position)
    # Raise outside the decoder's except block: the new public exception must
    # not retain JSONDecodeError.doc through __context__ or __cause__.
    raise failure from None


def parse_response_envelope(value, *, max_bytes=DEFAULT_MAX_JSON_BYTES):
    """Validate a legacy string envelope or v2 reference; never read a file here."""
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("Response max_bytes must be a nonnegative integer")
    if value is None:
        raise ResponseTransportError("response_missing", "envelope")
    outer = _parse_json(value, "outer_json", max_bytes) if isinstance(value, str) else value
    if isinstance(outer, dict) and ("transport_version" in outer or "response_file" in outer):
        if (set(outer) != {"transport_version", "response_file", "sha256", "size_bytes"} or
                type(outer.get("transport_version")) is not int or outer["transport_version"] != 2 or
                outer.get("response_file") != "submission.json" or
                not isinstance(outer.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", outer["sha256"]) or
                type(outer.get("size_bytes")) is not int or outer["size_bytes"] < 1):
            raise ResponseTransportError("file_reference_invalid", "envelope")
        if outer["size_bytes"] > max_bytes:
            raise ResponseTransportError("submission_size_exceeded", "submission")
        if isinstance(value, dict):
            _check_decoded_envelope_size(outer, max_bytes)
        return outer
    if not isinstance(outer, dict) or set(outer) != {"response_json"}:
        raise ResponseTransportError("envelope_shape_invalid", "envelope")
    if not isinstance(outer["response_json"], str):
        raise ResponseTransportError("response_json_type_invalid", "envelope")
    if isinstance(value, dict):
        _check_decoded_envelope_size(outer, max_bytes)
    return outer


def _check_decoded_envelope_size(envelope, max_bytes):
    """Decoded native envelopes have no separate raw payload to budget."""
    # Check a lower bound before serializing a large legacy response string.
    if isinstance(envelope.get("response_json"), str) and len(envelope["response_json"]) > max_bytes:
        raise ResponseTransportError("outer_size_exceeded", "outer_json")
    failure = None
    try:
        raw = json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (UnicodeError, ValueError, TypeError):
        failure = ResponseTransportError("outer_json_invalid", "outer_json")
    if failure is not None:
        raise failure from None
    if len(raw) > max_bytes:
        raise ResponseTransportError("outer_size_exceeded", "outer_json")


def parse_response_transport(value, *, max_bytes=DEFAULT_MAX_JSON_BYTES):
    """Decode a legacy object or return an explicitly typed, unconsumed v2 reference."""
    envelope = parse_response_envelope(value, max_bytes=max_bytes)
    if envelope.get("transport_version") == 2:
        return FileResponseReference(**envelope)
    document = _parse_json(envelope["response_json"], "inner_json", max_bytes)
    if not isinstance(document, dict):
        raise ResponseTransportError("inner_object_required", "inner_type")
    return document


def parse_submission_document(raw, *, max_bytes=DEFAULT_MAX_JSON_BYTES):
    """Decode verified submission-file bytes with the same strict JSON policy."""
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("Response max_bytes must be a nonnegative integer")
    document = _parse_json(raw, "submission", max_bytes)
    if not isinstance(document, dict):
        raise ResponseTransportError("submission_object_required", "submission")
    return document
