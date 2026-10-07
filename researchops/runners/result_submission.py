"""Programmatic, bounded result submission; also staged for native workers.

The helper has no credentials or application configuration. Its success is
never trusted by the importer, which independently verifies the referenced file.
"""

from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path

from researchops.strict_json import MAX_JSON_NESTING, strict_json_loads
from researchops.runners.response_transport import ResponseTransportError


MAX_RESPONSE_BYTES = 1_000_000
SUBMISSION_NAME = "submission.json"
SUBMISSION_DIRECTORY = ".researchops-submission"
HELPER_DIRECTORY = ".researchops-submit"
VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
    "source", "track", "wbr",
})
FILE_ENVELOPE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "transport_version": {"type": "integer", "const": 2},
        "response_file": {"type": "string", "const": SUBMISSION_NAME},
        "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "size_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_RESPONSE_BYTES},
    },
    "required": ["transport_version", "response_file", "sha256", "size_bytes"],
}


class HTMLTagBalanceParser(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.text = text
        self.line_starts = [0]
        pos = -1
        while True:
            pos = text.find("\n", pos + 1)
            if pos == -1:
                break
            self.line_starts.append(pos + 1)
        self.stack = []

    def _get_position(self, lineno=None, colno=None):
        if lineno is None or colno is None:
            lineno, colno = self.getpos()
        line = lineno if isinstance(lineno, int) and lineno >= 1 else None
        column = (colno + 1) if isinstance(colno, int) and colno >= 0 else None
        offset = None
        if line is not None and column is not None and 1 <= line <= len(self.line_starts):
            pos = self.line_starts[line - 1] + colno
            if 0 <= pos <= len(self.text):
                offset = pos
        return line, column, offset

    def handle_starttag(self, tag, attrs):
        if tag not in VOID_TAGS:
            line, column, offset = self._get_position()
            self.stack.append((tag, line, column, offset))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if not self.stack or self.stack[-1][0] != tag:
            line, column, offset = self._get_position()
            raise ResponseTransportError("submission_html_unbalanced", "compose_html",
                                         line=line, column=column, offset=offset)
        self.stack.pop()


def check_compose_html_balance(html_text):
    """Reject unbalanced HTML tags before writing a compose submission."""
    parser = HTMLTagBalanceParser(html_text)
    try:
        parser.feed(html_text)
        parser.close()
    except ResponseTransportError:
        raise
    except Exception:
        line, column, offset = parser._get_position()
        raise ResponseTransportError("submission_html_unbalanced", "compose_html",
                                     line=line, column=column, offset=offset) from None
    if parser.stack:
        _, line, column, offset = parser.stack[-1]
        raise ResponseTransportError("submission_html_unbalanced", "compose_html",
                                     line=line, column=column, offset=offset)


def prepare_result(document, stage, *, max_bytes=MAX_RESPONSE_BYTES):
    """Serialize and strictly reparse before creating any submitted result file."""
    if not isinstance(document, dict):
        raise ResponseTransportError("submission_object_required", "submission")
    try:
        raw = bytearray()
        for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(document):
            encoded = chunk.encode("utf-8")
            if len(raw) + len(encoded) > max_bytes:
                raise ResponseTransportError("submission_size_exceeded", "submission")
            raw.extend(encoded)
        raw = bytes(raw)
    except ResponseTransportError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ResponseTransportError("submission_json_invalid", "submission") from None
    if len(raw) > max_bytes:
        raise ResponseTransportError("submission_size_exceeded", "submission")
    # json.dumps accepts tuples and nonstring keys by changing their types.
    # Require an actual JSON object graph, preserving every supplied value.
    pending = [(document, 1)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, (dict, list)):
            if depth > MAX_JSON_NESTING:
                raise ResponseTransportError("submission_json_invalid", "submission")
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise ResponseTransportError("submission_json_invalid", "submission")
                pending.extend((child, depth + 1) for child in item.values())
            else:
                pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ResponseTransportError("submission_json_invalid", "submission")
    try:
        strict_json_loads(raw, max_bytes=max_bytes)
    except ValueError:
        raise ResponseTransportError("submission_json_invalid", "submission") from None
    if stage == "research":
        files = {"result.json": raw}
    elif stage == "compose":
        if (set(document) != {"composition_result", "html", "text"} or
                not isinstance(document["composition_result"], dict) or
                not isinstance(document["html"], str) or not isinstance(document["text"], str)):
            raise ResponseTransportError("submission_shape_invalid", "submission")
        composition = document["composition_result"]
        if composition.get("html_path") != "email.html" or composition.get("text_path") != "email.txt":
            raise ResponseTransportError("submission_shape_invalid", "submission")
        files = {"composition-result.json": json.dumps(composition, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                 "email.html": document["html"].encode("utf-8"), "email.txt": document["text"].encode("utf-8")}
    else:
        raise ResponseTransportError("submission_shape_invalid", "submission")
    if sum(map(len, files.values())) > max_bytes:
        raise ResponseTransportError("import_size_exceeded", "import")
    if stage == "compose":
        check_compose_html_balance(document["html"])
    return raw, files


def submit_result(document, stage, directory):
    """Write one validated JSON document and return its serialized CLI envelope.

    Call from analysis code with a Python dict. JSON nested inside a summary
    must be created with json.dumps too; do not manually escape the envelope.
    """
    raw, _ = prepare_result(document, stage)
    directory = Path(directory)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid():
            raise ResponseTransportError("submission_file_unsafe", "submission")
        out = os.open(SUBMISSION_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(out, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(fd)
    finally:
        os.close(fd)
    envelope = {"transport_version": 2, "response_file": SUBMISSION_NAME,
                "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
    encoded = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
    # Validate the exact envelope we print as well as the submitted document.
    from researchops.runners.response_transport import parse_response_transport
    parse_response_transport(encoded)
    return encoded


def stage_submission_helper(work_dir):
    """Place the standard-library helper in the authorized invocation workspace."""
    work_dir = Path(work_dir)
    helper = work_dir / HELPER_DIRECTORY
    package = helper / "researchops"
    runners = package / "runners"
    runners.mkdir(mode=0o700, parents=True)
    (package / "__init__.py").write_text("")
    (runners / "__init__.py").write_text("")
    source = Path(__file__).parent
    for src, target in ((source.parent / "strict_json.py", package / "strict_json.py"),
                        (source / "response_transport.py", runners / "response_transport.py"),
                        (source / "result_submission.py", runners / "result_submission.py")):
        target.write_bytes(src.read_bytes())
    (work_dir / SUBMISSION_DIRECTORY).mkdir(mode=0o700)
    return helper
