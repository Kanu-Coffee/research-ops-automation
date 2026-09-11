"""Synthetic sample contracts; fake evidence is not evidence of real AI quality."""

from decimal import Decimal
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker

from researchops.engine.message_validator import HTMLSecurityParser
from researchops.package.loader import TaskPackageLoader
from researchops.runners.fake import FakeRunner
from researchops.services.application import ApplicationService
from tests.support import SOURCE_ROOT, isolated_settings


SAMPLES_ROOT = SOURCE_ROOT / "examples/runner-validation"
SAMPLE_NAMES = ("document-extract", "numeric-compare", "partial-coverage", "no-updates")


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class TestValidationSampleContracts(unittest.TestCase):
    def test_all_packages_have_safe_defaults_and_exclude_answer_fixtures(self):
        loader = TaskPackageLoader(SOURCE_ROOT / "schemas")
        for name in SAMPLE_NAMES:
            with self.subTest(sample=name):
                package = SAMPLES_ROOT / name
                task, files, digest = loader.load_from_dir(package)
                expected = load_json(package / "fixtures/expected.json")
                self.assertEqual(task.id, expected["task_id"])
                self.assertFalse(task.enabled)
                self.assertEqual(task.runner["type"], "fake")
                self.assertEqual(task.delivery["mode"], "dry_run")
                self.assertEqual(task.schedule["timezone"], "Asia/Seoul")
                self.assertFalse(task.state["dedupe"]["enabled"])
                self.assertFalse(any(path.startswith("fixtures/") for path in files))
                self.assertEqual(len(digest), 64)
                self.assertTrue(any(path.startswith("source.") for path in task.instructions["research_files"]))
                self.assertFalse(any(path.startswith("source.") for path in task.instructions["compose_files"]))
                research = load_json(package / "fixtures/sample-result.json")
                composition = load_json(package / "fixtures/sample-composition-result.json")
                for payload, schema_name in ((research, "output.schema.json"), (composition, "composition.schema.json")):
                    Draft202012Validator(load_json(package / schema_name), format_checker=FormatChecker()).validate(payload)
                self.assertEqual(research["status"], expected["research_status"])
                self.assertEqual(len(research["records"]), expected["record_count"])
                self.assertNotIn("recipient_group_id", research)
                self.assertEqual(composition["included_record_ids"], expected["record_ids"])
                self.assertEqual(composition["recipient_group_id"], expected["recipient_group_id"])
                parser = HTMLSecurityParser()
                parser.feed((package / "fixtures/sample-email-preview.html").read_text(encoding="utf-8"))
                parser.close()
                self.assertFalse(parser.errors, parser.errors)
                self.assertFalse(parser.stack)
                self.assertEqual(parser.local_dates, {expected["logical_date"]})
                self.assertEqual(parser.record_ids_found, set(expected["record_ids"]))
                self.assertTrue(all(count == 1 for count in parser.record_counts.values()))

    def test_numeric_answers_are_derived_from_source_not_only_counted(self):
        package = SAMPLES_ROOT / "numeric-compare"
        source = load_json(package / "source.json")
        records = {record["record_id"]: record for record in load_json(package / "fixtures/sample-result.json")["records"]}
        for metric in source["metrics"]:
            record = records[metric["id"]]
            delta = Decimal(str(metric["current"])) - Decimal(str(metric["baseline"]))
            percentage = delta * 100 / Decimal(str(metric["baseline"]))
            self.assertEqual(Decimal(str(record["delta"])), delta)
            self.assertEqual(Decimal(str(record["change_percent"])), percentage)
        self.assertGreater(records["metric-alpha"]["delta"], 0)
        self.assertLess(records["metric-beta"]["delta"], 0)

    def test_document_answers_are_present_in_synthetic_source(self):
        package = SAMPLES_ROOT / "document-extract"
        source = (package / "source.txt").read_text(encoding="utf-8")
        for record in load_json(package / "fixtures/sample-result.json")["records"]:
            for field in ("record_id", "title", "change", "effective_date", "owner"):
                self.assertIn(record[field], source)

    def test_empty_and_partial_have_distinct_source_evidence(self):
        empty = load_json(SAMPLES_ROOT / "no-updates/source.json")
        self.assertTrue(all(item["checked"] and item["previous_version"] == item["current_version"] for item in empty["targets"]))
        partial = load_json(SAMPLES_ROOT / "partial-coverage/source.json")
        self.assertEqual([item["status"] for item in partial["targets"]], ["checked", "unavailable", "blocked"])


