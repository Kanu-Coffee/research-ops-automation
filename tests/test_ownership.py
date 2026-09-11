"""Owner migrations, account scopes and background identity use isolated data."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from researchops.delivery.smtp_config import BuiltinDeliveryConfig, SmtpSettings, delivery_revision
from researchops.domain.models import ScheduledRun
from researchops.errors import NotFoundError, ValidationError
from researchops.services.application import ApplicationService
from researchops.services.auth_service import AuthRequiredError, AuthorizationError, token_digest
from researchops.services.ownership import (creation_owner, entity_owner, installation_owner,
    require_task_delivery_ownership, scoped_delivery_config)
from researchops.storage.db import Database
from tests.support import isolated_settings

PASSWORD = "Synthetic ownership password"
PERMANENT = "Permanent ownership password"


class OwnershipMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "v2.db"
        import researchops.storage.migrations
        schema = Path(researchops.storage.migrations.__file__).with_name('schema.sql').read_text()
        schema = schema.replace("    owner_user_id TEXT REFERENCES auth_users(user_id),\n", "")
        schema = schema.replace("('admin','user','viewer')", "('admin','viewer')")
        schema = re.sub(r"CREATE TABLE IF NOT EXISTS auth_installation \(.*?\);", "", schema, flags=re.S)
        with sqlite3.connect(self.path) as conn:
            conn.executescript(schema)
            conn.executemany("INSERT INTO schema_migrations VALUES(?,?)", [(1, "old"), (2, "old")])
            for user, role in (("first-admin", "admin"), ("old-viewer", "viewer")):
                conn.execute("INSERT INTO auth_users VALUES(?,?,?,?,?,1,0,4,1000,1000)",
                    (user, user, user, "preserved-password-hash", role))
            conn.execute("INSERT INTO tasks(task_id,enabled,updated_at) VALUES('preserved-task',1,'old')")
            for kind, key in (("task", "preserved-task"), ("sender", "default"), ("recipient_group", "old-group")):
                conn.execute("INSERT INTO entity_catalog(kind,legacy_key,display_name,created_at,updated_at,request_key) VALUES(?,?,?,'old','old',?)",
                    (kind, key, key, "old-key-" + kind))
            conn.execute("INSERT INTO auth_user_tasks VALUES('old-viewer','preserved-task')")
            conn.execute("INSERT INTO auth_sessions VALUES(?,'old-viewer','authenticated',?,1000,1000,50000,4)",
                (token_digest("a" * 43), "b" * 43))
            conn.execute("""INSERT INTO task_drafts(draft_id,task_id,source_type,config_yaml,task_md,created_at,updated_at)
                VALUES('dormant-original','preserved-task','legacy','original-yaml','original-markdown','old','old')""")
        self.db = Database(self.path)

    def snapshot(self):
        with sqlite3.connect(self.path) as conn:
            tables = ("auth_users", "auth_sessions", "auth_user_tasks", "tasks", "task_drafts")
            return {table: conn.execute("SELECT * FROM " + table).fetchall() for table in tables}

    def test_v2_upgrade_preserves_accounts_sessions_grants_dormant_drafts_and_schedules(self):
        before = self.snapshot()
        self.db.init_schema()
        self.db.init_schema()
        self.assertEqual(self.snapshot(), before)
        with closing(self.db.get_connection()) as conn:
            self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 3)
            self.assertEqual(installation_owner(conn), "first-admin")
            self.assertEqual({row[0] for row in conn.execute("SELECT owner_user_id FROM entity_catalog")}, {"first-admin"})
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            conn.execute("INSERT INTO auth_users VALUES('new-user','new-user','User','hash','user',1,1,1,1001,1001)")
            conn.rollback()
        from researchops.services.catalog_service import CatalogService
        with creation_owner("first-admin"):
            replay = CatalogService(None, self.db).allocate("task", "preserved-task", request_key="old-key-task")
        self.assertEqual(replay["legacy_key"], "preserved-task")
        self.assertEqual(replay["request_key"], "old-key-task")

    def test_failed_migration_rolls_back_role_rebuild_owner_columns_and_metadata(self):
        before = self.snapshot()
        with patch("researchops.services.ownership.adopt_unowned", side_effect=RuntimeError("synthetic migration fault")):
            with self.assertRaisesRegex(RuntimeError, "synthetic migration fault"):
                self.db.init_schema()
        self.assertEqual(self.snapshot(), before)
        with closing(self.db.get_connection()) as conn:
            self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 2)
            self.assertNotIn("owner_user_id", {row[1] for row in conn.execute("PRAGMA table_info(entity_catalog)")})
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO auth_users VALUES('new-user','new-user','User','hash','user',1,1,1,1001,1001)")


class OwnershipServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = ApplicationService(isolated_settings(Path(self.tmp.name)))
        self.assertIsNone(installation_owner(self.app.db))
        self.admin = self.app.auth.setup(self.app.auth.issue_setup_token(), "owner-admin", PASSWORD).session.principal

    def user(self, name="owner-user", role="user", grants=()):
        record = self.app.auth.create_user(self.admin, name, name, role, PASSWORD, grants)
        initial = self.app.auth.login(name, PASSWORD)
        return record, self.app.auth.change_password(initial.session.principal, PASSWORD, PERMANENT)

    def task(self, owner):
        with creation_owner(owner.user_id):
            task = self.app.catalog.allocate("task", "Owned Task")
            sender = self.app.catalog.allocate("sender", "Owned Sender")
            group = self.app.catalog.allocate("recipient_group", "Owned Group")
        task_id = task["legacy_key"]
        definition = {"id": task_id, "delivery": {"sender_profile_id": sender["legacy_key"],
            "allowed_recipient_group_ids": [group["legacy_key"]]}}
        with self.app.db.transaction() as conn:
            conn.execute("INSERT INTO tasks(task_id,enabled,updated_at) VALUES(?,1,'2026-09-11')", (task_id,))
            conn.execute("INSERT INTO task_versions(version_hash,task_id,definition_json,package_files_json,sealed_at) VALUES(?,?,?,'{}','old')",
                (task_id + "-version", task_id, json.dumps(definition)))
            run = ScheduledRun(task_id + "-run", task_id, task_id + "-version", "2026-09-11T00:00:00Z",
                "Asia/Seoul", "2026-09-11", "2026.09.11", "schedule")
            columns = run.to_dict()
            conn.execute("INSERT INTO scheduled_runs(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", tuple(columns.values()))
        return task_id, sender["legacy_key"], group["legacy_key"], run.run_id, definition

    def test_first_admin_adopts_bootstrap_host_creation_and_existing_owner_never_moves(self):
        self.assertEqual(installation_owner(self.app.db), self.admin.user_id)
        self.assertEqual(entity_owner(self.app.db, "sender", "default"), self.admin.user_id)
        _, user = self.user()
        with creation_owner(user.session.principal.user_id):
            entry = self.app.catalog.allocate("sender", "User Sender")
            self.app.catalog.bootstrap_delivery(BuiltinDeliveryConfig(sender_profiles={"host-import": SmtpSettings()}))
        self.assertEqual(entity_owner(self.app.db, "sender", "host-import"), self.admin.user_id)
        self.app.catalog.ensure("sender", entry["legacy_key"], "Ignored host label")
        self.assertEqual(entity_owner(self.app.db, "sender", entry["legacy_key"]), user.session.principal.user_id)
        with self.assertRaises(sqlite3.IntegrityError), self.app.db.transaction() as conn:
            conn.execute("UPDATE entity_catalog SET owner_user_id=? WHERE kind='sender' AND legacy_key=?",
                (self.admin.user_id, entry["legacy_key"]))

    def test_creation_request_keys_are_owner_scoped_and_context_is_thread_local(self):
        _, user = self.user()
        owners = (self.admin.user_id, user.session.principal.user_id)
        barrier = threading.Barrier(2)
        def create(owner):
            with creation_owner(owner):
                barrier.wait()
                result = self.app.catalog.allocate("recipient_group", "Same name", request_key="same-browser-key")
                repeated = self.app.catalog.allocate("recipient_group", "Same name", request_key="same-browser-key")
                self.assertEqual(result["entity_id"], repeated["entity_id"])
                return result
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, owners))
        self.assertNotEqual(results[0]["entity_id"], results[1]["entity_id"])
        self.assertEqual([row["owner_user_id"] for row in results], list(owners))
        self.assertEqual(self.app.catalog.allocate("sender", "Host afterwards")["owner_user_id"], self.admin.user_id)

    def test_user_owns_tasks_runs_and_delivery_resources_while_viewer_keeps_explicit_grants(self):
        _, user = self.user()
        principal = user.session.principal
        own = self.task(principal)
        other = self.task(self.admin)
        _, viewer = self.user("read-only", "viewer", [own[0]])
        self.assertEqual(self.app.auth.require_task(principal, own[0], manage=True), own[0])
        self.assertEqual(self.app.auth.require_run(principal, own[3], manage=True), own[0])
        self.assertEqual(self.app.auth.require_task(self.admin, own[0], manage=True), own[0])
        self.assertEqual(self.app.auth.require_task(viewer.session.principal, own[0]), own[0])
        with self.assertRaises(AuthorizationError):
            self.app.auth.require_task(viewer.session.principal, own[0], manage=True)
        for method, key in ((self.app.auth.require_task, other[0]), (self.app.auth.require_run, other[3]),
                            (self.app.auth.require_sender, other[1]), (self.app.auth.require_group, other[2])):
            with self.subTest(key=key), self.assertRaises(NotFoundError):
                method(principal, key)
        with self.app.db.transaction() as conn:
            conn.execute("INSERT INTO auth_user_tasks VALUES(?,?)", (principal.user_id, other[0]))
        self.assertEqual(self.app.auth.allowed_task_ids(principal), [own[0]])
        user_record = next(row for row in self.app.auth.list_users(self.admin) if row["user_id"] == principal.user_id)
        self.assertEqual((user_record["owned_task_count"], user_record["owned_sender_count"], user_record["owned_recipient_group_count"]), (1, 1, 1))

    def test_delivery_references_and_revisions_use_task_owner_not_requester(self):
        _, user = self.user()
        own = self.task(user.session.principal)
        admin_task = self.task(self.admin)
        self.assertEqual(require_task_delivery_ownership(self.app.db, own[4]), user.session.principal.user_id)
        wrong = {**own[4], "delivery": {**own[4]["delivery"], "sender_profile_id": admin_task[1]}}
        with self.assertRaises(ValidationError):
            require_task_delivery_ownership(self.app.db, wrong)
        wrong = {**own[4], "delivery": {**own[4]["delivery"], "allowed_recipient_group_ids": [admin_task[2]]}}
        with self.assertRaises(ValidationError):
            require_task_delivery_ownership(self.app.db, wrong)
        config = BuiltinDeliveryConfig(recipient_groups={admin_task[2]: ["admin@example.test"]})
        original_revision = delivery_revision(config)
        config.recipient_groups[own[2]] = ["user@example.test"]
        filtered = scoped_delivery_config(self.app.db, config, self.admin.user_id)
        self.assertEqual(delivery_revision(filtered), original_revision)
        self.assertEqual(set(config.recipient_groups), {own[2], admin_task[2]})
        self.assertEqual(set(scoped_delivery_config(self.app.db, config, user.session.principal.user_id).recipient_groups), {own[2]})

    def test_disable_revokes_web_session_without_stopping_owned_automation(self):
        user_record, user = self.user()
        own = self.task(user.session.principal)
        self.app.auth.update_user(self.admin, user_record["user_id"], active=False)
        self.assertIsNone(self.app.auth.resolve_session(user.token))
        with self.assertRaises(AuthRequiredError):
            self.app.auth.require_task(user.session.principal, own[0])
        with closing(self.app.db.get_connection()) as conn:
            self.assertEqual(conn.execute("SELECT enabled FROM tasks WHERE task_id=?", (own[0],)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT status FROM scheduled_runs WHERE run_id=?", (own[3],)).fetchone()[0], "queued")
        self.assertEqual(require_task_delivery_ownership(self.app.db, own[4]), user_record["user_id"])
        config = BuiltinDeliveryConfig(recipient_groups={own[2]: ["continuing@example.test"]})
        self.assertIn(own[2], scoped_delivery_config(self.app.db, config, user_record["user_id"]).recipient_groups)


if __name__ == "__main__":
    unittest.main()
