"""Durable single-dispatcher SMTP queue and atomic local delivery evidence."""

from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
import sqlite3
import uuid

from researchops.domain.models import DeliveryReceipt
from researchops.errors import DeliveryError
from researchops.delivery.timeline import record_delivery_event


def add_delivery_schema(conn):
    sql = """CREATE TABLE IF NOT EXISTS smtp_attempts (
        job_id TEXT PRIMARY KEY,
        handoff_id TEXT REFERENCES delivery_handoffs(handoff_id),
        status TEXT NOT NULL CHECK(status IN ('queued','sending','smtp_accepted','connection_ok','failed','uncertain')),
        phase TEXT NOT NULL CHECK(phase IN ('prepared','data_started','finished')),
        claim_token TEXT, claim_pid INTEGER,
        message_id TEXT NOT NULL, mime_bytes BLOB NOT NULL, mime_sha256 TEXT NOT NULL,
        envelope_json TEXT NOT NULL, config_revision TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        error TEXT, server_reply TEXT,
        parent_job_id TEXT, attempt_number INTEGER NOT NULL DEFAULT 1,
        request_key TEXT UNIQUE, request_json TEXT,
        next_attempt_at TEXT, error_code TEXT, retryable INTEGER NOT NULL DEFAULT 0,
        diagnostics_json TEXT, expires_at TEXT
    )"""
    existing = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='smtp_attempts'").fetchone()
    if existing and 'attempt_number' not in {r[1] for r in conn.execute('PRAGMA table_info(smtp_attempts)')}:
        # Copy the original columns verbatim. Receipt proof IDs and terminal rows
        # remain valid; only handoff/Message-ID uniqueness moves to the logical message.
        columns = ','.join(r[1] for r in conn.execute('PRAGMA table_info(smtp_attempts)'))
        conn.execute(sql.replace('smtp_attempts (', 'smtp_attempts_upgrade ('))
        conn.execute(f'INSERT INTO smtp_attempts_upgrade ({columns}) SELECT {columns} FROM smtp_attempts')
        conn.execute('DROP TABLE smtp_attempts')
        conn.execute('ALTER TABLE smtp_attempts_upgrade RENAME TO smtp_attempts')
    else:
        conn.execute(sql)
    if 'expires_at' not in {r[1] for r in conn.execute('PRAGMA table_info(smtp_attempts)')}:
        conn.execute('ALTER TABLE smtp_attempts ADD COLUMN expires_at TEXT')
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_smtp_dispatcher ON smtp_attempts((1)) WHERE status='sending'")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS smtp_attempt_sequence ON smtp_attempts(handoff_id,attempt_number)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS smtp_one_pending_message ON smtp_attempts(handoff_id) WHERE status IN ('queued','sending')")
    conn.execute('''CREATE TABLE IF NOT EXISTS smtp_retry_requests (
        request_key TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES smtp_attempts(job_id),
        request_json TEXT NOT NULL, created_at TEXT NOT NULL)''')


def _now():
    return datetime.now(timezone.utc).isoformat()


