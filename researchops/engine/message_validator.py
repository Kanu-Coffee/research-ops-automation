"""Validate complete messages without rewriting worker-authored bytes."""
import hashlib
import json
import re
import struct
import zlib
from datetime import datetime
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from jsonschema import Draft202012Validator, FormatChecker

from researchops.domain.models import CompositionInput, CompositionResult, TaskDefinition
from researchops.errors import ValidationError
from researchops.strict_json import strict_json_loads
from researchops.workspace.security import read_safe_bytes


class HTMLSecurityParser(HTMLParser):
    FORBIDDEN_TAGS = {"script", "iframe", "object", "embed", "form", "input", "button", "select",
                      "textarea", "svg", "math", "base", "link", "audio", "video", "source", "applet",
                      "plaintext", "xmp", "noembed", "noframes", "noscript", "template", "frameset",
                      "frame", "keygen", "bgsound"}
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
                 "source", "track", "wbr"}
    SAFE_CSS_FUNCTIONS = {"rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklab", "oklch", "color",
        "color-mix", "calc", "min", "max", "clamp", "var", "env", "linear-gradient", "radial-gradient",
        "conic-gradient", "repeating-linear-gradient", "repeating-radial-gradient", "repeating-conic-gradient",
        "translate", "translatex", "translatey", "scale", "scalex", "scaley", "rotate", "skew", "matrix",
        "cubic-bezier", "steps", "not", "is", "where", "has", "nth-child", "nth-of-type", "nth-last-child",
        "nth-last-of-type", "lang"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.errors = []
        self.cids_found = set()
        self.cid_counts = Counter()
        self.cid_record_ids = {}
        self.record_ids_found = set()
        self.record_counts = Counter()
        self.local_dates = set()
        self.date_counts = Counter()
        self.remote_resources_found = []
        self.tags = set()
        self.tag_counts = Counter()
        self.stack = []
        self.record_stack = []

    def _css(self, value):
        # Escapes/comments can conceal url()/@import; email CSS must omit them.
        if re.search(r"url\s*\(|(?:image|image-set|cross-fade|expression)\s*\(|@import|behavior\s*:|-moz-binding|[\\]|/\*", value, re.I):
            self.errors.append("Forbidden resource or active expression in CSS")
        functions = re.findall(r"([A-Za-z_-][A-Za-z0-9_-]*)\(", value)
        if any(function.lower() not in self.SAFE_CSS_FUNCTIONS for function in functions):
            self.errors.append("CSS function is outside the passive email CSS allowlist")

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        self.tag_counts[tag] += 1
        if tag == "html" and (self.stack or self.tag_counts[tag] != 1):
            self.errors.append("HTML must have exactly one outer html element")
        if tag in ("head", "body") and (self.stack != ["html"] or self.tag_counts[tag] != 1):
            self.errors.append("HTML must have at most one head and exactly one body inside html")
        if tag in self.FORBIDDEN_TAGS:
            self.errors.append(f"Forbidden active content HTML tag: <{tag}>")
        names = [name for name, _ in attrs]
        if len(names) != len(set(names)):
            self.errors.append("Duplicate HTML attributes")
        attributes = dict(attrs)
        if tag == "meta":
            if "http-equiv" in attributes:
                self.errors.append("HTTP-equivalent meta directives are forbidden")
            if attributes.get("name") == "researchops-local-date":
                marker = attributes.get("content") or ""
                self.local_dates.add(marker)
                self.date_counts[marker] += 1
        for name, value in attrs:
            value = (value or "").strip()
            compact = re.sub(r"[\x00-\x20\x7f]", "", value).lower()
            if name.startswith("on") or name in ("srcdoc", "xlink:href", "xmlns", "ping"):
                self.errors.append(f"Forbidden HTML attribute: {name}")
            if compact.startswith(("javascript:", "vbscript:")):
                self.errors.append(f"Forbidden script URI '{value}'")
            if name == "style":
                self._css(value)
            if name in ("src", "srcset", "lowsrc", "dynsrc", "background", "poster", "data", "action", "formaction"):
                if tag == "img" and name == "src" and compact.startswith("cid:") and value[4:]:
                    cid = value[4:]
                    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@+-]*", cid):
                        self.errors.append("Invalid CID reference")
                    self.cids_found.add(cid)
                    self.cid_counts[cid] += 1
                    regions = {rid for rid in self.record_stack if rid}
                    if attributes.get("data-record-id"):
                        regions.add(attributes["data-record-id"].strip())
                    self.cid_record_ids.setdefault(cid, set()).update(regions)
                else:
                    self.remote_resources_found.append(value)
            if name == "href":
                if tag != "a" or urlsplit(value).scheme.lower() not in ("http", "https", "mailto"):
                    self.errors.append("Only ordinary http/https/mailto anchor links are allowed")
            if name == "data-record-id":
                if "body" not in self.stack and tag != "body":
                    self.errors.append("Record markers must occur in the HTML body")
                self.record_ids_found.add(value)
                self.record_counts[value] += 1
            if name == "data-local-date":
                self.local_dates.add(value)
                self.date_counts[value] += 1
        if tag not in self.VOID_TAGS:
            self.stack.append(tag)
            self.record_stack.append((attributes.get("data-record-id") or "").strip())

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID_TAGS:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"Unbalanced HTML closing tag: {tag}")
        else:
            self.stack.pop()
            self.record_stack.pop()

    def handle_data(self, data):
        if not self.stack and data.strip():
            self.errors.append("Text outside the HTML document")
        if self.stack and self.stack[-1] == "style":
            self._css(data)

    def handle_comment(self, data):
        if re.search(r"\[\s*if\b|\[\s*endif\b", data, re.I):
            self.errors.append("Conditional HTML comments cannot bypass message validation")


