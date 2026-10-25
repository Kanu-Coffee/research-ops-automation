"""Integration tests for worker brief -> app controller -> adapter -> Compose CID -> SMTP double.

Tests:
1. Worker/controller connection: worker calls generate_images() in staged work dir,
   ImageBroker handles request and delegates to adapter, returns images.
2. Disabled image generation (enabled=False): 0 calls made to adapter.
3. OpenCode worker with sanitized HOME: worker HOME has no credentials, but image
   generation succeeds via controller using controller's credentials.
4. Late result rejection after run completion: closed session rejects late request.
5. End-to-end identical bytes / CID / Compose / SMTP double verification.
6. Symlink attack prevention: pre-existing symlink in artifact root cannot overwrite victim file.
7. Transport redirect protection: ImageApiRedirectHandler blocks cross-origin / downgrade.
"""

import base64
from email import policy
from email.parser import BytesParser
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock
from urllib.request import Request

import tests.test_artifact_composition
from tests.delivery_fixtures import smtp_message_bytes

from researchops.runners.image_broker import ImageBroker
from researchops.runners.image_generation import generate_images
from researchops.services.image_generation import ImageGenerationService, ImageApiRedirectHandler

PNG = b"\x89PNG\r\n\x1a\nsynthetic-bytes-12345"


