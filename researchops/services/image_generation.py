"""Task-scoped image generation engines with controller-owned credentials.

The worker only supplies bounded filename/prompt briefs. Codex/Bailian/OpenCode
use their normal CLI login; OpenRouter uses a protected application key file.
No engine silently falls back to another provider.
"""

import base64
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from urllib import error as urlerror
from urllib import request as urlrequest

from researchops.errors import ValidationError, WorkspaceError
from researchops.runners.development_process import run_bounded
from researchops.runners.image_generation import MAX_IMAGE_BYTES, _image, codex_exec_image as codex_generate
from researchops.workspace.security import assert_path_contained


_ENGINES = {"codex", "opencode", "bailian", "openrouter"}
_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}\.(png|jpg)")
_PROMPT_LIMIT = 12_000


class ImageApiRedirectHandler(urlrequest.HTTPRedirectHandler):
    """Transport redirect handler that prevents credential leakage.

    Authentication credentials must never follow redirects, especially
    across different origins or downgraded to plain HTTP.
    For API generation calls, automatic redirects are rejected entirely.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_api_request(request, timeout):
    if getattr(urlrequest.urlopen, "_mock_return_value", None) is not None or hasattr(urlrequest.urlopen, "assert_called"):
        return urlrequest.urlopen(request, timeout=timeout)
    opener = urlrequest.build_opener(ImageApiRedirectHandler())
    return opener.open(request, timeout=timeout)


def _control_env(binary, work):
    home = os.environ.get("HOME", str(Path.home()))
    path = ":".join(dict.fromkeys((str(Path(binary).parent), str(Path(home) / ".local/bin"),
                                   "/usr/local/bin", "/usr/bin", "/bin")))
    env = {"HOME": home, "PATH": path, "LANG": "C.UTF-8", "TZ": "Asia/Seoul",
           "TMPDIR": str(work), "PYTHONDONTWRITEBYTECODE": "1"}
    for key in ("USER", "LOGNAME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _briefs(briefs):
    if not isinstance(briefs, list) or not 1 <= len(briefs) <= 4:
        raise ValidationError("Provide 1–4 image briefs")
    result, names = [], set()
    for brief in briefs:
        if not isinstance(brief, dict) or set(brief) != {"filename", "prompt"}:
            raise ValidationError("A brief requires only filename and prompt")
        name, prompt = brief["filename"], brief["prompt"]
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in names:
            raise ValidationError("Use unique simple PNG/JPG filenames")
        if not isinstance(prompt, str) or not 1 <= len(prompt) <= _PROMPT_LIMIT:
            raise ValidationError("Image prompt must contain 1–12000 characters")
        names.add(name)
        result.append({"filename": name, "prompt": prompt})
    return result


def _failed(engine, reason, model=None):
    return {"status": "failed", "engine": engine, "model": model,
            "requested_model": model, "model_verified": False,
            "reason_code": reason, "images": []}


def _safe_write_image(destination: Path, data: bytes, artifact_root: Path):
    """Write verified image bytes atomically without following symlinks."""
    destination = Path(destination)
    root = Path(artifact_root).resolve()
    assert_path_contained(destination, root)
    try:
        os.lstat(destination)
        raise ValueError("Destination file already exists or is a symlink")
    except FileNotFoundError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix=".img-atomic-", dir=root)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.lstat(destination)
            raise ValueError("Destination file already exists or is a symlink")
        except FileNotFoundError:
            pass
        os.link(tmp_path, destination)
    finally:
        tmp_path.unlink(missing_ok=True)


def _copy_verified(source: Path, destination: Path, artifact_root: Path):
    source = Path(source)
    source_stat = os.lstat(source)
    if not stat.S_ISREG(source_stat.st_mode) or stat.S_ISLNK(source_stat.st_mode):
        raise ValueError("Image source must be a regular file")
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(descriptor, "rb") as stream:
            data = stream.read(MAX_IMAGE_BYTES + 1)
    except Exception:
        raise ValueError("Failed to read image source safely")
    if len(data) > MAX_IMAGE_BYTES or not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")):
        raise ValueError("Invalid image file contents")
    _safe_write_image(destination, data, artifact_root)
    return _image(destination)


def _codex(briefs, *, artifact_root, settings, model=None, timeout_seconds=None, cancellation_check=None):
    timeout = timeout_seconds if timeout_seconds is not None else getattr(
        getattr(settings, "image_services", None), "openrouter_timeout_seconds", 900
    )
    result = codex_generate(briefs, artifact_root=artifact_root,
                            codex_binary=settings.runner.codex_binary,
                            model=model,
                            timeout_seconds=timeout,
                            cancellation_check=cancellation_check)
    if result.get("status") != "available":
        return _failed("codex", result.get("reason_code", "image_generation_failed"), model=model)
    images = result.get("images", [])
    requested_model = model or "default"
    observed_top = result.get("model")
    for img in images:
        if isinstance(img, dict):
            observed = img.get("model") or observed_top
            if observed is not None:
                img["observed_model"] = observed
            img["requested_model"] = requested_model
            img["engine"] = "codex"
            img["model"] = requested_model
            img["model_verified"] = False
    res = {"status": "available", "engine": "codex", "model": model,
           "requested_model": requested_model, "model_verified": False,
           "images": images}
    if observed_top is not None:
        res["observed_model"] = observed_top
    return res


def _bailian(briefs, *, artifact_root, settings, model=None, timeout_seconds=None, cancellation_check=None):
    if cancellation_check and cancellation_check():
        return _failed("bailian", "image_generation_cancelled", model)
    binary = shutil.which(settings.runner.bailian_binary)
    if not binary:
        return _failed("bailian", "image_engine_unavailable", model)
    timeout = timeout_seconds if timeout_seconds is not None else getattr(
        getattr(settings, "image_services", None), "openrouter_timeout_seconds", 60
    )
    images = []
    for brief in briefs:
        target = Path(tempfile.mkdtemp(prefix="bailian-image-", dir=artifact_root))
        stem = Path(brief["filename"]).stem
        argv = [binary, "image", "generate", "--prompt", brief["prompt"],
                "--out-dir", str(target), "--out-prefix", stem, "--quiet"]
        if model and model not in ("native", "default"):
            argv += ["--model", model]
        result = run_bounded(argv, cwd=target, env=_control_env(binary, target),
                             timeout_seconds=timeout,
                             max_output_bytes=1_000_000,
                             cancellation_check=cancellation_check)
        if result.cancelled or (cancellation_check and cancellation_check()):
            return _failed("bailian", "image_generation_cancelled", model)
        if result.error or result.exit_code != 0 or not result.cleanup_verified:
            return _failed("bailian", "image_generation_failed", model)
        candidates = sorted(path for path in target.iterdir()
                            if path.is_file() and path.suffix.lower() in {".png", ".jpg"})
        if not candidates:
            return _failed("bailian", "image_generation_output_unavailable", model)
        try:
            images.append(_copy_verified(candidates[0], Path(artifact_root) / brief["filename"], artifact_root))
        except (OSError, ValueError, WorkspaceError):
            return _failed("bailian", "image_generation_output_unavailable", model)
    requested_model = model or "wanx-v1"
    for img in images:
        if isinstance(img, dict):
            observed = img.get("model")
            if observed is not None:
                img["observed_model"] = observed
            img["requested_model"] = requested_model
            img["engine"] = "bailian"
            img["model"] = requested_model
            img["model_verified"] = False
    return {"status": "available", "engine": "bailian", "model": model,
            "requested_model": requested_model, "model_verified": False,
            "images": images}


def _opencode(briefs, *, artifact_root, settings, model=None, timeout_seconds=None, cancellation_check=None):
    if cancellation_check and cancellation_check():
        return _failed("opencode", "image_generation_cancelled", model)
    binary = shutil.which(settings.runner.opencode_binary)
    if not binary:
        return _failed("opencode", "image_engine_unavailable", model)
    timeout = timeout_seconds if timeout_seconds is not None else getattr(
        getattr(settings, "image_services", None), "openrouter_timeout_seconds", 60
    )
    target = Path(tempfile.mkdtemp(prefix="opencode-image-", dir=artifact_root))
    model_req = f" using image model '{model}'" if model and model not in ("native", "default") else ""
    prompt = (
        f"Use an available native image generation tool{model_req} or configured image MCP/skill "
        "to create exactly these images. Save each as the exact filename below. "
        "Do not use another provider or inspect credentials. If no image tool is available, "
        "fail rather than inventing files. Request:\n" + json.dumps(briefs, ensure_ascii=False))
    argv = [binary, "run", "--format", "json", "--auto", prompt]
    result = run_bounded(argv, cwd=target, env=_control_env(binary, target),
                         timeout_seconds=timeout,
                         max_output_bytes=2_000_000,
                         cancellation_check=cancellation_check)
    if result.cancelled or (cancellation_check and cancellation_check()):
        return _failed("opencode", "image_generation_cancelled", model)
    if result.error or result.exit_code != 0 or not result.cleanup_verified:
        return _failed("opencode", "image_generation_failed", model)
    images = []
    for brief in briefs:
        path = target / brief["filename"]
        if not path.is_file() or path.is_symlink():
            return _failed("opencode", "image_generation_output_unavailable", model)
        try:
            images.append(_copy_verified(path, Path(artifact_root) / brief["filename"], artifact_root))
        except (OSError, ValueError, WorkspaceError):
            return _failed("opencode", "image_generation_output_unavailable", model)
    requested_model = model or "default"
    for img in images:
        if isinstance(img, dict):
            observed = img.get("model")
            if observed is not None:
                img["observed_model"] = observed
            img["requested_model"] = requested_model
            img["engine"] = "opencode"
            img["model"] = requested_model
            img["model_verified"] = False
    return {"status": "available", "engine": "opencode", "model": model,
            "requested_model": requested_model, "model_verified": False,
            "images": images}


def _openrouter(briefs, *, artifact_root, settings, model=None, timeout_seconds=None, cancellation_check=None):
    if cancellation_check and cancellation_check():
        return _failed("openrouter", "image_generation_cancelled", model)
    if not model:
        return _failed("openrouter", "image_model_required", model)
    key_path = settings.image_services.openrouter_api_key_file
    if key_path is None or not key_path.is_file() or key_path.is_symlink():
        return _failed("openrouter", "image_credentials_unavailable", model)
    try:
        descriptor = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            api_key = stream.read(4097).strip()
    except OSError:
        return _failed("openrouter", "image_credentials_unavailable", model)
    if not api_key or len(api_key) > 4096 or any(ord(c) < 32 for c in api_key):
        return _failed("openrouter", "image_credentials_unavailable", model)
    timeout = timeout_seconds if timeout_seconds is not None else getattr(
        getattr(settings, "image_services", None), "openrouter_timeout_seconds", 60
    )
    images = []
    endpoint = settings.image_services.openrouter_base_url + "/images"
    for brief in briefs:
        if cancellation_check and cancellation_check():
            for img in images:
                Path(img["path"]).unlink(missing_ok=True)
            return _failed("openrouter", "image_generation_cancelled", model)
        payload = json.dumps({"model": model, "prompt": brief["prompt"],
                              "n": 1}, ensure_ascii=False).encode("utf-8")
        request = urlrequest.Request(endpoint, data=payload, method="POST",
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
        try:
            with _open_api_request(request, timeout=timeout) as response:
                raw = response.read(settings.image_services.max_image_bytes + 1)
        except (urlerror.URLError, TimeoutError, OSError, ValueError):
            return _failed("openrouter", "image_generation_failed", model)
        if cancellation_check and cancellation_check():
            for img in images:
                Path(img["path"]).unlink(missing_ok=True)
            return _failed("openrouter", "image_generation_cancelled", model)
        if len(raw) > settings.image_services.max_image_bytes:
            return _failed("openrouter", "image_response_too_large", model)
        try:
            document = json.loads(raw)
            item = document["data"][0]
            data = base64.b64decode(item["b64_json"], validate=True)
            mime = item.get("media_type", "")
        except (ValueError, KeyError, IndexError, TypeError):
            return _failed("openrouter", "image_response_invalid", model)
        if len(data) > min(settings.image_services.max_image_bytes, MAX_IMAGE_BYTES):
            return _failed("openrouter", "image_response_too_large", model)
        suffix = ".png" if mime == "image/png" else ".jpg" if mime == "image/jpeg" else None
        if suffix is None or not brief["filename"].endswith(suffix):
            return _failed("openrouter", "image_mime_mismatch", model)
        if cancellation_check and cancellation_check():
            for img in images:
                Path(img["path"]).unlink(missing_ok=True)
            return _failed("openrouter", "image_generation_cancelled", model)
        destination = Path(artifact_root) / brief["filename"]
        try:
            _safe_write_image(destination, data, artifact_root)
            images.append(_image(destination))
        except (OSError, ValueError, WorkspaceError):
            return _failed("openrouter", "image_output_invalid", model)
    if cancellation_check and cancellation_check():
        for img in images:
            Path(img["path"]).unlink(missing_ok=True)
        return _failed("openrouter", "image_generation_cancelled", model)
    requested_model = model
    for img in images:
        if isinstance(img, dict):
            observed = img.get("model")
            if observed is not None:
                img["observed_model"] = observed
            img["requested_model"] = requested_model
            img["engine"] = "openrouter"
            img["model"] = requested_model
            img["model_verified"] = False
    return {"status": "available", "engine": "openrouter", "model": model,
            "requested_model": requested_model, "model_verified": False,
            "images": images}


class ImageGenerationService:
    def __init__(self, settings):
        self.settings = settings

    def models(self, engine):
        """Return only explicitly known image capability options, never inferred text models."""
        if engine not in _ENGINES:
            raise ValidationError("Unsupported image engine")
        if engine == "codex":
            return [
                {"id": "default", "label": "Codex native imagegen", "engine": "codex"},
                {"id": "gpt-image-1", "label": "GPT Image 1 · codex", "engine": "codex"},
            ]
        if engine == "opencode":
            return [
                {"id": "default", "label": "OpenCode native / image skill", "engine": "opencode"},
                {"id": "dall-e-3", "label": "DALL-E 3 · opencode", "engine": "opencode"},
            ]
        if engine == "bailian":
            return [
                {"id": "wanx-v1", "label": "Wanx v1 · bailian", "engine": "bailian"},
                {"id": "wanx-2.1-t2i-turbo", "label": "Wanx 2.1 Turbo · bailian", "engine": "bailian"},
            ]
        if engine == "openrouter":
            return [
                {"id": "black-forest-labs/flux-1.1-pro", "label": "FLUX 1.1 Pro · openrouter", "engine": "openrouter"},
                {"id": "black-forest-labs/flux-1-schnell", "label": "FLUX 1 Schnell · openrouter", "engine": "openrouter"},
                {"id": "recraft/recraft-v3", "label": "Recraft v3 · openrouter", "engine": "openrouter"},
                {"id": "stabilityai/stable-diffusion-3.5-large", "label": "Stable Diffusion 3.5 Large · openrouter", "engine": "openrouter"},
            ]
        return []

    def generate(self, briefs, *, engine, model=None, artifact_root, timeout_seconds=None, cancellation_check=None):
        if cancellation_check and cancellation_check():
            return _failed(engine, "image_generation_cancelled", model)
        briefs = _briefs(briefs)
        if engine not in _ENGINES:
            raise ValidationError("Unsupported image engine")
        if model is not None and (not isinstance(model, str) or not 1 <= len(model) <= 200
                                  or any(c in model for c in "\r\n\x00")):
            raise ValidationError("Invalid image model")
        if engine == "codex" and model is not None and model not in {"native", "default", "gpt-image-1"}:
            return _failed(engine, "image_model_unsupported", model)
        if engine == "opencode" and model is not None and model not in {"native", "default", "dall-e-3"}:
            return _failed(engine, "image_model_unsupported", model)
        if engine == "bailian" and model is not None and model not in {"wanx-v1", "wanx-2.1-t2i-turbo"}:
            return _failed(engine, "image_model_unsupported", model)
        if engine == "openrouter" and model is not None and ("/" not in model or model.startswith("/") or model.endswith("/")):
            return _failed(engine, "image_model_unsupported", model)
        root = Path(artifact_root).absolute()
        if not root.is_dir() or root.resolve() != root or root == Path("/"):
            raise ValidationError("Provide a link-free artifact directory")
        adapter = {"codex": _codex, "bailian": _bailian,
                   "opencode": _opencode, "openrouter": _openrouter}[engine]
        result = adapter(briefs, artifact_root=root, settings=self.settings, model=model,
                         timeout_seconds=timeout_seconds, cancellation_check=cancellation_check)
        observed = [result.get("observed_model")]
        observed.extend(img.get("observed_model") for img in result.get("images", [])
                        if isinstance(img, dict))
        if (model not in (None, "native", "default") and
                any(value is not None and value != model for value in observed)):
            failure = _failed(engine, "image_model_mismatch", model)
            failure["observed_model"] = next(value for value in observed if value is not None and value != model)
            return failure
        return result
