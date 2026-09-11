"""Operator identities/labels and reversible delivery catalogs, no live effects."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from researchops.delivery.queue import SmtpQueue
from researchops.delivery.smtp_config import BuiltinDeliveryConfig, SmtpSettings, delivery_revision, load_delivery_config
from researchops.errors import ValidationError
from researchops.services.application import ApplicationService
from researchops.services.catalog_service import CatalogService
from tests.delivery_fixtures import DeliveryFixture
from tests.package_support import register_template
from tests.support import isolated_settings, register_fixture_task


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = isolated_settings(Path(self.tmp.name))
        self.app = ApplicationService(self.settings)
        self.catalog = self.app.catalog
        self.app.delivery.save_delivery_config(BuiltinDeliveryConfig(
            smtp=SmtpSettings(username="primary@example.test", password="catalog-synthetic-secret"),
            recipient_groups={"legacy-group": ["reader@example.test"], "researchops-admins": []},
            sender_profiles={"legacy-sender": SmtpSettings(username="other@example.test", password="other-synthetic-secret")}))

    def test_bootstrap_preserves_config_secrets_labels_deletion_and_numeric_identity(self):
        before = self.settings.paths.delivery_config_file.read_bytes()
        row = self.catalog.get("recipient_group", "legacy-group")
        self.catalog.rename("recipient_group", "legacy-group", "한글 그룹 이름")
        self.catalog.delete("recipient_group", "legacy-group")
        for _ in range(2):
            self.catalog.bootstrap()
        after = self.catalog.get("recipient_group", "legacy-group")
        self.assertEqual(row["entity_id"], after["entity_id"])
        self.assertEqual(after["display_name"], "한글 그룹 이름")
        self.assertTrue(after["deleted_at"])
        self.assertEqual(before, self.settings.paths.delivery_config_file.read_bytes())
        conn = self.app.db.get_connection()
        try:
            text = repr([dict(row) for row in conn.execute("SELECT * FROM entity_catalog")])
        finally:
            conn.close()
        self.assertNotIn("example.test", text)
        self.assertNotIn("synthetic-secret", text)

    def test_bootstrap_uses_active_name_and_unpublished_identity_without_draft_reads(self):
        version = register_fixture_task(self.app)
        register_template(self.app, "unpublished")
        with self.app.db.transaction() as conn:
            conn.execute("DELETE FROM entity_catalog WHERE kind='task'")
        self.catalog.bootstrap()
        self.assertEqual(self.catalog.get("task", version.task_id)["display_name"], version.definition.name)
        self.assertEqual(self.catalog.get("task", "unpublished")["display_name"], "unpublished")

    def test_numbers_do_not_reuse_deleted_identity_and_retry_is_idempotent(self):
        first = self.catalog.allocate("recipient_group", "그룹", request_key="_-retry")
        retry = self.catalog.allocate("recipient_group", "그룹", request_key="_-retry")
        self.assertEqual(first, retry)
        self.catalog.delete("recipient_group", first["legacy_key"])
        second = self.catalog.allocate("recipient_group", "그룹")
        self.assertGreater(second["entity_id"], first["entity_id"])
        self.assertEqual(second["legacy_key"], f"group-{second['entity_id']}")
        with self.assertRaises(ValidationError):
            self.catalog.allocate("recipient_group", "그룹", request_key="_-retry")

    def test_legacy_generated_token_collision_is_skipped(self):
        conn = self.app.db.get_connection()
        try:
            number = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='entity_catalog'").fetchone()[0] + 2
        finally:
            conn.close()
        self.catalog.ensure("sender", f"sender-{number}", "legacy collision")
        allocated = self.catalog.allocate("sender", "새 계정")
        self.assertGreater(allocated["entity_id"], number)
        self.assertNotEqual(allocated["legacy_key"], f"sender-{number}")

    def test_rename_and_soft_delete_keep_delivery_revision_and_identity(self):
        config = load_delivery_config(self.settings.paths.delivery_config_file)
        before = self.settings.paths.delivery_config_file.read_bytes()
        revisions = {key: delivery_revision(config, key) for key in config.all_senders()}
        sender = self.catalog.get("sender", "legacy-sender")
        self.app.delivery.rename_sender_account("legacy-sender", "사업부 발신 계정")
        self.app.delivery.rename_recipient_group("legacy-group", "새 수신인 명칭")
        self.app.delivery.delete_sender_account("legacy-sender")
        self.app.delivery.delete_recipient_group("legacy-group")
        self.assertNotIn("legacy-sender", self.catalog.names("sender"))
        self.assertNotIn("legacy-group", self.catalog.names("recipient_group"))
        for kind, key in (("sender", "legacy-sender"), ("recipient_group", "legacy-group")):
            with self.assertRaises(ValidationError):
                self.catalog.require_active(kind, key)
            self.catalog.restore(kind, key)
            self.catalog.require_active(kind, key)
        self.assertEqual(sender["entity_id"], self.catalog.get("sender", "legacy-sender")["entity_id"])
        self.assertEqual(before, self.settings.paths.delivery_config_file.read_bytes())
        config = load_delivery_config(self.settings.paths.delivery_config_file)
        self.assertEqual(revisions, {key: delivery_revision(config, key) for key in config.all_senders()})

    def test_created_accounts_and_groups_use_numbers_not_user_names(self):
        sender = self.app.delivery.create_sender_account("보고용 Gmail 계정", SmtpSettings(
            username="new@example.test", password="new-synthetic-secret"), request_key="new-sender")
        group = self.app.delivery.create_recipient_group("임원 보고 그룹", ["executive@example.test"], request_key="new-group")
        self.assertRegex(sender, r"^sender-[0-9]+$")
        self.assertRegex(group, r"^group-[0-9]+$")
        self.assertEqual(self.catalog.names("sender")[sender], "보고용 Gmail 계정")
        self.assertEqual(self.catalog.names("recipient_group")[group], "임원 보고 그룹")
        before = self.settings.paths.delivery_config_file.read_bytes()
        self.assertEqual(sender, self.app.delivery.create_sender_account("보고용 Gmail 계정", SmtpSettings(
            username="new@example.test", password="new-synthetic-secret"), request_key="new-sender"))
        self.assertEqual(group, self.app.delivery.create_recipient_group("임원 보고 그룹", ["executive@example.test"], request_key="new-group"))
        with self.assertRaises(ValidationError):
            self.app.delivery.create_sender_account("보고용 Gmail 계정", SmtpSettings(), request_key="new-sender")
        with self.assertRaises(ValidationError):
            self.app.delivery.create_recipient_group("임원 보고 그룹", [], request_key="new-group")
        with self.assertRaises(ValidationError):
            self.app.delivery.create_recipient_group("다른 그룹명", ["executive@example.test"], request_key="new-group")
        self.assertEqual(before, self.settings.paths.delivery_config_file.read_bytes())
        saved = load_delivery_config(self.settings.paths.delivery_config_file)
        self.assertEqual(saved.get_sender(sender).password, "new-synthetic-secret")
        self.assertEqual(saved.smtp.password, "catalog-synthetic-secret")

    def test_deleted_item_cannot_be_updated_but_can_be_restored(self):
        self.app.delivery.delete_sender_account("legacy-sender")
        self.app.delivery.delete_recipient_group("legacy-group")
        before = self.settings.paths.delivery_config_file.read_bytes()
        with self.assertRaises(ValidationError):
            self.app.delivery.save_sender_profile("legacy-sender", SmtpSettings())
        with self.assertRaises(ValidationError):
            self.app.delivery.save_recipient_group("legacy-group", [])
        self.assertFalse(self.app.delivery.test_smtp_connection(sender_profile_id="legacy-sender")[0])
        self.assertFalse(self.app.delivery.send_test_email("operator@example.test", sender_profile_id="legacy-sender")[0])
        self.assertEqual(before, self.settings.paths.delivery_config_file.read_bytes())
        self.assertEqual(self.app.delivery.operating_status()["nonempty_group_count"], 0)

    def test_concurrent_creations_do_not_overwrite_other_groups_or_duplicate_retry(self):
        second = ApplicationService(self.settings)
        def create(number):
            service = second.delivery if number % 2 else self.app.delivery
            return service.create_recipient_group(f"Group {number}", [f"user{number}@example.test"], request_key=f"group-{number}")
        with ThreadPoolExecutor(max_workers=4) as pool:
            keys = list(pool.map(create, [1, 2, 3, 1]))
        self.assertEqual(keys[0], keys[3])
        self.assertEqual(len(set(keys)), 3)
        config = load_delivery_config(self.settings.paths.delivery_config_file)
        for number, key in zip([1, 2, 3], keys):
            self.assertEqual(config.recipient_groups[key], [f"user{number}@example.test"])
        self.assertEqual(config.recipient_groups["legacy-group"], ["reader@example.test"])

    def test_reserved_entities_are_renameable_but_not_deletable(self):
        for kind, key in (("sender", "default"), ("recipient_group", "researchops-admins")):
            self.catalog.rename(kind, key, "변경 가능한 이름")
            with self.assertRaisesRegex(ValidationError, "system default"):
                self.catalog.delete(kind, key)
        with self.assertRaises(ValidationError):
            self.catalog.delete("task", "unknown")

    def test_task_rename_invalidates_stale_editor_without_rewriting_version(self):
        version = register_fixture_task(self.app)
        self.catalog.ensure("task", version.task_id, version.definition.name)
        before = self.app.task_repo.get_task_status(version.task_id)
        self.catalog.rename("task", version.task_id, "새 업무명")
        after = self.app.task_repo.get_task_status(version.task_id)
        self.assertNotEqual(before["updated_at"], after["updated_at"])
        self.assertEqual(before["active_version_hash"], after["active_version_hash"])
        self.assertEqual(version.definition.name, self.app.task_repo.get_active_version(version.task_id).definition.name)

    def test_active_task_reference_blocks_deletion_but_archived_task_does_not(self):
        version = register_fixture_task(self.app)
        definition = replace(version.definition, delivery={**version.definition.delivery,
            "allowed_recipient_group_ids": ["legacy-group"], "sender_profile_id": "legacy-sender"})
        updated = replace(version, definition=definition, version_hash="e" * 64)
        self.app.task_repo.save_version(updated)
        self.app.task_repo.set_active_version(version.task_id, updated.version_hash)
        self.catalog.ensure("task", version.task_id, definition.name)
        for kind, key in (("sender", "legacy-sender"), ("recipient_group", "legacy-group")):
            with self.assertRaisesRegex(ValidationError, "used by Task"):
                self.catalog.delete(kind, key)
        with self.app.db.transaction() as conn:
            conn.execute("UPDATE entity_catalog SET deleted_at='archived' WHERE kind='task' AND legacy_key=?", (version.task_id,))
        self.catalog.delete("sender", "legacy-sender")
        self.catalog.delete("recipient_group", "legacy-group")

    def test_dormant_legacy_draft_does_not_block_delivery_catalog_deletion(self):
        register_template(self.app, "legacy-unpublished")
        metadata = ('draft-legacy', 'legacy-unpublished', 'template',
                    'delivery: {sender_profile_id: legacy-sender, allowed_recipient_group_ids: [legacy-group]}',
                    'Preserved legacy document', '2026-09-01', '2026-09-01')
        with self.app.db.transaction() as conn:
            conn.execute("""INSERT INTO task_drafts(draft_id,task_id,source_type,config_yaml,task_md,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)""", metadata)
        for kind, key in (("sender", "legacy-sender"), ("recipient_group", "legacy-group")):
            self.catalog.delete(kind, key)
        with self.app.db.get_connection() as conn:
            saved = conn.execute("SELECT draft_id,task_id,source_type,config_yaml,task_md,created_at,updated_at FROM task_drafts").fetchone()
        self.assertEqual(tuple(saved), metadata)

    def test_pending_and_uncertain_sender_attempts_block_deletion_without_task(self):
        queue = SmtpQueue(self.app.db)
        queue.enqueue("pending-synthetic", None, "pending@example.test", b"synthetic", {"sender_profile_id": "legacy-sender"}, "revision")
        with self.assertRaisesRegex(ValidationError, "SMTP attempt"):
            self.catalog.delete("sender", "legacy-sender")
        with self.app.db.transaction() as conn:
            conn.execute("UPDATE smtp_attempts SET status='uncertain' WHERE job_id='pending-synthetic'")
        with self.assertRaisesRegex(ValidationError, "SMTP attempt"):
            self.catalog.delete("sender", "legacy-sender")

    def test_invalid_labels_do_not_allocate_or_modify_anything(self):
        before = self.catalog.list("sender", include_deleted=True)
        for name in ("", "  ", "x\nname", "x\x00name", "x" * 201, None):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.catalog.allocate("sender", name)
        self.assertEqual(before, self.catalog.list("sender", include_deleted=True))


class CatalogPendingDeliveryTests(DeliveryFixture, unittest.TestCase):
    def test_pending_real_handoff_blocks_group_deletion_even_after_task_archived(self):
        catalog = CatalogService(self.settings, self.db)
        catalog.bootstrap()
        handoff = self.publish()
        with self.db.transaction() as conn:
            conn.execute("UPDATE entity_catalog SET deleted_at='archived' WHERE kind='task' AND legacy_key=?", (self.task.id,))
        with self.assertRaisesRegex(ValidationError, "pending or uncertain"):
            catalog.delete("recipient_group", handoff.recipient_group_id)


if __name__ == "__main__":
    unittest.main()
