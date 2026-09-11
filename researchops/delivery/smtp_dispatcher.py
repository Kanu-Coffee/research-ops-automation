"""Durable built-in SMTP dispatcher with explicit DATA uncertainty handling."""

from datetime import datetime
from email import encoders, policy
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.utils import format_datetime, formataddr
import hashlib
import json
from pathlib import Path
import smtplib
import ssl
from typing import Optional
import uuid
from zoneinfo import ZoneInfo

from researchops.delivery.package import validate_outbox, read_file, publish_package
from researchops.delivery.policy import require_approval, require_live, sender_for_task_version
from researchops.delivery.queue import SmtpQueue
from researchops.delivery.smtp_config import (SmtpSettings, BuiltinDeliveryConfig,
    delivery_revision, load_delivery_config, preserve_sender_passwords, save_delivery_config, validate_address)
from researchops.errors import DeliveryError


class SmtpDispatcher:
    def __init__(self, settings, receipt_consumer, delivery_repo, state_repo, config_file=None):
        self.settings = settings
        self.receipt_consumer = receipt_consumer
        self.delivery_repo = delivery_repo
        self.state_repo = state_repo
        self.config_file = config_file or settings.paths.delivery_config_file
        self.queue = SmtpQueue(delivery_repo.db)

    def get_config(self) -> BuiltinDeliveryConfig:
        return load_delivery_config(self.config_file)

    def save_config(self, config: BuiltinDeliveryConfig) -> None:
        if self.config_file.exists():
            preserve_sender_passwords(config, self.get_config())
        save_delivery_config(config, self.config_file)

    @staticmethod
    def _connect(cfg, *, on_stage=None):
        import time

        cfg.validate()
        context = ssl.create_default_context()
        server = None
        started = stage_started = time.monotonic()
        stage = "connect"
        timings = {}

        def observe(name=None):
            nonlocal stage, stage_started
            now = time.monotonic()
            if name is not None and name != stage:
                timings[stage] = timings.get(stage, 0) + round(max(0, now - stage_started) * 1000)
                stage, stage_started = name, now
            current = dict(timings)
            current[stage] = current.get(stage, 0) + round(max(0, now - stage_started) * 1000)
            if on_stage is not None:
                on_stage({"schema_version": 1, "stage": stage, "body_started": False,
                    "body_completed": False, "final_reply_received": False,
                    "timeout_seconds": cfg.timeout_seconds, "stage_timings_ms": current,
                    "elapsed_ms": round(max(0, now - started) * 1000)})

        try:
            # SMTP constructors include TCP connection and the initial 220 greeting;
            # SMTP_SSL also includes its implicit TLS handshake in this stage.
            observe()
            if cfg.use_ssl:
                server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout_seconds, context=context)
            else:
                server = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout_seconds)
                observe("greeting")
                server.ehlo()
                observe("tls")
                server.starttls(context=context)
            observe("greeting")
            server.ehlo()
            if cfg.username:
                observe("auth")
                if not cfg.password:
                    raise DeliveryError("SMTP password is not configured")
                server.login(cfg.username, cfg.password)
            observe()
            return server
        except Exception:
            # Preserve the SMTP exception and its cause even if audit/close fails.
            try:
                observe()
            except Exception:
                pass
            if server is not None:
                try:
                    server.close()
                except Exception:
                    pass
            raise

    def test_connection(self, smtp_settings: Optional[SmtpSettings] = None, *, sender_profile_id="default"):
        """Enqueue a TLS/auth diagnostic; HTTP/CLI presentation never opens SMTP."""
        try:
            config = self.get_config()
            smtp = config.get_sender(sender_profile_id)
            if smtp_settings and smtp_settings != smtp:
                raise DeliveryError("Save SMTP settings before enqueueing a connection test")
            smtp.validate()
            job_id = "smtp-check-" + uuid.uuid4().hex
            self.queue.enqueue(job_id,None,"<" + job_id + "@researchops.local>",b"",
                {"connection_test":True, "sender_profile_id":sender_profile_id},
                delivery_revision(config, sender_profile_id, db=self.delivery_repo.db))
            return True, f"SMTP connection test queued: {job_id}"
        except Exception as exc:
            return False, str(exc) if isinstance(exc,DeliveryError) else f"SMTP diagnostic rejected ({type(exc).__name__})"

    @staticmethod
    def _close(server):
        if server is not None:
            try:
                server.quit()
            except Exception:
                try:
                    server.close()
                except Exception:
                    pass  # QUIT/close cannot undo an already accepted DATA response.

    @staticmethod
    def _reply_code(response):
        if (not isinstance(response, tuple) or len(response) != 2 or
                type(response[0]) is not int or not 100 <= response[0] <= 599):
            raise DeliveryError("Malformed SMTP reply")
        return response[0]

    @staticmethod
    def _mime(request, files, smtp, recipients, message_id):
        digest = hashlib.sha256(message_id.encode()).hexdigest()[:16]
        root = MIMEMultipart("mixed", boundary="ro-mix-" + digest)
        root["Subject"] = request["subject"]
        root["From"] = formataddr((smtp.sender_name, smtp.sender_email or smtp.username))
        root["To"] = ", ".join(recipients)
        created = datetime.fromisoformat(request["created_at"].replace("Z", "+00:00"))
        root["Date"] = format_datetime(created.astimezone(ZoneInfo("Asia/Seoul")))
        root["Message-ID"] = message_id
        related = MIMEMultipart("related", boundary="ro-rel-" + digest)
        alternative = MIMEMultipart("alternative", boundary="ro-alt-" + digest)
        root.attach(related)
        related.attach(alternative)
        for kind in ("text", "html"):
            info = request["body"][kind]
            # Base64 on original bytes preserves LF/CRLF and non-ASCII exactly.
            part = MIMEBase("text", "plain" if kind == "text" else "html", charset="utf-8")
            part.set_payload(files[info["path"]])
            encoders.encode_base64(part)
            alternative.attach(part)
        for info in request["attachments"]:
            major, minor = info["media_type"].split("/", 1)
            part = MIMEBase(major, minor)
            part.set_payload(files[info["path"]])
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", info["disposition"], filename=info["filename"])
            if info.get("content_id"):
                part["Content-ID"] = "<" + info["content_id"] + ">"
            (related if info["disposition"] == "inline" else root).attach(part)
        return root.as_bytes(policy=policy.SMTP)

    def _check_handoff(self, handoff, config, *, retry=False):
        if not handoff or handoff.mode != "handoff" or handoff.status not in (("published", "queued", "failed", "uncertain") if retry else ("published", "queued")):
            raise DeliveryError("Handoff is not queued for live SMTP delivery")
        require_approval(self.settings, self.delivery_repo.db, config, handoff.task_id,
            handoff.task_version_hash, handoff.recipient_group_id,
            system_alert=handoff.message_type == "system_alert", run_id=handoff.run_id,
            message_revision=handoff.message_revision)
        from researchops.storage.repositories import RunRepository
        run = RunRepository(self.delivery_repo.db).get_run(handoff.run_id)
        allowed = ("failed", "needs_attention", "cancelled", "succeeded") if handoff.message_type == "system_alert" else (("awaiting_receipt", "failed", "needs_attention") if retry else ("awaiting_receipt",))
        if not run or run.status not in allowed:
            raise DeliveryError("Handoff is not ready: run archive has not been finalized")
        archive = self.settings.paths.run_archive_dir / handoff.task_id / handoff.run_id
        manifest = json.loads(read_file(archive, "run-manifest.json", 1024 * 1024))
        from jsonschema import Draft202012Validator, FormatChecker
        schema = json.loads((self.settings.paths.schemas_dir / "run-manifest.schema.json").read_text())
        if list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(manifest)):
            raise DeliveryError("Run archive manifest is invalid")
        if manifest["run_id"] != handoff.run_id or manifest["task_id"] != handoff.task_id:
            raise DeliveryError("Run archive identity mismatch")
        if handoff.message_type != "system_alert" and manifest["status"] != "awaiting_receipt":
            raise DeliveryError("Run archive is not ready for SMTP")
        if handoff.message_type != "system_alert":
            archived_handoff = manifest["handoff"]
            if (archived_handoff.get("handoff_id") != handoff.handoff_id or
                    archived_handoff.get("delivery_request_sha256") != handoff.delivery_request_sha256):
                raise DeliveryError("Run archive handoff does not match SMTP job")
            archived_request = read_file(archive, archived_handoff["delivery_request_path"], 1024 * 1024)
            if hashlib.sha256(archived_request).hexdigest() != handoff.delivery_request_sha256:
                raise DeliveryError("Archived delivery request hash mismatch")
            from researchops.delivery.recipient_routing import require_stored_selection, routing_mode
            from researchops.storage.repositories import TaskRepository
            task = TaskRepository(self.delivery_repo.db).get_version(handoff.task_version_hash).definition
            from researchops.delivery.artifact_integrity import require_stored_composition
            require_stored_composition(self.delivery_repo.db, task, handoff.run_id,
                handoff.task_version_hash, handoff.message_revision, handoff.recipient_group_id,
                schemas_dir=self.settings.paths.schemas_dir, archive_root=archive,
                file_root=self.settings.paths.delivery_outbox_dir / handoff.handoff_id,
                request=handoff.delivery_request)
            if routing_mode(task) == "catalog_name":
                resolution = require_stored_selection(self.delivery_repo.db, task, handoff.run_id,
                    handoff.task_version_hash, handoff.message_revision, handoff.recipient_group_id,
                    schemas_dir=self.settings.paths.schemas_dir,
                    raw_result=read_file(archive, "composition-result.json", 1_000_000))
                archived_input = read_file(archive, "composition-input.json", self.settings.delivery.max_message_bytes)
                archived_resolution = json.loads(read_file(archive, "recipient-resolution.json", 1_000_000))
                if (hashlib.sha256(archived_input).hexdigest() != resolution["composition_input_sha256"] or
                        archived_resolution != resolution):
                    raise DeliveryError("Archived recipient input/resolution does not match this message revision")
                composition = RunRepository(self.delivery_repo.db).get_composition_result(
                    handoff.run_id, handoff.message_revision)
                request = handoff.delivery_request
                if (request["subject"] != composition.subject or
                        request["body"]["html"]["path"] != composition.html_path or
                        request["body"]["text"]["path"] != composition.text_path):
                    raise DeliveryError("Delivery message does not match the stored composition result")
        return validate_outbox(self.settings, handoff)

    def enqueue_handoff(self, handoff_id):
        config = self.get_config()
        handoff = self.delivery_repo.get_handoff(handoff_id)
        request, files = self._check_handoff(handoff, config)
        sender_profile_id = sender_for_task_version(self.delivery_repo.db, handoff.task_id, handoff.task_version_hash)
        smtp = config.get_sender(sender_profile_id)
        recipients = config.recipient_groups[handoff.recipient_group_id]
        message_id = "<ro-" + hashlib.sha256(handoff.idempotency_key.encode()).hexdigest() + "@researchops.local>"
        mime = self._mime(request, files, smtp, recipients, message_id)
        if len(mime) > self.settings.delivery.max_message_bytes:
            raise DeliveryError("Encoded SMTP MIME exceeds the configured message size limit")
        envelope = {"sender": smtp.sender_email or smtp.username, "recipients": recipients,
                    "sender_profile_id": sender_profile_id}
        existing = self.queue.get(handoff_id)
        if (existing and sender_profile_id == "default" and
                "sender_profile_id" not in json.loads(existing["envelope_json"])):
            envelope.pop("sender_profile_id")  # Keep legacy re-enqueue idempotent.
        self.queue.enqueue(handoff_id, handoff_id, message_id, mime, envelope,
                           delivery_revision(config, sender_profile_id, db=self.delivery_repo.db))
        return handoff_id

    def send_test_email(self, to_email: str, smtp_settings: Optional[SmtpSettings] = None, *, sender_profile_id="default"):
        """Explicit mailbox input authorizes a test job, never synchronous delivery."""
        try:
            validate_address(to_email)
            config = self.get_config()
            require_live(self.settings, config, sender_profile_id)
            smtp = config.get_sender(sender_profile_id)
            if smtp_settings and smtp_settings != smtp:
                raise DeliveryError("Save SMTP settings before enqueueing a test email")
            job_id = "smtp-test-" + uuid.uuid4().hex
            message_id = "<" + job_id + "@researchops.local>"
            request = {"subject": "[ResearchOps] SMTP test", "created_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
                "body": {"text": {"path": "email.txt"}, "html": {"path": "email.html"}}, "attachments": []}
            files = {"email.txt": b"ResearchOps SMTP test. Success means SMTP server acceptance.",
                     "email.html": b"<html><body><p>ResearchOps SMTP test. Success means SMTP server acceptance.</p></body></html>"}
            mime = self._mime(request, files, smtp, [to_email], message_id)
            self.queue.enqueue(job_id, None, message_id, mime,
                {"sender": smtp.sender_email or smtp.username, "recipients": [to_email], "test": True,
                 "sender_profile_id": sender_profile_id}, delivery_revision(config, sender_profile_id, db=self.delivery_repo.db))
            return True, f"Test email queued: {job_id}. Run the SMTP dispatcher to send it."
        except Exception as exc:
            return False, str(exc) if isinstance(exc, DeliveryError) else f"Test email rejected ({type(exc).__name__})"

    def dispatch_handoff(self, handoff_id):
        try:
            existing = self.queue.latest(handoff_id)
            if existing and existing["status"] != "queued":
                return False, None, f"SMTP job is {existing['status']}; automatic resend is blocked"
            if not existing:
                self.enqueue_handoff(handoff_id)
            return self._dispatch_job(existing["job_id"] if existing else handoff_id)
        except Exception as exc:
            return False, None, str(exc) if isinstance(exc, DeliveryError) else f"Dispatch rejected ({type(exc).__name__})"

    @staticmethod
    def _retry_result(job):
        return {key:job[key] for key in ('job_id','handoff_id','status','attempt_number','next_attempt_at')}

    def retry_status(self, handoff_id):
        handoff = self.delivery_repo.get_handoff(handoff_id)
        attempts = self.queue.history(handoff_id)
        status = {'eligible':False,'uncertain':False,'can_retry_uncertain':False,
                  'can_republish':False,'block_reason':'','attempts':attempts,'next_attempt_at':None}
        if not handoff:
            status['block_reason'] = 'Handoff was not found'
            return status
        status['can_republish'] = handoff.status == 'published' and not attempts
        if not attempts:
            if status['can_republish']:
                try:
                    self._check_handoff(handoff,self.get_config())
                except Exception:
                    status['can_republish'] = False
            status['block_reason'] = 'No SMTP attempt exists yet'
            return status
        job = self.queue.latest(handoff_id)
        status['uncertain'] = any(a['status']=='uncertain' for a in attempts) and not any(a['status']=='smtp_accepted' for a in attempts)
        status['next_attempt_at'] = job['next_attempt_at'] if job['status']=='queued' else None
        if any(a['status']=='smtp_accepted' for a in attempts):
            status['block_reason'] = 'SMTP server already accepted this email'
        elif job['status'] == 'sending':
            status['block_reason'] = 'Email transmission is in progress'
        elif job['status']=='queued' and (not job['parent_job_id'] or not job['next_attempt_at'] or job['next_attempt_at'] <= datetime.now(ZoneInfo('UTC')).isoformat()):
            status['block_reason'] = 'Email is already queued for immediate dispatch'
        elif handoff.message_type == 'system_alert':
            status['block_reason'] = 'System alerts do not support email retry'
        else:
            try:
                self._prepare_job(job, retry=True, allow_revision_change=True)
            except Exception as exc:
                status['block_reason'] = str(exc) if isinstance(exc,DeliveryError) else 'Email snapshot could not be verified'
            else:
                if status['uncertain']:
                    status['can_retry_uncertain'] = True
                    status['block_reason'] = 'SMTP delivery is uncertain; another attempt may deliver a duplicate'
                else:
                    status['eligible'] = True
        return status

    def retry_email(self, handoff_id, *, request_key, allow_uncertain=False, reason=''):
        if not isinstance(request_key,str) or not 1 <= len(request_key) <= 128 or any(ord(c)<33 for c in request_key):
            raise DeliveryError('A retry request key of 1–128 non-space characters is required')
        if type(allow_uncertain) is not bool or not isinstance(reason,str) or len(reason)>500:
            raise DeliveryError('Invalid email retry acknowledgement')
        reason = reason.strip()
        if allow_uncertain and not reason:
            raise DeliveryError('Explain the duplicate delivery acknowledgement before retrying an uncertain email')
        request = {'handoff_id':handoff_id,'mode':'manual','allow_uncertain':allow_uncertain,'reason':reason}
        cached = self.queue.requested(request_key,request)
        if cached:
            return self._retry_result(cached)
        status = self.retry_status(handoff_id)
        if not status['eligible'] and not (status['can_retry_uncertain'] and allow_uncertain):
            cached = self.queue.requested(request_key,request)
            if cached:
                return self._retry_result(cached)
            raise DeliveryError(status['block_reason'] or 'Email is not eligible for retry')
        job = self.queue.latest(handoff_id)
        config,_,envelope,_ = self._prepare_job(job,retry=True,allow_revision_change=True)
        revision = delivery_revision(config,envelope.get('sender_profile_id','default'),db=self.delivery_repo.db)
        if job['status']=='queued' and job['config_revision'] != revision:
            # Preserve the original queued snapshot; explicitly retire it without
            # opening SMTP before creating a new manual, current-policy attempt.
            token = self.queue.claim(job['job_id'],include_future=True)
            if not token:
                raise DeliveryError('Email transmission state changed; reload before retrying')
            self.queue.finish(job['job_id'],token,'failed',error='Queued retry superseded by current SMTP settings',
                error_code='smtp_retry_settings_changed',diagnostics={'schema_version':1,'stage':'preflight','body_started':False})
            self.archive_attempt(job['job_id'])
        # A manual request may use newly saved transport/auth settings only if
        # current policy and the complete original MIME/envelope still match.
        result = self.queue.retry(job['job_id'],request_key=request_key,request=request,
            config_revision=revision,
            allow_uncertain=allow_uncertain)
        return self._retry_result(result)

    def _prepare_job(self, job, *, retry=False, allow_revision_change=False):
        config = self.get_config()
        envelope = json.loads(job["envelope_json"])
        # Jobs created before sender profiles were introduced implicitly use the
        # default sender. Never redirect a pending message to a different account.
        sender_profile_id = envelope.get("sender_profile_id", "default")
        smtp = config.get_sender(sender_profile_id)
        diagnostic = envelope.get("connection_test") is True and job["handoff_id"] is None
        if diagnostic:
            smtp.validate()
        else:
            require_live(self.settings, config, sender_profile_id)
        if not allow_revision_change and job["config_revision"] != delivery_revision(config, sender_profile_id, db=self.delivery_repo.db):
            raise DeliveryError("SMTP sender or recipient configuration changed; create a new message revision before sending")
        if hashlib.sha256(job["mime_bytes"]).hexdigest() != job["mime_sha256"]:
            raise DeliveryError("SMTP MIME snapshot hash mismatch")
        if len(job["mime_bytes"]) > self.settings.delivery.max_message_bytes:
            raise DeliveryError("Encoded SMTP MIME exceeds the configured message size limit")
        if job["handoff_id"]:
            handoff = self.delivery_repo.get_handoff(job["handoff_id"])
            if retry:
                from researchops.delivery.retry_guard import require_retry_family_clear
                conn = self.delivery_repo.db.get_connection()
                try:
                    require_retry_family_clear(conn,handoff.run_id,email_only=True)
                finally:
                    conn.close()
            request, files = self._check_handoff(handoff, config, retry=retry)
            if sender_profile_id != sender_for_task_version(self.delivery_repo.db, handoff.task_id, handoff.task_version_hash):
                raise DeliveryError("SMTP sender profile differs from the immutable task version")
            recipients = config.recipient_groups[handoff.recipient_group_id]
            expected = self._mime(request, files, smtp, recipients, job["message_id"])
            expected_envelope = {"sender": smtp.sender_email or smtp.username, "recipients": recipients}
            if "sender_profile_id" in envelope:
                expected_envelope["sender_profile_id"] = sender_profile_id
            if expected != job["mime_bytes"] or envelope != expected_envelope:
                raise DeliveryError("SMTP snapshot differs from validated delivery package")
        elif envelope.get("test") is not True and not diagnostic:
            raise DeliveryError("Unbound SMTP job is not an explicit test")
        if not diagnostic:
            validate_address(envelope["sender"])
            for recipient in envelope["recipients"]:
                validate_address(recipient)
            if not envelope["recipients"]:
                raise DeliveryError("SMTP envelope requires a recipient")
        return config, smtp, envelope, diagnostic

    def _dispatch_job(self, job_id):
        from researchops.delivery.smtp_transport import send_message, failure_result
        job = self.queue.get(job_id)
        if not job or job["status"] != "queued":
            return False, None, "SMTP job is not queued"
        if job.get('expires_at') and job['expires_at'] <= datetime.now(ZoneInfo('UTC')).isoformat():
            token = self.queue.claim(job_id)
            if not token:
                return False,None,'Email attempt is not available'
            receipt = self.queue.finish(job_id,token,'failed',error='Automatic email retry expired before submission',
                error_code='smtp_retry_expired',diagnostics={'schema_version':1,'stage':'preflight','body_started':False})
            self.archive_attempt(job_id)
            return False,receipt,'Automatic email retry expired before submission'
        try:
            config, smtp, envelope, diagnostic = self._prepare_job(job)
        except DeliveryError:
            if not job.get('parent_job_id'):
                raise
            token = self.queue.claim(job_id)
            if not token:
                return False,None,'Email attempt is not available'
            receipt = self.queue.finish(job_id,token,'failed',error='Email retry blocked by current settings or snapshot validation',
                error_code='smtp_retry_preflight_rejected',diagnostics={'schema_version':1,'stage':'preflight','body_started':False})
            self.archive_attempt(job_id)
            return False,receipt,'Email retry blocked by current settings or snapshot validation'
        token = self.queue.claim(job_id)
        if not token:
            return False, None, "SMTP dispatcher is busy or this job was already claimed"
        if diagnostic:
            return self._connection_job(job_id,token,smtp)
        server = None
        data_started = False
        sender_profile_id = envelope.get("sender_profile_id", "default")
        def before_body():
            nonlocal data_started
            if job.get('expires_at') and job['expires_at'] <= datetime.now(ZoneInfo('UTC')).isoformat():
                raise DeliveryError('Automatic email retry expired before submission')
            # Repeat immutable content, current policy and cancellation checks
            # after the 354 reply and immediately before any body byte is sent.
            self._prepare_job(job)
            if self.settings.environment == "production":
                self.queue.start_data(job_id, token, production=True)
            else:
                self.queue.start_data(job_id, token)
            data_started = True
        stage = {"stage": "connect"}
        def on_stage(value):
            nonlocal stage
            stage = value
            self.queue.record_stage(job_id,token,value)
        try:
            on_stage(stage)
            server = self._connect(smtp, on_stage=on_stage)
            outcome = send_message(server, job["mime_bytes"], envelope, before_body=before_body,
                on_stage=on_stage, timeouts=smtp)
        except Exception as exc:
            outcome = failure_result(exc, stage=stage.get("stage", "connect"),
                body_started=data_started, diagnostics=stage)
        finally:
            self._close(server)
        result, error, reply = outcome.status, outcome.error, outcome.server_reply
        try:
            receipt = self.queue.finish(job_id, token, result, error=error, server_reply=reply,
                error_code=outcome.error_code, retryable=outcome.retryable,
                diagnostics=outcome.diagnostics, retry_policy=self.settings.delivery)
        except Exception:
            if not data_started:
                raise
            # Known remote acceptance and a failed local commit must never
            # re-enter the automatic queue.
            result,error = "uncertain","SMTP exchange completed but local result reconciliation failed"
            receipt = self.queue.finish(job_id,token,result,error=error,server_reply=reply,
                error_code="smtp_reconciliation_failed", diagnostics=outcome.diagnostics)
        archived = self.archive_attempt(job_id)
        message = "SMTP 서버 수락 (smtp_accepted)" if result == "smtp_accepted" else error
        if not archived:
            message += "; protected attempt audit export is pending"
        return result == "smtp_accepted", receipt, message

    def _connection_job(self,job_id,token,config):
        server = None
        try:
            server = self._connect(config)
            code = self._reply_code(server.noop())
            if not 200 <= code < 300:
                raise DeliveryError("SMTP NOOP was rejected")
            result,error = "connection_ok",None
        except Exception as exc:
            result,error = "failed", f"SMTP diagnostic failed ({type(exc).__name__})"
        finally:
            self._close(server)
        self.queue.finish(job_id,token,result,error=error)
        self.archive_attempt(job_id)
        return result == "connection_ok",None,"SMTP TLS/authentication connection verified; no message sent" if result == "connection_ok" else error

    def archive_attempt(self,job_id):
        """Immutable evidence sidecar; failures never reset or retransmit a SMTP attempt."""
        job = self.queue.get(job_id)
        if not job or job["status"] in ("queued","sending"):
            return False
        fields = ("job_id","handoff_id","status","phase","message_id","mime_sha256","config_revision",
                  "created_at","updated_at","error","server_reply")
        attempt = {key:job[key] for key in fields}
        if job.get("diagnostics_json") or job.get("parent_job_id"):
            attempt.update({key:job[key] for key in ("parent_job_id","attempt_number","next_attempt_at","error_code","retryable")})
            attempt["diagnostics"] = json.loads(job["diagnostics_json"]) if job["diagnostics_json"] else None
        files = {"attempt.json":json.dumps(attempt,sort_keys=True,indent=2).encode()}
        if job["handoff_id"]:
            conn = self.delivery_repo.db.get_connection()
            try:
                receipt = conn.execute("SELECT receipt_json FROM delivery_receipts WHERE external_receipt_id=?",
                    ("smtp-" + job_id,)).fetchone()
                if receipt:
                    files["receipt.json"] = receipt[0].encode()
            finally:
                conn.close()
        manifest = {"schema_version":1,"job_id":job_id,"files":[{"path":name,"size_bytes":len(content),
            "sha256":hashlib.sha256(content).hexdigest()} for name,content in files.items()]}
        try:
            publish_package(self.settings.paths.receipts_dir,job_id,
                json.dumps(manifest,sort_keys=True,indent=2).encode(),files,request_name="attempt-manifest.json")
            return True
        except (OSError,DeliveryError):
            return False

    def dispatch_all_pending(self):
        self.queue.recover_interrupted()
        for completed in self.queue.completed():
            self.archive_attempt(completed["job_id"])
        results = []
        # Only durable DB handoffs are candidates; unregistered filesystem directories are ignored.
        for handoff in self.delivery_repo.list_handoffs(status="published"):
            if not self.queue.get(handoff.handoff_id):
                try:
                    self.enqueue_handoff(handoff.handoff_id)
                except Exception as exc:
                    results.append((handoff.handoff_id, False,
                        str(exc) if isinstance(exc, DeliveryError) else f"Queue rejected ({type(exc).__name__})"))
        for job in self.queue.pending():
            try:
                ok, _, message = self._dispatch_job(job["job_id"])
                results.append((job["job_id"], ok, message))
            except Exception as exc:
                results.append((job["job_id"], False,
                    str(exc) if isinstance(exc, DeliveryError) else f"Dispatch rejected ({type(exc).__name__})"))
        return results
