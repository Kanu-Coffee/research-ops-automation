"""Invocation-scoped, application-owned acquisition during native Research.

The file channel is a convenience for trusted task code, not host isolation.
Only this in-memory ledger and protected originals authorize final file reuse.
Worker response files, editable copies and worker-declared IDs are not receipts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from researchops.engine.artifact_acquirer import _source, _write_new
from researchops.engine.message_validator import validate_media
from researchops.errors import HardGateError, WorkspaceError
from researchops.runners import file_acquisition
from researchops.runners.protected_media_fetch import CardRAGPDFFetcher
from researchops.runners.research_fetch import FetchPolicy, PublicResearchFetcher, ResearchFetchError
from researchops.workspace.security import assert_path_contained, read_safe_bytes


_EXTENSIONS = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif"}
_REQUEST = re.compile(r"[a-f0-9]{32}\.json\Z")
_CLOSE_TIMEOUT = 5.0
_NO_TRANSFER_ERRORS = frozenset(("invalid_url", "https_required", "host_denied", "port_denied",
    "method_denied", "request_too_large", "invalid_document_id", "provider_configuration_invalid", "worker_start_failed"))
_UNCERTAIN_TRANSFER_ERRORS = frozenset(("timeout", "cancelled", "worker_failed", "worker_cleanup_failed", "transport_failed"))


def _bytes(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("ascii")


class ResearchAcquisitionSession:
    def __init__(self, settings, *, run_id, task_id, attempt, fencing_token, storage_root,
                 cancellation_check=None):
        if (any(not isinstance(value, str) or not value or len(value) > 160
                for value in (run_id, task_id, fencing_token)) or type(attempt) is not int or attempt < 1):
            raise HardGateError("Invalid research acquisition identity")
        self.settings = settings
        self.run_id, self.task_id, self.attempt = run_id, task_id, attempt
        self.fencing_token = fencing_token
        self.storage_root = Path(os.path.abspath(storage_root))
        self.cancellation_check = cancellation_check
        self.pdf_fetcher = CardRAGPDFFetcher()
        self.session_id = uuid.uuid4().hex
        self.work_dir = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._records, self._identities, self._copies = {}, {}, {}
        self._seen_requests = set()
        self._request_failures = []
        self._closed = False
        self._cleanup_verified = True
        self._failure_reason = None
        self._consumed_bytes = self._consumed_count = 0
        self._elapsed_seconds = 0.0

    @property
    def consumed_bytes(self):
        return self._consumed_bytes

    @property
    def consumed_count(self):
        return self._consumed_count

    @property
    def elapsed_seconds(self):
        return self._elapsed_seconds

    @property
    def cleanup_verified(self):
        return self._cleanup_verified and (self._thread is None or not self._thread.is_alive())

    @property
    def failure_reason(self):
        return self._failure_reason

    def _cancelled(self):
        if self._stop.is_set():
            return True
        try:
            return bool(self.cancellation_check and self.cancellation_check())
        except Exception:
            # A lease/fence lookup failure must stop further acquisition.
            self._failure_reason = "acquisition_authorization_lost"
            self._stop.set()
            return True

    def start(self, work_dir):
        if self.work_dir is not None or self._closed:
            raise HardGateError("Research acquisition session cannot be restarted")
        work = Path(os.path.abspath(work_dir))
        assert_path_contained(work, work)
        assert_path_contained(self.storage_root, self.storage_root)
        if (not work.is_dir() or work == self.storage_root or work in self.storage_root.parents or
                self.storage_root in work.parents):
            raise HardGateError("Research acquisition storage must be outside the worker root")
        self.work_dir = work
        self.storage_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        originals = self.storage_root / "originals"
        originals.mkdir(mode=0o700)
        channel = work / file_acquisition.DIRECTORY
        channel.mkdir(mode=0o700)
        for name in ("requests", "responses", "files"):
            (channel / name).mkdir(mode=0o700)
        helper_root = work / ".researchops-submit" / "researchops" / "runners"
        assert_path_contained(helper_root, work)
        helper_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Namespace packages lose to a regular installed researchops package,
        # even when the staged directory is first on sys.path. Make this helper
        # self-contained when start() is used without stage_submission_helper().
        for package in (helper_root.parent, helper_root):
            marker = package / "__init__.py"
            if marker.exists() or marker.is_symlink():
                read_safe_bytes(marker, work, 0)
            else:
                _write_new(work, marker.relative_to(work).as_posix(), b"")
        _write_new(work, ".researchops-submit/researchops/runners/file_acquisition.py",
                   Path(file_acquisition.__file__).read_bytes())
        self._session_status("accepting")
        self._cleanup_verified = False
        self._thread = threading.Thread(target=self._serve, name="research-file-acquisition", daemon=True)
        self._thread.start()
        return self

    def _session_status(self, state):
        if self.work_dir is not None:
            file_acquisition._publish(self.work_dir, file_acquisition.DIRECTORY + "/session.json",
                _bytes({"schema_version": 1, "session_id": self.session_id, "state": state,
                        "reason_code": self._failure_reason,
                        "max_file_bytes": self.settings.media.max_total_bytes,
                        "request_timeout_seconds": min(605, self.settings.media.phase_timeout_seconds + 5)}))

    def _normalized(self, source):
        normalized = _source(source)
        if normalized is None:
            raise HardGateError("A remote acquisition source is required")
        key = hashlib.sha256(_bytes(normalized)).hexdigest()
        identity = ("cardrag_pdf", normalized["connection_id"], normalized["document_id"]) if (
            normalized["kind"] == "cardrag_pdf") else ("official_image", normalized["url_sha256"])
        previous = self._identities.get(identity)
        if previous is not None and previous != key:
            raise HardGateError("Research acquisition source differs from its original descriptor")
        return normalized, key, identity

    def _serve(self):
        try:
            while not self._cancelled():
                directory = file_acquisition._directory(self.work_dir, file_acquisition.DIRECTORY + "/requests")
                try:
                    with os.scandir(directory) as entries:
                        names = []
                        for count, entry in enumerate(entries, 1):
                            if count > self.settings.media.max_files * 4 + 1:
                                raise HardGateError("Research acquisition request limit exceeded")
                            if entry.name.startswith(".pending-"):
                                continue
                            if not _REQUEST.fullmatch(entry.name):
                                raise HardGateError("Unsafe acquisition request entry")
                            names.append(entry.name)
                            if len(names) > self.settings.media.max_files * 4:
                                raise HardGateError("Research acquisition request limit exceeded")
                finally:
                    os.close(directory)
                for name in sorted(names):
                    if self._cancelled():
                        break
                    if name in self._seen_requests:
                        continue
                    if len(self._seen_requests) >= self.settings.media.max_files * 4:
                        raise HardGateError("Research acquisition request limit exceeded")
                    self._seen_requests.add(name)
                    self._process_request(name)
                self._stop.wait(0.025)
        except (WorkspaceError, OSError, HardGateError):
            self._failure_reason = "acquisition_channel_unsafe"
            self._stop.set()
        except Exception:
            self._failure_reason = "acquisition_broker_failed"
            self._stop.set()
        finally:
            try:
                self._session_status("closed")
            except (OSError, ValueError):
                self._failure_reason = self._failure_reason or "acquisition_channel_unsafe"

    def _process_request(self, name):
        relative = f"{file_acquisition.DIRECTORY}/requests/{name}"
        raw = read_safe_bytes(self.work_dir / relative, self.work_dir, file_acquisition.MAX_REQUEST_BYTES)
        request_id = name[:-5]
        try:
            request = file_acquisition._json(raw)
            if (not isinstance(request, dict) or set(request) != {"schema_version", "session_id", "request_id", "source"} or
                    type(request.get("schema_version")) is not int or request["schema_version"] != 1 or
                    request["session_id"] != self.session_id or request["request_id"] != request_id):
                raise ValueError
            try:
                if _source(request["source"]) is None:
                    raise ValueError
            except HardGateError:
                raise ValueError from None
            record = self._acquire(request["source"])
            response = self._worker_response(record)
        except (ValueError, TypeError, KeyError, RecursionError):
            response = {"status": "failed", "reason_code": "invalid_acquisition_request"}
            self._request_failures.append({"request_id": request_id, "reason_code": response["reason_code"]})
        if not self._stop.is_set():
            response.update(session_id=self.session_id, request_id=request_id)
            file_acquisition._publish(self.work_dir,
                f"{file_acquisition.DIRECTORY}/responses/{name}", _bytes(response))

    def _acquire(self, raw_source):
        source, key, identity = self._normalized(raw_source)
        if key in self._records:
            record = self._records[key]
            if record["status"] == "available":
                self._verify_copy(record)
                self._read_original(record)
            return record
        acquisition_id = "acq-" + uuid.uuid4().hex
        record = {"acquisition_id": acquisition_id, "source": source, "status": "failed",
                  "reason_code": "acquisition_interrupted", "mime_type": None, "sha256": None,
                  "size_bytes": None, "original_path": None,
                  "charged_bytes": 0, "bytes_accounting": "not_started"}
        self._records[key], self._identities[identity] = record, key
        self._consumed_count += 1
        media = self.settings.media
        active_started = None
        accounted = False
        reserved_bytes = 0
        try:
            if self._cancelled():
                raise ResearchFetchError("cancelled")
            if self._consumed_count > media.max_files:
                raise ResearchFetchError("artifact_limit_exceeded")
            budget = media.max_total_bytes - self._consumed_bytes
            remaining = media.phase_timeout_seconds - self._elapsed_seconds
            if budget < 1 or source.get("size_bytes", 0) > budget:
                raise ResearchFetchError("total_bytes_limit")
            if remaining < 0.05:
                raise ResearchFetchError("phase_timeout")
            timeout = min(remaining, media.file_timeout_seconds)
            if source["kind"] == "cardrag_pdf":
                provider = media.providers.get(source["connection_id"])
                if provider is None:
                    raise ResearchFetchError("provider_unavailable")
                # The descriptor pins the original size; do not permit a larger
                # body and then under-reserve it if its child dies mid-response.
                reserved_bytes = source["size_bytes"]
                active_started = time.monotonic()
                fetched = self.pdf_fetcher.fetch(provider, source, max_bytes=reserved_bytes,
                    timeout_seconds=timeout, cancellation_check=self._cancelled)
            else:
                if not media.official_image_hosts:
                    raise ResearchFetchError("host_denied")
                policy = FetchPolicy(max_body_bytes=min(budget, media.max_image_bytes),
                    timeout_seconds=timeout, require_https=True, allowed_hosts=media.official_image_hosts)
                reserved_bytes = policy.max_body_bytes
                active_started = time.monotonic()
                fetched = PublicResearchFetcher(policy).fetch(raw_source["url"], cancellation_check=self._cancelled)
            elapsed = time.monotonic() - active_started
            active_started = None
            if self._closed:
                return record
            self._elapsed_seconds += elapsed
            raw = fetched.body
            self._consumed_bytes += len(raw)
            record.update(charged_bytes=len(raw), bytes_accounting="observed")
            accounted = True
            if self._cancelled():
                raise ResearchFetchError("cancelled")
            if fetched.status != 200:
                raise ResearchFetchError({401: "source_unauthorized", 403: "source_unauthorized",
                                          404: "source_not_found"}.get(fetched.status, "source_http_error"))
            if len(raw) > budget:
                raise ResearchFetchError("total_bytes_limit")
            mime = fetched.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if ((source["kind"] == "cardrag_pdf" and mime != "application/pdf") or
                    (source["kind"] == "official_image" and mime not in _EXTENSIONS.keys() - {"application/pdf"})):
                raise ResearchFetchError("mime_type_mismatch")
            digest = hashlib.sha256(raw).hexdigest()
            if source["kind"] == "cardrag_pdf":
                if digest != source["sha256"] or len(raw) != source["size_bytes"]:
                    raise ResearchFetchError("source_hash_mismatch" if digest != source["sha256"] else "source_size_mismatch")
            try:
                validate_media(raw, mime)
            except (ValueError, TypeError, UnicodeError):
                raise ResearchFetchError("invalid_media") from None
            if mime != "application/pdf" and len(raw) > media.max_image_bytes:
                raise ResearchFetchError("image_size_limit")
            original = "originals/" + acquisition_id + _EXTENSIONS[mime]
            working = file_acquisition.DIRECTORY + "/files/" + acquisition_id + _EXTENSIONS[mime]
            with self._lock:
                if self._stop.is_set() or self._closed:
                    raise ResearchFetchError("cancelled")
                _write_new(self.storage_root, original, raw)
                record.update(mime_type=mime, sha256=digest, size_bytes=len(raw), original_path=original)
                _write_new(self.work_dir, working, raw)
                self._copies[acquisition_id] = working
                record.update(status="available", reason_code="available", mime_type=mime,
                              sha256=digest, size_bytes=len(raw), original_path=original)
        except ResearchFetchError as exc:
            if not self._closed:
                record["reason_code"] = exc.code if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", exc.code) else "source_fetch_failed"
                if not accounted:
                    partial = exc.audit.get("body_bytes")
                    known = type(partial) is int and 0 <= partial < 2 ** 63
                    uncertain = (active_started is not None and exc.code not in _NO_TRANSFER_ERRORS and
                                 (not known or exc.code in _UNCERTAIN_TRANSFER_ERRORS))
                    charge = max(partial if known else 0, reserved_bytes if uncertain else 0)
                    self._consumed_bytes += charge
                    record.update(charged_bytes=charge, bytes_accounting="reserved" if uncertain else
                                  "observed" if known else "not_started")
                if exc.code == "worker_cleanup_failed":
                    self._failure_reason = "worker_cleanup_failed"
                    self._cleanup_verified = False
                    self._stop.set()
        finally:
            if active_started is not None and not self._closed:
                self._elapsed_seconds += time.monotonic() - active_started
        return record

    def _worker_response(self, record):
        result = {key: record[key] for key in ("status", "acquisition_id")}
        if record["status"] == "available":
            result.update({key: record[key] for key in ("mime_type", "sha256", "size_bytes")})
            result["path"] = str(self.work_dir / self._copies[record["acquisition_id"]])
        else:
            result["reason_code"] = record["reason_code"]
        return result

    def _verify_copy(self, record):
        path = self.work_dir / self._copies[record["acquisition_id"]]
        try:
            raw = read_safe_bytes(path, self.work_dir, self.settings.media.max_total_bytes)
        except WorkspaceError:
            raise HardGateError("Research acquisition working copy is unsafe") from None
        if len(raw) != record["size_bytes"] or hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise HardGateError("Research acquisition working copy changed")

    def _read_original(self, record):
        try:
            raw = read_safe_bytes(self.storage_root / record["original_path"], self.storage_root,
                                  self.settings.media.max_total_bytes)
        except WorkspaceError:
            raise HardGateError("Research acquisition original is unsafe") from None
        if len(raw) != record["size_bytes"] or hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise HardGateError("Research acquisition original changed")
        return raw

    def close(self):
        if self._closed:
            return self.cleanup_verified
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=_CLOSE_TIMEOUT)
        self._closed = True
        self._cleanup_verified = self._thread is None or not self._thread.is_alive()
        if self._failure_reason == "worker_cleanup_failed":
            self._cleanup_verified = False
        if not self._cleanup_verified:
            self._failure_reason = "acquisition_cleanup_failed"
        if self._cleanup_verified:
            try:
                for record in self._records.values():
                    if record["status"] == "available":
                        self._read_original(record)
                        self._verify_copy(record)
            except (HardGateError, OSError):
                self._failure_reason = "acquisition_file_changed"
        if self.work_dir is not None:
            try:
                self._session_status("closed")
            except (OSError, ValueError):
                self._failure_reason = self._failure_reason or "acquisition_channel_unsafe"
            file_acquisition._publish(self.storage_root, "ledger.json", _bytes(self.ledger()))
        return self.cleanup_verified

    def raise_if_failed(self):
        if self._failure_reason:
            raise HardGateError("Research acquisition failed: " + self._failure_reason)
        if not self._closed or not self.cleanup_verified:
            raise HardGateError("Research acquisition is not closed and verified")

    def lookup(self, source):
        self.raise_if_failed()
        _, key, _ = self._normalized(source)
        return copy.deepcopy(self._records.get(key))

    def read_original(self, record):
        self.raise_if_failed()
        authoritative = next((item for item in self._records.values()
            if item["acquisition_id"] == record.get("acquisition_id")), None) if isinstance(record, dict) else None
        if authoritative is None or record != authoritative or authoritative["status"] != "available":
            raise HardGateError("Unrecognized research acquisition receipt")
        return self._read_original(authoritative)

    def derived_from(self, ids):
        self.raise_if_failed()
        if (not isinstance(ids, list) or not ids or len(ids) > self.settings.media.max_files or
                any(not isinstance(item, str) for item in ids) or len(set(ids)) != len(ids)):
            raise HardGateError("Invalid derived artifact provenance")
        by_id = {item["acquisition_id"]: item for item in self._records.values()}
        result = []
        for acquisition_id in ids:
            record = by_id.get(acquisition_id)
            if record is None or record["status"] != "available":
                raise HardGateError("Derived artifact references an unavailable acquisition")
            self._read_original(record)
            result.append({"acquisition_id": acquisition_id, "source_sha256": record["sha256"]})
        return result

    def ledger(self):
        return {"schema_version": 1, "run_id": self.run_id, "task_id": self.task_id, "attempt": self.attempt,
                "fencing_token": self.fencing_token, "consumed_bytes": self.consumed_bytes,
                "consumed_count": self.consumed_count, "elapsed_seconds": round(self.elapsed_seconds, 6),
                "cleanup_verified": self.cleanup_verified, "failure_reason": self.failure_reason,
                "request_count": len(self._seen_requests), "request_failures": copy.deepcopy(self._request_failures),
                "records": copy.deepcopy(list(self._records.values()))}
