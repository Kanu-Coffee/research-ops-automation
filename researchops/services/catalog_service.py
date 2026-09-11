"""Stable numeric identities and reversible operator catalog administration.

Legacy package/SMTP keys remain opaque immutable routing tokens. SQLite owns
labels and soft deletion. Catalog routing includes only public ID/name snapshots
in worker inputs; labels never change SMTP revisions or expose mailbox mappings.
"""

from datetime import datetime, timezone
import json
import re

from researchops.delivery.smtp_config import load_delivery_config
from researchops.errors import DeliveryError, NotFoundError, ValidationError
from researchops.services.ownership import (creation_owner,
    resolve_creation_owner, scoped_request_key)


KINDS = {"task": "task", "recipient_group": "group", "sender": "sender"}
_ALL_OWNERS = object()


def _now():
    return datetime.now(timezone.utc).isoformat()


class CatalogService:
    def __init__(self, settings, db):
        self.settings, self.db = settings, db

    @staticmethod
    def _kind(kind):
        if kind not in KINDS:
            raise ValidationError("Unknown catalog kind")

    @staticmethod
    def validate_name(name):
        if (not isinstance(name, str) or not name.strip() or len(name.strip()) > 200 or
                any(ord(char) < 32 or ord(char) == 127 for char in name)):
            raise ValidationError("Display name must be 1–200 characters without control characters")
        return name.strip()

    @staticmethod
    def _key(key):
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", key):
            raise ValidationError("Invalid catalog key")

    @staticmethod
    def _audit(conn, kind, key, action, details=None):
        conn.execute("""INSERT INTO audit_events(entity_type,entity_id,event_type,details_json,occurred_at)
            VALUES(?,?,?,?,?)""", (kind, key, action, json.dumps(details or {}, ensure_ascii=False), _now()))

    def ensure(self, kind, key, name=None):
        """Import a legacy identity without overwriting labels or resurrecting it."""
        self._kind(kind)
        self._key(key)
        display_name = self.validate_name(name if name else key)
        now = _now()
        with self.db.transaction() as conn:
            # Avoid INSERT OR IGNORE consuming a new AUTOINCREMENT on every GET.
            row = conn.execute("SELECT * FROM entity_catalog WHERE kind=? AND legacy_key=?", (kind, key)).fetchone()
            if not row:
                conn.execute("""INSERT INTO entity_catalog(kind,legacy_key,display_name,created_at,updated_at,owner_user_id)
                    VALUES(?,?,?,?,?,?)""", (kind, key, display_name, now, now, resolve_creation_owner(conn)))
                row = conn.execute("SELECT * FROM entity_catalog WHERE kind=? AND legacy_key=?", (kind, key)).fetchone()
            return dict(row)

    def allocate(self, kind, display_name, request_key=None):
        self._kind(kind)
        display_name = self.validate_name(display_name)
        if request_key is not None and (not isinstance(request_key, str) or
                not re.fullmatch(r"[a-zA-Z0-9._:-]{1,128}", request_key)):
            raise ValidationError("Invalid catalog creation request key")
        now = _now()
        with self.db.transaction() as conn:
            owner = resolve_creation_owner(conn)
            stored_key = scoped_request_key(owner, request_key)
            if request_key is not None:
                # The legacy key fallback preserves retries made before v3,
                # while a different owner's key can never reveal their object.
                row = conn.execute("""SELECT * FROM entity_catalog WHERE kind=? AND owner_user_id IS ?
                    AND request_key IN (?,?) ORDER BY request_key=? DESC LIMIT 1""",
                    (kind, owner, stored_key, request_key, stored_key)).fetchone()
                if row:
                    if row["deleted_at"]:
                        raise ValidationError("This creation request refers to a deleted item; start a new creation")
                    if row["display_name"] != display_name:
                        raise ValidationError("This creation request was already used with a different display name; start a new creation")
                    return dict(row)
            # BEGIN IMMEDIATE reserves this next SQLite number. Legacy keys may
            # happen to use a future generated token, so skip those numbers.
            sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='entity_catalog'").fetchone()
            number = (sequence[0] if sequence else 0) + 1
            while conn.execute("SELECT 1 FROM entity_catalog WHERE kind=? AND legacy_key=?",
                               (kind, f"{KINDS[kind]}-{number}")).fetchone():
                number += 1
            key = f"{KINDS[kind]}-{number}"
            conn.execute("""INSERT INTO entity_catalog(entity_id,kind,legacy_key,display_name,created_at,updated_at,request_key,owner_user_id)
                VALUES(?,?,?,?,?,?,?,?)""", (number, kind, key, display_name, now, now, stored_key, owner))
            self._audit(conn, kind, key, "catalog_created", {"entity_id": number, "owner_user_id": owner})
            return dict(conn.execute("SELECT * FROM entity_catalog WHERE entity_id=?", (number,)).fetchone())

    def bootstrap(self):
        """Add mappings only: no config writes, secret copying, label changes or restores."""
        conn = self.db.get_connection()
        try:
            rows = [dict(row) for row in conn.execute("""SELECT t.task_id,v.definition_json
                FROM tasks t LEFT JOIN task_versions v ON v.version_hash=t.active_version_hash
                ORDER BY t.task_id""")]
        finally:
            conn.close()
        for row in rows:
            name = row["task_id"]
            try:
                definition = json.loads(row["definition_json"]) if row["definition_json"] else None
                if isinstance(definition, dict):
                    name = self.validate_name(definition.get("name") or name)
            except (ValueError, TypeError, ValidationError):
                pass
            with creation_owner(None):
                self.ensure("task", row["task_id"], name)
        self.bootstrap_delivery()

    def bootstrap_delivery(self, config=None):
        try:
            config = config or load_delivery_config(self.settings.paths.delivery_config_file)
        except DeliveryError:
            # A malformed private config must not prevent diagnostics/startup.
            return
        # Importing a private host configuration must not adopt its entries into
        # the account that happened to open a Web page triggering bootstrap.
        with creation_owner(None):
            for key in config.recipient_groups:
                self.ensure("recipient_group", key, key)
            for key in config.all_senders():
                self.ensure("sender", key, "기본 발신 계정" if key == "default" else key)

    def get(self, kind, key):
        self._kind(kind)
        conn = self.db.get_connection()
        try:
            row = conn.execute("SELECT * FROM entity_catalog WHERE kind=? AND legacy_key=?", (kind, key)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list(self, kind, include_deleted=False, *, owner_user_id=_ALL_OWNERS):
        self._kind(kind)
        conn = self.db.get_connection()
        try:
            scope = "" if owner_user_id is _ALL_OWNERS else " AND owner_user_id IS ?"
            params = (kind,) if owner_user_id is _ALL_OWNERS else (kind, owner_user_id)
            return [dict(row) for row in conn.execute("""SELECT *,
                (SELECT display_name FROM auth_users u WHERE u.user_id=entity_catalog.owner_user_id) owner_name
                FROM entity_catalog WHERE kind=?""" + scope +
                ("" if include_deleted else " AND deleted_at IS NULL") + " ORDER BY entity_id", params)]
        finally:
            conn.close()

    def names(self, kind, *, owner_user_id=_ALL_OWNERS):
        return {row["legacy_key"]: row["display_name"] for row in self.list(kind, owner_user_id=owner_user_id)}

    def require_active(self, kind, key):
        row = self.get(kind, key)
        if not row or row["deleted_at"]:
            raise ValidationError("Selected item is missing or deleted; select an active catalog item")
        return row

    def rename(self, kind, key, display_name):
        self._kind(kind)
        display_name = self.validate_name(display_name)
        with self.db.transaction() as conn:
            self._require_row(conn, kind, key, active=True)
            now = _now()
            conn.execute("UPDATE entity_catalog SET display_name=?,updated_at=? WHERE kind=? AND legacy_key=?",
                         (display_name, now, kind, key))
            if kind == "task":
                # Existing editors compare this timestamp before publishing;
                # they must not silently undo a later cosmetic rename.
                conn.execute("UPDATE tasks SET updated_at=? WHERE task_id=?", (now, key))
            self._audit(conn, kind, key, "catalog_renamed")
        return self.get(kind, key)

    @staticmethod
    def _require_row(conn, kind, key, *, active=False):
        row = conn.execute("SELECT * FROM entity_catalog WHERE kind=? AND legacy_key=?", (kind, key)).fetchone()
        if not row:
            raise NotFoundError("Catalog item not found")
        if active and row["deleted_at"]:
            raise ValidationError("Catalog item is deleted; restore it first")
        return row

    @staticmethod
    def _references(definition, kind, key):
        delivery = definition.get("delivery") or {}
        if kind == "sender":
            return delivery.get("sender_profile_id", "default") == key
        alerting = definition.get("alerting") or {}
        return (key in (delivery.get("allowed_recipient_group_ids") or []) or
                (bool(alerting.get("events")) and alerting.get("recipient_group_id", "researchops-admins") == key))

    def _assert_unused(self, conn, kind, key):
        active = conn.execute("""SELECT t.task_id,v.definition_json FROM tasks t
            JOIN task_versions v ON v.version_hash=t.active_version_hash
            LEFT JOIN entity_catalog e ON e.kind='task' AND e.legacy_key=t.task_id
            WHERE e.deleted_at IS NULL""")
        for row in active:
            if self._references(json.loads(row["definition_json"]), kind, key):
                raise ValidationError(f"This item is used by Task '{row['task_id']}'; change its selection or delete the Task first")
        for row in conn.execute("""SELECT h.recipient_group_id,v.definition_json FROM delivery_handoffs h
            JOIN task_versions v ON v.version_hash=h.task_version_hash
            WHERE h.mode='handoff' AND h.status IN ('prepared','published','queued','uncertain')"""):
            if ((kind == "recipient_group" and row["recipient_group_id"] == key) or
                    (kind == "sender" and self._references(json.loads(row["definition_json"]), kind, key))):
                raise ValidationError("This item is used by a pending or uncertain email; reconcile the email before deletion")
        if kind == "sender":
            for row in conn.execute("SELECT envelope_json FROM smtp_attempts WHERE status IN ('queued','sending','uncertain')"):
                if json.loads(row["envelope_json"]).get("sender_profile_id", "default") == key:
                    raise ValidationError("This account has a pending or uncertain SMTP attempt; reconcile it before deletion")

    def delete(self, kind, key):
        self._kind(kind)
        if kind == "task":
            raise ValidationError("Delete Tasks through the Task lifecycle service")
        if (kind, key) in {("sender", "default"), ("recipient_group", "researchops-admins")}:
            raise ValidationError("The system default account/admin group can be renamed but cannot be deleted")
        with self.db.transaction() as conn:
            row = self._require_row(conn, kind, key)
            if row["deleted_at"]:
                return dict(row)
            self._assert_unused(conn, kind, key)
            now = _now()
            conn.execute("UPDATE entity_catalog SET deleted_at=?,updated_at=? WHERE kind=? AND legacy_key=?", (now, now, kind, key))
            self._audit(conn, kind, key, "catalog_deleted", {"recoverable": True})
        return self.get(kind, key)

    def restore(self, kind, key):
        self._kind(kind)
        if kind == "task":
            raise ValidationError("Restore Tasks through the Task lifecycle service")
        with self.db.transaction() as conn:
            row = self._require_row(conn, kind, key)
            if not row["deleted_at"]:
                return dict(row)
            conn.execute("UPDATE entity_catalog SET deleted_at=NULL,updated_at=? WHERE kind=? AND legacy_key=?", (_now(), kind, key))
            self._audit(conn, kind, key, "catalog_restored")
        return self.get(kind, key)
