"""Research dates/history are controller inputs, independent of model prose."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.delivery_fixtures import smtp_server

import yaml

from researchops.delivery.smtp_config import BuiltinDeliveryConfig, SmtpSettings
from researchops.domain.models import ScheduledRun, TaskDefinition
from researchops.engine.archive import canonical_json
from researchops.engine.delivery_history import (build_delivery_history, matching_delivery_entry,
    attach_ledger_reconciliation, _bounded_history, MAX_HISTORY_BYTES)
from researchops.engine.research_context import build_research_context, validate_context_config
from researchops.errors import ValidationError
from researchops.services.application import ApplicationService
from tests.support import isolated_settings, fixture_runner, register_fixture_task


def context_config(reference="2026-08-02", series="example-replay-series"):
    return {"enabled": True, "active_issuers": ["woori", "shinhan", "kb", "samsung"],
        "reference_date": reference, "lookback_days": 7, "series_id": series, "delivery_history": True}


class TestResearchDateContract(unittest.TestCase):
    def context(self, reference, days=7):
        config = context_config(reference)
        config["lookback_days"] = days
        task = TaskDefinition("test-task", "Test", False, {}, {}, {}, {}, {}, state={"research_context": config})
        run = ScheduledRun("run-one", task.id, "a" * 64, "2026-09-09T15:30:00Z", "Asia/Seoul",
            "2026-09-10", "2026.09.10", "manual")
        return build_research_context(task, run)

    def test_replay_windows_include_both_bounds(self):
        for reference, start in [("2026-08-01", "2026-07-26"), ("2026-08-02", "2026-07-27"),
                ("2026-09-02", "2026-08-27"), ("2024-03-01", "2024-02-24"),
                ("2026-01-03", "2025-12-28")]:
            with self.subTest(reference=reference):
                context = self.context(reference)
                self.assertEqual((context["start_date"], context["end_date"]), (start, reference))
                self.assertEqual(context["logical_date"], "2026-09-10")

    def test_one_ten_thirty_day_ranges(self):
        for days, start in [(1, "2026-09-02"), (10, "2026-08-24"), (30, "2026-08-04")]:
            self.assertEqual(self.context("2026-09-02", days)["start_date"], start)

    def test_no_override_uses_frozen_seoul_logical_date(self):
        value = self.context(None)
        self.assertEqual(value["reference_date"], "2026-09-10")
        self.assertEqual(value["reference_date_source"], "logical_date")

    def test_invalid_configuration_does_not_fallback(self):
        for patch_value in [{"reference_date": "2026-02-30"}, {"reference_date": "2026-9-2"},
                {"reference_date": "0001-01-01"}, {"reference_date": 3}, {"lookback_days": True},
                {"lookback_days": 0}, {"lookback_days": "7"}, {"active_issuers": ["kb", "kb"]},
                {"active_issuers": ["invalid issuer label"]}, {"series_id": "../escape"}, {"series_id": "@secret"},
                {"delivery_history": "true"}, {"enabled": 1}, {"unexpected": 1}]:
            with self.subTest(patch=patch_value), self.assertRaises(ValidationError):
                validate_context_config(context_config() | patch_value)

    def test_disabled_is_backward_compatible(self):
        validate_context_config(None)
        validate_context_config({"enabled": False})


class TestSealedResearchContext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = isolated_settings(self.root)
        path = self.settings.paths.tasks_dir / "software-releases/task.yaml"
        config = yaml.safe_load(path.read_text())
        config["state"] = {"dedupe": {"enabled": False}, "research_context": context_config()}
        path.write_text(yaml.safe_dump(config))
        self.runner = fixture_runner(self.settings)
        self.app = ApplicationService(self.settings, custom_runner=self.runner)
        self.version = register_fixture_task(self.app)

    def execute(self):
        run = self.app.runs.enqueue_run(self.version.task_id)
        result = self.app.runs.execute_run(run.run_id)
        self.assertEqual(result.status, "succeeded", result.error_message)
        return result, self.settings.paths.run_archive_dir / result.task_id / result.run_id

    def test_sidecars_in_both_phases_and_immutable_composition(self):
        run, archive = self.execute()
        comp = self.app.run_repo.get_composition_input(run.run_id)
        for filename, field in [("research-context.json", "research_context"), ("delivery-history.json", "delivery_history")]:
            expected = canonical_json(comp[field])
            self.assertEqual((archive / filename).read_bytes(), expected)
            for phase in ["research", "compose"]:
                self.assertEqual((archive / "inputs" / phase / filename).read_bytes(), expected)
        self.assertEqual(comp["research_context"]["reference_date"], "2026-08-02")
        self.assertEqual(comp["delivery_history"]["entries"], [])
        self.assertEqual(self.app.run_repo.get_composition_input_record(run.run_id)["input_sha256"],
            hashlib.sha256(canonical_json(comp)).hexdigest())

    def test_compose_only_keeps_history_and_dates_without_new_projection(self):
        run, archive = self.execute()
        child = self.app.runs.compose_only(run.run_id)
        with patch.object(self.runner, "execute_research", side_effect=AssertionError("no research")), \
                patch("researchops.engine.delivery_history.build_delivery_history", side_effect=AssertionError("no new history")):
            result = self.app.runs.execute_run(child.run_id)
        self.assertEqual(result.status, "succeeded", result.error_message)
        next_archive = archive.parent / child.run_id
        for name in ["research-context.json", "delivery-history.json"]:
            self.assertEqual((archive / name).read_bytes(), (next_archive / name).read_bytes())
        self.assertEqual(child.local_date, run.local_date)

    def test_tampered_parent_sidecar_blocks_compose(self):
        run, archive = self.execute()
        (archive / "research-context.json").write_text("{}")
        child = self.app.runs.compose_only(run.run_id)
        with patch.object(self.runner, "execute_compose", side_effect=AssertionError("must not compose")):
            result = self.app.runs.execute_run(child.run_id)
        self.assertEqual(result.status, "failed")
        self.assertIn("sidecar", result.error_message)

    def test_retry_retains_original_context_and_new_parent_link(self):
        self.runner.simulate_failure = True
        run = self.app.runs.enqueue_run(self.version.task_id)
        result = self.app.runs.execute_run(run.run_id)
        self.assertEqual(result.status, "failed")
        archive = self.settings.paths.run_archive_dir / result.task_id / result.run_id
        original = (archive / "research-context.json").read_bytes()
        self.runner.simulate_failure = False
        child = self.app.runs.retry_run(run.run_id)
        completed = self.app.runs.execute_run(child.run_id)
        self.assertEqual(completed.status, "succeeded", completed.error_message)
        next_archive = archive.parent / child.run_id
        self.assertEqual(original, (next_archive / "research-context.json").read_bytes())
        self.assertEqual(json.loads((next_archive / "delivery-history.json").read_text())["parent_run_id"], run.run_id)

    def test_task_cannot_supply_controller_sidecar(self):
        from researchops.package.loader import TaskPackageLoader
        files = dict(self.version.package_files, **{"research-context.json": "{}"})
        with self.assertRaisesRegex(ValidationError, "application-owned"):
            TaskPackageLoader(self.settings.paths.schemas_dir).validate_package(self.version.definition.to_dict(), files)

    def test_retry_rejects_forged_origin_in_precomposition_archive(self):
        self.runner.simulate_failure = True
        run = self.app.runs.enqueue_run(self.version.task_id)
        self.app.runs.execute_run(run.run_id)
        archive = self.settings.paths.run_archive_dir / run.task_id / run.run_id
        path = archive / "research-context.json"
        value = json.loads(path.read_text())
        value["origin_run_id"] = "unrelated-run"
        path.write_bytes(canonical_json(value))
        child = self.app.runs.retry_run(run.run_id)
        with patch.object(self.runner, "execute_research", side_effect=AssertionError("must not run")):
            result = self.app.runs.execute_run(child.run_id)
        self.assertEqual(result.status, "failed")
        self.assertIn("origin", result.error_message)

    def test_repeated_retry_origin_is_original_failed_run(self):
        self.runner.simulate_failure = True
        first = self.app.runs.enqueue_run(self.version.task_id)
        self.app.runs.execute_run(first.run_id)
        second = self.app.runs.retry_run(first.run_id)
        self.app.runs.execute_run(second.run_id)
        self.runner.simulate_failure = False
        third = self.app.runs.retry_run(second.run_id)
        result = self.app.runs.execute_run(third.run_id)
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertEqual(self.app.run_repo.get_composition_input(third.run_id)["research_context"]["origin_run_id"], first.run_id)


class TestVerifiedDeliveryHistory(unittest.TestCase):
    def setUp(self):
        TestSealedResearchContext.setUp(self)
        self.settings.environment = "production"
        # Synthetic version only, using the ordinary publisher and SMTP double.
        path = self.settings.paths.tasks_dir / "software-releases/task.yaml"
        config = yaml.safe_load(path.read_text())
        config["delivery"]["mode"] = "handoff"
        path.write_text(yaml.safe_dump(config))
        self.version = register_fixture_task(self.app)
        with self.app.run_repo.db.transaction() as conn:
            conn.execute("UPDATE tasks SET delivery_mode='handoff' WHERE task_id=?", (self.version.task_id,))
        smtp = BuiltinDeliveryConfig(enabled=True, auto_dispatch=True,
            smtp=SmtpSettings(host="smtp.example.test", sender_email="sender@example.test"),
            recipient_groups={group: ["recipient@example.test"] for group in config["delivery"]["allowed_recipient_group_ids"]})
        self.app.delivery.save_delivery_config(smtp)
        self.settings.delivery.global_handoff_kill_switch = False

    def send(self, status="smtp_accepted"):
        run = self.app.runs.enqueue_run(self.version.task_id)
        result = self.app.runs.execute_run(run.run_id)
        self.assertEqual(result.status, "awaiting_receipt", result.error_message)
        server = smtp_server()
        if status == "failed":
            server.rcpt.return_value = (550, b"rejected")
        elif status == "uncertain":
            server.getreply.side_effect = [(354, b"send body"), TimeoutError("synthetic DATA timeout")]
        with patch("smtplib.SMTP", return_value=server):
            self.app.delivery.dispatch_all_pending()
        return self.app.run_repo.get_run(run.run_id)

    def history(self, series=None):
        task = self.version.definition
        run = ScheduledRun("next-run", task.id, self.version.version_hash, "2026-09-09T00:00:00Z",
            "Asia/Seoul", "2026-09-09", "2026.09.09", "manual")
        context = build_research_context(task, run)
        if series:
            context["series_id"] = series
        return build_delivery_history(self.settings, self.app.task_repo, self.app.run_repo,
            self.app.delivery_repo, task, run, context)

    # Base-case executions remain dry-run even in this production test fixture.
    def execute(self):
        run = self.app.runs.enqueue_run(self.version.task_id)
        result = self.app.runs.execute_run(run.run_id, force_dry_run=True)
        self.assertEqual(result.status, "succeeded", result.error_message)
        return result, self.settings.paths.run_archive_dir / result.task_id / result.run_id

    def test_smtp_acceptance_exact_bindings_and_no_addresses(self):
        run = self.send()
        history = self.history()
        self.assertTrue(history["history_complete"], history)
        entry, = history["entries"]
        self.assertEqual(entry["status"], "smtp_accepted")
        self.assertEqual(entry["run_id"], run.run_id)
        self.assertTrue(entry["record_bindings"])
        text = json.dumps(history)
        for denied in ["recipient@example.test", "sender@example.test", "server_reply", "envelope_json", "mime_bytes"]:
            self.assertNotIn(denied, text)
        self.assertEqual(self.history(), history)

    def test_predata_rejection_is_definite_not_sent(self):
        self.send("failed")
        self.assertEqual(self.history()["entries"][0]["status"], "failed_not_sent")

    def test_data_timeout_is_uncertain(self):
        self.send("uncertain")
        self.assertEqual(self.history()["entries"][0]["status"], "uncertain")

    def test_email_retry_acceptance_preserves_and_reconciles_original_failed_receipt(self):
        run = self.send("failed")
        before = self.history()["entries"][0]
        handoff = self.app.delivery_repo.get_handoff_for_run(run.run_id)
        original = self.app.smtp_dispatcher.queue.get(handoff.handoff_id)
        original_receipt = self.app.delivery_repo.get_receipt(before["receipt_id"]).to_dict()
        evidence = self.settings.paths.receipts_dir / handoff.handoff_id
        archived = {path.name: path.read_bytes() for path in evidence.iterdir() if path.is_file()}
        message = {key: before[key] for key in ("task_id", "series_id", "run_id", "composition_revision",
            "body_sha256", "record_bindings", "status", "receipt_id", "receipt_sha256")}
        ledger = canonical_json({"task_id": before["task_id"], "series_id": before["series_id"],
                                 "messages": {"original-message": message}})
        retry = self.app.delivery.retry_email(handoff.handoff_id, request_key="history-retry")
        pending_history = self.history()
        pending_entry = pending_history["entries"][0]
        self.assertEqual(pending_entry["status"], "pending")
        self.assertEqual(pending_entry["pending_attempt"]["attempt_number"], 2)
        self.assertIsNone(pending_entry["receipt_id"])
        pending_update, = attach_ledger_reconciliation(pending_history, ledger)["ledger_reconciliation"]["updates"]
        self.assertEqual(pending_update["status"], "pending")
        self.assertEqual(pending_update["transition_reason"], "smtp_retry_pending")
        self.assertEqual(pending_update["previous_receipt"]["receipt_id"], before["receipt_id"])
        with patch("smtplib.SMTP", return_value=smtp_server()):
            ok, _, error = self.app.smtp_dispatcher._dispatch_job(retry["job_id"])
        self.assertTrue(ok, error)
        history = self.history()
        entry = history["entries"][0]
        self.assertTrue(history["history_complete"], history)
        self.assertEqual([item["status"] for item in entry["smtp_attempt_receipts"]],
                         ["failed_not_sent", "smtp_accepted"])
        reconciliation = attach_ledger_reconciliation(history, ledger)["ledger_reconciliation"]
        update, = reconciliation["updates"]
        self.assertEqual(reconciliation["unresolved"], [])
        self.assertEqual(update["status"], "smtp_accepted")
        self.assertEqual(update["transition_reason"], "verified_smtp_retry")
        self.assertEqual(update["previous_receipt"]["receipt_id"], before["receipt_id"])
        self.assertEqual(update["previous_receipt"]["receipt_sha256"], before["receipt_sha256"])
        self.assertEqual(self.app.smtp_dispatcher.queue.get(handoff.handoff_id), original)
        self.assertEqual(self.app.delivery_repo.get_receipt(before["receipt_id"]).to_dict(), original_receipt)
        self.assertEqual({path.name: path.read_bytes() for path in evidence.iterdir() if path.is_file()}, archived)
        message["receipt_sha256"] = "0" * 64
        forged = canonical_json({"task_id": before["task_id"], "series_id": before["series_id"],
                                 "messages": {"original-message": message}})
        denied = attach_ledger_reconciliation(deepcopy(history), forged)["ledger_reconciliation"]
        self.assertEqual(denied["updates"], [])
        self.assertEqual(denied["unresolved"][0]["reason"], "previous_delivery_conflict")
        with self.app.run_repo.db.transaction() as conn:
            conn.execute("UPDATE smtp_attempts SET parent_job_id='unrelated-attempt' WHERE job_id=?", (retry["job_id"],))
        broken = self.history()
        self.assertFalse(broken["history_complete"])
        self.assertEqual(broken["entries"], [])
        self.assertEqual(broken["unresolved"][0]["reason"], "evidence_binding_failed")

    def test_failed_acknowledged_retry_does_not_erase_original_uncertainty(self):
        run = self.send("uncertain")
        before = self.history()["entries"][0]
        handoff = self.app.delivery_repo.get_handoff_for_run(run.run_id)
        retry = self.app.delivery.retry_email(handoff.handoff_id, request_key="uncertain-history-retry",
            allow_uncertain=True, reason="Synthetic duplicate-risk acknowledgement")
        server = smtp_server()
        server.rcpt.return_value = (550, b"rejected")
        with patch("smtplib.SMTP", return_value=server):
            ok, _, _ = self.app.smtp_dispatcher._dispatch_job(retry["job_id"])
        self.assertFalse(ok)
        entry = self.history()["entries"][0]
        self.assertEqual(entry["status"], "uncertain")
        self.assertEqual(entry["receipt_id"], before["receipt_id"])
        self.assertEqual(entry["receipt_sha256"], before["receipt_sha256"])
        self.assertEqual(self.app.run_repo.get_run(run.run_id).status, "needs_attention")
        self.app.delivery.retry_email(handoff.handoff_id, request_key="uncertain-history-third",
            allow_uncertain=True, reason="Synthetic duplicate-risk acknowledgement repeated")
        pending = self.history()["entries"][0]
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["pending_attempt"]["attempt_number"], 3)
        self.assertEqual([item["status"] for item in pending["smtp_attempt_receipts"]],
                         ["uncertain", "failed_not_sent"])

    def test_acknowledged_retry_acceptance_reconciles_verified_uncertain_predecessor(self):
        run = self.send("uncertain")
        before = self.history()["entries"][0]
        handoff = self.app.delivery_repo.get_handoff_for_run(run.run_id)
        retry = self.app.delivery.retry_email(handoff.handoff_id, request_key="uncertain-history-success",
            allow_uncertain=True, reason="Synthetic duplicate-risk acknowledgement")
        with patch("smtplib.SMTP", return_value=smtp_server()):
            ok, _, error = self.app.smtp_dispatcher._dispatch_job(retry["job_id"])
        self.assertTrue(ok, error)
        message = {key: before[key] for key in ("task_id", "series_id", "run_id", "composition_revision",
            "body_sha256", "record_bindings", "status", "receipt_id", "receipt_sha256")}
        ledger = canonical_json({"task_id": before["task_id"], "series_id": before["series_id"],
                                 "messages": {"original-message": message}})
        update, = attach_ledger_reconciliation(self.history(), ledger)["ledger_reconciliation"]["updates"]
        self.assertEqual(update["status"], "smtp_accepted")
        self.assertEqual(update["previous_receipt"]["status"], "uncertain")
        self.assertEqual(update["previous_receipt"]["receipt_sha256"], before["receipt_sha256"])

    def test_other_series_not_projected(self):
        self.send()
        self.assertEqual(self.history("another-series")["entries"], [])

    def test_body_tampering_stays_unresolved(self):
        run = self.send()
        archive = self.settings.paths.run_archive_dir / run.task_id / run.run_id
        (archive / "email.html").write_text("different body")
        history = self.history()
        self.assertFalse(history["history_complete"])
        self.assertEqual(history["entries"], [])
        self.assertEqual(history["unresolved"][0]["run_id"], run.run_id)

    def test_smtp_proof_mismatch_stays_unresolved(self):
        run = self.send()
        handoff = self.app.delivery_repo.get_handoff_for_run(run.run_id)
        with self.app.run_repo.db.transaction() as conn:
            conn.execute("UPDATE smtp_attempts SET status='failed' WHERE handoff_id=?", (handoff.handoff_id,))
        history = self.history()
        self.assertFalse(history["history_complete"])
        self.assertEqual(history["entries"], [])

    def test_old_sealed_task_bootstrap_is_exact(self):
        path = self.settings.paths.tasks_dir / "software-releases/task.yaml"
        config = yaml.safe_load(path.read_text())
        del config["state"]["research_context"]
        path.write_text(yaml.safe_dump(config))
        md = path.with_name("task.md")
        md.write_text(md.read_text() + '\n```json\n' + json.dumps({"active_issuers": ["kb"],
            "test_reference_date": "2026-08-01", "test_series_id": "example-replay-series"}) + '\n```\n')
        old = register_fixture_task(self.app)
        with self.app.run_repo.db.transaction() as conn:
            conn.execute("UPDATE tasks SET delivery_mode='handoff' WHERE task_id=?", (old.task_id,))
        self.version = old
        self.send()
        # Projection is requested by a new, opt-in context; historical source is unchanged.
        task = replace(old.definition, state={"research_context": context_config()})
        self.version = replace(old, definition=task)
        entry, = self.history()["entries"]
        self.assertTrue(entry["bootstrap"])
        self.assertEqual(entry["status"], "smtp_accepted")

    def test_prepared_match_requires_all_identity_and_body_fields(self):
        self.send()
        entry = self.history()["entries"][0]
        message = {key: entry[key] for key in ["task_id", "series_id", "run_id", "composition_revision",
            "body_sha256", "record_bindings"]}
        self.assertTrue(matching_delivery_entry(message, entry))
        for key in message:
            candidate = deepcopy(message)
            candidate[key] = None
            self.assertFalse(matching_delivery_entry(candidate, entry), key)
        self.assertFalse(matching_delivery_entry(message, entry | {"status": "unresolved"}))

    def test_legacy_prepared_projection_matches_exact_hash_and_map(self):
        self.send()
        history = self.history()
        entry = history["entries"][0]
        # Real legacy field shape, populated only with this isolated fixture.
        entry["record_bindings"] = [{"record_id": "record-a", "product_key": "card:" + "a" * 64}]
        message = {"task_id": entry["task_id"], "test_series_id": entry["series_id"],
            "run_id": entry["run_id"], "composition_revision": entry["composition_revision"],
            "html_sha256": entry["body_sha256"]["html"], "text_sha256": entry["body_sha256"]["text"],
            "included_record_ids": ["record-a"], "product_keys": ["card:" + "a" * 64],
            "record_product_map": {"record-a": "card:" + "a" * 64}, "receipt_id": None, "status": "prepared"}
        ledger = {"task_id": entry["task_id"], "test_series_id": entry["series_id"],
            "messages": {"message-one": message}}
        before = canonical_json(ledger)
        projected = attach_ledger_reconciliation(deepcopy(history), before)
        reconciliation = projected["ledger_reconciliation"]
        self.assertFalse(reconciliation["applied"])
        self.assertEqual(reconciliation["updates"][0]["status"], "smtp_accepted")
        self.assertEqual(reconciliation["updates"][0]["receipt_id"], entry["receipt_id"])
        self.assertEqual(before, canonical_json(ledger))
        message["text_sha256"] = "b" * 64
        mismatch = attach_ledger_reconciliation(deepcopy(history), canonical_json(ledger))["ledger_reconciliation"]
        self.assertEqual(mismatch["updates"], [])
        self.assertEqual(mismatch["unresolved"][0]["reason"], "no_exact_verified_message")

    def test_ledger_already_applied_receipt_is_idempotent(self):
        self.send()
        history = self.history()
        entry = history["entries"][0]
        message = {key: entry[key] for key in ["task_id", "series_id", "run_id", "composition_revision",
            "body_sha256", "record_bindings", "receipt_id", "receipt_sha256", "status"]}
        ledger = {"task_id": entry["task_id"], "series_id": entry["series_id"], "messages": {"one": message}}
        result = attach_ledger_reconciliation(history, canonical_json(ledger))
        self.assertEqual(result["ledger_reconciliation"]["updates"], [])
        message["receipt_sha256"] = "b" * 64
        result = attach_ledger_reconciliation(history, canonical_json(ledger))
        self.assertEqual(result["ledger_reconciliation"]["updates"], [])
        self.assertEqual(result["ledger_reconciliation"]["unresolved"][0]["reason"], "previous_delivery_conflict")
        del message["receipt_sha256"]
        result = attach_ledger_reconciliation(history, canonical_json(ledger))
        self.assertEqual(result["ledger_reconciliation"]["updates"][0]["receipt_sha256"], entry["receipt_sha256"])

    def test_history_limit_includes_unresolved_diagnostics_and_reconciliation(self):
        history = {"schema_version": 1, "task_id": "test-task", "series_id": "series",
            "origin_run_id": "r", "parent_run_id": None, "entries": [], "history_complete": True,
            "unresolved": [{"reason": "x" * 1024} for _ in range(500)], "limits": {}}
        result = _bounded_history(history)
        self.assertLessEqual(len(canonical_json(result)), MAX_HISTORY_BYTES)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["history_complete"])

    def test_corrupt_handoff_does_not_hide_later_valid_message(self):
        first, second = self.send(), self.send()
        corrupt = self.app.delivery_repo.get_handoff_for_run(second.run_id).handoff_id
        original = self.app.delivery_repo.get_handoff
        def read(handoff_id):
            if handoff_id == corrupt:
                raise ValueError("synthetic malformed JSON with private content")
            return original(handoff_id)
        with patch.object(self.app.delivery_repo, "get_handoff", side_effect=read):
            history = self.history()
        self.assertEqual([entry["run_id"] for entry in history["entries"]], [first.run_id])
        self.assertFalse(history["history_complete"])
        self.assertNotIn("private content", json.dumps(history))

    def test_august_replay_seals_reconciliation_into_research_and_compose(self):
        path = self.settings.paths.tasks_dir / "software-releases/task.yaml"
        config = yaml.safe_load(path.read_text())
        config["state"]["research_context"]["reference_date"] = "2026-08-01"
        path.write_text(yaml.safe_dump(config))
        self.version = register_fixture_task(self.app)
        with self.app.run_repo.db.transaction() as conn:
            conn.execute("UPDATE tasks SET delivery_mode='handoff' WHERE task_id=?", (self.version.task_id,))
        first = self.send()
        entry = self.history()["entries"][0]
        message = {key: entry[key] for key in ["task_id", "series_id", "run_id", "composition_revision",
            "body_sha256", "record_bindings"]}
        message.update(status="prepared", receipt_id=None)
        ledger = {"task_id": first.task_id, "series_id": "example-replay-series", "messages": {"first": message}}
        project = self.app.workspace_mgr.get_task_workspace_dir(first.task_id) / "project"
        ledger_path = project / "state/test-series/example-replay-series/sent-products.json"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        original = canonical_json(ledger)
        ledger_path.write_bytes(original)
        config["state"]["research_context"]["reference_date"] = "2026-08-02"
        path.write_text(yaml.safe_dump(config))
        self.version = register_fixture_task(self.app)
        run = self.app.runs.enqueue_run(self.version.task_id)
        result = self.app.runs.execute_run(run.run_id, force_dry_run=True)
        self.assertEqual(result.status, "succeeded", result.error_message)
        comp = self.app.run_repo.get_composition_input(run.run_id)
        self.assertEqual(comp["research_context"]["start_date"], "2026-07-27")
        update = comp["delivery_history"]["ledger_reconciliation"]["updates"][0]
        self.assertEqual(update["run_id"], first.run_id)
        self.assertEqual(update["status"], "smtp_accepted")
        archive = self.settings.paths.run_archive_dir / run.task_id / run.run_id
        for stage in ["research", "compose"]:
            staged = json.loads((archive / "inputs" / stage / "delivery-history.json").read_text())
            self.assertEqual(staged, comp["delivery_history"])
        self.assertEqual(original, ledger_path.read_bytes())
