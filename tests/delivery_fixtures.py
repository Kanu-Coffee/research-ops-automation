"""Fully isolated delivery fixtures; no real runner, credentials or SMTP network."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

from researchops.config import Settings, PathsConfig, DeliveryConfig
from researchops.delivery.handoff import HandoffPublisher
from researchops.delivery.receipt import ReceiptConsumer
from researchops.delivery.smtp_config import BuiltinDeliveryConfig, SmtpSettings, delivery_revision, save_delivery_config
from researchops.delivery.smtp_dispatcher import SmtpDispatcher
from researchops.domain.models import TaskDefinition, TaskVersion, ScheduledRun, CompositionInput, CompositionResult
from researchops.storage.db import Database
from researchops.storage.repositories import TaskRepository, RunRepository, DeliveryRepository, StateRepository


def smtp_server():
    """Synthetic SMTP peer with separate DATA readiness and final reply."""
    server = MagicMock()
    server.ehlo.return_value = (250, b"ready")
    server.starttls.return_value = (220, b"TLS ready")
    server.login.return_value = (235, b"authenticated")
    server.mail.return_value = server.rcpt.return_value = (250, b"ok")
    server.noop.return_value = (250, b"ok")
    server.getreply.side_effect = [(354, b"send body"), (250, b"accepted")]
    server.sock.gettimeout.return_value = 15
    return server


def smtp_message_bytes(server):
    """Decode captured SMTP DATA framing for original MIME integrity checks."""
    server.send.assert_called_once()
    wire = server.send.call_args.args[0]
    if not isinstance(wire, bytes) or not wire.endswith(b"\r\n.\r\n"):
        raise AssertionError("SMTP body must end with a complete DATA terminator")
    return b"".join(line[1:] if line.startswith(b"..") else line
                    for line in wire[:-3].splitlines(keepends=True))


class DeliveryFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict("os.environ", {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.root = Path(self.temp.name)
        paths = PathsConfig(repo_root=self.root, tasks_dir=self.root/"tasks", data_dir=self.root/"var",
            task_drafts_dir=self.root/"drafts",task_versions_dir=self.root/"versions",
            task_workspaces_dir=self.root/"workspaces",run_archive_dir=self.root/"archive",
            delivery_outbox_dir=self.root/"outbox",receipts_dir=self.root/"receipts",
            database=self.root/"database"/"test.db",schemas_dir=Path(__file__).resolve().parents[1]/"schemas",
            delivery_config_file=self.root/"secrets"/"smtp.yaml")
        self.settings = Settings(paths=paths, delivery=DeliveryConfig(global_handoff_kill_switch=False))
        self.db = Database(paths.database)
        self.db.init_schema()
        self.task_repo, self.run_repo = TaskRepository(self.db), RunRepository(self.db)
        self.delivery_repo, self.state_repo = DeliveryRepository(self.db), StateRepository(self.db)
        self.consumer = ReceiptConsumer(self.settings,self.delivery_repo,self.run_repo,self.state_repo,self.task_repo)
        self.publisher = HandoffPublisher(self.settings,self.delivery_repo,self.state_repo)
        self.dispatcher = SmtpDispatcher(self.settings,self.consumer,self.delivery_repo,self.state_repo)
        self.config = BuiltinDeliveryConfig(enabled=True,
            smtp=SmtpSettings(host="smtp.example.test",sender_email="sender@example.test"),
            recipient_groups={"test-team":["first@example.test","second@example.test"],"researchops-admins":["admin@example.test"]})
        save_delivery_config(self.config,paths.delivery_config_file)
        self.task = TaskDefinition(id="test-task",name="SMTP tests",enabled=False,
            workspace={"mode":"persistent_task"},runner={"type":"fake"},instructions={},output={},
            delivery={"mode":"handoff","allowed_recipient_group_ids":["test-team"]},
            state={"dedupe":{"enabled":True,"key_fields":["issuer"],"content_fields":["benefit"]}},
            alerting={"events":["failed"],"recipient_group_id":"researchops-admins"})
        self.version = TaskVersion(task_id=self.task.id,version_hash="a"*64,sealed_at="2026-09-04T00:00:00Z",
            definition=self.task,package_files={})
        self.task_repo.save_version(self.version)
        self.task_repo.set_active_version(self.task.id,self.version.version_hash)
        self.stage = self.root/"stage"
        self.stage.mkdir()
        self.html = "<html><body><p>서울 보고서\r\nSecond line</p></body></html>".encode()
        self.plain = "서울 보고서\nSecond line\r\n".encode()
        (self.stage/"email.html").write_bytes(self.html)
        (self.stage/"email.txt").write_bytes(self.plain)
        self.hashes = {"html":hashlib.sha256(self.html).hexdigest(),"text":hashlib.sha256(self.plain).hexdigest()}
        self.result = CompositionResult(recipient_group_id="test-team",recipient_group_reason="Test",
            subject="Seoul digest",html_path="email.html",text_path="email.txt",included_record_ids=["c1"])
        dry = self.make_run("dry-1", "succeeded", "candidate_dry_run")
        dry_input = self.make_input(dry)
        self.publisher.create_and_publish_handoff(self.task,dry,dry_input,self.result,self.stage,
            self.root/"unused",self.hashes,force_mode="dry_run")
        self.task_repo.set_delivery_approved(self.task.id,True,delivery_revision(self.config))
        self.run = self.make_run("live-1", "awaiting_receipt")
        self.comp_input = self.make_input(self.run)
        self.run_repo.save_composition_input(self.comp_input,"b"*64)
        archive = paths.run_archive_dir/self.task.id/self.run.run_id
        archive.mkdir(parents=True)
        (archive/"run-manifest.json").write_text(json.dumps({"run_id":self.run.run_id,"task_id":self.task.id,
            "status":"awaiting_receipt","phase":"finalize","attempt":1,"result_outcome":{"status":"success"},
            "composition":{"status":"validated","revision":1,"recipient_group_id":"test-team",
                "recipient_group_reason":"Test","subject":"Seoul digest","html_path":"email.html",
                "text_path":"email.txt","included_record_ids":["c1"]},
            "handoff":{"status":"pending","mode":"handoff"},
            "workspace":{"task_workspace_id":self.task.id,"generation":1,"attempt_root":"staging/live-1/attempt-1",
                "fencing_token":"fixture-test-fence","child_group_ids":[]}}))

    def make_run(self, run_id, status="awaiting_receipt", trigger="manual"):
        run = ScheduledRun(run_id=run_id,task_id=self.task.id,task_version_hash=self.version.version_hash,
            scheduled_for="2026-09-04T15:30:00Z",timezone="Asia/Seoul",local_date="2026-09-05",
            local_date_display="2026.09.05",trigger_type=trigger,status=status,phase="finalize",attempt=1)
        self.run_repo.create_run(run)
        return run

    def make_input(self, run):
        return CompositionInput(schema_version=2,task_id=self.task.id,run_id=run.run_id,
            task_version_hash=self.version.version_hash,composition_revision=1,
            run={"scheduled_for":run.scheduled_for,"timezone":"Asia/Seoul","local_date":run.local_date,
                "local_date_display":run.local_date_display},
            result={"status":"success","summary":"Test","warnings":[]},
            coverage={"complete":True,"expected_target_count":1,"completed_target_count":1,"issues":[]},
            allowed_recipient_group_ids=["test-team"],reportable_records=[{"record_id":"c1","issuer":"Bank","benefit":"Benefit"}],
            inline_artifacts=[],attachments=[])

    def publish(self):
        handoff = self.publisher.create_and_publish_handoff(self.task,self.run,self.comp_input,self.result,
            self.stage,self.root/"unused",self.hashes,force_mode="handoff")
        archive = self.settings.paths.run_archive_dir/self.task.id/self.run.run_id
        manifest = json.loads((archive/"run-manifest.json").read_text())
        manifest["handoff"] = {"status":"published","mode":"handoff","message_type":"market_digest",
            "recipient_group_id":handoff.recipient_group_id,"message_revision":1,"handoff_id":handoff.handoff_id,
            "idempotency_key":handoff.idempotency_key,"delivery_request_path":"delivery-request.json",
            "delivery_request_sha256":handoff.delivery_request_sha256,"published_at":handoff.published_at}
        (archive/"run-manifest.json").write_text(json.dumps(manifest))
        (archive/"delivery-request.json").write_bytes((self.settings.paths.delivery_outbox_dir/handoff.handoff_id/"delivery-request.json").read_bytes())
        return handoff
