"""Unit tests for Orchestrator end-to-end workflow execution."""

from datetime import datetime, timezone
import hashlib
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from zoneinfo import ZoneInfo

from researchops.config import Settings, PathsConfig, DeliveryConfig
from researchops.domain.models import TaskDefinition, TaskVersion, ScheduledRun
from researchops.engine.orchestrator import Orchestrator
from researchops.runners.fake import FakeRunner
from researchops.storage.db import Database
from researchops.storage.repositories import (
    DeliveryRepository, RunRepository, StateRepository, TaskRepository
)
from researchops.workspace.manager import WorkspaceManager
from researchops.errors import ConcurrencyError, HardGateError
from researchops.engine.archive import RunArchive
from tests.support import SOURCE_ROOT


class TestOrchestrator(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.archive_dir = Path(self.temp_dir) / "run-archive"
        self.ws_dir = Path(self.temp_dir) / "workspaces"
        self.outbox_dir = Path(self.temp_dir) / "outbox"
        self.receipts_dir = Path(self.temp_dir) / "receipts"
        for d in (self.archive_dir, self.ws_dir, self.outbox_dir, self.receipts_dir):
            d.mkdir(parents=True)

        paths = PathsConfig(
            repo_root=Path(self.temp_dir),
            tasks_dir=Path(self.temp_dir) / "tasks",
            data_dir=Path(self.temp_dir) / "var",
            task_workspaces_dir=self.ws_dir,
            run_archive_dir=self.archive_dir,
            delivery_outbox_dir=self.outbox_dir,
            receipts_dir=self.receipts_dir,
            task_drafts_dir=Path(self.temp_dir) / "task-drafts",
            task_versions_dir=Path(self.temp_dir) / "task-versions",
            database=Path(self.temp_dir) / "test.db",
            schemas_dir=SOURCE_ROOT / "schemas"
        )
        self.settings = Settings(paths=paths, delivery=DeliveryConfig(global_handoff_kill_switch=False))
        self.db = Database(paths.database)
        self.db.init_schema()

        self.task_repo = TaskRepository(self.db)
        self.run_repo = RunRepository(self.db)
        self.delivery_repo = DeliveryRepository(self.db)
        self.state_repo = StateRepository(self.db)
        self.ws_mgr = WorkspaceManager(self.settings)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _setup_task(self, send_on_empty=True):
        task_def = TaskDefinition(
            id="market-task",
            name="Market Monitoring",
            enabled=True,
            workspace={"mode": "persistent_task"},
            runner={"type": "fake"},
            instructions={"research_files": ["task.md"], "compose_files": ["task.md"]},
            output={"research_schema": "output.schema.json"},
            delivery={
                "mode": "dry_run",
                "send_on_empty": send_on_empty,
                "allowed_recipient_group_ids": ["release-team"]
            },
            state={"dedupe": {"enabled": False}}
        )
        ver_hash = "b" * 64
        version = TaskVersion(
            task_id="market-task",
            version_hash=ver_hash,
            sealed_at="2026-09-04T00:00:00Z",
            definition=task_def,
            package_files={"task.yaml": "version: 2", "task.md": "Research and compose the complete records."}
        )
        self.task_repo.save_version(version)
        self.task_repo.set_active_version("market-task", ver_hash)

        now = datetime.now(timezone.utc)
        run = ScheduledRun(
            run_id="run-orch-001",
            task_id="market-task",
            task_version_hash=ver_hash,
            scheduled_for=now.isoformat(),
            timezone="Asia/Seoul",
            local_date=now.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat(),
            local_date_display=now.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y.%m.%d"),
            trigger_type="manual"
        )
        self.run_repo.create_run(run)
        return task_def, version, run

    def test_full_successful_dry_run_and_archive_manifests(self):
        task_def, version, run = self._setup_task(send_on_empty=True)
        fixtures_dir = SOURCE_ROOT / "tasks/software-releases/fixtures"
        runner = FakeRunner(fixtures_dir=fixtures_dir)

        orchestrator = Orchestrator(
            settings=self.settings,
            task_repo=self.task_repo,
            run_repo=self.run_repo,
            delivery_repo=self.delivery_repo,
            state_repo=self.state_repo,
            workspace_mgr=self.ws_mgr,
            custom_runner=runner
        )

        completed_run = orchestrator.execute_run(run.run_id)
        self.assertEqual(completed_run.status, "succeeded")
        self.assertEqual(completed_run.phase, "finalize")

        # Verify archive directory structure and manifests
        run_arch_dir = self.archive_dir / "market-task" / completed_run.run_id
        self.assertTrue(run_arch_dir.exists())

        # Check all 7 canonical archived artifacts
        expected_files = [
            "result.json",
            "composition-result.json",
            "email.html",
            "email.txt",
            "delivery-request.json",
            "run-manifest.json",
            "artifact-manifest.json"
        ]
        for ef in expected_files:
            self.assertTrue((run_arch_dir / ef).exists(), f"Missing expected artifact: {ef}")

        # Check run-manifest content
        run_manifest = json.loads((run_arch_dir / "run-manifest.json").read_text())
        self.assertEqual(run_manifest["status"], "succeeded")
        self.assertEqual(run_manifest["task_id"], "market-task")

        # Check artifact-manifest content
        art_manifest = json.loads((run_arch_dir / "artifact-manifest.json").read_text())
        self.assertGreater(len(art_manifest["artifacts"]), 0)

    def _orchestrator(self,runner=None):
        return Orchestrator(self.settings,self.task_repo,self.run_repo,self.delivery_repo,self.state_repo,self.ws_mgr,custom_runner=runner or FakeRunner())

    def test_archive_initialization_failure_does_not_leak_claim(self):
        _,_,run=self._setup_task()
        with patch("researchops.engine.orchestrator.RunArchive",side_effect=OSError("simulated filesystem failure")):
            with self.assertRaises(OSError):
                self._orchestrator().execute_run(run.run_id)
        self.assertEqual(self.run_repo.get_run(run.run_id).status,"failed")
        self.assertIsNone(self.run_repo.get_lease(run.run_id))
        self.assertFalse(self.ws_mgr.is_locked(run.task_id)[0])

    def test_manifest_failure_blocks_delivery_and_runs_cleanup(self):
        _,_,run=self._setup_task()
        with patch.object(RunArchive,"finish",side_effect=OSError("simulated fsync failure")):
            with self.assertRaises(OSError):
                self._orchestrator().execute_run(run.run_id)
        self.assertEqual(self.run_repo.get_run(run.run_id).status,"needs_attention")
        self.assertEqual(self.delivery_repo.get_handoff_for_run(run.run_id).status,"failed")
        self.assertFalse((self.archive_dir/run.task_id/run.run_id).exists())
        self.assertTrue(list((self.archive_dir/run.task_id).glob(".*.pending-*")))
        self.assertIsNone(self.run_repo.get_lease(run.run_id))
        self.assertFalse(self.ws_mgr.is_locked(run.task_id)[0])

    def test_cancel_after_handoff_before_finalize_is_preserved(self):
        _,_,run=self._setup_task()
        orchestrator=self._orchestrator()
        original=orchestrator.handoff_publisher.create_and_publish_handoff
        def publish_then_cancel(*args,**kwargs):
            handoff=original(*args,**kwargs)
            self.run_repo.request_cancel(run.run_id)
            return handoff
        with patch.object(orchestrator.handoff_publisher,"create_and_publish_handoff",side_effect=publish_then_cancel):
            finished=orchestrator.execute_run(run.run_id)
        self.assertEqual(finished.status,"cancelled")
        manifest=json.loads((self.archive_dir/run.task_id/run.run_id/"run-manifest.json").read_text())
        self.assertEqual(manifest["status"],"cancelled")

    def test_stale_fencing_cannot_publish_archive(self):
        _,_,run=self._setup_task()
        runner=FakeRunner()
        original=runner.execute_compose
        def steal_after_compose(*args,**kwargs):
            result=original(*args,**kwargs)
            with self.db.transaction() as conn:
                conn.execute("UPDATE run_leases SET fencing_token='new-owner' WHERE run_id=?",(run.run_id,))
                conn.execute("UPDATE task_claims SET fencing_token='new-owner' WHERE run_id=?",(run.run_id,))
            return result
        with patch.object(runner,"execute_compose",side_effect=steal_after_compose):
            with self.assertRaises(ConcurrencyError):
                self._orchestrator(runner).execute_run(run.run_id)
        self.assertFalse((self.archive_dir/run.task_id/run.run_id).exists())
        self.assertEqual(self.run_repo.get_lease(run.run_id).fencing_token,"new-owner")

    def test_missing_optional_artifact_warns_without_dropping_records(self):
        _,_,run=self._setup_task()
        research={"status":"success","summary":"Research complete","records":[{"record_id":"record-one","title":"kept"}],
            "artifacts":[{"path":"missing.png","role":"inline_image","mime_type":"image/png"}]}
        finished=self._orchestrator(FakeRunner(custom_research_result=research)).execute_run(run.run_id)
        self.assertEqual(finished.status,"succeeded",finished.error_message)
        result=self.run_repo.get_research_result(run.run_id)
        self.assertEqual(len(result.records),1)
        manifest=json.loads((self.archive_dir/run.task_id/run.run_id/"run-manifest.json").read_text())
        self.assertTrue(any("unavailable" in warning for warning in manifest["warnings"]))

    def test_malformed_coverage_never_becomes_complete(self):
        task,_,_=self._setup_task()
        validator=self._orchestrator().technical_gate
        for coverage in ({"complete":True},{"complete":True,"expected_target_count":None,"completed_target_count":"0","issues":[]},
                         {"complete":True,"expected_target_count":0,"completed_target_count":0,"issues":["invalid"]},
                         {"complete":True,"expected_target_count":0,"completed_target_count":0,"issues":"invalid"}):
            result,_=validator.validate_research_output(json.dumps({"status":"success","summary":"ok","records":[],"coverage":coverage}),task)
            self.assertFalse(result.coverage["complete"])


if __name__ == "__main__":
    unittest.main()