class MessageValidator:
    def __init__(self, schemas_dir: Path, max_message_bytes: int = 20_000_000):
        self.schemas_dir = schemas_dir
        self.max_message_bytes = max_message_bytes

    @property
    def composition_result_schema(self):
        return json.loads((self.schemas_dir / "composition-result.schema.json").read_text(encoding="utf-8"))

    def validate_composition(self, compose_output_dir: Path, comp_input: CompositionInput,
                             task_def: TaskDefinition, task_composition_schema=None):
        errors, warnings = [], []
        root = compose_output_dir
        if comp_input.schema_version == 4:
            from researchops.engine.composition_input import CompositionInputBuilder
            from researchops.delivery.artifact_integrity import validate_artifact_contract
            schema = CompositionInputBuilder(self.schemas_dir).schema
            if any(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(comp_input.to_dict())):
                raise ValidationError("Composition artifact input schema validation failed")
            validate_artifact_contract(comp_input)
        try:
            result_bytes = read_safe_bytes(root / "composition-result.json", root, 1_000_000)
            data = strict_json_loads(result_bytes)
        except Exception as exc:
            raise ValidationError(f"Cannot safely read composition-result.json: {exc}") from exc
        for schema in (self.composition_result_schema, task_composition_schema):
            if schema:
                errors.extend(error.message for error in Draft202012Validator(
                    schema, format_checker=FormatChecker()).iter_errors(data))
        if errors:
            raise ValidationError("Composition result schema validation failed", errors=errors)
        from researchops.delivery.recipient_routing import resolve_recipient, routing_mode, recipient_resolution
        try:
            selected_group_id = resolve_recipient(data, comp_input, task_def)
        except ValidationError as exc:
            raise ValidationError("Message recipient validation failed", errors=[str(exc)]) from exc
        if task_def.id != comp_input.task_id:
            errors.append("Composition task identity mismatch")
        try:
            scheduled = datetime.fromisoformat(comp_input.run["scheduled_for"].replace("Z","+00:00"))
            if (scheduled.tzinfo is None or comp_input.run["timezone"] != "Asia/Seoul" or
                    scheduled.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat() != comp_input.run["local_date"]):
                errors.append("Composition business date must be derived from the scheduled Seoul time")
        except (KeyError,TypeError,ValueError):
            errors.append("Invalid canonical composition run date")
        if any(ord(c) < 32 or ord(c) == 127 for c in data["subject"]):
            errors.append("Subject contains forbidden newline/CR/control characters")
        if not data["subject"].strip():
            errors.append("Message subject must not be blank")
        expected = [record["record_id"] for record in comp_input.reportable_records]
        included = data["included_record_ids"]
        if len(expected) != len(set(expected)) or len(included) != len(set(included)):
            errors.append("Duplicate record identity")
        if set(expected) != set(included):
            errors.append("Composition record parity failed; silent drop forbidden")
        try:
            html_bytes = read_safe_bytes(root / data["html_path"], root, self.max_message_bytes)
            text_bytes = read_safe_bytes(root / data["text_path"], root, self.max_message_bytes)
            html_content, text_content = html_bytes.decode("utf-8"), text_bytes.decode("utf-8")
        except Exception as exc:
            raise ValidationError(f"Unsafe or invalid UTF-8 message file: {exc}", errors=errors) from exc
        if not html_content.strip() or not text_content.strip():
            errors.append("Both HTML and plain text bodies must be nonempty")
        for key in ("html_path", "text_path"):
            if task_def.output.get(key) and data[key] != task_def.output[key]:
                errors.append(f"Composition {key} does not match the sealed task destination")
        if "\x00" in html_content or "\x00" in text_content:
            errors.append("Message text contains a NUL character")
        parser = HTMLSecurityParser()
        try:
            parser.feed(html_content)
            parser.close()
        except Exception as exc:
            errors.append(f"HTML parsing failed: {exc}")
        errors.extend(parser.errors)
        if parser.stack or parser.tag_counts["html"] != 1 or parser.tag_counts["body"] != 1:
            errors.append("HTML must contain balanced html/body elements")
        if parser.remote_resources_found:
            errors.append("HTML references remote or undeclared embedded resources")
        if parser.record_ids_found != set(expected) or any(n != 1 for n in parser.record_counts.values()):
            errors.append("HTML record markers do not match composition input exactly")
        if parser.local_dates != {comp_input.run["local_date"]} or sum(parser.date_counts.values()) != 1:
            errors.append("HTML canonical local-date marker does not match Asia/Seoul business date")
        artifacts = comp_input.inline_artifacts + comp_input.attachments
        declared = [art["cid"] for art in comp_input.inline_artifacts]
        if (len(declared) != len(set(declared)) or parser.cids_found != set(declared) or
                any(count != 1 for count in parser.cid_counts.values()) or
                any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@+-]*",cid) for cid in declared)):
            errors.append("HTML CID references must match declared inline artifacts")
        if comp_input.schema_version == 4:
            for artifact in comp_input.inline_artifacts:
                if (len(artifact["record_ids"]) == 1 and
                        parser.cid_record_ids.get(artifact["cid"], set()) != set(artifact["record_ids"])):
                    errors.append("Inline artifact CID must occur inside its associated record region")
        paths = {data["html_path"], data["text_path"], "composition-result.json"}
        if len(paths) != 3:
            errors.append("HTML/text/result paths must be distinct")
        total = len(html_bytes) + len(text_bytes) + len(result_bytes)
        file_evidence = {
            data["html_path"]: {"sha256": hashlib.sha256(html_bytes).hexdigest(), "size_bytes": len(html_bytes)},
            data["text_path"]: {"sha256": hashlib.sha256(text_bytes).hexdigest(), "size_bytes": len(text_bytes)}}
        for artifact in artifacts:
            try:
                if artifact in comp_input.inline_artifacts and not artifact["mime_type"].startswith("image/"):
                    raise ValueError("Inline CID artifacts must be validated raster images")
                path = artifact["path"]
                if path in paths:
                    raise ValueError("Duplicate artifact/message path")
                paths.add(path)
                raw = read_safe_bytes(root / path, root, self.max_message_bytes)
                if len(raw) != artifact["size_bytes"] or hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
                    raise ValueError("Artifact size/hash changed after research import")
                validate_media(raw, artifact["mime_type"])
                file_evidence[path] = {"sha256": artifact["sha256"], "size_bytes": len(raw)}
                total += len(raw)
            except Exception as exc:
                errors.append(f"Artifact contract failed: {exc}")
        if total > self.max_message_bytes:
            errors.append("Total message size limit exceeded")
        if errors:
            raise ValidationError("Message validation failed", errors=errors, warnings=warnings)
        hashes = {"html": hashlib.sha256(html_bytes).hexdigest(),
                  "text": hashlib.sha256(text_bytes).hexdigest(),
                  "composition_result": hashlib.sha256(result_bytes).hexdigest(), "warnings": warnings}
        if routing_mode(task_def) == "catalog_name":
            hashes["recipient_resolution"] = recipient_resolution(data, comp_input, task_def, result_bytes)
        if comp_input.schema_version == 4:
            from researchops.delivery.artifact_integrity import composition_binding
            hashes["composition_binding"] = composition_binding(comp_input, result_bytes, file_evidence)
        result = CompositionResult(recipient_group_id=selected_group_id, **{key: data[key] for key in (
            "recipient_group_reason", "subject", "html_path", "text_path", "included_record_ids")})
        return result, html_content, text_content, hashes