class TestValidationSamplePipeline(unittest.TestCase):
    def execute_sample(self, name):
        with tempfile.TemporaryDirectory(prefix="researchops-sample-") as directory:
            root = Path(directory)
            settings = isolated_settings(root)
            copied = root / "sample-tasks" / name
            shutil.copytree(SAMPLES_ROOT / name, copied)
            fixtures = copied / "fixtures"
            expected = load_json(fixtures / "expected.json")
            runner = FakeRunner(fixtures_dir=fixtures)
            with patch("subprocess.Popen", side_effect=AssertionError("Synthetic test cannot spawn real AI")), patch("smtplib.SMTP", side_effect=AssertionError("Synthetic test cannot send SMTP")), patch("smtplib.SMTP_SSL", side_effect=AssertionError("Synthetic test cannot send SMTP")):
                app = ApplicationService(settings, custom_runner=runner)
                version = app.tasks.sync_canonical_tasks(root / "sample-tasks")[0]
                self.assertFalse(version.is_active)
                run = app.runs.enqueue_run(expected["task_id"], trigger_type="candidate_dry_run", candidate_version_hash=version.version_hash, scheduled_for=expected["scheduled_for"], force_dry_run=True)
                result = app.runs.execute_run(run.run_id, force_dry_run=True)
            self.assertEqual(result.status, expected["run_status"], result.error_message)
            self.assertEqual(result.local_date, expected["logical_date"])
            self.assertEqual(result.timezone, "Asia/Seoul")
            self.assertFalse(app.workspace_mgr.is_locked(expected["task_id"])[0])
            compose_input = app.run_repo.get_composition_input(run.run_id)
            records = compose_input["reportable_records"]
            self.assertEqual(len(records), expected["record_count"])
            self.assertEqual([record["record_id"] for record in records], expected["record_ids"])
            original = load_json(fixtures / "sample-result.json")
            self.assertEqual(records, original["records"])
            coverage = compose_input["coverage"]
            self.assertEqual(coverage["complete"], expected["coverage_complete"])
            self.assertEqual(coverage["expected_target_count"], expected["expected_target_count"])
            self.assertEqual(coverage["completed_target_count"], expected["completed_target_count"])
            self.assertGreaterEqual(len(compose_input["result"]["warnings"]), expected["min_warning_count"])
            self.assertEqual(compose_input["result"]["status"], expected["research_status"])
            by_id = {record["record_id"]: record for record in records}
            for record_id, assertions in expected["record_assertions"].items():
                for key, value in assertions.items():
                    self.assertEqual(by_id[record_id][key], value)
            composition = app.run_repo.get_composition_result(run.run_id)
            self.assertEqual(composition.recipient_group_id, expected["recipient_group_id"])
            self.assertEqual(composition.included_record_ids, expected["record_ids"])
            handoff = app.delivery_repo.get_handoff_for_run(run.run_id)
            self.assertEqual(handoff.mode, "dry_run")
            self.assertEqual(handoff.status, "prepared")
            self.assertIsNone(handoff.published_at)
            archive = settings.paths.run_archive_dir / expected["task_id"] / run.run_id
            self.assertEqual((archive / "result.json").read_bytes(), (fixtures / "sample-result.json").read_bytes())
            for artifact in ("email.html", "email.txt", "run-manifest.json", "artifact-manifest.json", "validation-report.json"):
                self.assertTrue((archive / artifact).is_file(), artifact)
            html = (archive / "email.html").read_text(encoding="utf-8")
            self.assertIn(expected["logical_date"], html)
            research_input = archive / "inputs/research"
            compose_input_dir = archive / "inputs/compose"
            self.assertTrue((research_input / "task.md").is_file())
            self.assertFalse(any(path.name == "fixtures" for path in research_input.rglob("*")))
            self.assertTrue(any(research_input.glob("source.*")))
            self.assertFalse(any(compose_input_dir.glob("source.*")))
            return original, composition

    def test_document_extraction_dry_run(self):
        self.execute_sample("document-extract")

    def test_numeric_comparison_dry_run(self):
        self.execute_sample("numeric-compare")

    def test_partial_coverage_preserves_records_and_warnings(self):
        self.execute_sample("partial-coverage")

    def test_no_updates_still_composes_dry_run(self):
        self.execute_sample("no-updates")
