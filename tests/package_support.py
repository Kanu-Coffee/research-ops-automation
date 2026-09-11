"""Direct immutable-package fixtures, with no saved working-copy lifecycle."""

from datetime import datetime, timezone
import yaml

from researchops.domain.models import TaskDefinition, TaskVersion
from researchops.package.loader import TaskPackageLoader, compute_package_hash
from researchops.package.publisher import publish_version
from researchops.package.templates import get_template_by_id


def template_package(settings, task_id, template_id="software-releases", *, mode=None):
    template = get_template_by_id(settings, template_id)
    assert template is not None, template_id
    config = yaml.safe_load(template.config_yaml)
    config.update(id=task_id, enabled=False)
    config["delivery"]["mode"] = mode or ("handoff" if settings.environment == "production" else "dry_run")
    files = dict(template.schemas)
    files.update({"task.yaml": yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
                  "task.md": template.task_md})
    if template.email_spec_md is not None:
        files["email_spec.md"] = template.email_spec_md
    return files


def register_package(app, files, *, active=False, schedule_enabled=False):
    """Validate and register fixture bytes; activating is a test setup choice."""
    config = yaml.safe_load(files["task.yaml"])
    TaskPackageLoader(app.settings.paths.schemas_dir).validate_package(config, files)
    version = TaskVersion(task_id=config["id"],
        version_hash=compute_package_hash({key: text.encode("utf-8") for key, text in files.items()}),
        sealed_at=datetime.now(timezone.utc).isoformat(), definition=TaskDefinition(**config),
        package_files=dict(files), is_active=False)
    publish_version(app.settings.paths.task_versions_dir, version)
    app.task_repo.save_version(version)
    if active:
        app.task_repo.set_active_version(version.task_id, version.version_hash,
            delivery_mode=config["delivery"]["mode"], schedule_enabled=schedule_enabled)
    return app.task_repo.get_version(version.version_hash)


def register_template(app, task_id, template_id="software-releases", *, active=False, mode=None):
    return register_package(app, template_package(app.settings, task_id, template_id, mode=mode), active=active)
