-- ResearchOps SQLite Schema v3. All absolute instants use UTC ISO 8601.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Mutable operator labels are independent of immutable legacy package tokens.
-- IDs are allocated by SQLite and never reused, including after soft deletion.
CREATE TABLE IF NOT EXISTS entity_catalog (
    entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('task','recipient_group','sender')),
    legacy_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    request_key TEXT,
    owner_user_id TEXT REFERENCES auth_users(user_id),
    UNIQUE(kind,legacy_key),
    UNIQUE(kind,request_key)
);

CREATE TRIGGER IF NOT EXISTS immutable_catalog_identity
BEFORE UPDATE OF entity_id,kind,legacy_key,created_at,request_key ON entity_catalog
BEGIN SELECT RAISE(ABORT,'Catalog identity is immutable'); END;

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    active_version_hash TEXT REFERENCES task_versions(version_hash) DEFERRABLE INITIALLY DEFERRED,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    delivery_mode TEXT NOT NULL DEFAULT 'dry_run' CHECK(delivery_mode IN ('disabled','dry_run','handoff')),
    delivery_approved INTEGER NOT NULL DEFAULT 0 CHECK(delivery_approved IN (0,1)),
    approved_version_hash TEXT REFERENCES task_versions(version_hash),
    approved_delivery_revision TEXT,
    approval_dry_run_id TEXT REFERENCES scheduled_runs(run_id),
    approved_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK(delivery_approved=0 OR (approved_version_hash=active_version_hash AND approved_delivery_revision IS NOT NULL AND approval_dry_run_id IS NOT NULL AND approved_at IS NOT NULL))
);

