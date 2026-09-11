"""Production publication has no fixture-run prerequisite or implicit sends."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from researchops.delivery.smtp_config import BuiltinDeliveryConfig, save_delivery_config
from researchops.engine.composition_input import CompositionInputBuilder
from researchops.engine.message_validator import MessageValidator
from researchops.engine.technical_gate import TechnicalGateValidator
from researchops.errors import ValidationError
from researchops.services.application import ApplicationService
from tests.package_support import register_template
from tests.support import isolated_settings


class TestProductionLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = isolated_settings(Path(self.temp.name))
        self.settings.environment = "production"
        self.app = ApplicationService(self.settings)
        save_delivery_config(BuiltinDeliveryConfig(recipient_groups={
            "research-team": ["recipient@example.test"],
            "release-team": ["recipient@example.test"],
            "release-stakeholders": ["recipient@example.test"],
        }), self.settings.paths.delivery_config_file)

    def create(self, **changes):
        args = {"task_id": "policy-digest", "name": "기술 정책 동향", "instructions":
                "공식 기관의 기술 정책 발표를 조사하고 주요 변경점과 출처를 요약하라.",
                "runner_type": "codex_exec", "recipient_group_id": "research-team"}
        args.update(changes)
        return self.app.tasks.create_production_task(**args)

    def test_direct_publication_is_real_and_has_no_dry_run_or_smtp_job(self):
        version = self.create()
        status = self.app.task_repo.get_task_status(version.task_id)
        self.assertTrue(version.is_active)
        self.assertEqual(status["active_version_hash"], version.version_hash)
        self.assertEqual(status["delivery_mode"], "handoff")
        self.assertFalse(status["enabled"])
        self.assertIsNone(status["approval_dry_run_id"])
        self.assertTrue(self.app.tasks.show_task(version.task_id)["status"]["delivery_approved"])
        self.assertFalse(self.app.task_repo.has_dry_run_evidence(version.task_id, version.version_hash))
        conn = self.app.db.get_connection()
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM scheduled_runs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM smtp_attempts").fetchone()[0], 0)
        finally:
            conn.close()
        self.assertIn("공식 기관", version.package_files["task.md"])
        self.assertNotIn("software-releases", "".join(version.package_files.values()))
        self.assertNotIn("recipient@example.test", "".join(version.package_files.values()))
        self.assertIn('data-local-date="YYYY-MM-DD"', version.package_files["email_spec.md"])
        valid, errors, _ = self.app.tasks.validate_task(version.task_id)
        self.assertTrue(valid, errors)

    def test_enabled_schedule_enqueues_one_real_seoul_run(self):
        version = self.create(cron="30 0 * * *", schedule_enabled=True, runner_type="antigravity_exec")
        self.assertEqual(version.definition.schedule["timezone"], "Asia/Seoul")
        now = datetime(2026, 9, 6, 15, 30, tzinfo=timezone.utc)
        results = self.app.scheduler.schedule_tick(now)
        self.assertEqual(results[0]["status"], "enqueued")
        run = self.app.run_repo.get_run(results[0]["run_id"])
        self.assertEqual(run.local_date, "2026-09-07")
        self.assertEqual(run.trigger_type, "schedule")
        self.assertFalse(self.app.run_repo.get_execution_controls(run.run_id)["force_dry_run"])
        self.assertNotEqual(self.app.scheduler.schedule_tick(now)[0]["status"], "enqueued")

    def test_identical_submission_is_idempotent_and_never_reenables_schedule(self):
        version = self.create(schedule_enabled=True, request_key="publish-1")
        self.app.tasks.set_task_enabled(version.task_id, False)
        duplicate = self.create(schedule_enabled=True, request_key="publish-1")
        self.assertEqual(duplicate.version_hash, version.version_hash)
        self.assertEqual(duplicate.sealed_at, version.sealed_at)
        self.assertFalse(self.app.task_repo.get_task_status(version.task_id)["enabled"])
        with self.assertRaises(ValidationError):
            self.create(instructions="A different research request")
        self.assertEqual(len(self.app.task_repo.list_versions(version.task_id)), 1)

    def test_concurrent_identical_publications_create_one_task(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            versions = list(pool.map(lambda _: self.create(), range(2)))
        self.assertEqual(versions[0].version_hash, versions[1].version_hash)
        self.assertEqual(len(self.app.task_repo.list_tasks()), 1)
        self.assertEqual(len(self.app.task_repo.list_versions("policy-digest")), 1)

    def test_invalid_request_cannot_publish(self):
        for changes in ({"recipient_group_id": "missing-team"}, {"cron": "61 9 * * *"},
                        {"runner_type": "fake"}, {"instructions": "  "},
                        {"task_id": "../outside"}, {"schedule_enabled": "true"},
                        {"request_key": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.create(**changes)
        self.assertEqual(self.app.task_repo.list_tasks(), [])

    def test_production_package_candidate_can_activate_without_development_evidence(self):
        candidate = register_template(self.app, "edited-research")
        self.app.tasks.activate_version(candidate.task_id, candidate.version_hash)
        version = self.app.task_repo.get_active_version(candidate.task_id)
        self.assertTrue(version.is_active)
        self.assertEqual(version.definition.delivery["mode"], "handoff")
        self.assertEqual(self.app.task_repo.get_task_status(version.task_id)["delivery_mode"], "handoff")
        self.assertFalse(self.app.task_repo.has_dry_run_evidence(version.task_id, version.version_hash))

    def test_generic_package_does_not_require_industry_specific_fields(self):
        version = self.create()
        record = {"title": "정책 발표", "custom_data": {"effective_date": None}}
        result, _ = TechnicalGateValidator(self.settings.paths.schemas_dir).validate_research_output(
            json.dumps({"status": "success", "summary": "공식 발표", "records": [record]}),
            version.definition, json.loads(version.package_files["output.schema.json"]))
        self.assertEqual(result.records[0]["custom_data"], record["custom_data"])
        self.assertIn("record_id", result.records[0])

    def test_generic_research_to_compose_contract_and_real_run_controls(self):
        version = self.create()
        run = self.app.runs.enqueue_run(version.task_id, scheduled_for="2026-09-06T15:30:00Z",
                                        request_key="send-once")
        again = self.app.runs.enqueue_run(version.task_id, scheduled_for="2026-09-06T15:30:00Z",
                                          request_key="send-once")
        self.assertEqual(run.run_id, again.run_id)
        self.assertFalse(self.app.run_repo.get_execution_controls(run.run_id)["force_dry_run"])
        result, _ = TechnicalGateValidator(self.settings.paths.schemas_dir).validate_research_output(
            json.dumps({"status": "success", "summary": "공식 발표 확인", "records": [
                {"title": "정책 발표", "details": {"effective_date": None},
                 "sources": [{"url": "https://example.test/official-release"}]}],
                "coverage": {"complete": True, "expected_target_count": 1,
                             "completed_target_count": 1, "issues": []}, "warnings": []}),
            version.definition, json.loads(version.package_files["output.schema.json"]))
        composition = CompositionInputBuilder(self.settings.paths.schemas_dir).build_composition_input(
            version.definition, run, result, result.records,
            record_schema=json.loads(version.package_files["composition-record.schema.json"]))
        record_id = composition.reportable_records[0]["record_id"]
        output = Path(self.temp.name) / "protocol-output"
        output.mkdir()
        message = {"recipient_group_id": "research-team", "recipient_group_reason": "지정 조사 그룹",
                   "subject": "기술 정책 동향 2026.09.07", "html_path": "email.html",
                   "text_path": "email.txt", "included_record_ids": [record_id]}
        (output / "composition-result.json").write_text(json.dumps(message), encoding="utf-8")
        (output / "email.html").write_text('<!DOCTYPE html><html><head><title>정책</title></head>'
            '<body data-local-date="2026-09-07"><h1>2026.09.07 기술 정책</h1>'
            f'<article data-record-id="{record_id}">정책 발표 '
            '<a href="https://example.test/official-release">공식 출처</a></article></body></html>', encoding="utf-8")
        (output / "email.txt").write_text(f"2026.09.07 기술 정책\n{record_id}: 정책 발표\n"
            "https://example.test/official-release", encoding="utf-8")
        validated, *_ = MessageValidator(self.settings.paths.schemas_dir).validate_composition(
            output, composition, version.definition,
            json.loads(version.package_files["composition.schema.json"]))
        self.assertEqual(validated.included_record_ids, [record_id])
        self.assertEqual(composition.reportable_records[0]["details"]["effective_date"], None)

    def test_production_delivery_toggle_has_no_trial_gate_and_preserves_schedule(self):
        version = self.create(schedule_enabled=True)
        self.app.tasks.approve_delivery(version.task_id, False)
        status = self.app.task_repo.get_task_status(version.task_id)
        self.assertTrue(status["enabled"])
        self.assertEqual(status["delivery_mode"], "disabled")
        self.app.tasks.approve_delivery(version.task_id, True)
        status = self.app.task_repo.get_task_status(version.task_id)
        self.assertTrue(status["enabled"])
        self.assertEqual(status["delivery_mode"], "handoff")
        self.assertIsNone(status["approval_dry_run_id"])

    def test_direct_production_api_does_not_change_development_policy(self):
        self.settings.environment = "test"
        with self.assertRaises(ValidationError):
            self.create()
        version = register_template(self.app, "development-task")
        with self.assertRaises(ValidationError):
            self.app.tasks.activate_version(version.task_id, version.version_hash)
