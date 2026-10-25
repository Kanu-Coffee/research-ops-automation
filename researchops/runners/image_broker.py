"""Controller-owned image generation broker for Research worker invocations.

The worker session writes bounded briefs to an app-owned request channel.
The controller executes the configured engine (Codex, OpenCode, Bailian, OpenRouter)
using application credentials outside the worker's sanitized environment.
"""

from datetime import datetime, timezone
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import uuid

from researchops.config import load_settings
from researchops.services.image_generation import ImageGenerationService
from researchops.workspace.security import assert_path_contained


class ImageBroker:
    def __init__(self, work_dir, image_settings, settings=None, *,
                 cancellation_check=None, timeout_seconds=None):
        self.work_dir = Path(work_dir)
        self.image_settings = dict(image_settings or {})
        self.settings = settings or load_settings()
        self.cancellation_check = cancellation_check
        self.timeout_seconds = timeout_seconds
        self._deadline = (time.monotonic() + timeout_seconds) if timeout_seconds is not None else None
        self._stop = threading.Event()
        self._thread = None
        self._session_dir = self.work_dir / ".researchops-images"
        self._requests_dir = self._session_dir / "requests"
        self._responses_dir = self._session_dir / "responses"
        self.session_id = uuid.uuid4().hex
        self.completed_jobs = []
        self.invocations = 0
        self.cleanup_verified = False

    def start(self):
        self._session_dir.mkdir(mode=0o700, exist_ok=True)
        self._requests_dir.mkdir(mode=0o700, exist_ok=True)
        self._responses_dir.mkdir(mode=0o700, exist_ok=True)
        session_info = {
            "schema_version": 1,
            "session_id": self.session_id,
            "engine": self.image_settings.get("engine", "codex"),
            "model": self.image_settings.get("model"),
            "enabled": self.image_settings.get("enabled", True),
            "state": "accepting",
            "started_at": datetime.now(timezone.utc).isoformat()
        }
        (self._session_dir / "session.json").write_text(
            json.dumps(session_info, ensure_ascii=False), encoding="utf-8"
        )
        self._thread = threading.Thread(target=self._serve, name="image-broker", daemon=True)
        self._thread.start()
        return self

    def _serve(self):
        service = ImageGenerationService(self.settings)
        engine = self.image_settings.get("engine", "codex")
        configured_model = self.image_settings.get("model")
        processed = set()
        while not self._stop.is_set():
            try:
                if self._requests_dir.is_dir():
                    for req_file in sorted(self._requests_dir.glob("*.json")):
                        if req_file.name.startswith(".") or req_file.name in processed:
                            continue
                        try:
                            st = os.lstat(req_file)
                            if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_size > 65536:
                                processed.add(req_file.name)
                                continue
                        except OSError:
                            continue
                        processed.add(req_file.name)
                        briefs = []
                        default_engine_model = ("wanx-v1" if engine == "bailian" else
                                                "default" if engine in ("codex", "opencode") else None)
                        effective_model = configured_model or default_engine_model
                        res = None
                        def cancel_or_stop():
                            if self._stop.is_set():
                                return True
                            if self.cancellation_check and self.cancellation_check():
                                return True
                            if self._deadline is not None and time.monotonic() >= self._deadline:
                                return True
                            return False

                        try:
                            data = json.loads(req_file.read_text(encoding="utf-8"))
                            briefs = data.get("briefs", [])
                            req_timeout = data.get("timeout_seconds")
                            if self._deadline is not None:
                                remaining = max(0.0, self._deadline - time.monotonic())
                                timeout = min(req_timeout, remaining) if req_timeout is not None else remaining
                            else:
                                timeout = req_timeout
                            if cancel_or_stop() or (timeout is not None and timeout <= 0.0):
                                res = {"status": "failed", "engine": engine, "model": effective_model,
                                       "reason_code": "image_generation_cancelled", "images": []}
                            else:
                                self.invocations += 1
                                res = service.generate(briefs, engine=engine, model=effective_model,
                                                       artifact_root=self.work_dir, timeout_seconds=timeout,
                                                       cancellation_check=cancel_or_stop)
                        except Exception:
                            res = {"status": "failed", "engine": engine, "model": effective_model,
                                   "reason_code": "image_generation_failed", "images": []}

                        # If cancelled or closed during execution, clean up only files actually created by this job (R7-01, R8-01, R9-01)
                        if cancel_or_stop():
                            if res and isinstance(res.get("images"), list):
                                for item in res["images"]:
                                    if isinstance(item, dict) and item.get("path"):
                                        try:
                                            p = Path(item["path"])
                                            assert_path_contained(p, self.work_dir)
                                            st = os.lstat(p)
                                            if stat.S_ISLNK(st.st_mode):
                                                os.unlink(p)
                                            elif stat.S_ISREG(st.st_mode):
                                                # Verify identity: single link, size, inode/device (if known), and SHA-256 digest
                                                if st.st_nlink != 1:
                                                    continue
                                                if item.get("size_bytes") is not None and st.st_size != item["size_bytes"]:
                                                    continue
                                                if "ino" in item and (st.st_ino != item["ino"] or st.st_dev != item.get("dev")):
                                                    continue
                                                if item.get("sha256"):
                                                    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
                                                    try:
                                                        with os.fdopen(fd, "rb") as stream:
                                                            data = stream.read(st.st_size + 1)
                                                    except Exception:
                                                        continue
                                                    if hashlib.sha256(data).hexdigest() != item["sha256"]:
                                                        continue
                                                p.unlink(missing_ok=True)
                                        except Exception:
                                            pass
                        if self._stop.is_set():
                            continue

                        if res and res.get("status") == "available" and isinstance(res.get("images"), list) and not cancel_or_stop():
                            job_record = {
                                "job_id": uuid.uuid4().hex,
                                "created_at": datetime.now(timezone.utc).isoformat(),
                                "engine": res.get("engine", engine),
                                "model": res.get("model", effective_model),
                                "status": "available",
                                "images": [
                                    {
                                        "filename": Path(img["path"]).name if img.get("path") else None,
                                        "sha256": img.get("sha256"),
                                        "size_bytes": img.get("size_bytes"),
                                        "mime_type": img.get("mime_type"),
                                        "requested_model": img.get("requested_model", effective_model),
                                        "observed_model": img.get("observed_model"),
                                        "model_verified": img.get("model_verified", False),
                                    }
                                    for img in res["images"]
                                    if isinstance(img, dict)
                                ],
                            }
                            self.completed_jobs.append(job_record)
                            ledger_data = {
                                "schema_version": 1,
                                "session_id": self.session_id,
                                "engine": engine,
                                "model": effective_model,
                                "status": "available",
                                "jobs": self.completed_jobs,
                            }
                            fd_l, tmp_l = tempfile.mkstemp(prefix=".ledger-atomic-", dir=self._session_dir)
                            tmp_lp = Path(tmp_l)
                            try:
                                with os.fdopen(fd_l, "w", encoding="utf-8") as s:
                                    s.write(json.dumps(ledger_data, ensure_ascii=False, indent=2))
                                    s.flush()
                                    os.fsync(s.fileno())
                                os.replace(tmp_lp, self._session_dir / "ledger.json")
                            finally:
                                tmp_lp.unlink(missing_ok=True)

                        # Safe atomic write to response directory preventing symlink traversal (R6-04)
                        resp_file = self._responses_dir / req_file.name
                        try:
                            st = os.lstat(resp_file)
                            if stat.S_ISLNK(st.st_mode):
                                os.unlink(resp_file)
                        except FileNotFoundError:
                            pass
                        assert_path_contained(resp_file, self._responses_dir)
                        fd, tmp_name = tempfile.mkstemp(prefix=".resp-atomic-", dir=self._responses_dir)
                        tmp_path = Path(tmp_name)
                        try:
                            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                                stream.write(json.dumps(res, ensure_ascii=False))
                                stream.flush()
                                os.fsync(stream.fileno())
                            os.replace(tmp_path, resp_file)
                        finally:
                            tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            time.sleep(0.02)

    def snapshot(self):
        """Copy completed receipts after close, without trusting worker JSON."""
        if not self.cleanup_verified or not self.completed_jobs:
            return None
        return {"schema_version": 1, "session_id": self.session_id,
                "engine": self.image_settings.get("engine", "codex"),
                "model": self.completed_jobs[0].get("model"),
                "status": "available", "jobs": deepcopy(self.completed_jobs)}

    def close(self, timeout=2) -> bool:
        self._stop.set()
        try:
            session_file = self._session_dir / "session.json"
            if session_file.is_file():
                st = os.lstat(session_file)
                if not stat.S_ISLNK(st.st_mode):
                    data = json.loads(session_file.read_text(encoding="utf-8"))
                    data["state"] = "closed"
                    tmp_file = self._session_dir / ".tmp_session.json"
                    tmp_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                    os.replace(tmp_file, session_file)
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=timeout)
        self.cleanup_verified = not (self._thread and self._thread.is_alive())
        return self.cleanup_verified