def fake_settings(root, **overrides):
    values = dict(
        runner=SimpleNamespace(codex_binary="codex", bailian_binary="bl", opencode_binary="opencode", cgroup_root=None),
        image_services=SimpleNamespace(
            openrouter_api_key_file=None,
            openrouter_base_url="https://openrouter.example/api/v1",
            openrouter_timeout_seconds=5,
            max_image_bytes=1_000_000,
        )
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class ImageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.briefs = [{"filename": "sample.png", "prompt": "Draw a chart"}]

    def test_worker_brief_to_controller_broker_codex(self):
        """Worker brief is brokered to controller and calls ImageGenerationService."""
        cfg = fake_settings(self.root)
        broker = ImageBroker(self.work, {"enabled": True, "engine": "codex", "model": "explicit-model"}, cfg)
        broker.start()
        self.addCleanup(broker.close)

        fake_result = {
            "status": "available", "engine": "codex", "model": "explicit-model",
            "images": [{"path": str(self.work / "sample.png"), "mime_type": "image/png",
                        "size_bytes": len(PNG), "sha256": "abc"}]
        }
        with patch.object(ImageGenerationService, "generate", return_value=fake_result) as mock_gen:
            res = generate_images(self.briefs, artifact_root=self.work, timeout_seconds=5)

        self.assertEqual(res["status"], "available")
        self.assertEqual(res["engine"], "codex")
        self.assertEqual(res["model"], "explicit-model")
        mock_gen.assert_called_once()
        self.assertEqual(mock_gen.call_args.kwargs["model"], "explicit-model")
        self.assertEqual(mock_gen.call_args.kwargs["engine"], "codex")

    def test_disabled_image_generation_makes_zero_calls(self):
        """When enabled=False, broker is either not started or rejects, making 0 adapter calls."""
        cfg = fake_settings(self.root)
        broker = ImageBroker(self.work, {"enabled": False, "engine": "codex", "model": None}, cfg)
        broker.start()
        self.addCleanup(broker.close)

        with patch.object(ImageGenerationService, "generate") as mock_gen:
            res = generate_images(self.briefs, artifact_root=self.work, timeout_seconds=2)

        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["reason_code"], "image_generation_unavailable")
        mock_gen.assert_not_called()

    def test_opencode_sanitized_home_succeeds_via_controller(self):
        """Worker with sanitized HOME (no auth files) generates image via controller broker."""
        sanitized_home = self.root / "worker_home"
        sanitized_home.mkdir()
        # Ensure sanitized_home has no auth / config files
        self.assertEqual(list(sanitized_home.iterdir()), [])

        key_file = self.root / "controller.key"
        key_file.write_text("secret-controller-key")
        cfg = fake_settings(self.root, image_services=SimpleNamespace(
            openrouter_api_key_file=key_file,
            openrouter_base_url="https://openrouter.example/api/v1",
            openrouter_timeout_seconds=5,
            max_image_bytes=1_000_000
        ))

        broker = ImageBroker(self.work, {"enabled": True, "engine": "openrouter",
                                         "model": "black-forest-labs/flux-1.1-pro"}, cfg)
        broker.start()
        self.addCleanup(broker.close)

        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit):
                return json.dumps({
                    "data": [{"b64_json": base64.b64encode(PNG).decode(), "media_type": "image/png"}]
                }).encode("utf-8")

        # Simulate running in worker with sanitized HOME
        with patch.dict(os.environ, {"HOME": str(sanitized_home)}):
            with patch("researchops.services.image_generation._open_api_request", return_value=FakeResponse()):
                res = generate_images(self.briefs, artifact_root=self.work, timeout_seconds=5)

        self.assertEqual(res["status"], "available")
        self.assertEqual(res["engine"], "openrouter")
        self.assertEqual(res["model"], "black-forest-labs/flux-1.1-pro")
        self.assertTrue((self.work / "sample.png").is_file())
        self.assertEqual((self.work / "sample.png").read_bytes(), PNG)

    def test_late_result_rejected_after_close(self):
        """Requests sent after broker is closed are rejected and not executed."""
        cfg = fake_settings(self.root)
        broker = ImageBroker(self.work, {"enabled": True, "engine": "codex", "model": None}, cfg)
        broker.start()
        broker.close()

        with patch.object(ImageGenerationService, "generate") as mock_gen:
            res = generate_images(self.briefs, artifact_root=self.work, timeout_seconds=2)

        self.assertEqual(res["status"], "failed")
        mock_gen.assert_not_called()

    def test_cancellation_check_aborts_image_request(self):
        """When run is cancelled, broker aborts generation and returns cancelled status."""
        cfg = fake_settings(self.root)
        broker = ImageBroker(self.work, {"enabled": True, "engine": "codex", "model": None}, cfg,
                             cancellation_check=lambda: True)
        broker.start()
        self.addCleanup(broker.close)

        with patch.object(ImageGenerationService, "generate") as mock_gen:
            res = generate_images(self.briefs, artifact_root=self.work, timeout_seconds=3)

        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["reason_code"], "image_generation_cancelled")
        mock_gen.assert_not_called()

    def test_symlink_overwriting_prevented(self):
        """Pre-existing symlink in destination cannot overwrite victim outside artifact root."""
        victim = self.root / "victim_file"
        victim.write_bytes(b"untouchable content")
        (self.work / "sample.png").symlink_to(victim)

        key_file = self.root / "key"
        key_file.write_text("synthetic")
        cfg = fake_settings(self.root, image_services=SimpleNamespace(
            openrouter_api_key_file=key_file,
            openrouter_base_url="https://openrouter.example/api/v1",
            openrouter_timeout_seconds=5,
            max_image_bytes=1_000_000
        ))

        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit):
                return json.dumps({
                    "data": [{"b64_json": base64.b64encode(PNG).decode(), "media_type": "image/png"}]
                }).encode("utf-8")

        with patch("researchops.services.image_generation._open_api_request", return_value=FakeResponse()):
            service = ImageGenerationService(cfg)
            res = service.generate(self.briefs, engine="openrouter", model="vendor/model",
                                   artifact_root=self.work)

        self.assertEqual(res["status"], "failed")
        self.assertEqual(victim.read_bytes(), b"untouchable content")

    def test_redirect_handler_blocks_cross_origin_and_downgrade(self):
        """ImageApiRedirectHandler rejects any redirect for authenticated generation requests."""
        handler = ImageApiRedirectHandler()
        req = Request("https://openrouter.example/api/v1/images", data=b"{}",
                      headers={"Authorization": "Bearer secret"}, method="POST")

        redirect = handler.redirect_request(req, None, 302, "Found", {}, "http://different.example/target")
        self.assertIsNone(redirect)

        redirect_cross = handler.redirect_request(req, None, 307, "Temporary Redirect", {}, "https://evil.example/target")
        self.assertIsNone(redirect_cross)

    def test_worker_cannot_override_task_model(self):
        """ImageBroker enforces the Task's configured model; worker override is ignored."""
        cfg = fake_settings(self.root)
        broker = ImageBroker(self.work, {"enabled": True, "engine": "codex", "model": "default"}, cfg)
        broker.start()
        self.addCleanup(broker.close)

        with patch.object(ImageGenerationService, "generate",
                          return_value={"status": "available", "images": []}) as mock_gen:
            res = generate_images(self.briefs, artifact_root=self.work, timeout_seconds=2,
                                  model="worker-override")

        self.assertEqual(res["status"], "available")
        self.assertEqual(mock_gen.call_args.kwargs["model"], "default")

    def test_codex_controller_adapter_no_reentrancy(self):
        """Controller-side Codex adapter does not re-enter ImageBroker."""
        cfg = fake_settings(self.root)
        broker = ImageBroker(self.work, {"enabled": True, "engine": "codex", "model": "default"}, cfg)
        broker.start()
        self.addCleanup(broker.close)

        fake_proc = MagicMock()
        fake_proc.pid = 999999
        fake_proc.poll.return_value = 0
        fake_proc.returncode = 0
        fake_proc.stdin = MagicMock()
        fake_proc.wait.return_value = 0

        with patch("researchops.runners.image_generation.subprocess.Popen", return_value=fake_proc) as mock_popen, \
             patch("researchops.runners.image_generation._image", return_value={"path": "x.png", "mime_type": "image/png", "size_bytes": 10, "sha256": "abc"}):
            # Write a dummy file so target / name exists
            (self.work / "sample.png").write_bytes(PNG)
            res = generate_images([{"filename": "sample.png", "prompt": "test"}],
                                  artifact_root=self.work, timeout_seconds=2)

        self.assertEqual(mock_popen.call_count, 1)
        # There should be exactly 1 request file from the worker, not 2 from re-entrance
        self.assertEqual(len(list(broker._requests_dir.glob("*.json"))), 1)

    def test_ipc_response_symlink_does_not_overwrite_victim(self):
        """Response symlink to outside file is unlinked before write, victim is untouched."""
        cfg = fake_settings(self.root)
        victim = self.root / "outside_victim.txt"
        victim.write_text("original-content")

        broker = ImageBroker(self.work, {"enabled": True, "engine": "codex", "model": "default"}, cfg)
        broker.start()
        self.addCleanup(broker.close)

        # Place symlink in responses dir
        symlink_target = broker._responses_dir / "req123.json"
        symlink_target.symlink_to(victim)

        with patch.object(ImageGenerationService, "generate", return_value={"status": "available", "images": []}):
            tmp = broker._requests_dir / ".tmp_req123.json"
            tmp.write_text(json.dumps({"briefs": []}))
            os.replace(tmp, broker._requests_dir / "req123.json")

            deadline = time.monotonic() + 2
            while symlink_target.is_symlink() and time.monotonic() < deadline:
                time.sleep(0.02)

        self.assertEqual(victim.read_text(), "original-content")
        self.assertFalse(symlink_target.is_symlink())

    def test_unsupported_image_model_rejected(self):
        """Unlisted/invalid image models are rejected upfront by ImageGenerationService."""
        service = ImageGenerationService(fake_settings(self.root))
        res = service.generate(self.briefs, engine="codex", model="unlisted-invalid-model",
                               artifact_root=self.work)
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["reason_code"], "image_model_unsupported")

    def test_mcp_query_allows_release_updates_and_posts_but_denies_transfer(self):
        """MCP permissions allow read_release_updates and search_posts while denying get_and_transfer."""
        from researchops.runners.opencode import _permission_rules
        rules = _permission_rules(stage="research", network_profile="none", work=self.work,
                                  project_dir=self.root, input_dir=self.root, control_dir=self.root,
                                  mcp_servers=["docs"])
        def effect(action):
            out = None
            for r in rules:
                if fnmatch.fnmatchcase(action, r["action"]) and r["resource"] == "*":
                    out = r["effect"]
            return out

        self.assertEqual(effect("docs_read_release_updates"), "allow")
        self.assertEqual(effect("docs_search_posts"), "allow")
        self.assertEqual(effect("docs_get_and_transfer"), "deny")
        self.assertEqual(effect("docs_delete_record"), "deny")
        self.assertEqual(effect("docs_send_email"), "deny")

    def test_orchestrator_passes_application_settings_to_runners(self):
        """Orchestrator injects application settings into resolved runners."""
        from researchops.engine.orchestrator import Orchestrator
        cfg = fake_settings(self.root)
        host = SimpleNamespace(custom_runner=None, settings=cfg)
        runner_opencode = Orchestrator.resolve_runner(host, SimpleNamespace(runner={"type": "opencode_exec"}))
        self.assertIs(runner_opencode.settings, cfg)
        runner_codex = Orchestrator.resolve_runner(host, SimpleNamespace(runner={"type": "codex_exec"}))
        self.assertIs(runner_codex.settings, cfg)


