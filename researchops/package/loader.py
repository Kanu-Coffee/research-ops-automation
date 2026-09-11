"""Task package loader, canonical hasher, and schema validator."""

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Set, Tuple
import yaml
import jsonschema
from jsonschema import Draft202012Validator

from researchops.domain.models import TaskDefinition, TaskVersion
from researchops.errors import ValidationError, NotFoundError


def compute_package_hash(package_files: Dict[str, bytes]) -> str:
    """Deterministically compute SHA-256 hash for a set of canonical package files."""
    hasher = hashlib.sha256()
    for rel_path in sorted(package_files.keys()):
        hasher.update(rel_path.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(package_files[rel_path])
        hasher.update(b"\x00")
    return hasher.hexdigest()


class TaskPackageLoader:
    def __init__(self, schemas_dir: Path):
        self.schemas_dir = schemas_dir
        self._task_schema: Optional[Dict[str, Any]] = None

    @property
    def task_schema(self) -> Dict[str, Any]:
        if self._task_schema is None:
            schema_file = self.schemas_dir / "task.schema.json"
            if not schema_file.exists():
                raise NotFoundError(f"Task schema not found at {schema_file}")
            with open(schema_file, "r", encoding="utf-8") as f:
                self._task_schema = json.load(f)
        return self._task_schema

    def load_from_dir(self, package_dir: Path) -> Tuple[TaskDefinition, Dict[str, str], str]:
        """Load and validate task package from a directory.

        Returns:
            (TaskDefinition, package_files_dict, canonical_hash)
        """
        if package_dir.is_symlink():
            raise ValidationError("Task package root cannot be a symlink")
        package_dir = package_dir.resolve()
        if not package_dir.is_dir():
            raise NotFoundError(f"Package directory not found: {package_dir}")

        task_yaml_path = package_dir / "task.yaml"
        if not task_yaml_path.exists():
            raise ValidationError(f"Missing task.yaml in {package_dir}")

        package_files: Dict[str, bytes] = {}
        package_files_str: Dict[str, str] = {}

        # Collect all files in package_dir
        for file_path in package_dir.rglob("*"):
            rel_path = file_path.relative_to(package_dir).as_posix()
            if rel_path.startswith(".") or "/." in rel_path:
                continue
            info = file_path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValidationError(f"Package entry must be an unlinked regular file: {rel_path}")
            # Test fixtures are not canonical task inputs and may contain binary
            # assets. Validate their entry type, but exclude from sealed text/hash.
            if rel_path.startswith("fixtures/"):
                continue
            self.validate_relative_path(rel_path)
            if len(package_files) >= 512 or info.st_size > 8*1024*1024:
                raise ValidationError("Task package file count/size limit exceeded")
            fd = os.open(file_path,os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd,"rb") as source:
                checked = os.fstat(source.fileno())
                if not stat.S_ISREG(checked.st_mode) or checked.st_nlink != 1:
                    raise ValidationError("Task package entry changed during import")
                content_bytes = source.read(8*1024*1024+1)
                if len(content_bytes)>8*1024*1024:
                    raise ValidationError("Task package file exceeds size limit")
                package_files[rel_path] = content_bytes
                try:
                    package_files_str[rel_path] = content_bytes.decode("utf-8",errors="strict")
                except UnicodeError as exc:
                    raise ValidationError(f"Canonical package files must be UTF-8: {rel_path}") from exc
        if sum(map(len,package_files.values())) > 16*1024*1024:
            raise ValidationError("Task package exceeds total size limit")
        task_dict = yaml.safe_load(package_files_str["task.yaml"]) or {}

        # Validate task.yaml and package integrity
        self.validate_package(task_dict, package_files_str)

        version_hash = compute_package_hash(package_files)
        task_def = TaskDefinition(**task_dict)
        return task_def, package_files_str, version_hash

    def validate_package(self, task_dict: Dict[str, Any], package_files: Dict[str, str]) -> List[str]:
        """Validates task dictionary and package files. Returns warnings if any."""
        errors: List[str] = []
        warnings: List[str] = []
        for path in package_files:
            self.validate_relative_path(path)

        # 1. Validate against task.schema.json
        validator = Draft202012Validator(self.task_schema,format_checker=jsonschema.FormatChecker())
        for err in validator.iter_errors(task_dict):
            path_str = ".".join(str(p) for p in err.path) if err.path else "root"
            errors.append(f"task.yaml schema error at {path_str}: {err.message}")

        if errors:
            raise ValidationError("Task package schema validation failed", errors=errors)
        from researchops.engine.research_context import validate_context_config
        validate_context_config((task_dict.get("state") or {}).get("research_context"))
        reserved = {"research-context.json", "delivery-history.json", "composition-input.json"}
        if reserved.intersection(package_files):
            raise ValidationError("Task package cannot replace application-owned execution inputs")
        schedule = task_dict.get("schedule")
        if schedule:
            from researchops.services.scheduler import CronExpression
            if schedule.get("timezone") != "Asia/Seoul":
                raise ValidationError("Task schedules must use Asia/Seoul",errors=["schedule.timezone must be Asia/Seoul"])
            CronExpression(schedule["cron"])

        # 2. Check instructions files exist
        instr = task_dict.get("instructions", {})
        research_files = instr.get("research_files", [])
        compose_files = instr.get("compose_files", [])

        for rf in research_files:
            if rf not in package_files:
                errors.append(f"Declared research file missing from package: {rf}")
        for cf in compose_files:
            if cf not in package_files:
                errors.append(f"Declared compose file missing from package: {cf}")

        # 3. Check canonical task.md exists and has invocation_stage guard
        if "task.md" not in package_files:
            errors.append("Canonical task.md is missing from package")
        else:
            task_md_content = package_files["task.md"]
            if "invocation_stage" not in task_md_content:
                warnings.append("task.md does not explicitly mention invocation_stage guard")

        # 4. Check output schemas exist and are valid JSON Schema Draft 2020-12
        output_conf = task_dict.get("output", {})
        research_schema_path = output_conf.get("research_schema")
        composition_record_schema_path = output_conf.get("composition_record_schema")
        composition_schema_path = output_conf.get("composition_schema")

        comp_schema_obj = None
        for schema_role, path in [
            ("research_schema", research_schema_path),
            ("composition_record_schema", composition_record_schema_path),
            ("composition_schema", composition_schema_path)
        ]:
            if not path or path not in package_files:
                errors.append(f"Declared {schema_role} missing: {path}")
                continue
            try:
                parsed_schema = json.loads(package_files[path])
                Draft202012Validator.check_schema(parsed_schema)
                self._validate_schema_refs(parsed_schema)
                if schema_role == "composition_schema":
                    comp_schema_obj = parsed_schema
            except json.JSONDecodeError as jde:
                errors.append(f"{schema_role} ({path}) is not valid JSON: {jde}")
            except Exception as se:
                errors.append(f"{schema_role} ({path}) is not valid Draft 2020-12 schema: {se}")

        # 5. Catalog labels are resolved from a run snapshot, never a static enum.
        delivery = task_dict.get("delivery", {})
        catalog_names = delivery.get("recipient_routing_mode", "legacy_ids") == "catalog_name"
        allowed_groups = delivery.get("allowed_recipient_group_ids", [])
        if not catalog_names and not allowed_groups:
            errors.append("delivery.allowed_recipient_group_ids cannot be empty")

        if comp_schema_obj and catalog_names:
            props = comp_schema_obj.get("properties", {})
            required = comp_schema_obj.get("required", [])
            group_prop = props.get("recipient_group_name", {})
            if (not isinstance(group_prop, dict) or group_prop.get("type") != "string" or
                    "recipient_group_name" not in required or "recipient_group_reason" not in required):
                errors.append("catalog_name composition_schema must require recipient_group_name (string) and recipient_group_reason")
            if isinstance(group_prop, dict) and any(key in group_prop for key in ("enum", "const")):
                errors.append("catalog_name recipient_group_name must use the runtime catalog, not a fixed enum or const")
            if "recipient_group_id" in props or "recipient_group_id" in required:
                errors.append("catalog_name composition_schema must return recipient_group_name instead of recipient_group_id")
        elif comp_schema_obj:
            props = comp_schema_obj.get("properties", {})
            group_prop = props.get("recipient_group_id", {})
            schema_enum = group_prop.get("enum")
            if schema_enum is not None:
                if set(allowed_groups) != set(schema_enum):
                    errors.append(
                        f"delivery.allowed_recipient_group_ids {allowed_groups} does not match "
                        f"composition_schema recipient_group_id enum {schema_enum}"
                    )
            else:
                warnings.append("composition_schema recipient_group_id does not restrict to an enum")

        # 6. Check message_type not system_alert
        msg_type = task_dict.get("delivery", {}).get("message_type")
        if msg_type == "system_alert":
            errors.append("General task delivery cannot use reserved message_type 'system_alert'")

        if errors:
            raise ValidationError("Task package integrity check failed", errors=errors, warnings=warnings)

        return warnings

    @staticmethod
    def validate_relative_path(path: str) -> None:
        if not isinstance(path,str) or not path or "\\" in path or "\x00" in path:
            raise ValidationError("Invalid package relative path")
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or any(part in {"..","."} or part.startswith(".") for part in parsed.parts) or parsed.as_posix() != path:
            raise ValidationError(f"Unsafe package path: {path}")

    @staticmethod
    def _validate_schema_refs(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in {"$ref","$dynamicRef"} and (not isinstance(item,str) or not item.startswith("#")):
                    raise ValidationError("Task schemas may only use local fragment references")
                TaskPackageLoader._validate_schema_refs(item)
        elif isinstance(value,list):
            for item in value:
                TaskPackageLoader._validate_schema_refs(item)
