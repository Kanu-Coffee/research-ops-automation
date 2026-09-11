"""Protected, read-only CLI model discovery for the two native runners.

Only picker metadata crosses this boundary. No prompt, credential file, CLI
configuration write or task workspace is involved in catalog discovery.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from researchops.runners.development_process import _group_quiescent, run_bounded
from researchops.errors import ValidationError, WorkspaceError
from researchops.strict_json import strict_json_loads
from researchops.workspace.security import assert_path_contained, read_safe_bytes


TTL_SECONDS = 900
MAX_CATALOG_BYTES = 1_000_000
MAX_MODELS = 256
_PROVIDERS = {"codex_exec": "Codex", "antigravity_exec": "Antigravity (Agy)"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_EFFORT = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_ORDER = {name: n for n, name in enumerate(("minimal", "low", "medium", "high", "xhigh", "max", "ultra"))}


def _stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _age(stamp):
    try:
        return max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds())
    except (ValueError, TypeError, AttributeError):
        return float("inf")


def _label(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 160 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("MODEL_CATALOG_INVALID")
    return value


def _identifier(value, pattern=_ID):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError("MODEL_CATALOG_INVALID")
    return value


def normalize_codex_models(values, *, cache=False):
    if not isinstance(values, list) or not values or len(values) > MAX_MODELS:
        raise ValueError("MODEL_CATALOG_INVALID")
    models, seen = [], set()
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("MODEL_CATALOG_INVALID")
        hidden = value.get("visibility") == "hide" if cache else value.get("hidden") is True
        if hidden:
            continue
        model = _identifier(value.get("slug") if cache else value.get("model"))
        if model in seen:
            raise ValueError("MODEL_CATALOG_INVALID")
        seen.add(model)
        advertised = value.get("supported_reasoning_levels") if cache else value.get("supportedReasoningEfforts")
        if not isinstance(advertised, list) or len(advertised) > 32:
            raise ValueError("MODEL_CATALOG_INVALID")
        efforts = [{"value": "", "label": "모델 기본값", "model": model}]
        for effort in advertised:
            if not isinstance(effort, dict):
                raise ValueError("MODEL_CATALOG_INVALID")
            name = _identifier(effort.get("effort") if cache else effort.get("reasoningEffort"), _EFFORT)
            if any(item["value"] == name for item in efforts):
                raise ValueError("MODEL_CATALOG_INVALID")
            efforts.append({"value": name, "label": name, "model": model})
        default = value.get("default_reasoning_level") if cache else value.get("defaultReasoningEffort")
        if default is not None and default not in {item["value"] for item in efforts}:
            raise ValueError("MODEL_CATALOG_INVALID")
        models.append({"id": model, "label": _label(value.get("display_name") if cache else value.get("displayName")),
                       "default_effort": default or "", "efforts": efforts, "family": False})
    if not models:
        raise ValueError("MODEL_CATALOG_INVALID")
    return models


def normalize_agy_models(raw):
    """Group only actual low/medium/high siblings; never infer CLI --effort support."""
    if len(raw) > MAX_CATALOG_BYTES:
        raise ValueError("MODEL_CATALOG_INVALID")
    rows = []
    for line in raw.decode("utf-8", errors="strict").splitlines():
        if line == "Fetching available models..." or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            raise ValueError("MODEL_CATALOG_INVALID")
        rows.append((_identifier(parts[0]), _label(parts[1])))
    if not rows or len(rows) > MAX_MODELS or len({item[0] for item in rows}) != len(rows):
        raise ValueError("MODEL_CATALOG_INVALID")
    groups = {}
    for model, label in rows:
        match = re.fullmatch(r"(.+)-(low|medium|high)", model)
        if match:
            groups.setdefault(match[1], []).append((match[2], model, label))
    result, added = [], set()
    for model, label in rows:
        match = re.fullmatch(r"(.+)-(low|medium|high)", model)
        family = match[1] if match and len(groups[match[1]]) > 1 else None
        if family:
            if family in added:
                continue
            added.add(family)
            variants = sorted(groups[family], key=lambda v: _ORDER[v[0]])
            efforts = [{"value": effort, "label": effort, "model": actual} for effort, actual, _ in variants]
            # This is an app picker default, not an inferred provider default.
            default = "medium" if any(v[0] == "medium" for v in variants) else variants[0][0]
            result.append({"id": family, "label": re.sub(r"\s*\((?:Low|Medium|High)\)$", "", label),
                           "default_effort": default, "efforts": efforts, "family": True})
        else:
            result.append({"id": model, "label": label, "default_effort": "", "family": False,
                           "efforts": [{"value": "", "label": "모델 기본값", "model": model}]})
    return result


def _control_env(binary):
    names = {"HOME", "CODEX_HOME", "USER", "LOGNAME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
    env = {name: os.environ[name] for name in names if name in os.environ}
    home = Path(env.get("HOME", str(Path.home())))
    env.update(PATH=":".join(dict.fromkeys((str(Path(binary).parent), str(home / ".local/bin"),
        str(home / ".npm-global/bin"), "/usr/local/bin", "/usr/bin", "/bin"))), LANG="C.UTF-8", TZ="Asia/Seoul")
    return env


def _codex_models(binary, cwd):
    """Bounded metadata-only stdio client; no thread/turn or config write methods."""
    process = subprocess.Popen([binary, "app-server", "--stdio", "-c", "features.hooks=false",
        "-c", "features.plugins=false", "-c", "features.apps=false", "-c", "mcp_servers={}"],
        cwd=cwd, env=_control_env(binary), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True)
    selector = selectors.DefaultSelector()
    total, buffer, models, request_id, pages = 0, b"", [], 1, 0
    deadline = time.monotonic() + 10

    def send(message):
        process.stdin.write((json.dumps(message) + "\n").encode())
        process.stdin.flush()

    try:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {
            "name": "researchops-model-catalog", "version": "1"}}})
        while time.monotonic() < deadline:
            for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > MAX_CATALOG_BYTES:
                    raise ValueError("MODEL_CATALOG_OUTPUT_LIMIT")
                if key.data != "stdout":
                    continue
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    event = strict_json_loads(line, max_bytes=MAX_CATALOG_BYTES)
                    if not isinstance(event, dict) or event.get("id") != request_id:
                        continue
                    if "error" in event or not isinstance(event.get("result"), dict):
                        raise ValueError("MODEL_CATALOG_DISCOVERY_FAILED")
                    result = event["result"]
                    if request_id == 1:
                        send({"method": "initialized", "params": {}})
                        params = {"limit": MAX_MODELS, "includeHidden": False}
                    else:
                        values = result.get("data")
                        if not isinstance(values, list):
                            raise ValueError("MODEL_CATALOG_INVALID")
                        models.extend(values)
                        pages += 1
                        if len(models) > MAX_MODELS or pages > 8:
                            raise ValueError("MODEL_CATALOG_OUTPUT_LIMIT")
                        cursor = result.get("nextCursor")
                        if cursor is None:
                            return normalize_codex_models(models)
                        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096:
                            raise ValueError("MODEL_CATALOG_INVALID")
                        params = {"limit": MAX_MODELS, "includeHidden": False, "cursor": cursor}
                    request_id += 1
                    send({"id": request_id, "method": "model/list", "params": params})
            if process.poll() is not None and not selector.get_map():
                break
        raise ValueError("MODEL_CATALOG_DISCOVERY_TIMEOUT")
    finally:
        selector.close()
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
        process.stdout.close()
        process.stderr.close()
        # The metadata CLI leader may exit before its children. A successful
        # response does not excuse leaving that newly created process group.
        if not _group_quiescent(process.pid):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stop_deadline = time.monotonic() + 1
            while not _group_quiescent(process.pid) and time.monotonic() < stop_deadline:
                time.sleep(0.02)
            if not _group_quiescent(process.pid):
                raise ValueError("MODEL_CATALOG_CLEANUP_UNVERIFIED")


class ModelCatalogService:
    def __init__(self, settings=None):
        self.settings = settings
        self._cache = None
        self._lock = threading.Lock()
        self._path = (settings.paths.data_dir / "model-catalog" / "snapshot.json"
                      if settings is not None and settings.paths is not None else None)

    def _load(self):
        if self._path is None:
            return None
        try:
            value = strict_json_loads(read_safe_bytes(self._path, Path(self._path.anchor), MAX_CATALOG_BYTES),
                                      max_bytes=MAX_CATALOG_BYTES)
            # Cache contains normalized metadata only; revalidate every field.
            if value.get("version") != 1 or not isinstance(value.get("providers"), list):
                return None
            providers = value["providers"]
            if len(providers) != 2 or {p.get("type") for p in providers} != set(_PROVIDERS):
                return None
            for provider in providers:
                if set(provider) - {"type", "label", "status", "source", "fetched_at", "error_code", "models"}:
                    return None
                _label(provider["label"])
                if provider["status"] not in {"ready", "stale", "unavailable"} or provider["source"] not in {
                        "codex_model_list", "codex_models_cache", "agy_models", "none"}:
                    return None
                if provider.get("error_code") not in {None, "MODEL_CATALOG_UNAVAILABLE"}:
                    return None
                if provider.get("fetched_at") is not None and _age(provider["fetched_at"]) == float("inf"):
                    return None
                if not isinstance(provider["models"], list) or len(provider["models"]) > MAX_MODELS:
                    return None
                for model in provider["models"]:
                    if set(model) != {"id", "label", "default_effort", "efforts", "family"}:
                        return None
                    _identifier(model["id"])
                    _label(model["label"])
                    if type(model["family"]) is not bool or not isinstance(model["efforts"], list) or not 1 <= len(model["efforts"]) <= 33:
                        return None
                    for effort in model["efforts"]:
                        if set(effort) != {"value", "label", "model"}:
                            return None
                        _identifier(effort["model"])
                        _label(effort["label"])
                        if effort["value"]:
                            _identifier(effort["value"], _EFFORT)
                    if model["default_effort"] not in {e["value"] for e in model["efforts"]}:
                        return None
            return {"version": 1, "checked_at": value.get("checked_at"), "providers": providers}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None
        except Exception:
            # Unsafe path/oversized cache is an unavailable cache, never UI text.
            return None

    def _save(self):
        if self._path is None:
            return
        temporary = None
        try:
            assert_path_contained(self._path, Path(self._path.anchor))
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".catalog-", dir=self._path.parent)
            temporary = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self._cache, stream, ensure_ascii=False)
            os.replace(temporary, self._path)
        except (OSError, WorkspaceError):
            pass
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _discover(self, provider):
        binary_name = getattr(self.settings.runner, "codex_binary" if provider == "codex_exec" else "antigravity_binary")
        binary = shutil.which(binary_name)
        if not binary:
            raise ValueError("MODEL_CATALOG_UNAVAILABLE")
        with tempfile.TemporaryDirectory(prefix="model-catalog-") as directory:
            if provider == "antigravity_exec":
                result = run_bounded([binary, "models"], cwd=Path(directory), env=_control_env(binary),
                    timeout_seconds=10, max_output_bytes=MAX_CATALOG_BYTES)
                if result.exit_code or result.error or not result.cleanup_verified:
                    raise ValueError("MODEL_CATALOG_UNAVAILABLE")
                return normalize_agy_models(result.stdout), "agy_models", _stamp()
            try:
                return _codex_models(binary, Path(directory)), "codex_model_list", _stamp()
            except (OSError, ValueError, subprocess.SubprocessError):
                home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
                path = home / "models_cache.json"
                document = strict_json_loads(read_safe_bytes(path, Path(path.anchor), MAX_CATALOG_BYTES),
                                             max_bytes=MAX_CATALOG_BYTES)
                stamp = document.get("fetched_at")
                if _age(stamp) == float("inf"):
                    raise ValueError("MODEL_CATALOG_UNAVAILABLE")
                return normalize_codex_models(document.get("models"), cache=True), "codex_models_cache", stamp

    def snapshot(self, refresh=False):
        if type(refresh) is not bool:
            raise ValueError("MODEL_CATALOG_INVALID_REFRESH")
        with self._lock:
            if self._cache is None:
                self._cache = self._load() or {"version": 1, "checked_at": None, "providers": [
                    {"type": kind, "label": label, "status": "unavailable", "source": "none", "fetched_at": None,
                     "models": [], "error_code": "MODEL_CATALOG_UNAVAILABLE"} for kind, label in _PROVIDERS.items()]}
            # Rendering/validation reads the last metadata snapshot only. An
            # explicit CSRF-protected refresh performs CLI discovery; stale
            # providers remain selectable with their freshness warning.
            if (refresh and self.settings is not None and self.settings.environment == "production"):
                for provider in self._cache["providers"]:
                    try:
                        models, source, stamp = self._discover(provider["type"])
                        provider.update(models=models, source=source, fetched_at=stamp, error_code=None,
                                        status="ready" if _age(stamp) < TTL_SECONDS else "stale")
                    except Exception:
                        provider.update(status="stale" if provider["models"] else "unavailable", error_code="MODEL_CATALOG_UNAVAILABLE")
                self._cache["checked_at"] = _stamp()
                self._save()
            result = deepcopy(self._cache)
        for provider in result["providers"]:
            if provider["models"] and _age(provider["fetched_at"]) >= TTL_SECONDS:
                provider["status"] = "stale"
        result["fetched_at"] = result.pop("checked_at")
        result["stale"] = any(p["status"] != "ready" for p in result["providers"])
        result["revision"] = hashlib.sha256(json.dumps(result["providers"], sort_keys=True).encode()).hexdigest()
        return result

    def validate_stage(self, selection, legacy=None):
        try:
            return self._validate_stage(selection, legacy)
        except ValueError as exc:
            messages = {
                "MODEL_PROVIDER_INVALID": "사용할 제공자를 목록에서 선택해 주세요.",
                "MODEL_REQUIRED_FOR_EFFORT": "추론 강도를 지정하려면 모델을 먼저 선택해 주세요.",
                "MODEL_EFFORT_UNSUPPORTED": "선택한 모델이 지원하는 추론 강도를 선택해 주세요.",
                "MODEL_SELECTION_UNAVAILABLE": "선택한 모델을 현재 목록에서 확인할 수 없습니다. 목록을 새로고침하거나 기존 설정을 유지해 주세요.",
            }
            code = str(exc) if str(exc) in messages else "MODEL_SELECTION_INVALID"
            raise ValidationError(messages.get(code, "제공자·모델·추론 강도 선택값이 올바르지 않습니다."), errors=[code]) from None

    def _validate_stage(self, selection, legacy=None):
        if not isinstance(selection, dict) or set(selection) - {"type", "model", "reasoning_effort"}:
            raise ValueError("MODEL_SELECTION_INVALID")
        if any(selection.get(key) is not None and not isinstance(selection[key], str)
               for key in ("model", "reasoning_effort")):
            raise ValueError("MODEL_SELECTION_INVALID")
        stage = {"type": selection.get("type"), "model": selection.get("model") or None,
                 "reasoning_effort": selection.get("reasoning_effort") or None}
        if stage["type"] not in _PROVIDERS:
            raise ValueError("MODEL_PROVIDER_INVALID")
        for key, pattern in (("model", _ID), ("reasoning_effort", _EFFORT)):
            if stage[key] is not None:
                _identifier(stage[key], pattern)
        if legacy is not None and stage == {key: legacy.get(key) or None for key in stage}:
            return stage
        if stage["model"] is None:
            if stage["reasoning_effort"] is not None:
                raise ValueError("MODEL_REQUIRED_FOR_EFFORT")
            return stage
        provider = next(p for p in self.snapshot()["providers"] if p["type"] == stage["type"])
        for model in provider["models"]:
            if stage["type"] == "antigravity_exec":
                if stage["reasoning_effort"] is None and any(e["model"] == stage["model"] for e in model["efforts"]):
                    return stage
                if stage["model"] == model["id"]:
                    for effort in model["efforts"]:
                        if effort["value"] == (stage["reasoning_effort"] or ""):
                            return {"type": stage["type"], "model": effort["model"], "reasoning_effort": None}
                    raise ValueError("MODEL_EFFORT_UNSUPPORTED")
            elif model["id"] == stage["model"]:
                if (stage["reasoning_effort"] or "") in {e["value"] for e in model["efforts"]}:
                    return stage
                raise ValueError("MODEL_EFFORT_UNSUPPORTED")
        raise ValueError("MODEL_SELECTION_UNAVAILABLE")
