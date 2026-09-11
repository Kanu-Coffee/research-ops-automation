"""Transactional migrations retaining legacy evidence and stable identities.

Legacy running work and approvals are deliberately not re-enabled. Operators
must reconcile surviving process groups and repeat a candidate dry-run.
"""

from datetime import datetime, timezone
import json
import re
from pathlib import Path
import sqlite3


TABLES = (
    "tasks", "task_versions", "task_drafts", "scheduled_runs", "run_leases",
    "research_results", "composition_inputs", "composition_results",
    "delivery_handoffs", "delivery_receipts", "reported_items",
    "task_workspaces", "audit_events",
)


def _schema(conn):
    statement = ""
    for line in Path(__file__).with_name("schema.sql").read_text(encoding="utf-8").splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""


def migrate(conn: sqlite3.Connection, db_path: Path) -> None:
    now = datetime.now(timezone.utc).isoformat()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] if "schema_migrations" in tables else 0
    if version and version > 3:
        raise RuntimeError(f"Database schema {version} is newer than supported schema 3")
    # SQLite cannot widen a CHECK in place. Disable FK enforcement only for the
    # v2 table replacement, outside its transaction; validate every FK before
    # commit and restore enforcement even when migration rolls back.
    rebuild_auth = version == 2 and "auth_users" in tables
    if rebuild_auth:
        conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if version in (2, 3):
            _schema(conn)
        elif "tasks" not in tables:
            _schema(conn)
            conn.execute("INSERT INTO schema_migrations VALUES(1,?)", (now,))
            conn.execute("INSERT INTO schema_migrations VALUES(2,?)", (now,))
        else:
            # These original tables are retained, including any invalid rows.
            original = {}
            for table in TABLES:
                if table in tables:
                    original[table] = [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"')]
                    conn.execute(f'ALTER TABLE "{table}" RENAME TO "legacy_v1_{table}"')
            for row in list(conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")):
                conn.execute('DROP INDEX "' + row[0].replace('"', '""') + '"')
            _schema(conn)
            for table in TABLES:
                columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
                rows = original.get(table, [])
                if table == "scheduled_runs":
                    rows = sorted(rows, key=lambda r: r.get("created_at") or "")
                for raw in rows:
                    data = {k: v for k, v in raw.items() if k in columns}
                    if table == "tasks":
                        data.update(active_version_hash=None, enabled=0, delivery_approved=0, delivery_mode="dry_run")
                    elif table == "task_versions":
                        data["is_active"] = 0
                    elif table == "scheduled_runs":
                        if data.get("status") in {"running", "queued", "awaiting_receipt"}:
                            data.update(status="needs_attention", error_message="Legacy execution requires operator reconciliation")
                        # Preserve the old timestamp verbatim in legacy_v1_*.
                        try:
                            instant = datetime.fromisoformat(data["scheduled_for"].replace("Z", "+00:00"))
                            if instant.tzinfo is None:
                                raise ValueError("ambiguous naive scheduled_for")
                            data["scheduled_for"] = instant.astimezone(timezone.utc).isoformat()
                        except (ValueError, TypeError):
                            _quarantine(conn, table, raw, "Invalid or ambiguous scheduled_for", now)
                            continue
                    elif table == "delivery_handoffs":
                        data["receipt_trust_status"] = "legacy_unverified"
                        if data.get("status") not in {"sent", "acknowledged", "failed", "skipped"}:
                            data["status"] = "uncertain"
                    try:
                        names = ",".join('"' + k + '"' for k in data)
                        conn.execute(f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in data)})', tuple(data.values()))
                    except sqlite3.IntegrityError as exc:
                        _quarantine(conn, table, raw, str(exc), now)
            # Existing running process trees stay fenced even after lease expiry.
            for raw in original.get("scheduled_runs", []):
                if raw.get("status") == "running":
                    try:
                        conn.execute("INSERT INTO task_claims VALUES(?,?,?,?)", (raw["task_id"], raw["run_id"], "legacy-" + raw["run_id"], now))
                    except sqlite3.IntegrityError:
                        pass
            conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(1,?)", (now,))
            conn.execute("INSERT INTO schema_migrations VALUES(2,?)", (now,))
            conn.execute("INSERT INTO audit_events(entity_type,entity_id,event_type,details_json,occurred_at) VALUES('database','schema','migration_v2',?,?)", (json.dumps({"legacy_tables_preserved": list(original), "tasks_disabled": True, "approvals_revoked": True}), now))
        _ownership_schema(conn)
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(3,?)", (now,))
        # Delivery owns its queue schema; same migration transaction.
        from researchops.delivery.queue import add_delivery_schema
        add_delivery_schema(conn)
        failures = list(conn.execute("PRAGMA foreign_key_check"))
        if failures:
            raise RuntimeError(f"Migration foreign key validation failed: {len(failures)} rows")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if rebuild_auth:
            conn.execute("PRAGMA foreign_keys=ON")


def _ownership_schema(conn):
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='auth_users'").fetchone()[0]
    if not re.search(r"['\"]user['\"]", sql):
        replacement = re.sub(r"CHECK\s*\(\s*role\s+IN\s*\([^)]*\)\s*\)",
            "CHECK(role IN ('admin','user','viewer'))", sql, flags=re.I)
        if replacement == sql:
            raise RuntimeError("Unrecognized account role schema; migration stopped")
        replacement = re.sub(r"\bauth_users\b", "auth_users_v3", replacement, count=1)
        conn.execute(replacement)
        conn.execute("INSERT INTO auth_users_v3 SELECT * FROM auth_users")
        conn.execute("DROP TABLE auth_users")
        conn.execute("ALTER TABLE auth_users_v3 RENAME TO auth_users")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(entity_catalog)")}
    if "owner_user_id" not in columns:
        conn.execute("ALTER TABLE entity_catalog ADD COLUMN owner_user_id TEXT REFERENCES auth_users(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS catalog_owner ON entity_catalog(owner_user_id,kind,deleted_at,entity_id)")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS immutable_catalog_owner
        BEFORE UPDATE OF owner_user_id ON entity_catalog
        WHEN OLD.owner_user_id IS NOT NULL AND NEW.owner_user_id IS NOT OLD.owner_user_id
        BEGIN SELECT RAISE(ABORT,'Catalog owner is immutable'); END""")
    row = conn.execute("SELECT user_id FROM auth_users WHERE role='admin' ORDER BY created_at,user_id LIMIT 1").fetchone()
    if row:
        from researchops.services.ownership import adopt_unowned
        adopt_unowned(conn, row[0])


def _quarantine(conn, table, row, reason, now):
    conn.execute("INSERT INTO migration_quarantine(source_table,source_key,row_json,reason,quarantined_at) VALUES(?,?,?,?,?)", (table, str(next(iter(row.values()), "")), json.dumps(row, ensure_ascii=False), reason, now))