class SmtpQueue:
    def __init__(self, db):
        self.db = db

    def enqueue(self, job_id, handoff_id, message_id, mime_bytes, envelope, config_revision):
        now = _now()
        with self.db.transaction() as conn:
            inserted = conn.execute("""INSERT INTO smtp_attempts(job_id,handoff_id,status,phase,message_id,
                mime_bytes,mime_sha256,envelope_json,config_revision,created_at,updated_at)
                VALUES(?,?,'queued','prepared',?,?,?,?,?,?,?) ON CONFLICT(job_id) DO NOTHING""",
                (job_id, handoff_id, message_id, mime_bytes, hashlib.sha256(mime_bytes).hexdigest(),
                 json.dumps(envelope, sort_keys=True), config_revision, now, now)).rowcount
            row = conn.execute("SELECT * FROM smtp_attempts WHERE job_id=?", (job_id,)).fetchone()
            if (row["message_id"] != message_id or row["mime_bytes"] != mime_bytes or
                    row["config_revision"] != config_revision or
                    json.loads(row["envelope_json"]) != envelope):
                raise DeliveryError("Immutable SMTP queue entry does not match request")
            if inserted:
                record_delivery_event(conn, job_id, "run_delivery_queued", now)

    def get(self, job_id):
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT * FROM smtp_attempts WHERE job_id=?", (job_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def latest(self, handoff_id):
        conn = self.db.get_connection()
        try:
            row = conn.execute('SELECT * FROM smtp_attempts WHERE handoff_id=? ORDER BY attempt_number DESC LIMIT 1', (handoff_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def history(self, handoff_id):
        conn = self.db.get_connection()
        try:
            return [dict(r) for r in conn.execute('''SELECT job_id,status,phase,attempt_number,
                created_at,updated_at,error,error_code,server_reply,retryable,next_attempt_at,diagnostics_json
                FROM smtp_attempts WHERE handoff_id=? ORDER BY attempt_number''', (handoff_id,))]
        finally:
            conn.close()

    def requested(self, request_key, request):
        conn = self.db.get_connection()
        try:
            cached = conn.execute('SELECT * FROM smtp_retry_requests WHERE request_key=?', (request_key,)).fetchone()
            if cached:
                if cached['request_json'] != json.dumps(request,sort_keys=True):
                    raise DeliveryError('Retry request key was already used for a different request')
                return dict(conn.execute('SELECT * FROM smtp_attempts WHERE job_id=?',(cached['job_id'],)).fetchone())
            row = conn.execute('SELECT * FROM smtp_attempts WHERE request_key=?', (request_key,)).fetchone()
            if row and row['request_json'] != json.dumps(request, sort_keys=True):
                raise DeliveryError('Retry request key was already used for a different request')
            return dict(row) if row else None
        finally:
            conn.close()

    def retry(self, job_id, *, request_key, request, config_revision, allow_uncertain=False):
        with self.db.transaction() as conn:
            cached = conn.execute('SELECT * FROM smtp_retry_requests WHERE request_key=?',(request_key,)).fetchone()
            if cached:
                if cached['request_json'] != json.dumps(request,sort_keys=True):
                    raise DeliveryError('Retry request key was already used for a different request')
                return dict(conn.execute('SELECT * FROM smtp_attempts WHERE job_id=?',(cached['job_id'],)).fetchone())
            prior = conn.execute('SELECT * FROM smtp_attempts WHERE request_key=?', (request_key,)).fetchone()
            if prior:
                if prior['request_json'] != json.dumps(request, sort_keys=True):
                    raise DeliveryError('Retry request key was already used for a different request')
                return dict(prior)
            row = conn.execute('SELECT * FROM smtp_attempts WHERE job_id=?', (job_id,)).fetchone()
            if not row or not row['handoff_id'] or row['status'] not in ('failed','uncertain','queued'):
                raise DeliveryError('Only a completed failed email can be retried')
            prior_uncertain = conn.execute("SELECT 1 FROM smtp_attempts WHERE handoff_id=? AND status='uncertain'", (row['handoff_id'],)).fetchone()
            if prior_uncertain and not allow_uncertain:
                raise DeliveryError('SMTP result is uncertain; duplicate delivery acknowledgement is required')
            latest = conn.execute('SELECT job_id FROM smtp_attempts WHERE handoff_id=? ORDER BY attempt_number DESC LIMIT 1', (row['handoff_id'],)).fetchone()
            if latest[0] != job_id:
                raise DeliveryError('A newer email attempt already exists')
            now = _now()
            if row['status'] == 'queued':
                if not row['parent_job_id'] or not row['next_attempt_at'] or row['next_attempt_at'] <= now:
                    raise DeliveryError('Email is already queued for immediate dispatch')
                conn.execute('UPDATE smtp_attempts SET next_attempt_at=?,updated_at=? WHERE job_id=?', (now,now,job_id))
                conn.execute("""INSERT INTO audit_events(entity_type,entity_id,event_type,details_json,occurred_at)
                    VALUES('handoff',?,'email_retry_expedited',?,?)""",(row['handoff_id'],json.dumps({'job_id':job_id,**request},sort_keys=True),now))
                result = dict(conn.execute('SELECT * FROM smtp_attempts WHERE job_id=?',(job_id,)).fetchone())
            else:
                result = self._insert_retry(conn, row, now, now, request_key, request, config_revision)
            conn.execute('INSERT INTO smtp_retry_requests VALUES(?,?,?,?)',(request_key,result['job_id'],json.dumps(request,sort_keys=True),now))
            return result

    @staticmethod
    def _insert_retry(conn, row, now, due, request_key, request, config_revision, expires_at=None):
        handoff = conn.execute('SELECT * FROM delivery_handoffs WHERE handoff_id=?', (row['handoff_id'],)).fetchone()
        if not handoff or handoff['message_type'] == 'system_alert':
            raise DeliveryError('Email retries require a research handoff')
        from researchops.delivery.retry_guard import require_retry_family_clear
        require_retry_family_clear(conn,handoff['run_id'],email_only=True)
        controls = conn.execute('SELECT cancel_requested,force_dry_run FROM execution_controls WHERE run_id=?', (handoff['run_id'],)).fetchone()
        if not controls or controls[0] or controls[1]:
            raise DeliveryError('Cancelled or dry-run email cannot be retried')
        if conn.execute("SELECT 1 FROM smtp_attempts WHERE handoff_id=? AND status IN ('queued','sending','smtp_accepted')", (row['handoff_id'],)).fetchone():
            raise DeliveryError('Email is already pending or accepted')
        job_id = 'smtp-retry-' + uuid.uuid4().hex
        conn.execute('''INSERT INTO smtp_attempts(job_id,handoff_id,status,phase,message_id,mime_bytes,
            mime_sha256,envelope_json,config_revision,created_at,updated_at,parent_job_id,attempt_number,
            request_key,request_json,next_attempt_at,expires_at) VALUES(?,?,'queued','prepared',?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (job_id,row['handoff_id'],row['message_id'],row['mime_bytes'],row['mime_sha256'],row['envelope_json'],
             config_revision,now,now,row['job_id'],row['attempt_number']+1,request_key,json.dumps(request,sort_keys=True),due,expires_at))
        conn.execute('''UPDATE delivery_handoffs SET status='queued',external_receipt_id=NULL,
            acknowledged_at=NULL,external_delivery_status=NULL,receipt_sha256=NULL,receipt_trust_status=NULL,
            decision_reason='Email retry queued' WHERE handoff_id=?''', (row['handoff_id'],))
        changed = conn.execute("""UPDATE scheduled_runs SET status='awaiting_receipt',phase='finalize',finished_at=NULL,
            error_message=NULL WHERE run_id=? AND status IN ('failed','needs_attention')""", (handoff['run_id'],)).rowcount
        if not changed:
            raise DeliveryError('Run state changed before email retry')
        conn.execute("""INSERT INTO audit_events(entity_type,entity_id,event_type,details_json,actor,occurred_at)
            VALUES('handoff',?,'email_retry_queued',?,'smtp-queue',?)""", (row['handoff_id'],
            json.dumps({'job_id':job_id,'parent_job_id':row['job_id'],'attempt_number':row['attempt_number']+1,
                        'next_attempt_at':due,**request},sort_keys=True), now))
        record_delivery_event(conn, job_id, 'run_delivery_queued', now)
        return dict(conn.execute('SELECT * FROM smtp_attempts WHERE job_id=?',(job_id,)).fetchone())

    def pending(self):
        conn = self.db.get_connection()
        try:
            return [dict(r) for r in conn.execute("SELECT job_id,handoff_id,status,phase,created_at FROM smtp_attempts WHERE status='queued' AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY created_at,job_id", (_now(),))]
        finally:
            conn.close()

    def completed(self):
        conn = self.db.get_connection()
        try:
            return [dict(r) for r in conn.execute("SELECT job_id FROM smtp_attempts WHERE status NOT IN ('queued','sending') ORDER BY updated_at")]
        finally:
            conn.close()

    def claim(self, job_id, *, include_future=False):
        token = uuid.uuid4().hex
        now = _now()
        with self.db.transaction() as conn:
            try:
                changed = conn.execute("""UPDATE smtp_attempts SET status='sending',claim_token=?,claim_pid=?,updated_at=?
                    WHERE job_id=? AND status='queued' AND (? OR next_attempt_at IS NULL OR next_attempt_at<=?)""", (token, os.getpid(), now, job_id, int(include_future),now)).rowcount
            except sqlite3.IntegrityError:
                return None
            if changed:
                record_delivery_event(conn, job_id, "run_step_started", now)
            return token if changed else None

    def start_data(self, job_id, token, *, production=False):
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM smtp_attempts WHERE job_id=?", (job_id,)).fetchone()
            now = _now()
            if row and row['expires_at'] and row['expires_at'] <= now:
                raise DeliveryError('Automatic email retry expired before submission')
            if row and row["handoff_id"]:
                permission = conn.execute("""SELECT t.delivery_approved,t.delivery_mode,t.approved_version_hash,
                    t.approved_delivery_revision,t.active_version_hash,h.task_version_hash,h.message_type,
                    e.cancel_requested,e.force_dry_run,r.status,r.trigger_type,h.task_id,h.run_id
                    FROM delivery_handoffs h JOIN tasks t ON t.task_id=h.task_id
                    JOIN scheduled_runs r ON r.run_id=h.run_id
                    JOIN execution_controls e ON e.run_id=h.run_id WHERE h.handoff_id=?""",
                    (row["handoff_id"],)).fetchone()
                if (not permission or permission["delivery_mode"] != "handoff" or
                        permission["force_dry_run"] or permission["trigger_type"] == "candidate_dry_run" or
                        (not production and (not permission["delivery_approved"] or
                         permission["approved_version_hash"] != permission["task_version_hash"] or
                         permission["approved_delivery_revision"] != row["config_revision"])) or
                        (permission["message_type"] != "system_alert" and
                         (permission["cancel_requested"] or permission["status"] != "awaiting_receipt"))):
                    raise DeliveryError("SMTP permission revoked before DATA submission")
                from researchops.delivery.authorization import assert_delivery_version
                assert_delivery_version(conn, permission["task_id"], permission["task_version_hash"],
                                        run_id=permission["run_id"], production=production,
                                        system_alert=permission["message_type"] == "system_alert")
            changed = conn.execute("""UPDATE smtp_attempts SET phase='data_started',updated_at=?
                WHERE job_id=? AND status='sending' AND claim_token=? AND phase='prepared'""",
                (now, job_id, token)).rowcount
            if not changed:
                raise DeliveryError("SMTP dispatch ownership lost")
            record_delivery_event(conn, job_id, "run_delivery_data_started", now)

    def record_stage(self, job_id, token, diagnostics):
        now = _now()
        with self.db.transaction() as conn:
            changed = conn.execute("""UPDATE smtp_attempts SET diagnostics_json=?,updated_at=?
                WHERE job_id=? AND status='sending' AND claim_token=?""",
                (json.dumps(diagnostics,sort_keys=True),now,job_id,token)).rowcount
            if not changed:
                raise DeliveryError('SMTP dispatch ownership lost')

    def cancel_pending_for_run(self, run_id):
        """Cancel before a sender owns the job; active senders observe the durable flag."""
        now = _now()
        with self.db.transaction() as conn:
            running = conn.execute("""SELECT 1 FROM smtp_attempts a JOIN delivery_handoffs h ON h.handoff_id=a.handoff_id
                WHERE h.run_id=? AND h.message_type!='system_alert' AND a.status='sending' LIMIT 1""",(run_id,)).fetchone()
            if running:
                return False
            changed = conn.execute("""UPDATE scheduled_runs SET status='cancelled',phase='finalize',finished_at=?,
                error_message='Cancelled before SMTP submission' WHERE run_id=? AND status='awaiting_receipt'
                AND EXISTS(SELECT 1 FROM execution_controls WHERE run_id=? AND cancel_requested=1)""",
                (now,run_id,run_id)).rowcount
            if not changed:
                return False
            pending_jobs = conn.execute("""SELECT a.job_id FROM smtp_attempts a
                JOIN delivery_handoffs h ON h.handoff_id=a.handoff_id
                WHERE h.run_id=? AND h.message_type!='system_alert' AND a.status='queued'""",
                (run_id,)).fetchall()
            conn.execute("""UPDATE smtp_attempts SET status='failed',phase='finished',error='Cancelled before SMTP submission',updated_at=?
                WHERE status='queued' AND handoff_id IN(SELECT handoff_id FROM delivery_handoffs WHERE run_id=? AND message_type!='system_alert')""",
                (now,run_id))
            conn.execute("""UPDATE delivery_handoffs SET status='failed',decision_reason='Cancelled before SMTP submission'
                WHERE run_id=? AND message_type!='system_alert' AND status IN ('prepared','published','queued')""",(run_id,))
            uncertain = conn.execute("""SELECT r.* FROM smtp_attempts a
                JOIN delivery_handoffs h ON h.handoff_id=a.handoff_id
                JOIN delivery_receipts r ON r.external_receipt_id='smtp-'||a.job_id
                WHERE h.run_id=? AND h.message_type!='system_alert' AND a.status='uncertain'
                AND NOT EXISTS(SELECT 1 FROM smtp_attempts accepted WHERE accepted.handoff_id=h.handoff_id
                    AND accepted.status='smtp_accepted') ORDER BY a.attempt_number DESC LIMIT 1""", (run_id,)).fetchone()
            if uncertain:
                conn.execute("""UPDATE delivery_handoffs SET status='uncertain',external_delivery_status='uncertain',
                    external_receipt_id=?,receipt_sha256=?,acknowledged_at=?,receipt_trust_status='verified_local_smtp',
                    decision_reason='Retry cancelled; earlier SMTP delivery remains uncertain' WHERE handoff_id=?""",
                    (uncertain['external_receipt_id'],uncertain['receipt_sha256'],uncertain['occurred_at'],uncertain['handoff_id']))
                conn.execute("""UPDATE scheduled_runs SET status='needs_attention',
                    error_message='Retry cancelled; earlier SMTP delivery remains uncertain' WHERE run_id=?""",(run_id,))
            for pending in pending_jobs:
                record_delivery_event(conn, pending["job_id"], "run_step_finished", now, state="cancelled")
            return True

    def recover_interrupted(self):
        """Never resend dead-owner jobs; pre-DATA is failed, post-DATA uncertain."""
        conn = self.db.get_connection()
        try:
            rows = [dict(r) for r in conn.execute("SELECT * FROM smtp_attempts WHERE status='sending'")]
        finally:
            conn.close()
        recovered = []
        for row in rows:
            try:
                os.kill(row["claim_pid"], 0)
                continue
            except ProcessLookupError:
                pass
            except (PermissionError, TypeError):
                continue
            status = "uncertain" if row["phase"] == "data_started" else "failed"
            self.finish(row["job_id"], row["claim_token"], status, error="Dispatcher exited before completion")
            recovered.append(row["job_id"])
        return recovered

    def finish(self, job_id, token, status, *, error=None, server_reply=None,
               error_code=None, retryable=False, diagnostics=None, retry_policy=None):
        if status not in ("smtp_accepted", "connection_ok", "failed", "uncertain"):
            raise DeliveryError("Invalid SMTP result")
        now = _now()
        receipt = None
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM smtp_attempts WHERE job_id=?", (job_id,)).fetchone()
            if not row or row["status"] != "sending" or row["claim_token"] != token:
                raise DeliveryError("SMTP dispatch ownership lost")
            if status == "smtp_accepted" and row["phase"] != "data_started":
                raise DeliveryError("SMTP acceptance requires a completed DATA exchange")
            if status == "connection_ok" and (row["handoff_id"] or not json.loads(row["envelope_json"]).get("connection_test")):
                raise DeliveryError("Connection evidence requires a queued diagnostic")
            retryable = bool(retryable and status == 'failed')
            conn.execute("""UPDATE smtp_attempts SET status=?,phase='finished',updated_at=?,error=?,server_reply=?,
                error_code=?,retryable=?,diagnostics_json=COALESCE(?,diagnostics_json)
                WHERE job_id=? AND claim_token=?""", (status, now, error, server_reply,error_code,int(retryable),
                json.dumps(diagnostics,sort_keys=True) if diagnostics is not None else None,job_id,token))
            if not row["handoff_id"]:
                return None
            handoff = conn.execute("SELECT * FROM delivery_handoffs WHERE handoff_id=?", (row["handoff_id"],)).fetchone()
            receipt = DeliveryReceipt(external_receipt_id="smtp-" + job_id,
                handoff_id=handoff["handoff_id"], idempotency_key=handoff["idempotency_key"],
                delivery_request_sha256=handoff["delivery_request_sha256"], status=status, occurred_at=now,
                proof={"type": "local_smtp_attempt", "issuer": "researchops-smtp", "value": job_id},
                external_message_ref=row["message_id"],
                error={"code": error_code or "smtp_" + status, "message": error or status, "retryable": retryable} if status != "smtp_accepted" else None)
            raw = json.dumps(receipt.to_dict(), sort_keys=True)
            digest = hashlib.sha256(raw.encode()).hexdigest()
            conn.execute("""INSERT INTO delivery_receipts(external_receipt_id,handoff_id,idempotency_key,
                delivery_request_sha256,status,occurred_at,external_message_ref,error_json,proof_json,
                receipt_json,receipt_sha256,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (receipt.external_receipt_id, receipt.handoff_id, receipt.idempotency_key,
                 receipt.delivery_request_sha256,status,now,row["message_id"],
                 json.dumps(receipt.error) if receipt.error else None,json.dumps(receipt.proof),raw,digest,now))
            conn.execute("""UPDATE delivery_handoffs SET status=?,external_receipt_id=?,acknowledged_at=?,
                external_delivery_status=?,receipt_sha256=?,receipt_trust_status='verified_local_smtp',decision_reason=?
                WHERE handoff_id=?""", (status,receipt.external_receipt_id,now,status,digest,
                    "SMTP server accepted all recipients and DATA" if status == "smtp_accepted" else error,
                    handoff["handoff_id"]))
            # Alert failure must never alter the research run or recursively create an alert.
            if handoff["message_type"] != "system_alert":
                run_status = {"smtp_accepted":"succeeded", "failed":"failed", "uncertain":"needs_attention"}[status]
                prior_uncertain = conn.execute("""SELECT r.* FROM smtp_attempts a
                    JOIN delivery_receipts r ON r.external_receipt_id='smtp-'||a.job_id
                    WHERE a.handoff_id=? AND a.status='uncertain' AND a.job_id!=?
                    ORDER BY a.attempt_number DESC LIMIT 1""",(row['handoff_id'],job_id)).fetchone()
                if status == 'failed' and prior_uncertain:
                    # An acknowledged retry that wasn't sent cannot establish
                    # that the older uncertain message was not delivered.
                    run_status = 'needs_attention'
                    conn.execute('''UPDATE delivery_handoffs SET status='uncertain',external_delivery_status='uncertain',
                        external_receipt_id=?,acknowledged_at=?,receipt_sha256=?,
                        decision_reason='Previous SMTP attempt remains uncertain; latest retry failed'
                        WHERE handoff_id=?''',(prior_uncertain['external_receipt_id'],prior_uncertain['occurred_at'],
                        prior_uncertain['receipt_sha256'],row['handoff_id']))
                controls = conn.execute("SELECT cancel_requested FROM execution_controls WHERE run_id=?",(handoff["run_id"],)).fetchone()
                if status == "failed" and not prior_uncertain and row["phase"] != "data_started" and controls and controls[0]:
                    run_status = "cancelled"
                conn.execute("""UPDATE scheduled_runs SET status=?,phase='finalize',finished_at=?,error_message=?
                    WHERE run_id=? AND status IN ('awaiting_receipt','running')""",
                    (run_status,now,error,handoff["run_id"]))
                if status == "smtp_accepted":
                    self._commit_reported(conn, handoff, now)
                record_delivery_event(conn, job_id, "run_step_finished", now, state=run_status)
            conn.execute("""INSERT INTO audit_events(entity_type,entity_id,event_type,details_json,actor,occurred_at)
                VALUES('handoff',?,? ,?,'smtp-dispatcher',?)""", (handoff["handoff_id"],
                    "smtp_" + status, json.dumps({"job_id":job_id,"message_id":row["message_id"]}), now))
            if (retryable and retry_policy and retry_policy.smtp_auto_retry
                    and handoff['message_type'] != 'system_alert'
                    and not prior_uncertain
                    and row['attempt_number'] < retry_policy.smtp_max_attempts
                    and controls and not controls[0]):
                first = conn.execute('SELECT created_at FROM smtp_attempts WHERE handoff_id=? ORDER BY attempt_number LIMIT 1', (row['handoff_id'],)).fetchone()[0]
                due = datetime.fromisoformat(now) + timedelta(seconds=retry_policy.smtp_retry_base_seconds * (2 ** (row['attempt_number']-1)))
                expiry = datetime.fromisoformat(first) + timedelta(seconds=retry_policy.smtp_retry_expiry_seconds)
                if due <= expiry:
                    # Scheduling policy must never erase an observed terminal
                    # SMTP result when a related run/configuration wins a race.
                    conn.execute('SAVEPOINT smtp_schedule_retry')
                    try:
                        self._insert_retry(conn, row, now, due.isoformat(), 'auto-' + job_id,
                            {'handoff_id':row['handoff_id'],'mode':'automatic','allow_uncertain':False,'reason':''}, row['config_revision'],expiry.isoformat())
                    except (DeliveryError, sqlite3.IntegrityError):
                        conn.execute('ROLLBACK TO smtp_schedule_retry')
                        conn.execute("""INSERT INTO audit_events(entity_type,entity_id,event_type,details_json,actor,occurred_at)
                            VALUES('handoff',?,'email_auto_retry_blocked',?,'smtp-queue',?)""",(row['handoff_id'],
                            json.dumps({'job_id':job_id,'reason_code':'retry_state_conflict'}),now))
                    finally:
                        conn.execute('RELEASE smtp_schedule_retry')
        return receipt

    @staticmethod
    def _commit_reported(conn, handoff, now):
        from researchops.engine.dedupe import derive_entity_key, derive_content_fingerprint
        comp = conn.execute("SELECT input_json FROM composition_inputs WHERE run_id=? AND revision=?",
                            (handoff["run_id"], handoff["message_revision"])).fetchone()
        version = conn.execute("SELECT definition_json FROM task_versions WHERE version_hash=?",
                               (handoff["task_version_hash"],)).fetchone()
        if not comp or not version:
            return
        cfg = json.loads(version["definition_json"]).get("state", {}).get("dedupe", {})
        keys, fields = cfg.get("key_fields", []), cfg.get("content_fields", [])
        if not keys or not fields:
            return
        for record in json.loads(comp[0]).get("reportable_records", []):
            # Missing identity/content fields never become dedupe evidence.
            if any(key not in record or record[key] is None for key in [*keys, *fields]):
                continue
            key = derive_entity_key(record, keys)
            fingerprint = derive_content_fingerprint(record, fields)
            if key and fingerprint:
                conn.execute("""INSERT OR IGNORE INTO reported_items(task_id,entity_key,content_fingerprint,
                    run_id,handoff_id,reported_at) VALUES(?,?,?,?,?,?)""",
                    (handoff["task_id"],key,fingerprint,handoff["run_id"],handoff["handoff_id"],now))
