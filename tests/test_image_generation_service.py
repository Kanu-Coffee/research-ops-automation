"""Task-scoped image adapter doubles; no real provider or network."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from researchops.services.image_generation import ImageGenerationService

PNG = b"\x89PNG\r\n\x1a\nsynthetic"


def settings(root, **overrides):
    values = dict(
        runner=SimpleNamespace(codex_binary="codex", bailian_binary="bl",
                               opencode_binary="opencode"),
        image_services=SimpleNamespace(
            openrouter_api_key_file=None,
            openrouter_base_url="https://openrouter.example/api/v1",
            openrouter_timeout_seconds=10, max_image_bytes=1_000_000))
    values.update(overrides)
    return SimpleNamespace(**values)


class ImageGenerationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.briefs = [{"filename": "first.png", "prompt": "Synthetic first"}]

    def fake_cli(self, name, mode="success"):
        path = self.root / name
        path.write_text('''#!/usr/bin/python3
import json,pathlib,sys
if "image" in sys.argv:
    out = pathlib.Path(sys.argv[sys.argv.index("--out-dir") + 1])
    prefix = sys.argv[sys.argv.index("--out-prefix") + 1]
    (out / (prefix + ".png")).write_bytes(b"\\x89PNG\\r\\n\\x1a\\nsynthetic")
else:
    prompt = sys.argv[-1]
    request = json.loads(prompt.split("Request:\\n", 1)[1])
    for brief in request:
        pathlib.Path(brief["filename"]).write_bytes(b"\\x89PNG\\r\\n\\x1a\\nsynthetic")
sys.exit(0 if ''' + repr(mode) + ''' == "success" else 1)
''')
        path.chmod(0o700)
        return str(path)

    def test_codex_uses_existing_adapter(self):
        service = ImageGenerationService(settings(self.root))
        with patch("researchops.services.image_generation.codex_generate",
                   return_value={"status": "available", "images": [
                       {"path": str(self.root / "first.png"), "mime_type": "image/png",
                        "size_bytes": 16, "sha256": "a" * 64}]}) as generate:
            result = service.generate(self.briefs, engine="codex", artifact_root=self.root)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["engine"], "codex")
        generate.assert_called_once()

    def test_bailian_and_opencode_require_real_output_files(self):
        cli = self.fake_cli("bl")
        service = ImageGenerationService(settings(self.root, runner=SimpleNamespace(
            codex_binary="codex", bailian_binary=cli, opencode_binary=cli)))
        result = service.generate(self.briefs, engine="bailian", artifact_root=self.root)
        self.assertEqual(result["status"], "available")
        self.assertTrue((self.root / "first.png").is_file())

        (self.root / "first.png").unlink()
        result = service.generate(self.briefs, engine="opencode", artifact_root=self.root)
        self.assertEqual(result["status"], "available")
        self.assertTrue((self.root / "first.png").is_file())

    def test_openrouter_writes_verified_base64_and_never_leaks_key(self):
        key = self.root / "openrouter.key"
        key.write_text("synthetic-key")
        service = ImageGenerationService(settings(self.root, image_services=SimpleNamespace(
            openrouter_api_key_file=key, openrouter_base_url="https://openrouter.example/api/v1",
            openrouter_timeout_seconds=10, max_image_bytes=1_000_000)))

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode(),
                                             "media_type": "image/png"}]}).encode()

        with patch("researchops.services.image_generation.urlrequest.urlopen",
                   return_value=Response()) as urlopen:
            result = service.generate(self.briefs, engine="openrouter",
                                      model="vendor/image-model", artifact_root=self.root)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["engine"], "openrouter")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://openrouter.example/api/v1/images")
        self.assertNotIn("synthetic-key", json.dumps(result))

    def test_openrouter_requires_model_and_protected_key(self):
        service = ImageGenerationService(settings(self.root))
        missing_model = service.generate(self.briefs, engine="openrouter", artifact_root=self.root)
        self.assertEqual(missing_model["reason_code"], "image_model_required")
        missing_key = service.generate(self.briefs, engine="openrouter", model="vendor/model",
                                       artifact_root=self.root)
        self.assertEqual(missing_key["reason_code"], "image_credentials_unavailable")

    def test_unknown_engine_or_invalid_brief_is_rejected_before_io(self):
        service = ImageGenerationService(settings(self.root))
        with self.assertRaises(Exception):
            service.generate(self.briefs, engine="unknown", artifact_root=self.root)
        with self.assertRaises(Exception):
            service.generate([{"filename": "../escape.png", "prompt": "x"}],
                             engine="codex", artifact_root=self.root)

    def test_adapter_model_mismatch_not_silently_overwritten(self):
        """When transport returns model='different-model', service preserves observed_model and sets model_verified=False."""
        service = ImageGenerationService(settings(self.root))
        actual = {
            "status": "available",
            "model": "different-model",
            "images": [{"path": str(self.root / "first.png"), "engine": "different-engine", "model": "different-model"}]
        }
        with patch("researchops.services.image_generation.codex_generate", return_value=actual):
            result = service.generate(self.briefs, engine="codex", model="gpt-image-1", artifact_root=self.root)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason_code"], "image_model_mismatch")
        self.assertEqual(result["observed_model"], "different-model")
        self.assertEqual(result["requested_model"], "gpt-image-1")
        self.assertFalse(result["model_verified"])

    def test_adapter_sets_model_verified_false_for_unverifiable_engines(self):
        """For bailian, opencode, and openrouter, model_verified=False is always set on result and images."""
        cli = self.fake_cli("bl")
        service = ImageGenerationService(settings(self.root, runner=SimpleNamespace(
            codex_binary="codex", bailian_binary=cli, opencode_binary=cli)))
        res_bl = service.generate(self.briefs, engine="bailian", model="wanx-v1", artifact_root=self.root)
        self.assertEqual(res_bl["status"], "available")
        self.assertFalse(res_bl["model_verified"])
        self.assertFalse(res_bl["images"][0]["model_verified"])
        self.assertEqual(res_bl["images"][0]["requested_model"], "wanx-v1")

        (self.root / "first.png").unlink()
        res_oc = service.generate(self.briefs, engine="opencode", model="default", artifact_root=self.root)
        self.assertEqual(res_oc["status"], "available")
        self.assertFalse(res_oc["model_verified"])
        self.assertFalse(res_oc["images"][0]["model_verified"])
        self.assertEqual(res_oc["images"][0]["requested_model"], "default")

        (self.root / "first.png").unlink()
        key = self.root / "openrouter.key"
        key.write_text("synthetic-key")
        service_or = ImageGenerationService(settings(self.root, image_services=SimpleNamespace(
            openrouter_api_key_file=key, openrouter_base_url="https://openrouter.example/api/v1",
            openrouter_timeout_seconds=10, max_image_bytes=1_000_000)))

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, limit):
                return json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode(),
                                             "media_type": "image/png"}]}).encode()

        with patch("researchops.services.image_generation.urlrequest.urlopen", return_value=Response()):
            res_or = service_or.generate(self.briefs, engine="openrouter",
                                         model="vendor/image-model", artifact_root=self.root)
        self.assertEqual(res_or["status"], "available")
        self.assertFalse(res_or["model_verified"])
        self.assertFalse(res_or["images"][0]["model_verified"])
        self.assertEqual(res_or["images"][0]["requested_model"], "vendor/image-model")


if __name__ == "__main__":
    unittest.main()
