"""Task templates repository."""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
import yaml

from researchops.config import Settings, bundled_path
from researchops.package.loader import TaskPackageLoader


@dataclass
class TaskTemplate:
    template_id: str
    name: str
    description: str
    config_yaml: str
    task_md: str
    email_spec_md: str
    schemas: Dict[str, str]


def get_available_templates(settings: Settings) -> List[TaskTemplate]:
    """Discover templates from examples/tasks directory."""
    templates: List[TaskTemplate] = []
    examples_dir = bundled_path(settings.paths.repo_root, "examples/tasks")
    if not examples_dir.exists():
        return templates
    loader=TaskPackageLoader(settings.paths.schemas_dir)

    for item in examples_dir.iterdir():
        if item.is_dir() and (item / "task.yaml").exists() and (item / "task.md").exists():
            definition,files,_=loader.load_from_dir(item)
            task_yaml_content=files["task.yaml"]
            task_md_content=files["task.md"]
            email_spec_content=files.get("email_spec.md","")

            schemas: Dict[str, str] = {}
            for name,content in files.items():
                if name.endswith(".schema.json"):
                    schemas[name]=content

            parsed = yaml.safe_load(task_yaml_content) or {}
            templates.append(
                TaskTemplate(
                    template_id=item.name,
                    name=parsed.get("name", item.name),
                    description=parsed.get("description", ""),
                    config_yaml=task_yaml_content,
                    task_md=task_md_content,
                    email_spec_md=email_spec_content,
                    schemas=schemas,
                )
            )
    return templates


def get_template_by_id(settings: Settings, template_id: str) -> Optional[TaskTemplate]:
    for t in get_available_templates(settings):
        if t.template_id == template_id:
            return t
    return None


list_templates = get_available_templates
