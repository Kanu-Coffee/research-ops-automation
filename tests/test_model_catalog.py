"""Dropdown catalog contracts without credentials, model turns or external I/O."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from researchops.errors import ValidationError
from researchops.services.model_catalog import (
    ModelCatalogService, _codex_models, normalize_agy_models, normalize_codex_models,
)


CODEX_MODELS = [{"model": "unit-model", "displayName": "Unit Model", "hidden": False,
    "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [
        {"reasoningEffort": name, "description": "not copied"} for name in ("low", "medium", "high", "max", "ultra")]}]
AGY_MODELS = b"Fetching available models...\ngemini-test-high\tGemini Test (High)\ngemini-test-low\tGemini Test (Low)\nclaude-test\tClaude Test\ngpt-test-medium\tGPT Test (Medium)\n"


def settings(root, environment="development"):
    return SimpleNamespace(environment=environment, paths=SimpleNamespace(data_dir=root),
        runner=SimpleNamespace(codex_binary="codex", antigravity_binary="agy"))


def install_catalog(service):
    service._cache = {"version": 1, "checked_at": datetime.now(timezone.utc).isoformat(), "providers": [
        {"type": kind, "label": label, "status": "ready", "source": source,
         "fetched_at": datetime.now(timezone.utc).isoformat(), "models": models, "error_code": None}
        for kind, label, source, models in (
            ("codex_exec", "Codex", "codex_model_list", normalize_codex_models(CODEX_MODELS)),
            ("antigravity_exec", "Antigravity (Agy)", "agy_models", normalize_agy_models(AGY_MODELS)))]}


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = ModelCatalogService(settings(self.root))
        install_catalog(self.service)

    def test_exact_codex_efforts_are_advertised_and_hidden_models_excluded(self):
        hidden = {**CODEX_MODELS[0], "hidden": True, "model": "internal-agent"}
        models = normalize_codex_models([*CODEX_MODELS, hidden])
        self.assertEqual(len(models), 1)
        self.assertEqual([e["value"] for e in models[0]["efforts"]], ["", "low", "medium", "high", "max", "ultra"])
        for effort in (None, "max", "ultra"):
            stage = {"type": "codex_exec", "model": "unit-model", "reasoning_effort": effort}
            self.assertEqual(self.service.validate_stage(stage), stage)
        with self.assertRaises(ValidationError):
            self.service.validate_stage({"type": "codex_exec", "model": "unit-model", "reasoning_effort": "minimal"})

    def test_agy_groups_only_real_variants_and_persists_exact_id(self):
        values = normalize_agy_models(AGY_MODELS)
        self.assertEqual([m["id"] for m in values], ["gemini-test", "claude-test", "gpt-test-medium"])
        self.assertEqual([e["value"] for e in values[0]["efforts"]], ["low", "high"])
        self.assertEqual(values[1]["efforts"], [{"value": "", "label": "모델 기본값", "model": "claude-test"}])
        self.assertEqual(self.service.validate_stage({"type": "antigravity_exec", "model": "gemini-test", "reasoning_effort": "high"}),
                         {"type": "antigravity_exec", "model": "gemini-test-high", "reasoning_effort": None})
        with self.assertRaises(ValidationError):
            self.service.validate_stage({"type": "antigravity_exec", "model": "gemini-test", "reasoning_effort": "medium"})
        with self.assertRaises(ValidationError):
            self.service.validate_stage({"type": "antigravity_exec", "model": "claude-test", "reasoning_effort": "high"})

    def test_persisted_agy_exact_variant_round_trips(self):
        stage = {"type": "antigravity_exec", "model": "gemini-test-low", "reasoning_effort": None}
        self.assertEqual(self.service.validate_stage(stage), stage)

    def test_no_provider_model_cross_contamination(self):
        with self.assertRaises(ValidationError):
            self.service.validate_stage({"type": "codex_exec", "model": "gemini-test-high", "reasoning_effort": None})

    def test_legacy_unknown_and_default_need_no_discovery(self):
        service = ModelCatalogService()
        legacy = {"type": "codex_exec", "model": "retired-model", "reasoning_effort": "future-effort"}
        with patch.object(service, "snapshot", side_effect=AssertionError("unexpected discovery")):
            self.assertEqual(service.validate_stage(legacy, legacy=legacy), legacy)
            self.assertEqual(service.validate_stage({"type": "codex_exec"}),
                             {"type": "codex_exec", "model": None, "reasoning_effort": None})
        with self.assertRaises(ValidationError):
            service.validate_stage({**legacy, "reasoning_effort": "low"}, legacy=legacy)

    def test_invalid_fields_use_safe_domain_errors(self):
        cases = [{"type": "codex_exec", "model": False}, {"type": "codex_exec", "model": "--secret"},
            {"type": "codex_exec", "model": "secret\nvalue"}, {"type": "codex_exec", "reasoning_effort": "high"},
            {"type": "codex_exec", "executable_path": "/tmp/untrusted"}, {"type": "invalid"}]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValidationError) as raised:
                self.service.validate_stage(case)
            self.assertNotIn("secret", str(raised.exception))

    def test_development_render_never_runs_installed_clis(self):
        with patch("subprocess.Popen", side_effect=AssertionError("unexpected CLI")):
            result = ModelCatalogService(settings(self.root)).snapshot(refresh=True)
        self.assertTrue(result["stale"])
        self.assertFalse((self.root / "model-catalog").exists())

    def test_cache_ttl_failure_preserves_last_valid_and_is_bounded_safe_metadata(self):
        service = ModelCatalogService(settings(self.root, "production"))
        install_catalog(service)
        with patch.object(service, "_discover", side_effect=AssertionError("fresh cache")):
            service.snapshot()
        service._cache["checked_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with patch.object(service, "_discover", side_effect=ValueError("SECRET-CREDENTIAL")) as discover:
            result = service.snapshot(refresh=True)
        self.assertEqual(discover.call_count, 2)
        self.assertEqual(result["providers"][0]["status"], "stale")
        self.assertEqual(result["providers"][0]["models"][0]["id"], "unit-model")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual((self.root / "model-catalog/snapshot.json").stat().st_mode & 0o777, 0o600)
        restored = ModelCatalogService(settings(self.root)).snapshot()
        self.assertEqual(restored["providers"], result["providers"])
        with patch.object(service, "_discover", side_effect=AssertionError("retry cooldown")):
            service.snapshot()

    def test_refresh_updates_catalog_without_overwriting_returned_snapshots(self):
        service = ModelCatalogService(settings(self.root, "production"))
        install_catalog(service)
        before = service.snapshot()
        def discover(provider):
            return (normalize_codex_models([{**CODEX_MODELS[0], "model": "new-model"}]), "codex_model_list", datetime.now(timezone.utc).isoformat()) if provider == "codex_exec" else (normalize_agy_models(AGY_MODELS), "agy_models", datetime.now(timezone.utc).isoformat())
        with patch.object(service, "_discover", side_effect=discover):
            after = service.snapshot(refresh=True)
        self.assertNotEqual(before["revision"], after["revision"])
        self.assertEqual(before["providers"][0]["models"][0]["id"], "unit-model")

    def test_unsafe_cache_symlink_and_unrecognized_fields_are_not_exposed(self):
        private = self.root / "private"
        private.write_text('SECRET')
        cache = self.root / "model-catalog"
        cache.mkdir()
        (cache / "snapshot.json").symlink_to(private)
        result = ModelCatalogService(settings(self.root)).snapshot()
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(result["providers"][0]["models"], [])

    def test_cli_model_metadata_rejects_malformed_duplicate_or_oversized_ids(self):
        for raw in (b"not TSV\n", b"--option\tBad\n", b"a\tA\na\tA\n", b"a\tunsafe\x00value\n"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_agy_models(raw)
        for values in ([CODEX_MODELS[0], CODEX_MODELS[0]], [{**CODEX_MODELS[0], "supportedReasoningEfforts": [{"reasoningEffort": "--bad"}]}]):
            with self.assertRaises(ValueError):
                normalize_codex_models(values)

    def test_codex_metadata_rpc_only_initializes_and_paginates_without_turns(self):
        binary = self.root / "codex-double"
        binary.write_text("#!/usr/bin/env python3\n" + '''import json,sys
for line in sys.stdin:
    message=json.loads(line)
    method=message['method']
    assert method in ('initialize','initialized','model/list'),method
    if method=='initialize':
        print(json.dumps({'id':message['id'],'result':{}}),flush=True)
    elif method=='model/list':
        cursor=message['params'].get('cursor')
        result={'data':''' + repr(CODEX_MODELS) + ''','nextCursor':None} if cursor else {'data':[],'nextCursor':'page-two'}
        print(json.dumps({'id':message['id'],'result':result}),flush=True)
''')
        binary.chmod(0o700)
        self.assertEqual(_codex_models(str(binary), self.root)[0]["id"], "unit-model")

    def test_metadata_cleanup_reaps_group_after_leader_exits(self):
        from researchops.runners.development_process import _group_quiescent
        binary = self.root / "codex-with-child"
        child_source = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('ready',flush=True);time.sleep(60)"
        binary.write_text("#!/usr/bin/env python3\n" + "import json,sys,os,subprocess\n" +
            "child=subprocess.Popen([sys.executable,'-c'," + repr(child_source) + "],stdout=subprocess.PIPE)\n" +
            "child.stdout.readline()\n" +
            "open('group-id','w').write(str(os.getpgrp()))\n" + '''for line in sys.stdin:
    message=json.loads(line)
    if message['method']=='initialize':
        print(json.dumps({'id':message['id'],'result':{}}),flush=True)
    elif message['method']=='model/list':
        print(json.dumps({'id':message['id'],'result':{'data':''' + repr(CODEX_MODELS) + ''','nextCursor':None}}),flush=True)
''')
        binary.chmod(0o700)
        self.assertEqual(_codex_models(str(binary), self.root)[0]["id"], "unit-model")
        self.assertTrue(_group_quiescent(int((self.root / "group-id").read_text())))


if __name__ == "__main__":
    unittest.main()