class EndToEndImageDeliveryTests(tests.test_artifact_composition.TestArtifactComposition):
    def test_real_broker_receipt_to_new_cid_and_smtp(self):
        """Only the provider transport is doubled; broker, service and mail are real."""
        from dataclasses import replace
        from researchops.runners.development_process import ProcessResult
        self.task = replace(self.task, image_generation={
            "enabled": True, "engine": "bailian", "model": "wanx-v1"})
        validate = self.validator.validate_composition
        def compose_with_image(stage, comp_input, task, *args, **kwargs):
            work = self.root / "image-worker"
            work.mkdir()
            raw = (stage / "card.png").read_bytes()
            def transport(argv, *, cwd, **options):
                self.assertIn("wanx-v1", argv)
                (Path(cwd) / "newcard.png").write_bytes(raw)
                return ProcessResult(0, b"", b"", True, True)
            broker = ImageBroker(work, task.image_generation, fake_settings(self.root)).start()
            self.assertEqual(broker.invocations, 0)
            try:
                with patch("researchops.services.image_generation.shutil.which", return_value="/synthetic/bl"), \
                     patch("researchops.services.image_generation.run_bounded", side_effect=transport):
                    result = generate_images([{"filename": "newcard.png", "prompt": "Synthetic chart"}],
                                             artifact_root=work, timeout_seconds=5)
                    self.assertEqual(result["status"], "available", result)
                    self.assertTrue(broker.close())
                self.assertEqual(broker.invocations, 1)
                receipt = broker.snapshot()
                self.assertFalse(receipt["jobs"][0]["images"][0]["model_verified"])
                (stage / "newcard.png").write_bytes((work / "newcard.png").read_bytes())
                html = stage / "email.html"
                html.write_bytes(html.read_bytes().replace(b"</section>", b'<img src="cid:newcard"></section>', 1))
                return validate(stage, comp_input, task, *args, image_ledger=receipt, **kwargs)
            finally:
                broker.close()
        with patch.object(self.validator, "validate_composition", side_effect=compose_with_image):
            self.configure(inline=True)
        handoff = self.publish()
        expected = (self.stage / "newcard.png").read_bytes()
        (self.archive / "newcard.png").write_bytes(expected)
        server = self.smtp_server()
        with patch("smtplib.SMTP", return_value=server):
            ok, _, detail = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok, detail)
        message = BytesParser(policy=policy.default).parsebytes(smtp_message_bytes(server))
        image = next(p for p in message.walk() if str(p["Content-ID"]) == "<newcard>")
        self.assertEqual(image.get_payload(decode=True), expected)

    def test_consistent_worker_ledger_is_not_controller_evidence(self):
        from dataclasses import replace
        from researchops.errors import ValidationError
        self.task = replace(self.task, image_generation={
            "enabled": True, "engine": "bailian", "model": "wanx-v1"})
        validate = self.validator.validate_composition
        def forged(stage, comp_input, task, *args, **kwargs):
            raw = (stage / "card.png").read_bytes()
            (stage / "forged.png").write_bytes(raw)
            (stage / ".image-ledger.json").write_text(json.dumps({
                "schema_version": 1, "session_id": "foreign", "engine": "bailian",
                "model": "wanx-v1", "status": "available", "jobs": [{
                    "status": "available", "engine": "bailian", "model": "wanx-v1", "images": [{
                        "filename": "forged.png", "sha256": hashlib.sha256(raw).hexdigest(),
                        "size_bytes": len(raw), "mime_type": "image/png"}]}]}))
            html = stage / "email.html"
            html.write_bytes(html.read_bytes().replace(b"</section>", b'<img src="cid:forged"></section>', 1))
            return validate(stage, comp_input, task, *args, **kwargs)
        with patch.object(self.validator, "validate_composition", side_effect=forged):
            with self.assertRaises(ValidationError):
                self.configure(inline=True)

    def test_disabled_image_generation_rejects_unregistered_compose_image(self):
        """When image_generation.enabled=False, unregistered compose CID images are rejected."""
        from dataclasses import replace
        from researchops.errors import ValidationError
        self.task = replace(self.task, image_generation={"enabled": False, "engine": "bailian", "model": "wanx-v1"})
        validate = self.validator.validate_composition
        def add_unregistered(stage, comp_input, task, *args, **kwargs):
            (stage / "unregistered.png").write_bytes((stage / "card.png").read_bytes())
            html = (stage / "email.html").read_bytes().replace(
                b"</section>", b'<img src="cid:unregistered"></section>', 1)
            (stage / "email.html").write_bytes(html)
            return validate(stage, comp_input, task, *args, **kwargs)

        with patch.object(self.validator, "validate_composition", side_effect=add_unregistered):
            with self.assertRaises(ValidationError) as ctx:
                self.configure(inline=True)
            self.assertIn("HTML CID references must match declared inline artifacts", ctx.exception.errors)

    def test_enabled_task_without_image_job_ledger_rejects_unregistered_compose_image(self):
        """When image_generation.enabled=True but image job ledger is absent, unregistered images are rejected."""
        from dataclasses import replace
        from researchops.errors import ValidationError
        self.task = replace(self.task, image_generation={"enabled": True, "engine": "bailian", "model": "wanx-v1"})
        validate = self.validator.validate_composition
        def add_unregistered(stage, comp_input, task, *args, **kwargs):
            (stage / "unregistered.png").write_bytes((stage / "card.png").read_bytes())
            html = (stage / "email.html").read_bytes().replace(
                b"</section>", b'<img src="cid:unregistered"></section>', 1)
            (stage / "email.html").write_bytes(html)
            return validate(stage, comp_input, task, *args, **kwargs)

        with patch.object(self.validator, "validate_composition", side_effect=add_unregistered):
            with self.assertRaises(ValidationError) as ctx:
                self.configure(inline=True)
            self.assertIn("HTML CID references must match declared inline artifacts", ctx.exception.errors)

    def test_enabled_task_with_valid_image_job_ledger_approves_compose_image(self):
        """When image_generation.enabled=True and valid image ledger matches, compose image is approved and delivered."""
        from dataclasses import replace
        self.task = replace(self.task, image_generation={"enabled": True, "engine": "bailian", "model": "wanx-v1"})
        validate = self.validator.validate_composition
        def add_with_ledger(stage, comp_input, task, *args, **kwargs):
            raw = (stage / "card.png").read_bytes()
            (stage / "newcard.png").write_bytes(raw)
            ledger = {
                "schema_version": 1,
                "session_id": "sess-123",
                "engine": "bailian",
                "model": "wanx-v1",
                "status": "available",
                "jobs": [{
                    "job_id": "job-abc",
                    "status": "available",
                    "engine": "bailian",
                    "model": "wanx-v1",
                    "images": [{
                        "filename": "newcard.png",
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "size_bytes": len(raw),
                        "mime_type": "image/png"
                    }]
                }]
            }
            (stage / ".image-ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
            html = (stage / "email.html").read_bytes().replace(
                b"</section>", b'<img src="cid:newcard"></section>', 1)
            (stage / "email.html").write_bytes(html)
            return validate(stage, comp_input, task, *args, image_ledger=ledger, **kwargs)

        with patch.object(self.validator, "validate_composition", side_effect=add_with_ledger):
            self.configure(inline=True)
        handoff = self.publish()
        (self.archive / "newcard.png").write_bytes((self.stage / "newcard.png").read_bytes())
        server = self.smtp_server()
        with patch("smtplib.SMTP", return_value=server):
            ok, _, msg = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok, msg)
        parsed = BytesParser(policy=policy.default).parsebytes(smtp_message_bytes(server))
        images = [p for p in parsed.walk() if p.get_content_type() == "image/png"]
        cids = sorted(p["Content-ID"].strip("<>") for p in images if p["Content-ID"])
        self.assertEqual(cids, ["card-1", "newcard"])

    def test_forged_ledger_mismatched_job_engine_rejected(self):
        """When ledger top-level matches task but job engine mismatches, image is rejected."""
        from dataclasses import replace
        from researchops.errors import ValidationError
        self.task = replace(self.task, image_generation={"enabled": True, "engine": "bailian", "model": "wanx-v1"})
        validate = self.validator.validate_composition
        def add_mismatched_engine(stage, comp_input, task, *args, **kwargs):
            raw = (stage / "card.png").read_bytes()
            (stage / "forged.png").write_bytes(raw)
            ledger = {
                "schema_version": 1,
                "session_id": "sess-123",
                "engine": "bailian",
                "model": "wanx-v1",
                "status": "available",
                "jobs": [{
                    "job_id": "job-forged",
                    "status": "available",
                    "engine": "codex",
                    "model": "wanx-v1",
                    "images": [{
                        "filename": "forged.png",
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "size_bytes": len(raw),
                        "mime_type": "image/png"
                    }]
                }]
            }
            (stage / ".image-ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
            html = (stage / "email.html").read_bytes().replace(
                b"</section>", b'<img src="cid:forged"></section>', 1)
            (stage / "email.html").write_bytes(html)
            return validate(stage, comp_input, task, *args, **kwargs)

        with patch.object(self.validator, "validate_composition", side_effect=add_mismatched_engine):
            with self.assertRaises(ValidationError):
                self.configure(inline=True)

    def test_forged_ledger_missing_top_level_engine_rejected(self):
        """When ledger is missing top-level engine field, image is rejected."""
        from dataclasses import replace
        from researchops.errors import ValidationError
        self.task = replace(self.task, image_generation={"enabled": True, "engine": "bailian", "model": "wanx-v1"})
        validate = self.validator.validate_composition
        def add_missing_engine(stage, comp_input, task, *args, **kwargs):
            raw = (stage / "card.png").read_bytes()
            (stage / "forged.png").write_bytes(raw)
            ledger = {
                "schema_version": 1,
                "session_id": "sess-123",
                "model": "wanx-v1",
                "status": "available",
                "jobs": [{
                    "job_id": "job-forged",
                    "status": "available",
                    "engine": "bailian",
                    "model": "wanx-v1",
                    "images": [{
                        "filename": "forged.png",
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "size_bytes": len(raw),
                        "mime_type": "image/png"
                    }]
                }]
            }
            (stage / ".image-ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
            html = (stage / "email.html").read_bytes().replace(
                b"</section>", b'<img src="cid:forged"></section>', 1)
            (stage / "email.html").write_bytes(html)
            return validate(stage, comp_input, task, *args, **kwargs)

        with patch.object(self.validator, "validate_composition", side_effect=add_missing_engine):
            with self.assertRaises(ValidationError):
                self.configure(inline=True)

    def test_past_run_ledger_mismatched_model_rejected(self):
        """When job model differs from expected model, image is rejected."""
        from dataclasses import replace
        from researchops.errors import ValidationError
        self.task = replace(self.task, image_generation={"enabled": True, "engine": "bailian", "model": "wanx-v1"})
        validate = self.validator.validate_composition
        def add_mismatched_model(stage, comp_input, task, *args, **kwargs):
            raw = (stage / "card.png").read_bytes()
            (stage / "forged.png").write_bytes(raw)
            ledger = {
                "schema_version": 1,
                "session_id": "sess-123",
                "engine": "bailian",
                "model": "wanx-v1",
                "status": "available",
                "jobs": [{
                    "job_id": "job-forged",
                    "status": "available",
                    "engine": "bailian",
                    "model": "wrong-model",
                    "images": [{
                        "filename": "forged.png",
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "size_bytes": len(raw),
                        "mime_type": "image/png"
                    }]
                }]
            }
            (stage / ".image-ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
            html = (stage / "email.html").read_bytes().replace(
                b"</section>", b'<img src="cid:forged"></section>', 1)
            (stage / "email.html").write_bytes(html)
            return validate(stage, comp_input, task, *args, **kwargs)

        with patch.object(self.validator, "validate_composition", side_effect=add_mismatched_model):
            with self.assertRaises(ValidationError):
                self.configure(inline=True)

    def test_end_to_end_image_compose_cid_and_smtp_double(self):
        """Image generated by engine preserves exact bytes through Compose CID into SMTP double."""
        self.configure(inline=True)
        expected_bytes = (self.stage / self.media["path"]).read_bytes()
        handoff = self.publish()
        server = self.smtp_server()
        with patch("smtplib.SMTP", return_value=server):
            ok, _, msg = self.dispatcher.dispatch_handoff(handoff.handoff_id)
        self.assertTrue(ok, msg)
        parsed = BytesParser(policy=policy.default).parsebytes(smtp_message_bytes(server))
        images = [p for p in parsed.walk() if p.get_content_type() == "image/png"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].get_payload(decode=True), expected_bytes)
        self.assertEqual(images[0]["Content-ID"].strip("<>"), "card-1")
        self.assertIn("cid:card-1", parsed.get_body(("html",)).get_content())



if __name__ == "__main__":
    unittest.main()
