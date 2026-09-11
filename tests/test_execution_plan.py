"""Provider changes preserve sealed inputs, retry scope and command identity."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from researchops.engine.execution_plan import build_execution_plan, resolve_task_stages
from researchops.errors import ValidationError
from researchops.services.application import ApplicationService
from tests.support import fixture_runner, isolated_settings, register_fixture_task


def choice(provider, model=None, effort=None):
    return {"type": provider, "model": model, "reasoning_effort": effort}


class TestExecutionPlans(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = isolated_settings(Path(self.tmp.name))
        self.runner = fixture_runner(self.settings)
        self.app = ApplicationService(self.settings, custom_runner=self.runner)
        self.version = register_fixture_task(self.app)
        self.app.runs.model_catalog = Mock()
        self.app.runs.model_catalog.validate_stage.side_effect = lambda value, legacy=None: value

    def source(self):
        run = self.app.runs.enqueue_run(self.version.task_id, force_dry_run=True)
        result = self.app.runs.execute_run(run.run_id)
        self.assertEqual(result.status, "succeeded", result.error_message)
        return result

    def publish_stages(self, stages):
        runner = copy.deepcopy(self.version.definition.runner)
        runner["stages"] = stages
        definition = replace(self.version.definition, runner=runner)
        version = replace(self.version, definition=definition,
            version_hash=hashlib.sha256(json.dumps(stages, sort_keys=True).encode()).hexdigest())
        self.app.task_repo.save_version(version)
        self.app.task_repo.set_active_version(version.task_id, version.version_hash)
        return version

    def test_legacy_task_resolution_and_additive_database(self):
        stages = resolve_task_stages(self.version.definition)
        self.assertEqual(stages["research"], stages["compose"])
        source = self.source()
        before = source.to_dict()
        with self.app.run_repo.db.transaction() as conn:
            conn.execute("DELETE FROM run_execution_plans WHERE run_id=?", (source.run_id,))
        self.app.run_repo.db.init_schema()
        self.assertEqual(self.app.run_repo.get_run(source.run_id).to_dict(), before)
        self.assertEqual(self.app.runs.get_execution_plan(source.run_id)["stages"], stages)
        self.assertIsNone(self.app.run_repo.get_execution_plan(source.run_id))

    def test_mixed_providers_select_each_runner_and_context(self):
        stages = {"research": choice("antigravity_exec", "agy-test", "high"),
                  "compose": choice("codex_exec", "codex-test", "medium")}
        self.publish_stages(stages)
        self.app.orchestrator.custom_runner = None
        research_runner, compose_runner = fixture_runner(self.settings), fixture_runner(self.settings)
        with patch("researchops.engine.orchestrator.AntigravityRunner", return_value=research_runner) as agy, \
                patch("researchops.engine.orchestrator.CodexRunner", return_value=compose_runner) as codex, \
                patch.object(research_runner, "execute_research", wraps=research_runner.execute_research) as research, \
                patch.object(compose_runner, "execute_compose", wraps=compose_runner.execute_compose) as compose:
            result = self.source()
        agy.assert_called_once()
        codex.assert_called_once()
        self.assertEqual(research.call_args.kwargs["context"].model, "agy-test")
        self.assertEqual(compose.call_args.kwargs["context"].reasoning_effort, "medium")
        archive = self.settings.paths.run_archive_dir / result.task_id / result.run_id
        self.assertEqual(json.loads((archive / "execution-plan.json").read_text())["stages"], stages)

    def test_compose_override_keeps_version_input_and_date_after_task_change(self):
        source = self.source()
        old_input = self.app.run_repo.get_composition_input_record(source.run_id)
        changed = self.publish_stages({"research": choice("antigravity_exec"), "compose": choice("antigravity_exec")})
        self.assertNotEqual(changed.version_hash, source.task_version_hash)
        selected = choice("codex_exec", "codex-test", "high")
        child = self.app.runs.compose_only(source.run_id, execution_settings={"compose": selected})
        with patch.object(self.runner, "execute_research", side_effect=AssertionError("research must be reused")), \
                patch.object(self.runner, "execute_compose", wraps=self.runner.execute_compose) as compose:
            result = self.app.runs.execute_run(child.run_id)
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertEqual(child.task_version_hash, source.task_version_hash)
        self.assertEqual(child.local_date, source.local_date)
        self.assertEqual(self.app.run_repo.get_composition_input_record(source.run_id), old_input)
        self.assertEqual(compose.call_args.kwargs["context"].model, "codex-test")
        self.assertEqual(self.app.runs.show_run(child.run_id)["execution_settings"]["compose"], selected)

    def test_compose_retry_inherits_overrides_and_scope(self):
        source = self.source()
        child = self.app.runs.compose_only(source.run_id,
            execution_settings={"compose": choice("codex_exec", "first-model", "high")})
        self.runner.simulate_failure = True
        failed = self.app.runs.execute_run(child.run_id)
        self.assertEqual(failed.status, "failed")
        self.runner.simulate_failure = False
        retry = self.app.runs.retry_run(child.run_id)
        self.assertEqual(self.app.runs.get_execution_plan(retry.run_id)["scope"], "compose_only")
        with patch.object(self.runner, "execute_research", side_effect=AssertionError("no research")):
            result = self.app.runs.execute_run(retry.run_id)
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertEqual(self.app.runs.get_execution_plan(retry.run_id)["stages"]["compose"]["model"], "first-model")
        full = self.app.runs.retry_run(retry.run_id, scope="full")
        self.assertEqual(self.app.runs.get_execution_plan(full.run_id)["scope"], "full")
        self.assertIsNone(self.app.runs.get_execution_plan(full.run_id)["source_composition"])

    def test_recompose_failure_before_input_save_keeps_original_source(self):
        source = self.source()
        child = self.app.runs.compose_only(source.run_id)
        with patch.object(self.app.run_repo, "save_composition_input", side_effect=ValidationError("prepare failed")):
            result = self.app.runs.execute_run(child.run_id)
        self.assertEqual(result.status, "failed", result.error_message)
        self.assertIsNone(self.app.run_repo.get_composition_input(child.run_id))
        retry = self.app.runs.retry_run(child.run_id)
        plan = self.app.runs.get_execution_plan(retry.run_id)
        self.assertEqual(plan["source_composition"]["run_id"], source.run_id)
        self.assertEqual(retry.parent_run_id, child.run_id)
        self.assertEqual(self.app.run_repo.get_execution_controls(retry.run_id)["composition_revision"], 3)
        with patch.object(self.runner, "execute_research", side_effect=AssertionError("no research")):
            completed = self.app.runs.execute_run(retry.run_id)
        self.assertEqual(completed.status, "succeeded", completed.error_message)

    def test_parallel_same_command_and_changed_settings_conflict(self):
        source = self.source()
        selected = {"compose": choice("codex_exec", "first-model", "high")}
        def enqueue(_):
            return self.app.runs.compose_only(source.run_id, request_key="same-click", execution_settings=selected)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(enqueue, range(2))
        self.assertEqual(first.run_id, second.run_id)
        with self.assertRaises(ValidationError):
            self.app.runs.compose_only(source.run_id, request_key="same-click",
                execution_settings={"compose": choice("codex_exec", "another-model", "high")})
        self.assertEqual(len(self.app.run_repo.list_runs()), 2)

    def test_compose_only_rejects_research_changes_and_catalog_invalid_selection(self):
        source = self.source()
        with self.assertRaises(ValidationError):
            self.app.runs.compose_only(source.run_id, execution_settings={"research": choice("codex_exec")})
        self.app.runs.model_catalog.validate_stage.side_effect = ValueError("unsupported_model")
        with self.assertRaisesRegex(ValidationError, "unsupported_model"):
            self.app.runs.compose_only(source.run_id, execution_settings={"compose": choice("codex_exec", "invalid")})
        self.assertEqual(len(self.app.run_repo.list_runs()), 1)

    def test_input_tampering_blocks_worker_and_plan_tampering_is_detected(self):
        source = self.source()
        child = self.app.runs.compose_only(source.run_id)
        archive = self.settings.paths.run_archive_dir / source.task_id / source.run_id
        (archive / "composition-input.json").write_text("{}")
        with patch.object(self.runner, "execute_compose", side_effect=AssertionError("must not compose")):
            result = self.app.runs.execute_run(child.run_id)
        self.assertEqual(result.status, "failed")
        self.assertIn("hash mismatch", result.error_message)
        with self.app.run_repo.db.transaction() as conn:
            conn.execute("UPDATE run_execution_plans SET plan_sha256=? WHERE run_id=?", ("0" * 64, child.run_id))
        with self.assertRaisesRegex(ValidationError, "integrity mismatch"):
            self.app.runs.get_execution_plan(child.run_id)

    def test_task_snapshot_selection_never_changes_input_version(self):
        source = self.source()
        selected = choice("codex_exec", "retired-model", "high")
        version = self.publish_stages({"research": choice("antigravity_exec"), "compose": selected})
        child = self.app.runs.compose_only(source.run_id, execution_settings={"compose": selected},
            selection_source={"kind": "task_version", "task_version_hash": version.version_hash})
        self.app.runs.model_catalog.validate_stage.assert_called_once_with(selected, legacy=selected)
        self.assertEqual(child.task_version_hash, source.task_version_hash)
        self.assertEqual(self.app.runs.get_execution_plan(child.run_id)["selection_source"]["task_version_hash"], version.version_hash)

    def test_invalid_source_and_unknown_settings_fields_are_rejected(self):
        stages = resolve_task_stages(self.version.definition)
        with self.assertRaises(ValidationError):
            build_execution_plan(stages, scope="compose_only", source_composition={"run_id": "x"})
        stages["compose"]["timeout_seconds"] = 20
        with self.assertRaises(ValidationError):
            build_execution_plan(stages)

    def test_execution_plan_failure_rolls_back_run_and_command(self):
        source = self.source()
        with patch.object(self.app.run_repo, "insert_execution_plan", side_effect=ValidationError("invalid plan")):
            with self.assertRaises(ValidationError):
                self.app.runs.compose_only(source.run_id, request_key="atomic-command")
        self.assertEqual(len(self.app.run_repo.list_runs()), 1)
        child = self.app.runs.compose_only(source.run_id, request_key="atomic-command")
        self.assertIsNotNone(self.app.run_repo.get_execution_plan(child.run_id))

    def test_source_must_belong_to_the_actual_retry_ancestry(self):
        from researchops.engine.execution_plan import encode_execution_plan
        first, unrelated = self.source(), self.source()
        child = self.app.runs.compose_only(first.run_id)
        plan = self.app.runs.get_execution_plan(child.run_id)
        plan["source_composition"] = self.app.runs._source_reference(
            self.app.run_repo.get_composition_input_record(unrelated.run_id))
        raw, digest = encode_execution_plan(plan)
        with self.app.run_repo.db.transaction() as conn:
            conn.execute("UPDATE run_execution_plans SET plan_json=?,plan_sha256=? WHERE run_id=?",
                         (raw, digest, child.run_id))
        with patch.object(self.runner, "execute_compose", side_effect=AssertionError("no unrelated source")):
            result = self.app.runs.execute_run(child.run_id)
        self.assertEqual(result.status, "failed", result.error_message)
        self.assertIn("ancestry", result.error_message)

    def test_same_command_replays_when_its_selected_model_leaves_the_catalog(self):
        source = self.source()
        selected = {"compose": choice("codex_exec", "retired-later", "high")}
        first = self.app.runs.compose_only(source.run_id, request_key="catalog-refresh", execution_settings=selected)
        self.app.runs.model_catalog.validate_stage.side_effect = AssertionError("do not revalidate accepted command")
        replay = self.app.runs.compose_only(source.run_id, request_key="catalog-refresh", execution_settings=selected)
        self.assertEqual(first.run_id, replay.run_id)
        with self.assertRaises(ValidationError):
            self.app.runs.compose_only(source.run_id, request_key="catalog-refresh",
                execution_settings={"compose": choice("codex_exec", "different-model", "high")})


if __name__ == "__main__":
    unittest.main()