-- Web authentication and owner-scoped automation require matching v3 services.
CREATE TABLE IF NOT EXISTS auth_users (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','user','viewer')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    must_change_password INTEGER NOT NULL DEFAULT 1 CHECK(must_change_password IN (0,1)),
    credential_revision INTEGER NOT NULL DEFAULT 1 CHECK(credential_revision>0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_installation (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    owner_user_id TEXT NOT NULL REFERENCES auth_users(user_id)
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT REFERENCES auth_users(user_id),
    kind TEXT NOT NULL CHECK(kind IN ('preauth','authenticated')),
    csrf_token TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    credential_revision INTEGER,
    CHECK((kind='preauth' AND user_id IS NULL AND credential_revision IS NULL) OR
          (kind='authenticated' AND user_id IS NOT NULL AND credential_revision IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions(user_id);
CREATE INDEX IF NOT EXISTS auth_sessions_expiry ON auth_sessions(expires_at);

CREATE TABLE IF NOT EXISTS auth_user_tasks (
    user_id TEXT NOT NULL REFERENCES auth_users(user_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    PRIMARY KEY(user_id,task_id)
);

CREATE TABLE IF NOT EXISTS auth_bootstrap (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    token_hash TEXT NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_login_attempts (
    attempt_key TEXT PRIMARY KEY,
    window_started REAL NOT NULL,
    attempt_count INTEGER NOT NULL CHECK(attempt_count>0)
);

CREATE TABLE IF NOT EXISTS task_drafts (
    draft_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    source_type TEXT NOT NULL,
    source_id TEXT,
    config_yaml TEXT NOT NULL,
    task_md TEXT NOT NULL,
    email_spec_md TEXT,
    schemas_json TEXT NOT NULL DEFAULT '{}',
    validation_report_json TEXT,
    candidate_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_versions (
    version_hash TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    definition_json TEXT NOT NULL,
    package_files_json TEXT NOT NULL,
    sealed_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0,1)),
    UNIQUE(task_id,version_hash)
);

CREATE TABLE IF NOT EXISTS scheduled_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    task_version_hash TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    timezone TEXT NOT NULL,
    local_date TEXT NOT NULL,
    local_date_display TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','awaiting_receipt','succeeded','failed','timed_out','cancelled','needs_attention')),
    phase TEXT NOT NULL DEFAULT 'queued' CHECK(phase IN ('queued','preflight','research','validate','dedupe','compose','validate_message','handoff','finalize')),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt>=0),
    workspace_generation INTEGER NOT NULL DEFAULT 1 CHECK(workspace_generation>0),
    parent_run_id TEXT REFERENCES scheduled_runs(run_id),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    last_heartbeat_at TEXT,
    last_progress_at TEXT,
    error_message TEXT,
    FOREIGN KEY(task_id,task_version_hash) REFERENCES task_versions(task_id,version_hash)
);

CREATE TABLE IF NOT EXISTS run_leases (
    run_id TEXT PRIMARY KEY REFERENCES scheduled_runs(run_id),
    worker_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    fencing_token TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_results (
    run_id TEXT PRIMARY KEY REFERENCES scheduled_runs(run_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    records_json TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS composition_inputs (
    run_id TEXT NOT NULL REFERENCES scheduled_runs(run_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    task_version_hash TEXT NOT NULL REFERENCES task_versions(version_hash),
    revision INTEGER NOT NULL DEFAULT 1,
    input_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id,revision)
);

CREATE TABLE IF NOT EXISTS composition_results (
    run_id TEXT NOT NULL REFERENCES scheduled_runs(run_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    recipient_group_id TEXT NOT NULL,
    recipient_group_reason TEXT NOT NULL,
    subject TEXT NOT NULL,
    html_path TEXT NOT NULL,
    text_path TEXT NOT NULL,
    included_record_ids_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id,revision)
);

CREATE TABLE IF NOT EXISTS delivery_handoffs (
    handoff_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    run_id TEXT NOT NULL REFERENCES scheduled_runs(run_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    task_version_hash TEXT NOT NULL REFERENCES task_versions(version_hash),
    message_revision INTEGER NOT NULL,
    message_type TEXT NOT NULL,
    recipient_group_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    delivery_request_json TEXT NOT NULL,
    delivery_request_sha256 TEXT NOT NULL,
    published_at TEXT,
    external_receipt_id TEXT,
    acknowledged_at TEXT,
    external_delivery_status TEXT,
    receipt_sha256 TEXT,
    receipt_trust_status TEXT,
    decision_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_receipts (
    external_receipt_id TEXT PRIMARY KEY,
    handoff_id TEXT NOT NULL REFERENCES delivery_handoffs(handoff_id),
    idempotency_key TEXT NOT NULL,
    delivery_request_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    external_message_ref TEXT,
    error_json TEXT,
    proof_json TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reported_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    entity_key TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES scheduled_runs(run_id),
    handoff_id TEXT NOT NULL REFERENCES delivery_handoffs(handoff_id),
    reported_at TEXT NOT NULL,
    UNIQUE(task_id, entity_key, content_fingerprint)
);

CREATE TABLE IF NOT EXISTS task_workspaces (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    generation INTEGER NOT NULL DEFAULT 1,
    lock_owner TEXT,
    lock_fencing_token TEXT,
    locked_at TEXT,
    last_run_id TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'single-operator',
    occurred_at TEXT NOT NULL
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_scheduled_runs_task_status ON scheduled_runs(task_id, status);
CREATE INDEX IF NOT EXISTS idx_scheduled_runs_status_due ON scheduled_runs(status, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_delivery_handoffs_run ON delivery_handoffs(run_id);
CREATE INDEX IF NOT EXISTS idx_task_versions_task ON task_versions(task_id);
CREATE INDEX IF NOT EXISTS idx_reported_items_lookup ON reported_items(task_id, entity_key);
CREATE INDEX IF NOT EXISTS idx_audit_events_entity ON audit_events(entity_type, entity_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_schedule_occurrence ON scheduled_runs(task_id,scheduled_for) WHERE trigger_type='schedule';
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_version ON task_versions(task_id) WHERE is_active=1;

CREATE TABLE IF NOT EXISTS task_claims (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    run_id TEXT UNIQUE NOT NULL REFERENCES scheduled_runs(run_id),
    fencing_token TEXT UNIQUE NOT NULL,
    claimed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_controls (
    run_id TEXT PRIMARY KEY REFERENCES scheduled_runs(run_id),
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
    force_dry_run INTEGER NOT NULL DEFAULT 0 CHECK(force_dry_run IN (0,1)),
    composition_revision INTEGER NOT NULL DEFAULT 1 CHECK(composition_revision > 0),
    child_cleanup_verified INTEGER NOT NULL DEFAULT 0 CHECK(child_cleanup_verified IN (0,1))
);
CREATE TABLE IF NOT EXISTS run_execution_plans (
    run_id TEXT PRIMARY KEY REFERENCES scheduled_runs(run_id),
    plan_json TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_delivery_authorizations (
    run_id TEXT PRIMARY KEY REFERENCES scheduled_runs(run_id),
    authorization_json TEXT NOT NULL,
    authorization_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduler_watermarks (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    evaluated_through TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_commands (
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    request_key TEXT NOT NULL,
    run_id TEXT UNIQUE NOT NULL REFERENCES scheduled_runs(run_id),
    request_json TEXT NOT NULL,
    PRIMARY KEY(task_id,request_key)
);
CREATE TABLE IF NOT EXISTS migration_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_table TEXT NOT NULL,
    source_key TEXT,
    row_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    quarantined_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS immutable_version_content
BEFORE UPDATE OF task_id,version_hash,definition_json,package_files_json,sealed_at ON task_versions
BEGIN SELECT RAISE(ABORT,'Sealed version content is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_research_result
BEFORE UPDATE ON research_results
BEGIN SELECT RAISE(ABORT,'Research evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_composition_input
BEFORE UPDATE ON composition_inputs
BEGIN SELECT RAISE(ABORT,'Composition input revision is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_composition_result
BEFORE UPDATE ON composition_results
BEGIN SELECT RAISE(ABORT,'Composition result revision is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_receipt
BEFORE UPDATE ON delivery_receipts
BEGIN SELECT RAISE(ABORT,'Receipt evidence is immutable'); END;
