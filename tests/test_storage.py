"""Unit tests for researchops storage layer."""

from datetime import datetime, timezone, timedelta
import shutil
import tempfile
import unittest
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from researchops.domain.events import AuditEvent
from researchops.domain.models import (
    DeliveryHandoff, DeliveryReceipt, ReportedItem, ScheduledRun, TaskDefinition,
    TaskVersion, ResearchResult, CompositionInput, CompositionResult, RunLease
)
from researchops.storage.db import Database
from researchops.errors import ValidationError, ConcurrencyError, ResearchOpsError
from researchops.storage.repositories import (
    DeliveryRepository, RunRepository, StateRepository, TaskRepository
)


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test.db"
        self.db = Database(self.db_path)
        self.db.init_schema()
        self.task_repo = TaskRepository(self.db)
        self.run_repo = RunRepository(self.db)
        self.delivery_repo = DeliveryRepository(self.db)
        self.state_repo = StateRepository(self.db)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _sample_task_def(self, task_id="test-task"):
        return TaskDefinition(
            id=task_id,
            name="Test Task",
            enabled=False,
            workspace={"mode": "persistent_task"},
            runner={"type": "fake"},
            instructions={"research_files": ["task.md"], "compose_files": ["task.md"]},
            output={"research_schema": "output.schema.json"},
            delivery={"mode": "dry_run"}
        )

    def test_database_initialization(self):
        conn = self.db.get_connection()
        try:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(journal_mode.lower(), "wal")
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            self.assertIn("tasks", tables)
            self.assertIn("task_versions", tables)
            self.assertIn("scheduled_runs", tables)
            self.assertIn("delivery_handoffs", tables)
            self.assertIn("delivery_receipts", tables)
            self.assertIn("reported_items", tables)
            self.assertEqual([r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")],[1,2,3])
            self.assertTrue(conn.execute("PRAGMA foreign_key_list(scheduled_runs)").fetchall())
            self.assertEqual(self.db_path.stat().st_mode & 0o777,0o600)
        finally:
            conn.close()

    def _register(self):
        version=TaskVersion("test-task","hash123","2026-09-04T00:00:00+00:00",self._sample_task_def(),{"task.md":"original"})
        self.task_repo.save_version(version)
        self.task_repo.set_active_version(version.task_id,version.version_hash)
        return version

    def _run(self,run_id="run-a",trigger="manual"):
        run=ScheduledRun(run_id,"test-task","hash123","2026-09-04T15:01:00+00:00","Asia/Seoul","2026-09-05","2026.09.05",trigger)
        self.run_repo.create_run(run)
        return run

    def test_sealed_version_cannot_be_overwritten(self):
        version=self._register()
        self.task_repo.save_version(version)
        with self.assertRaises(ValidationError):
            self.task_repo.save_version(replace(version,package_files={"task.md":"changed"}))
        self.assertEqual(self.task_repo.get_version(version.version_hash).package_files,{"task.md":"original"})

    def test_connection_blocks_offline_restore_until_closed(self):
        from researchops.operations import runtime_guard
        connection=self.db.get_connection()
        try:
            with self.assertRaises(ResearchOpsError):
                with runtime_guard(self.db_path,exclusive=True):
                    pass
        finally:
            connection.close()
        with runtime_guard(self.db_path,exclusive=True):
            with self.assertRaises(ResearchOpsError):
                self.db.get_connection()
        connection=self.db.get_connection()
        connection.close()

    def test_atomic_claim_fences_other_run_and_stale_writes(self):
        self._register()
        self._run("run-a")
        self._run("run-b")
        barrier=threading.Barrier(2)
        def claim(worker):
            barrier.wait()
            return self.run_repo.claim_next_run(worker)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims=list(pool.map(claim,["worker-a","worker-b"]))
        claimed=[claim for claim in claims if claim]
        self.assertEqual(len(claimed),1)
        run,lease=claimed[0]
        with self.assertRaises(ConcurrencyError):
            self.run_repo.update_run_status(run.run_id,"succeeded","finalize",fencing_token="wrong")
        with self.assertRaises(ConcurrencyError):
            self.run_repo.release_lease(run.run_id,lease.fencing_token)
        self.run_repo.request_cancel(run.run_id)
        self.assertEqual(self.run_repo.get_run(run.run_id).status,"running")
        with self.assertRaises(ConcurrencyError):
            self.run_repo.assert_run_owner(run.run_id,lease.fencing_token)
        self.run_repo.update_run_status(run.run_id,"cancelled","finalize",fencing_token=lease.fencing_token)
        self.run_repo.mark_cleanup_verified(run.run_id,lease.fencing_token)
        self.run_repo.release_lease(run.run_id,lease.fencing_token)
        self.assertIsNotNone(self.run_repo.claim_next_run("next-worker"))

    def test_foreign_keys_and_schedule_occurrence_uniqueness(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._run()
        self._register()
        self._run("run-a","schedule")
        with self.assertRaises(sqlite3.IntegrityError):
            self._run("run-b","schedule")
        self._run("run-retry","retry")

    def test_migration_preserves_conflicting_legacy_rows(self):
        path=Path(self.temp_dir)/"legacy.db"
        conn=sqlite3.connect(path)
        conn.executescript("""CREATE TABLE tasks(task_id TEXT PRIMARY KEY,active_version_hash TEXT,enabled INTEGER,delivery_mode TEXT,delivery_approved INTEGER,updated_at TEXT);
            CREATE TABLE task_versions(version_hash TEXT PRIMARY KEY,task_id TEXT,definition_json TEXT,package_files_json TEXT,sealed_at TEXT,is_active INTEGER);""")
        conn.execute("INSERT INTO tasks VALUES('test-task','hash123',1,'handoff',1,'2026-09-01T00:00:00Z')")
        conn.execute("INSERT INTO task_versions VALUES(?,?,?,?,?,?)",("hash123","test-task",json.dumps(self._sample_task_def().to_dict()),"{}","2026-09-01T00:00:00Z",1))
        first=ScheduledRun("legacy-a","test-task","hash123","2026-09-04T09:00:00+09:00","Asia/Seoul","2026-09-04","2026.09.04","schedule",status="running")
        data=first.to_dict()
        conn.execute("CREATE TABLE scheduled_runs("+",".join(k+" TEXT" for k in data)+")")
        for record in (first,replace(first,run_id="legacy-b",scheduled_for="2026-09-04T00:00:00Z"),replace(first,run_id="legacy-orphan",task_id="missing")):
            conn.execute("INSERT INTO scheduled_runs VALUES("+",".join("?" for _ in data)+")",tuple(record.to_dict().values()))
        conn.commit()
        conn.close()
        db=Database(path)
        db.init_schema()
        db.init_schema()
        conn=db.get_connection()
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM legacy_v1_scheduled_runs").fetchone()[0],3)
            self.assertEqual(conn.execute("SELECT count(*) FROM migration_quarantine").fetchone()[0],2)
            self.assertEqual(conn.execute("SELECT count(*) FROM scheduled_runs").fetchone()[0],1)
            task=conn.execute("SELECT * FROM tasks").fetchone()
            self.assertEqual((task["enabled"],task["delivery_approved"],task["active_version_hash"]),(0,0,None))
            self.assertEqual(conn.execute("SELECT status FROM scheduled_runs").fetchone()[0],"needs_attention")
            self.assertEqual(conn.execute("SELECT count(*) FROM task_claims").fetchone()[0],1)
            self.assertEqual(list(conn.execute("PRAGMA foreign_key_check")),[])
        finally:
            conn.close()

    def test_task_and_version_lifecycle(self):
        task_def = self._sample_task_def()
        version = TaskVersion(
            task_id="test-task",
            version_hash="hash123",
            sealed_at="2026-09-04T00:00:00Z",
            definition=task_def,
            package_files={"task.yaml": "version: 2"}
        )
        self.task_repo.save_version(version)

        self.task_repo.set_active_version("test-task", "hash123")
        active_ver = self.task_repo.get_active_version("test-task")
        self.assertIsNotNone(active_ver)
        self.assertEqual(active_ver.version_hash, "hash123")
        self.assertEqual(active_ver.definition.id, "test-task")

        status = self.task_repo.get_task_status("test-task")
        self.assertIsNotNone(status)
        self.assertEqual(status["active_version_hash"], "hash123")
        self.assertEqual(status["enabled"], 0)

        self.task_repo.set_task_enabled("test-task", True)
        status = self.task_repo.get_task_status("test-task")
        self.assertEqual(status["enabled"], 1)

    def test_run_lifecycle_and_lease(self):
        task_def = self._sample_task_def()
        version = TaskVersion(
            task_id="test-task",
            version_hash="hash123",
            sealed_at="2026-09-04T00:00:00Z",
            definition=task_def,
            package_files={"task.yaml": "version: 2"}
        )
        self.task_repo.save_version(version)
        self.task_repo.set_active_version("test-task", "hash123")

        now = datetime.now(timezone.utc)
        run = ScheduledRun(
            run_id="run-100",
            task_id="test-task",
            task_version_hash="hash123",
            status="queued",
            phase="queued",
            local_date="2026-09-04",
            local_date_display="2026.09.04",
            timezone="Asia/Seoul",
            scheduled_for="2026-09-04T09:00:00Z",
            trigger_type="manual",
            created_at=now.isoformat(),
            attempt=1
        )
        self.run_repo.create_run(run)

        fetched = self.run_repo.get_run("run-100")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.status, "queued")

        # Acquire lease
        lease1 = RunLease(
            run_id="run-100",
            worker_id="worker-1",
            attempt=1,
            fencing_token="token-1",
            claimed_at=now.isoformat(),
            heartbeat_at=now.isoformat(),
            lease_expires_at=(now + timedelta(seconds=60)).isoformat()
        )
        self.assertTrue(self.run_repo.acquire_lease(lease1))

        # Re-acquiring with different fencing token before expiry should fail
        lease2 = RunLease(
            run_id="run-100",
            worker_id="worker-2",
            attempt=1,
            fencing_token="token-2",
            claimed_at=now.isoformat(),
            heartbeat_at=now.isoformat(),
            lease_expires_at=(now + timedelta(seconds=60)).isoformat()
        )
        self.assertFalse(self.run_repo.acquire_lease(lease2))

        # Release lease
        self.run_repo.release_lease("run-100", "token-1")
        # Now lease2 should succeed
        self.assertTrue(self.run_repo.acquire_lease(lease2))

    def test_reported_items_dedupe(self):
        task_def = self._sample_task_def()
        version = TaskVersion(
            task_id="test-task",
            version_hash="hash123",
            sealed_at="2026-09-04T00:00:00Z",
            definition=task_def,
            package_files={"task.yaml": "version: 2"}
        )
        self.task_repo.save_version(version)
        self.task_repo.set_active_version("test-task", "hash123")

        run = ScheduledRun(
            run_id="run-100",
            task_id="test-task",
            task_version_hash="hash123",
            scheduled_for="2026-09-04T09:00:00Z",
            timezone="Asia/Seoul",
            local_date="2026-09-04",
            local_date_display="2026.09.04",
            trigger_type="manual"
        )
        self.run_repo.create_run(run)

        handoff = DeliveryHandoff(
            handoff_id="handoff-1",
            idempotency_key="ro-idem-1",
            run_id="run-100",
            task_id="test-task",
            task_version_hash="hash123",
            message_revision=1,
            message_type="market_digest",
            recipient_group_id="team-a",
            mode="handoff",
            status="smtp_accepted",
            receipt_trust_status="verified_local_smtp",
            delivery_request={"msg": "test"},
            delivery_request_sha256="sha_dummy"
        )
        self.delivery_repo.save_handoff(handoff)

        item = ReportedItem(
            task_id="test-task",
            entity_key="issuer:product",
            content_fingerprint="contenthash999",
            run_id="run-100",
            handoff_id="handoff-1",
            reported_at="2026-09-04T09:00:00Z"
        )
        self.state_repo.save_reported_item(item)

        self.assertTrue(
            self.state_repo.is_reported_item_unchanged("test-task", "issuer:product", "contenthash999")
        )
        self.assertFalse(
            self.state_repo.is_reported_item_unchanged("test-task", "issuer:product", "differenthash")
        )
        self.assertFalse(
            self.state_repo.is_reported_item_unchanged("test-task", "other:product", "contenthash999")
        )


if __name__ == "__main__":
    unittest.main()
