"""Research acquisition lifecycle at the native runner boundary; no model calls."""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from researchops.errors import HardGateError
from researchops.runners.antigravity import AntigravityRunner
from researchops.runners.base import RunnerInvocationContext
from researchops.runners.codex import CodexRunner
from researchops.runners.production import _import, _prompt
from researchops.runners.result_submission import HELPER_DIRECTORY
from tests.test_production_runner import response
from tests.test_runner_tool_events import agy_step


PROVIDERS = (("codex_exec", CodexRunner), ("antigravity_exec", AntigravityRunner))


class SessionDouble:
    def __init__(self, *, start_error=False, close_error=False, cleanup=True, fatal=False):
        self.start_error, self.close_error = start_error, close_error
        self.cleanup, self.fatal = cleanup, fatal
        self.cleanup_verified = False
        self.starts = self.closes = self.validations = 0
        self.work = None

    def __repr__(self):
        return "SYNTHETIC_APP_ONLY_CREDENTIAL"

    def start(self, work):
        self.starts += 1
        self.work = work
        assert (work / HELPER_DIRECTORY / "researchops/runners/result_submission.py").is_file()
        if self.start_error:
            raise RuntimeError("SYNTHETIC_APP_ONLY_CREDENTIAL")
        return self

    def close(self):
        self.closes += 1
        if self.close_error:
            raise RuntimeError("SYNTHETIC_APP_ONLY_CREDENTIAL")
        self.cleanup_verified = self.cleanup
        return self.cleanup_verified

    def raise_if_failed(self):
        self.validations += 1
        if self.fatal:
            raise HardGateError("Research acquisition IPC integrity failed")


class ResearchAcquisitionRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        environment = patch.dict(os.environ, {"HOME": str(self.home), "CODEX_HOME": str(self.home / ".codex")})
        environment.start()
        self.addCleanup(environment.stop)
        self.case_number = 0

    def case(self, session, *, stage="research", network="public-research"):
        self.case_number += 1
        root = self.root / str(self.case_number)
        paths = [root / name for name in ("input", "tmp", "output", "project")]
        for path in paths:
            path.mkdir(parents=True)
        (paths[0] / "task.md").write_text("Read a synthetic source and create the requested local image.")
        if stage == "compose":
            (paths[0] / "composition-input.json").write_text("{}")
        context = RunnerInvocationContext("task", "run", 1, stage,
            network_profile=network, acquisition_session=session, local_date="2026-09-11")
        return paths, context

    def execute(self, runner, paths, context, process):
        kwargs = {"side_effect": process} if callable(process) else {"return_value": process}
        with patch("shutil.which", return_value="/usr/bin/synthetic-model"), patch(
                "researchops.runners.production.run_bounded", **kwargs) as bounded:
            result = getattr(runner, "execute_" + context.invocation_stage)(*paths, context)
        return result, bounded

    def test_both_providers_close_and_validate_before_import(self):
        for provider, runner in PROVIDERS:
            with self.subTest(provider=provider):
                session = SessionDouble()
                paths, context = self.case(session)

                def checked_import(*args):
                    self.assertEqual(session.closes, 1)
                    self.assertEqual(session.validations, 1)
                    self.assertTrue(session.cleanup_verified)
                    return _import(*args)

                with patch("researchops.runners.production._import", side_effect=checked_import):
                    result, bounded = self.execute(runner(), paths, context, response(provider, {"records": []}))
                self.assertTrue(result.success, result.error_message)
                self.assertTrue(result.cleanup_verified)
                self.assertEqual((session.starts, session.closes), (1, 1))
                bounded.assert_called_once()
                self.assertEqual(result.isolation["research_acquisition"],
                    {"enabled": True, "started": True, "cleanup_verified": True})

    def test_session_credentials_never_enter_context_prompt_or_events(self):
        session = SessionDouble()
        paths, context = self.case(session)
        prompt = _prompt(paths[0], paths[3], paths[1], context)
        for text in (repr(context), prompt):
            self.assertNotIn("SYNTHETIC_APP_ONLY_CREDENTIAL", text)
        for instruction in ("Optional file access during Research", "acquire_file(source",
                            "Keep the original source descriptor", "do not write bytes to its final declared path",
                            "derived_from=[acquired['acquisition_id']]", "source=null"):
            self.assertIn(instruction, prompt)
        result, _ = self.execute(CodexRunner(), paths, context, response("codex_exec", {"records": []}))
        self.assertNotIn("SYNTHETIC_APP_ONLY_CREDENTIAL", json.dumps([result.events, result.isolation]))

    def test_compose_and_offline_research_reject_session_before_native_spawn(self):
        for provider, runner in PROVIDERS:
            for stage, network in (("compose", "none"), ("compose", "public-research"), ("research", "none")):
                with self.subTest(provider=provider, stage=stage, network=network):
                    session = SessionDouble()
                    paths, context = self.case(session, stage=stage, network=network)
                    self.assertNotIn("acquire_file(", _prompt(paths[0], paths[3], paths[1], context))
                    result, bounded = self.execute(runner(), paths, context, None)
                    bounded.assert_not_called()
                    self.assertFalse(result.success)
                    self.assertTrue(result.cleanup_verified)
                    self.assertEqual((session.starts, session.closes), (0, 1))
                    self.assertEqual(list(paths[2].iterdir()), [])

    def test_absent_session_keeps_helper_out_of_prompt(self):
        paths, context = self.case(None)
        self.assertNotIn("acquire_file(", _prompt(paths[0], paths[3], paths[1], context))

    def test_start_failure_closes_and_redacts_app_exception(self):
        session = SessionDouble(start_error=True)
        paths, context = self.case(session)
        result, bounded = self.execute(CodexRunner(), paths, context, None)
        bounded.assert_not_called()
        self.assertFalse(result.success)
        self.assertTrue(result.cleanup_verified)
        self.assertEqual(session.closes, 1)
        self.assertEqual(result.error_message, "Research file acquisition could not be started")
        self.assertNotIn("SYNTHETIC_APP_ONLY_CREDENTIAL", json.dumps([result.events, result.isolation]))

    def test_native_spawn_exception_closes_session(self):
        session = SessionDouble()
        paths, context = self.case(session)

        def failed_spawn(*args, **kwargs):
            raise OSError("Synthetic native spawn failure")

        result, _ = self.execute(CodexRunner(), paths, context, failed_spawn)
        self.assertFalse(result.success)
        self.assertTrue(result.cleanup_verified)
        self.assertEqual(session.closes, 1)
        self.assertIn("Synthetic native spawn failure", result.error_message)

    def test_cleanup_failure_never_imports_and_preserves_native_terminal(self):
        for provider, runner in PROVIDERS:
            for raises in (False, True):
                with self.subTest(provider=provider, raises=raises):
                    session = SessionDouble(cleanup=False, close_error=raises)
                    paths, context = self.case(session)
                    with patch("researchops.runners.production._import") as importer:
                        result, _ = self.execute(runner(), paths, context, response(provider, {"records": []}))
                    importer.assert_not_called()
                    self.assertFalse(result.success)
                    self.assertFalse(result.cleanup_verified)
                    self.assertEqual(session.closes, 1)
                    self.assertIn("cleanup could not be verified", result.error_message)
                    self.assertTrue(result.events[0]["terminal"])
                    self.assertTrue(result.events[0]["successful_terminal"])
                    self.assertEqual(result.isolation["model_invocations"], 1)
                    self.assertNotIn("SYNTHETIC_APP_ONLY_CREDENTIAL", result.error_message)

    def test_session_integrity_failure_blocks_import_after_clean_close(self):
        for provider, runner in PROVIDERS:
            with self.subTest(provider=provider):
                session = SessionDouble(fatal=True)
                paths, context = self.case(session)
                with patch("researchops.runners.production._import") as importer:
                    result, _ = self.execute(runner(), paths, context, response(provider, {"records": []}))
                importer.assert_not_called()
                self.assertFalse(result.success)
                self.assertTrue(result.cleanup_verified)
                self.assertEqual((session.closes, session.validations), (1, 1))
                self.assertTrue(result.events[0]["terminal"])
                self.assertEqual(result.error_message, "Research acquisition IPC integrity failed")

    def test_timeout_and_cancellation_preserve_partial_tools_and_close(self):
        for provider, runner in PROVIDERS:
            for cancelled in (False, True):
                with self.subTest(provider=provider, cancelled=cancelled):
                    session = SessionDouble()
                    paths, context = self.case(session)
                    if provider == "codex_exec":
                        tool = {"id": "read", "type": "command_execution", "command": "read synthetic file",
                                "status": "completed", "exit_code": 0}
                        process = response(provider, {"records": []}, [tool])
                        lines = process.stdout.splitlines()[:3]
                    else:
                        process = response(provider, {"records": []})
                        lines = [process.stdout.splitlines()[0], json.dumps(agy_step(1, "run_command")).encode()]
                    process = replace(process, stdout=b"\n".join(lines) + b"\n", exit_code=130 if cancelled else 124,
                                      error="cancelled" if cancelled else "timed out", cancelled=cancelled, timed_out=not cancelled)
                    result, _ = self.execute(runner(), paths, context, process)
                    self.assertFalse(result.success)
                    self.assertEqual(result.exit_code, 130 if cancelled else 124)
                    self.assertEqual(session.closes, 1)
                    self.assertFalse(result.events[0]["terminal"])
                    self.assertEqual(len(result.events[0]["tools"]), 1)
                    self.assertTrue(result.isolation["research_acquisition"]["cleanup_verified"])
                    # Agy still needs its independent remote-turn completion proof.
                    self.assertEqual(result.cleanup_verified, provider == "codex_exec")

    def test_cache_files_cannot_be_declared_as_local_artifacts(self):
        session = SessionDouble()
        paths, context = self.case(session)

        def process(*args, **kwargs):
            file = session.work / ".researchops-files/files/private.pdf"
            file.parent.mkdir(parents=True)
            file.write_bytes(b"synthetic cache file")
            return response("codex_exec", {"records": [], "artifacts": [
                {"path": ".researchops-files/files/private.pdf", "role": "evidence", "scope": "run"}]})

        result, _ = self.execute(CodexRunner(), paths, context, process)
        self.assertFalse(result.success)
        self.assertIn("reserved artifact path", result.error_message)
        self.assertTrue(result.cleanup_verified)
        self.assertEqual(list(paths[2].iterdir()), [])

    def test_both_native_adapters_run_staged_helper_read_pdf_and_import_derived_png(self):
        from researchops.config import MediaProviderConfig
        from researchops.engine.research_acquisition import ResearchAcquisitionSession
        from researchops.runners.research_fetch import FetchResult
        from tests.support import isolated_settings
        from tests.test_artifact_acquirer import pdf_bytes

        raw = pdf_bytes("Synthetic source read by native task code")
        source = {"kind": "cardrag_pdf", "connection_id": "synthetic", "document_id": "doc_" + "1" * 64,
                  "issuer": "synthetic", "product_code": "fixture", "sha256": hashlib.sha256(raw).hexdigest(),
                  "size_bytes": len(raw)}
        worker_code = '''import hashlib, json, pathlib, struct, sys, zlib
work = pathlib.Path(sys.argv[1])
source = json.loads(sys.argv[2])
sys.path.insert(0, str(work / ".researchops-submit"))
from researchops.runners.file_acquisition import acquire_file
from researchops.runners.result_submission import submit_result
acquired = acquire_file(source, timeout_seconds=3)
assert acquired["status"] == "available", acquired
original = pathlib.Path(acquired["path"]).read_bytes()
assert original.startswith(b"%PDF-")
assert hashlib.sha256(original).hexdigest() == source["sha256"] == acquired["sha256"]
assert len(original) == source["size_bytes"] == acquired["size_bytes"]
def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
rgb = hashlib.sha256(original).digest()[:3]
png = bytes.fromhex("89504e470d0a1a0a") + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(bytes([0]) + rgb)) + chunk(b"IEND", b"")
(work / "derived").mkdir()
(work / "derived/source-digest.png").write_bytes(png)
document = {"status": "success", "records": [{"record_id": "r1"}], "artifacts": [
    {"path": "attachments/original.pdf", "role": "attachment", "mime_type": "application/pdf",
     "record_ids": ["r1"], "source": source},
    {"path": "derived/source-digest.png", "role": "inline_image", "mime_type": "image/png",
     "record_ids": ["r1"], "source": None, "derived_from": [acquired["acquisition_id"]]}]}
assert not (work / "attachments/original.pdf").exists()
print(submit_result(document, "research", work / ".researchops-submission"))
'''
        for provider, runner in PROVIDERS:
            with self.subTest(provider=provider):
                paths, context = self.case(None)
                settings = isolated_settings(paths[0].parent / "app")
                secret_path = paths[0].parent / "app" / "SYNTHETIC_APP_ONLY_CREDENTIAL"
                settings.media.providers["synthetic"] = MediaProviderConfig("http://127.0.0.1:9", secret_path)
                session = ResearchAcquisitionSession(settings, run_id="run", task_id="task", attempt=1,
                    fencing_token="synthetic-fence", storage_root=paths[0].parent / "protected-originals")
                self.addCleanup(session.close)
                session.pdf_fetcher = Mock()
                session.pdf_fetcher.fetch.return_value = FetchResult(200, "synthetic-only",
                    {"content-type": "application/pdf"}, raw, {})
                context.acquisition_session = session
                context.artifact_connection_ids = ["synthetic"]

                def fake_native(argv, **kwargs):
                    work = Path(argv[argv.index("--add-dir") + 1])
                    self.assertFalse(session.cleanup_verified)
                    self.assertNotIn("SYNTHETIC_APP_ONLY_CREDENTIAL", json.dumps(argv))
                    self.assertNotIn(b"SYNTHETIC_APP_ONLY_CREDENTIAL", kwargs["stdin"] or b"")
                    worker = subprocess.run([sys.executable, "-I", "-c", worker_code, str(work), json.dumps(source)],
                        cwd=paths[3], env={"PATH": os.defpath}, capture_output=True, timeout=10)
                    self.assertEqual(worker.returncode, 0, worker.stderr.decode())
                    envelope = json.loads(worker.stdout)
                    process = response(provider, {})
                    events = [json.loads(line) for line in process.stdout.splitlines()]
                    if provider == "codex_exec":
                        events[-2]["item"]["text"] = json.dumps(envelope)
                    else:
                        events[-1]["result"]["structured_output"] = envelope
                    return replace(process, stdout=("\n".join(json.dumps(event) for event in events) + "\n").encode())

                def checked_import(*args):
                    self.assertTrue(session.cleanup_verified)
                    return _import(*args)

                with patch("researchops.runners.production._import", side_effect=checked_import):
                    result, bounded = self.execute(runner(), paths, context, fake_native)
                self.assertTrue(result.success, result.error_message)
                self.assertTrue(result.cleanup_verified)
                bounded.assert_called_once()
                session.pdf_fetcher.fetch.assert_called_once()
                self.assertFalse(secret_path.exists())
                document = json.loads((paths[2] / "result.json").read_bytes())
                self.assertEqual(document["artifacts"][0]["source"], source)
                self.assertFalse((paths[2] / "attachments/original.pdf").exists())
                self.assertEqual((paths[2] / "derived/source-digest.png").read_bytes(),
                    (session.work_dir / "derived/source-digest.png").read_bytes())
                acquisition_id, = document["artifacts"][1]["derived_from"]
                self.assertRegex(acquisition_id, r"^acq-[a-f0-9]{32}$")
                self.assertEqual((session.storage_root / "originals" / (acquisition_id + ".pdf")).read_bytes(), raw)
                self.assertEqual(session.consumed_count, 1)
                self.assertEqual(result.isolation["response_submission"]["transport_version"], 2)


if __name__ == "__main__":
    unittest.main()
