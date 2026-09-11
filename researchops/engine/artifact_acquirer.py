"""Acquire only declared Research files and record their independently verified state."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import re
import time
from urllib.parse import urlsplit, urlunsplit

from researchops.engine.message_validator import validate_media
from researchops.errors import HardGateError, ValidationError, WorkspaceError
from researchops.runners.protected_media_fetch import CardRAGPDFFetcher
from researchops.runners.research_fetch import FetchPolicy, PublicResearchFetcher, ResearchFetchError
from researchops.strict_json import strict_json_loads
from researchops.workspace.security import assert_path_contained, open_safe_file, read_safe_bytes


MAX_ARTIFACTS = 64
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_RESERVED = frozenset(("result.json", "composition-input.json", "composition-result.json", "email.html", "email.txt",
    "artifact-report.json", "composition-binding.json", "recipient-resolution.json", "artifact-manifest.json",
    "run-manifest.json", "validation-report.json", "dedupe-report.json", "delivery-request.json"))
_RESERVED_ROOTS = frozenset(("logs", "inputs", "outputs", "task-snapshot", "research-artifacts"))
_IMAGE_TYPES = frozenset(("image/png", "image/jpeg", "image/gif"))
_NO_TRANSFER_ERRORS = frozenset(("invalid_url", "https_required", "host_denied", "port_denied",
    "method_denied", "request_too_large", "invalid_document_id", "provider_configuration_invalid", "worker_start_failed"))
_UNCERTAIN_TRANSFER_ERRORS = frozenset(("timeout", "cancelled", "worker_failed", "worker_cleanup_failed", "transport_failed"))


class _ArtifactContractError(HardGateError):
    """A declaration error, distinct from unsafe paths or changed file bytes."""

    def __init__(self, reason_code, message):
        self.reason_code = reason_code
        super().__init__(message)


@dataclass
class AcquisitionResult:
    inline_artifacts: list = field(default_factory=list)
    attachments: list = field(default_factory=list)
    file_paths: dict[str, Path] = field(default_factory=dict)
    report: dict = field(default_factory=dict)
    hold: bool = False


def mime_part_bytes(size: int, header_bytes: int = 4096) -> int:
    """SMTP base64 uses 76 columns with CRLF, including the final line."""
    encoded = 4 * ((size + 2) // 3)
    return encoded + 2 * ((encoded + 75) // 76) + header_bytes


def _relative_path(value):
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024 or
            "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise HardGateError("Unsafe research artifact path")
    path = PurePosixPath(value)
    if (path.is_absolute() or ".." in path.parts or str(path) != value or not path.parts or
            any(len(part.encode("utf-8")) > 255 for part in path.parts) or
            path.parts[0] in _RESERVED_ROOTS or value in _RESERVED):
        raise HardGateError("Unsafe or reserved research artifact path")
    return path


def _source(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise HardGateError("Malformed artifact source")
    kind = value.get("kind")
    if kind == "cardrag_pdf":
        keys = {"kind", "connection_id", "document_id", "issuer", "product_code", "sha256", "size_bytes"}
        if set(value) != keys:
            raise HardGateError("Malformed CardRAG PDF descriptor")
        for key in ("connection_id", "document_id", "issuer", "product_code"):
            item = value[key]
            if (not isinstance(item, str) or not item or len(item) > 160 or
                    any(ord(c) <= 32 or ord(c) == 127 for c in item)):
                raise HardGateError("Malformed CardRAG PDF descriptor")
        if not re.fullmatch(r"doc_[a-f0-9]{64}", value["document_id"]):
            raise HardGateError("Malformed CardRAG document ID")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value["connection_id"]):
            raise HardGateError("Malformed media connection ID")
        if not isinstance(value["sha256"], str) or not _SHA.fullmatch(value["sha256"]):
            raise HardGateError("Malformed CardRAG PDF hash")
        if type(value["size_bytes"]) is not int or value["size_bytes"] < 1:
            raise HardGateError("Malformed CardRAG PDF size")
        return dict(value)
    if kind == "official_image":
        if set(value) != {"kind", "url"} or not isinstance(value["url"], str):
            raise HardGateError("Malformed official image descriptor")
        url = value["url"]
        if len(url.encode("utf-8")) > 8192:
            raise HardGateError("Malformed official image URL")
        try:
            parts = urlsplit(url)
            # Report has no query, userinfo, fragments or caller-supplied headers.
            hostname = parts.hostname or ""
            safe_url = urlunsplit((parts.scheme, hostname, parts.path, "", ""))
        except ValueError:
            raise HardGateError("Malformed official image URL") from None
        return {"kind": kind, "url": safe_url, "url_sha256": hashlib.sha256(url.encode()).hexdigest()}
    raise HardGateError("Unsupported artifact source")


def _write_new(root: Path, relative: str, raw: bytes) -> Path:
    """No overwrite, links or worker-chosen parent redirection during acquisition."""
    target = assert_path_contained(root / relative, root)
    descriptors = []
    created = False
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            descriptors.append(descriptor)
        try:
            fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
        except FileExistsError:
            raise HardGateError("Acquired artifact would overwrite an existing file") from None
        created = True
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return target
    except OSError:
        if created:
            try:
                os.unlink(PurePosixPath(relative).name, dir_fd=descriptor)
            except OSError:
                pass
        raise ResearchFetchError("artifact_write_failed") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _identity(stream):
    info = os.fstat(stream.fileno())
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _existing_files(prepared, root, media, result, cancellation_check, *, reserved_bytes=0):
    """Reserve every original file before a remote request can consume space.

    Hash and validate one file at a time, retaining metadata only. This includes
    evidence and files filtered by dedupe; their original bytes still exist.
    """
    existing, total = {}, reserved_bytes
    for artifact, entry in prepared:
        path = root / entry["path"]
        if entry["source"]:
            if path.exists() or path.is_symlink():
                entry["reason_code"] = "unsafe_artifact"
                raise HardGateError("Remote artifact path already contains worker output")
            continue
        try:
            if cancellation_check and cancellation_check():
                raise ResearchFetchError("cancelled")
            with open_safe_file(path, root, 2 ** 63 - 1) as (stream, size):
                if total + size > media.max_total_bytes:
                    entry["reason_code"] = "total_bytes_limit"
                    raise HardGateError("Existing research files exceed the combined raw byte limit")
                identity = _identity(stream)
                raw = stream.read(size + 1)
            digest = hashlib.sha256(raw).hexdigest()
            if (len(raw) != size or artifact.get("sha256", digest) != digest or
                    artifact.get("size_bytes", size) != size):
                raise HardGateError("Declared local research artifact hash or size mismatch")
            mime_type = artifact.get("mime_type", "application/octet-stream")
            invalid = None
            if mime_type in _IMAGE_TYPES and size > media.max_image_bytes:
                invalid = "image_size_limit"
            else:
                try:
                    validate_media(raw, mime_type)
                except (ValueError, TypeError, UnicodeError):
                    invalid = "invalid_media"
            if not invalid and entry["role"] == "inline_image" and mime_type not in _IMAGE_TYPES:
                raise HardGateError("Inline artifact must be a validated image")
            existing[entry["path"]] = (digest, size, mime_type, invalid, identity)
            total += size
            result.file_paths[entry["path"]] = path
            entry.update(sha256=digest, size_bytes=size)
        except WorkspaceError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                existing[entry["path"]] = None
                continue
            entry["reason_code"] = "unsafe_artifact"
            raise HardGateError("Unsafe or changed local research artifact") from None
        except HardGateError:
            if entry["reason_code"] != "total_bytes_limit":
                entry["reason_code"] = "unsafe_artifact"
            raise
        except ResearchFetchError as exc:
            entry["reason_code"] = exc.code
            raise
    return existing, total


class ArtifactAcquirer:
    def __init__(self, settings):
        self.settings = settings
        self.last_report = None
        self.last_file_paths: dict[str, Path] = {}
        self.pdf_fetcher = CardRAGPDFFetcher()

    def acquire(self, artifacts, records, reportable_records, root, *, run_id, task_id,
                task_version_hash, composition_revision, dedupe_enabled,
                cancellation_check=None, network_allowed=False, acquisition_session=None):
        report = {"schema_version": 1, "task_id": task_id, "run_id": run_id,
                  "task_version_hash": task_version_hash, "composition_revision": composition_revision, "entries": []}
        result = AcquisitionResult(report=report)
        self.last_report, self.last_file_paths = report, result.file_paths
        media = self.settings.media
        root = Path(root)
        if acquisition_session is not None:
            ledger = acquisition_session.ledger()
            if not isinstance(ledger, dict) or ledger.get("run_id") != run_id or ledger.get("task_id") != task_id:
                raise HardGateError("Research acquisition session belongs to another Run or Task")
            if ledger.get("cleanup_verified") is not True:
                raise ResearchFetchError("worker_cleanup_failed")
            acquisition_session.raise_if_failed()
            # Non-native runners can close an unused, never-started session.
            # Every started session is bound, including one with no requests.
            if getattr(acquisition_session, "work_dir", None) is not None or acquisition_session.consumed_count:
                try:
                    storage = Path(acquisition_session.storage_root)
                    ledger_bytes = read_safe_bytes(storage / "ledger.json", storage, 2_000_000)
                    if strict_json_loads(ledger_bytes, max_bytes=2_000_000) != ledger or type(ledger.get("attempt")) is not int or ledger["attempt"] < 1:
                        raise HardGateError("Closed Research acquisition ledger changed")
                except (ValueError, WorkspaceError):
                    raise HardGateError("Closed Research acquisition ledger is unsafe or invalid") from None
                report["acquisition_evidence"] = {"run_id": run_id, "attempt": ledger["attempt"],
                    "ledger_sha256": hashlib.sha256(ledger_bytes).hexdigest()}
        known = {record["record_id"] for record in records}
        selected = {record["record_id"] for record in reportable_records}
        if not selected <= known:
            raise HardGateError("Reportable artifact records are outside the Research result")
        if not isinstance(artifacts, list):
            raise HardGateError("Research artifacts must be a list")
        # Build every bounded diagnostic before touching the network. A malformed
        # later descriptor must not hide behind dedupe or cause earlier downloads.
        seen_paths, seen_ids, seen_cids, prepared = set(), set(), set(), []
        for index, artifact in enumerate(artifacts[:media.max_files]):
            entry = {"artifact_id": f"artifact-{index + 1}", "path": f"invalid-artifact-{index + 1}",
                     "role": "evidence", "scope": "legacy", "requested_scope": None,
                     "requested_record_ids": [], "record_ids": [],
                     "declared_status": "unspecified", "status": "failed", "reason_code": "acquisition_interrupted",
                     "source": None, "sha256": None, "size_bytes": None,
                     "include_in_compose": False, "announce_missing": False, "on_failure": "continue"}
            report["entries"].append(entry)
            try:
                if not isinstance(artifact, dict):
                    raise HardGateError("Malformed research artifact")
                requested_scope = artifact.get("scope")
                if (isinstance(requested_scope, str) and len(requested_scope) <= 64 and
                        all(char.isprintable() for char in requested_scope)):
                    entry["requested_scope"] = requested_scope
                if requested_scope in ("record", "run", "legacy"):
                    # Preserve a valid explicit scope even if an earlier path or
                    # role check fails. Unknown scope values stay diagnostic only.
                    entry["scope"] = requested_scope
                path = str(_relative_path(artifact.get("path")))
                entry["path"] = path
                artifact_id = artifact.get("artifact_id", entry["artifact_id"])
                if not isinstance(artifact_id, str) or not _ID.fullmatch(artifact_id):
                    raise HardGateError("Invalid artifact ID")
                if path in seen_paths or artifact_id in seen_ids:
                    raise HardGateError("Duplicate research artifact ID or path")
                seen_paths.add(path)
                seen_ids.add(artifact_id)
                entry["artifact_id"] = artifact_id
                entry["source"] = _source(artifact.get("source"))
                if "derived_from" in artifact:
                    ids = artifact["derived_from"]
                    if (acquisition_session is None or not isinstance(ids, list) or not 1 <= len(ids) <= MAX_ARTIFACTS
                            or any(not isinstance(item, str) or not _ID.fullmatch(item) for item in ids)
                            or len(set(ids)) != len(ids)):
                        raise HardGateError("Derived artifact requires valid current Research acquisition references")
                    try:
                        references = acquisition_session.derived_from(ids)
                        from researchops.delivery.artifact_integrity import validate_acquisition_references
                        validate_acquisition_references(references)
                        if {reference["acquisition_id"] for reference in references} != set(ids):
                            raise HardGateError("Derived artifact acquisition identities changed")
                        entry["derived_from"] = [dict(reference) for reference in references]
                    except (ValidationError, WorkspaceError):
                        raise HardGateError("Derived artifact references unavailable or changed Research acquisitions") from None
                role = artifact.get("role", "evidence")
                if role not in ("attachment", "inline_image", "evidence"):
                    raise _ArtifactContractError("invalid_artifact_role", "Unsupported research artifact role")
                entry["role"] = role
                if "mime_type" in artifact and (not isinstance(artifact["mime_type"], str) or
                        len(artifact["mime_type"]) > 128 or any(ord(c) < 32 for c in artifact["mime_type"])):
                    raise HardGateError("Malformed artifact MIME type")
                ids = artifact.get("record_ids", [])
                if (not isinstance(ids, list) or any(not isinstance(item, str) for item in ids) or
                        len(ids) != len(set(ids)) or not set(ids) <= known):
                    raise HardGateError("Artifact links unknown or duplicate records")
                scope = artifact.get("scope", "record" if ids else "legacy")
                if scope not in ("record", "run", "legacy"):
                    raise _ArtifactContractError("invalid_artifact_scope", "Unsupported research artifact scope")
                entry.update(scope=scope, requested_record_ids=list(ids), record_ids=[item for item in ids if item in selected])
                if ((scope == "record" and not ids) or
                        (scope == "run" and (ids or entry["source"] or role not in ("attachment", "evidence"))) or
                        (scope == "legacy" and (ids or entry["source"])) or (entry["source"] and not ids)):
                    raise _ArtifactContractError("invalid_artifact_role_scope", "Invalid research artifact role, scope or record association")
                declared = artifact.get("declared_status", artifact.get("status", "unspecified"))
                if not isinstance(declared, str) or len(declared) > 64 or any(ord(c) < 32 for c in declared):
                    raise HardGateError("Invalid artifact declared status")
                entry["declared_status"] = declared
                if type(artifact.get("announce_missing", False)) is not bool or artifact.get("on_failure", "continue") not in ("continue", "hold"):
                    raise HardGateError("Invalid artifact failure policy")
                entry["announce_missing"] = artifact.get("announce_missing", False)
                entry["on_failure"] = artifact.get("on_failure", "continue")
                for key in ("sha256", "size_bytes"):
                    if key in artifact:
                        value = artifact[key]
                        if ((key == "sha256" and (not isinstance(value, str) or not _SHA.fullmatch(value))) or
                                (key == "size_bytes" and (type(value) is not int or value < 0))):
                            raise HardGateError("Malformed declared artifact hash or size")
                source = entry["source"]
                if source and source["kind"] == "cardrag_pdf":
                    if role != "attachment":
                        raise _ArtifactContractError("invalid_artifact_role", "CardRAG PDF artifacts require the attachment role")
                    if artifact.get("mime_type") != "application/pdf":
                        raise HardGateError("CardRAG PDFs require application/pdf attachment metadata")
                    if any(key in artifact and artifact[key] != source[key] for key in ("sha256", "size_bytes")):
                        raise HardGateError("Artifact and source descriptor hash or size disagree")
                if role == "attachment":
                    filename = artifact.get("filename", PurePosixPath(path).name)
                    if (not isinstance(filename, str) or not filename or len(filename.encode()) > 255 or
                            any(c in filename for c in ("/", "\\")) or any(ord(c) < 32 or ord(c) == 127 for c in filename)):
                        raise HardGateError("Unsafe artifact filename")
                if "cid" in artifact and (not isinstance(artifact["cid"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,254}", artifact["cid"])):
                    raise HardGateError("Invalid artifact CID")
                if "cid" in artifact:
                    if artifact["cid"] in seen_cids:
                        raise HardGateError("Duplicate artifact CID")
                    seen_cids.add(artifact["cid"])
                assert_path_contained(root / path, root)
                prepared.append((artifact, entry))
            except _ArtifactContractError as exc:
                entry["reason_code"] = exc.reason_code
                raise HardGateError(str(exc)) from None
            except (HardGateError, WorkspaceError):
                entry["reason_code"] = "unsafe_artifact"
                raise HardGateError("Research artifact failed structural or path validation") from None
        if len(artifacts) > media.max_files:
            if report["entries"]:
                report["entries"][-1]["reason_code"] = "artifact_limit_exceeded"
            raise HardGateError("Research artifact count exceeds the acquisition limit")

        consumed_bytes, consumed_count, elapsed = 0, 0, 0.0
        receipts = {}
        if acquisition_session is not None:
            consumed_bytes = acquisition_session.consumed_bytes
            consumed_count = acquisition_session.consumed_count
            elapsed = acquisition_session.elapsed_seconds
            if (type(consumed_bytes) is not int or consumed_bytes < 0
                    or type(consumed_count) is not int or consumed_count < 0
                    or isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
                    or not math.isfinite(elapsed) or elapsed < 0):
                raise HardGateError("Invalid Research acquisition budget evidence")
            if consumed_bytes > media.max_total_bytes:
                if prepared:
                    prepared[-1][1]["reason_code"] = "total_bytes_limit"
                raise HardGateError("Research acquisitions exceed the combined raw byte limit")
            for artifact, entry in prepared:
                if entry["source"]:
                    receipt = acquisition_session.lookup(artifact["source"])
                    if receipt is not None:
                        if receipt.get("source") != entry["source"] or receipt.get("status") not in {"available", "failed"}:
                            raise HardGateError("Research acquisition receipt source mismatch")
                        receipts[entry["path"]] = receipt
        consumed_count += sum(not entry["source"] for _, entry in prepared)
        if consumed_count > media.max_files:
            if prepared:
                prepared[-1][1]["reason_code"] = "artifact_limit_exceeded"
            raise HardGateError("Research acquisitions and local artifacts exceed the combined file limit")
        deadline = time.monotonic() + media.phase_timeout_seconds - elapsed
        existing, total_bytes = _existing_files(prepared, root, media, result, cancellation_check,
                                               reserved_bytes=consumed_bytes)
        message_bytes = media.mime_reserve_bytes
        for index, (artifact, entry) in enumerate(prepared):
            wanted = not (entry["scope"] == "record" and not entry["record_ids"] or
                          entry["scope"] == "legacy" and dedupe_enabled and bool(known - selected) and entry["role"] != "evidence")
            exclusion = "records_excluded" if entry["scope"] == "record" else "legacy_unscoped_dedupe"
            source = entry["source"]
            if source and not wanted:
                entry.update(status="excluded", reason_code=exclusion, announce_missing=False)
                continue
            reserved_bytes, transfer_accounted = 0, False
            try:
                if cancellation_check and cancellation_check():
                    raise ResearchFetchError("cancelled")
                remaining = deadline - time.monotonic()
                receipt = receipts.get(entry["path"])
                if remaining < 0.05 and (acquisition_session is None or (source and receipt is None)):
                    raise ResearchFetchError("phase_timeout")
                path = root / entry["path"]
                if receipt is not None:
                    if receipt["status"] == "failed":
                        raise ResearchFetchError(receipt.get("reason_code") or "acquisition_failed")
                    try:
                        raw = acquisition_session.read_original(receipt)
                    except (ValidationError, WorkspaceError):
                        raise HardGateError("Research acquisition original changed before artifact import") from None
                    if (not isinstance(raw, bytes) or receipt.get("sha256") != hashlib.sha256(raw).hexdigest()
                            or receipt.get("size_bytes") != len(raw)):
                        raise HardGateError("Research acquisition original does not match its receipt")
                    mime_type = receipt.get("mime_type")
                    if ((source["kind"] == "cardrag_pdf" and mime_type != "application/pdf")
                            or (source["kind"] == "official_image" and mime_type not in _IMAGE_TYPES)
                            or ("mime_type" in artifact and artifact["mime_type"] != mime_type)):
                        raise ResearchFetchError("mime_type_mismatch")
                elif source:
                    if consumed_count >= media.max_files:
                        raise ResearchFetchError("artifact_limit_exceeded")
                    consumed_count += 1
                    if not network_allowed:
                        raise ResearchFetchError("network_disabled")
                    budget = media.max_total_bytes - total_bytes
                    if budget < 1 or source.get("size_bytes", 0) > budget:
                        raise ResearchFetchError("total_bytes_limit")
                    if (source["kind"] == "cardrag_pdf" and
                            message_bytes + mime_part_bytes(source["size_bytes"], media.mime_part_header_bytes) >
                            self.settings.delivery.max_message_bytes):
                        raise ResearchFetchError("message_size_limit")
                    if source["kind"] == "cardrag_pdf":
                        provider = media.providers.get(source["connection_id"])
                        if provider is None:
                            raise ResearchFetchError("provider_unavailable")
                        reserved_bytes = source["size_bytes"]
                        fetched = self.pdf_fetcher.fetch(provider, source, max_bytes=reserved_bytes,
                            timeout_seconds=min(remaining, media.file_timeout_seconds), cancellation_check=cancellation_check)
                    else:
                        if not media.official_image_hosts:
                            raise ResearchFetchError("host_denied")
                        policy = FetchPolicy(max_body_bytes=min(budget, media.max_image_bytes),
                            timeout_seconds=min(remaining, media.file_timeout_seconds), require_https=True,
                            allowed_hosts=media.official_image_hosts)
                        reserved_bytes = policy.max_body_bytes
                        fetched = PublicResearchFetcher(policy).fetch(artifact["source"]["url"], cancellation_check=cancellation_check)
                    raw = fetched.body
                    total_bytes += len(raw)
                    transfer_accounted = True
                    if len(raw) > budget:
                        raise ResearchFetchError("total_bytes_limit")
                    if fetched.status != 200:
                        raise ResearchFetchError({401: "source_unauthorized", 403: "source_unauthorized",
                                                  404: "source_not_found"}.get(fetched.status, "source_http_error"))
                    actual_type = fetched.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if ((source["kind"] == "cardrag_pdf" and actual_type != "application/pdf") or
                            (source["kind"] == "official_image" and actual_type not in _IMAGE_TYPES) or
                            ("mime_type" in artifact and artifact["mime_type"] != actual_type)):
                        raise ResearchFetchError("mime_type_mismatch")
                    mime_type = actual_type
                else:
                    cached = existing.get(entry["path"])
                    if cached is None:
                        raise ResearchFetchError("local_file_missing")
                    digest, size, mime_type, invalid, identity = cached
                    try:
                        with open_safe_file(path, root, media.max_total_bytes) as (stream, _):
                            if _identity(stream) != identity:
                                raise HardGateError("Previously verified local research file changed")
                    except WorkspaceError:
                        raise HardGateError("Previously verified local research file is unsafe or missing") from None
                    if invalid:
                        raise ResearchFetchError(invalid)
                if source:
                    digest, size = hashlib.sha256(raw).hexdigest(), len(raw)
                if total_bytes > media.max_total_bytes:
                    raise ResearchFetchError("total_bytes_limit")
                # Failed remote MIME/hash checks still consumed the phase's raw
                # transfer budget; errors cannot multiply the byte allowance.
                expected = source if source and source["kind"] == "cardrag_pdf" else artifact
                for key, actual in (("sha256", digest), ("size_bytes", size)):
                    if key in expected and expected[key] != actual:
                        if source:
                            raise ResearchFetchError("source_hash_mismatch" if key == "sha256" else "source_size_mismatch")
                        raise HardGateError("Declared local research artifact hash or size mismatch")
                if mime_type in _IMAGE_TYPES and size > media.max_image_bytes:
                    raise ResearchFetchError("image_size_limit")
                if source:
                    try:
                        validate_media(raw, mime_type)
                    except (ValueError, TypeError, UnicodeError):
                        raise ResearchFetchError("invalid_media") from None
                if entry["role"] == "inline_image" and mime_type not in _IMAGE_TYPES:
                    raise HardGateError("Inline artifact must be a validated image")
                if source:
                    if cancellation_check and cancellation_check():
                        raise ResearchFetchError("cancelled")
                    path = _write_new(root, entry["path"], raw)
                result.file_paths[entry["path"]] = path
                entry.update(sha256=digest, size_bytes=size)
                if not wanted:
                    entry.update(status="excluded", reason_code=exclusion, announce_missing=False)
                    continue
                if entry["role"] == "evidence":
                    entry.update(status="available", reason_code="evidence_only")
                    continue
                part_bytes = mime_part_bytes(size, media.mime_part_header_bytes)
                if message_bytes + part_bytes > self.settings.delivery.max_message_bytes:
                    raise ResearchFetchError("message_size_limit")
                item = {"artifact_id": entry["artifact_id"], "path": entry["path"], "mime_type": mime_type,
                        "sha256": digest, "size_bytes": size, "record_ids": entry["record_ids"]}
                if source:
                    item["source"] = source
                if "derived_from" in entry:
                    item["derived_from"] = [dict(reference) for reference in entry["derived_from"]]
                if entry["role"] == "inline_image":
                    item["cid"] = artifact.get("cid") or f"artifact-{index + 1}-{digest[:16]}"
                    result.inline_artifacts.append(item)
                else:
                    item["filename"] = artifact.get("filename") or PurePosixPath(entry["path"]).name
                    result.attachments.append(item)
                message_bytes += part_bytes
                entry.update(status="available", reason_code="available", include_in_compose=True)
            except ResearchFetchError as exc:
                # A killed or interrupted transport may not report bytes already
                # read. Reserve its bounded request allowance in that case.
                if not transfer_accounted and reserved_bytes:
                    consumed = exc.audit.get("body_bytes")
                    known = type(consumed) is int and 0 <= consumed < 2 ** 63
                    uncertain = exc.code not in _NO_TRANSFER_ERRORS and (not known or exc.code in _UNCERTAIN_TRANSFER_ERRORS)
                    charge = max(consumed if known else 0, reserved_bytes if uncertain else 0)
                    total_bytes += min(charge, max(0, media.max_total_bytes - total_bytes))
                entry.update(status="excluded" if exc.code == "message_size_limit" else "failed", reason_code=exc.code)
                if artifact.get("on_failure", "continue") == "hold" and wanted:
                    result.hold = True
                if exc.code in ("cancelled", "worker_cleanup_failed"):
                    raise
            except (HardGateError, WorkspaceError):
                entry.update(status="failed", reason_code="unsafe_artifact")
                raise
            except BaseException:
                entry.update(status="failed", reason_code="acquisition_interrupted")
                raise
        return result
