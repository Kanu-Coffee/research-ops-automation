"""Hermetic fixtures: no test is allowed to use the operator's runtime."""

from pathlib import Path
import shutil
import yaml

from researchops.config import load_settings
from researchops.runners.fake import FakeRunner

SOURCE_ROOT = Path(__file__).resolve().parents[1]


def isolated_settings(root: Path):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for relative in ("schemas", "examples/tasks", "tasks"):
        shutil.copytree(SOURCE_ROOT / relative, root / relative, dirs_exist_ok=True)
    for task_file in (root / "tasks").glob("*/task.yaml"):
        data = yaml.safe_load(task_file.read_text())
        data["runner"]["type"] = "fake"
        data["delivery"]["mode"] = "dry_run"
        data["enabled"] = False
        task_file.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    config = {
        "environment": "test", "timezone": "Asia/Seoul",
        "paths": {"tasks_dir": str(root / "tasks"), "data_dir": str(root / "var"),
                  "database": str(root / "var/researchops.db")},
        "runner": {"default_type": "fake", "codex_binary": "/nonexistent/codex",
                   "antigravity_binary": "/nonexistent/agy"},
        "delivery": {"default_mode": "dry_run", "global_handoff_kill_switch": True},
        "web": {"enabled": True, "allowed_hosts": ["127.0.0.1", "localhost"],
                "trusted_proxy_cidrs": ["127.0.0.1/32"]},
    }
    config_path = root / "settings.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    settings = load_settings(config_path)
    settings.paths.repo_root = root
    settings.paths.schemas_dir = root / "schemas"
    return settings


def fixture_runner(settings):
    return FakeRunner(fixtures_dir=settings.paths.repo_root / "examples/tasks/software-releases/fixtures")


def register_fixture_task(app, task_id="software-releases"):
    """Install a test fixture without pretending it passed operator activation."""
    versions = app.tasks.sync_canonical_tasks()
    version = next(v for v in versions if v.task_id == task_id)
    app.task_repo.set_active_version(task_id, version.version_hash)
    return version