def validate_media(raw: bytes, media_type: str):
    """Check safe media signatures; active images/HTML are never mail artifacts."""
    signatures = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff",
                  "application/pdf": b"%PDF-"}
    if media_type in signatures and not raw.startswith(signatures[media_type]):
        raise ValueError(f"Invalid {media_type} signature")
    if media_type == "image/png":
        _validate_png(raw)
    if media_type == "image/jpeg":
        _validate_jpeg(raw)
    if media_type == "image/gif" and not raw.startswith((b"GIF87a", b"GIF89a")):
        raise ValueError("Invalid GIF signature")
    if media_type == "image/gif":
        _validate_gif(raw)
    if media_type == "application/pdf" and not raw.rstrip().endswith(b"%%EOF"):
        raise ValueError("Incomplete PDF artifact")
    if media_type in ("text/plain", "text/csv", "application/json"):
        raw.decode("utf-8")
        if media_type == "application/json":
            strict_json_loads(raw, max_bytes=20_000_000)
    elif media_type not in (*signatures, "image/gif", "application/octet-stream"):
        raise ValueError(f"Unsupported artifact MIME type {media_type}")


def _validate_png(raw):
    """Validate chunks, CRCs and bounded scanline data, not just the magic prefix."""
    position, header, image_data, ended = 8, None, [], False
    palette = False
    while position < len(raw):
        if position + 12 > len(raw):
            raise ValueError("Truncated PNG chunk")
        length = struct.unpack_from(">I",raw,position)[0]
        kind = raw[position+4:position+8]
        end = position + 12 + length
        if end > len(raw) or not re.fullmatch(rb"[A-Za-z]{4}",kind):
            raise ValueError("Invalid PNG chunk length or type")
        contents = raw[position+8:position+8+length]
        crc = struct.unpack_from(">I",raw,position+8+length)[0]
        if zlib.crc32(kind+contents) & 0xffffffff != crc:
            raise ValueError("Invalid PNG chunk CRC")
        if header is None and kind != b"IHDR":
            raise ValueError("PNG must start with IHDR")
        if kind == b"IHDR":
            if header is not None or length != 13:
                raise ValueError("Invalid PNG header")
            header = struct.unpack(">IIBBBBB",contents)
        elif kind == b"IDAT":
            image_data.append(contents)
        elif kind == b"PLTE":
            if image_data or length == 0 or length > 768 or length % 3:
                raise ValueError("Invalid PNG palette")
            palette = True
        elif kind == b"IEND":
            if length or end != len(raw):
                raise ValueError("Invalid PNG end or trailing payload")
            ended = True
        elif kind[0] < 97:
            raise ValueError("Unsupported critical PNG chunk")
        position = end
    if not header or not ended or not image_data:
        raise ValueError("Incomplete PNG image")
    width,height,depth,color,compression,filtering,interlace = header
    depths = {0:{1,2,4,8,16},2:{8,16},3:{1,2,4,8},4:{8,16},6:{8,16}}
    if (not width or not height or color not in depths or depth not in depths[color] or
            compression or filtering or interlace not in (0,1) or (color == 3 and not palette)):
        raise ValueError("Invalid PNG pixel format")
    channels = {0:1,2:3,3:1,4:2,6:4}[color]
    passes = [(0,0,1,1)] if not interlace else [(0,0,8,8),(4,0,8,8),(0,4,4,8),(2,0,4,4),(0,2,2,4),(1,0,2,2),(0,1,1,2)]
    scanlines = []
    expected = 0
    for x,y,dx,dy in passes:
        w,h = max(0,(width-x+dx-1)//dx),max(0,(height-y+dy-1)//dy)
        if w and h:
            stride = (w*channels*depth+7)//8+1
            expected += stride*h
            scanlines.append((h,stride))
    if expected > 64*1024*1024:
        raise ValueError("Decoded PNG exceeds the image safety limit")
    try:
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(b"".join(image_data),expected+1)
        if len(decoded) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError("PNG scanline data does not match its dimensions")
    except zlib.error as exc:
        raise ValueError("Invalid PNG compression") from exc
    offset = 0
    for rows,stride in scanlines:
        for _ in range(rows):
            if decoded[offset] > 4:
                raise ValueError("Invalid PNG scanline filter")
            offset += stride


def _validate_jpeg(raw):
    position, in_scan, frame, scan = 2, False, False, False
    while position < len(raw):
        if in_scan:
            position = raw.find(b"\xff",position)
            if position < 0:
                break
        if raw[position] != 0xff:
            raise ValueError("Invalid JPEG marker")
        while position < len(raw) and raw[position] == 0xff:
            position += 1
        if position >= len(raw):
            break
        marker = raw[position]
        position += 1
        if in_scan and (marker == 0 or 0xd0 <= marker <= 0xd7):
            continue
        in_scan = False
        if marker == 0xd9:
            if not frame or not scan or position != len(raw):
                raise ValueError("Incomplete JPEG or trailing payload")
            return
        if marker in (0,0xd8) or position+2 > len(raw):
            raise ValueError("Invalid JPEG marker sequence")
        length = int.from_bytes(raw[position:position+2],"big")
        end = position+length
        if length < 2 or end > len(raw):
            raise ValueError("Truncated JPEG segment")
        if marker in (0xc0,0xc1,0xc2):
            if length < 11:
                raise ValueError("Invalid JPEG frame")
            height = int.from_bytes(raw[position+3:position+5],"big")
            width = int.from_bytes(raw[position+5:position+7],"big")
            components = raw[position+7]
            if not width or not height or width*height > 16*1024*1024 or length != 8+3*components:
                raise ValueError("Invalid JPEG dimensions or components")
            frame = True
        if marker == 0xda:
            if not frame or length < 6 or length != 6+2*raw[position+2]:
                raise ValueError("Invalid JPEG scan")
            scan,in_scan = True,True
        position = end
    raise ValueError("JPEG has no complete end marker")


def _validate_gif(raw):
    if len(raw) < 14:
        raise ValueError("Truncated GIF header")
    width,height = struct.unpack_from("<HH",raw,6)
    if not width or not height or width*height > 16*1024*1024:
        raise ValueError("Invalid GIF dimensions")
    position = 13 + (3*(2**((raw[10]&7)+1)) if raw[10]&0x80 else 0)
    images = 0
    while position < len(raw):
        block = raw[position]
        position += 1
        if block == 0x3b:
            if not images or position != len(raw):
                raise ValueError("Incomplete GIF or trailing payload")
            return
        if block == 0x2c:
            if position+9 >= len(raw):
                raise ValueError("Truncated GIF image")
            _,_,w,h = struct.unpack_from("<HHHH",raw,position)
            if not w or not h or w*h > 16*1024*1024:
                raise ValueError("Invalid GIF image dimensions")
            packed = raw[position+8]
            position += 9 + (3*(2**((packed&7)+1)) if packed&0x80 else 0)
            if position >= len(raw) or not 2 <= raw[position] <= 8:
                raise ValueError("Invalid GIF LZW code size")
            position += 1
            images += 1
        elif block == 0x21:
            position += 1  # Extension label; content consists of bounded subblocks.
        else:
            raise ValueError("Invalid GIF block")
        while True:
            if position >= len(raw):
                raise ValueError("Truncated GIF subblock")
            size = raw[position]
            position += 1+size
            if position > len(raw):
                raise ValueError("Truncated GIF subblock data")
            if not size:
                break
    raise ValueError("GIF has no complete trailer")
