from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import json
import re
import yaml

from researchops.config import Settings
from researchops.domain.models import TaskDefinition, TaskVersion
from researchops.domain.events import AuditEvent
from researchops.errors import NotFoundError, ValidationError
from researchops.package.loader import TaskPackageLoader, compute_package_hash
from researchops.package.templates import get_available_templates
from researchops.package.publisher import publish_version
from researchops.storage.repositories import TaskRepository, StateRepository
from researchops.services.catalog_service import CatalogService


class TaskService:
    def __init__(
        self,
        settings: Settings,
        task_repo: TaskRepository,
        state_repo: StateRepository,
        model_catalog=None
    ):
        self.settings = settings
        from researchops.services.model_catalog import ModelCatalogService
        self.model_catalog = model_catalog or ModelCatalogService()
        self.task_repo = task_repo
        self.state_repo = state_repo
        self.loader = TaskPackageLoader(settings.paths.schemas_dir)
        self.catalog = CatalogService(settings, task_repo.db)

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if not isinstance(task_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", task_id):
            raise ValidationError("Task ID must begin with a lowercase letter and contain 2-64 lowercase letters, digits or hyphens")

    def _normalize_stage_settings(self, values, source=None):
        if values is None:
            return None
        if not isinstance(values, dict) or set(values) != {"research", "compose"}:
            raise ValidationError("Research and Compose settings are required")
        from researchops.engine.execution_plan import resolve_task_stages
        legacy = resolve_task_stages(source.definition) if source else {}
        return {stage: self.model_catalog.validate_stage(value, legacy=legacy.get(stage))
                for stage, value in values.items()}

    def _require_not_deleted(self, task_id: str) -> None:
        from researchops.storage.repositories import require_catalog_active
        conn = self.task_repo.db.get_connection()
        try:
            require_catalog_active(conn, "task", task_id)
        finally:
            conn.close()

    def sync_canonical_tasks(self, tasks_dir: Optional[Path] = None) -> List[TaskVersion]:
        """Register immutable candidates; activation always remains a separate command."""
        target_dir = tasks_dir or self.settings.paths.tasks_dir
        if not target_dir.exists():
            # If tasks/ does not exist, look into examples/tasks/
            alt_dir = self.settings.paths.repo_root / "examples/tasks"
            if alt_dir.exists():
                target_dir = alt_dir
            else:
                return []

        synced = []
        for item in target_dir.iterdir():
            if item.is_dir() and (item / "task.yaml").exists():
                try:
                    task_def, pkg_files, version_hash = self.loader.load_from_dir(item)
                    version = TaskVersion(
                        task_id=task_def.id,
                        version_hash=version_hash,
                        sealed_at=datetime.now(timezone.utc).isoformat(),
                        definition=task_def,
                        package_files=pkg_files,
                        is_active=False
                    )
                    publish_version(self.settings.paths.task_versions_dir,version)
                    self.task_repo.save_version(version)
                    synced.append(version)
                    self.state_repo.save_audit_event(
                        AuditEvent(
                            entity_type="task",
                            entity_id=task_def.id,
                            event_type="task_candidate_synced",
                            details={"version_hash": version_hash, "path": str(item)}
                        )
                    )
                except Exception as e:
                    self.state_repo.save_audit_event(AuditEvent(entity_type="task_package",entity_id=item.name,event_type="task_sync_failed",details={"error":str(e),"path":str(item)}))
                    raise ValidationError(f"Cannot sync task package {item.name}: {e}") from e
        return synced

    def list_tasks(self, *, include_deleted: bool = False) -> List[Dict[str, Any]]:
        tasks_meta = []
        conn = self.task_repo.db.get_connection()
        try:
            rows = conn.execute("SELECT * FROM tasks ORDER BY task_id ASC").fetchall()
            for row in rows:
                task_id = row["task_id"]
                active_hash = row["active_version_hash"]
                version = self.task_repo.get_version(active_hash) if active_hash else None
                entity = self.catalog.ensure("task", task_id, version.definition.name if version else task_id)
                if entity["deleted_at"] and not include_deleted:
                    continue
                tasks_meta.append({
                    "task_id": task_id,
                    "name": entity["display_name"],
                    "display_name": entity["display_name"], "entity_id": entity["entity_id"],
                    "deleted_at": entity["deleted_at"],
                    "enabled": bool(row["enabled"]),
                    "delivery_mode": row["delivery_mode"],
                    "delivery_approved": bool(row["delivery_approved"]) or (
                        self.settings.environment == "production" and bool(active_hash) and
                        row["delivery_mode"] == "handoff"),
                    "active_version_hash": active_hash,
                    "updated_at": row["updated_at"]
                })
        finally:
            conn.close()
        return tasks_meta

    def show_task(self, task_id: str) -> Dict[str, Any]:
        status = self.task_repo.get_task_status(task_id)
        if not status:
            raise NotFoundError(f"Task '{task_id}' not found")
        active_hash = status.get("active_version_hash")
        active_version = self.task_repo.get_version(active_hash) if active_hash else None
        entity = self.catalog.ensure("task", task_id, active_version.definition.name if active_version else task_id)
        status.update(entity_id=entity["entity_id"], display_name=entity["display_name"], deleted_at=entity["deleted_at"])
        if self.settings.environment == "production":
            # Production authorization is the operator's published handoff task,
            # not legacy approval-history fields retained for development.
            status["delivery_approved"] = bool(active_version and status.get("delivery_mode") == "handoff")

        versions = self.task_repo.list_versions(task_id)

        return {
            "status": status,
            "active_version": {
                "hash": active_version.version_hash,
                "sealed_at": active_version.sealed_at,
                "definition": active_version.definition.to_dict()
            } if active_version else None,
            "versions_count": len(versions),
            "versions": [{"hash": v.version_hash, "sealed_at": v.sealed_at, "is_active": v.is_active} for v in versions]
        }

    def delete_task(self, task_id: str) -> Dict[str, Any]:
        """Hide an idle task and stop its schedule without deleting any history."""
        status = self.task_repo.get_task_status(task_id)
        if not status:
            raise NotFoundError("Task not found")
        version = self.task_repo.get_active_version(task_id)
        self.catalog.ensure("task", task_id, version.definition.name if version else task_id)
        now = datetime.now(timezone.utc).isoformat()
        with self.task_repo.db.transaction() as conn:
            entity = conn.execute("SELECT * FROM entity_catalog WHERE kind='task' AND legacy_key=?", (task_id,)).fetchone()
            if entity["deleted_at"] is not None:
                return dict(entity)
            self.task_repo._require_idle_for_edit(conn, task_id)
            uncertain = conn.execute("""SELECT h.handoff_id FROM delivery_handoffs h
                LEFT JOIN smtp_attempts a ON a.handoff_id=h.handoff_id
                WHERE h.task_id=? AND h.mode='handoff'
                AND (h.status='uncertain' OR a.status='uncertain') LIMIT 1""", (task_id,)).fetchone()
            if uncertain:
                raise ValidationError("Task has an uncertain SMTP outcome after DATA. Review the delivery outcome "
                                      "before deleting; it must not be resent or marked failed automatically.")
            conn.execute("UPDATE tasks SET enabled=0,updated_at=? WHERE task_id=?", (now, task_id))
            conn.execute("UPDATE entity_catalog SET deleted_at=?,updated_at=? WHERE entity_id=?",
                         (now, now, entity["entity_id"]))
            conn.execute("""INSERT INTO audit_events(entity_type,entity_id,event_type,details_json,occurred_at)
                VALUES('task',?,'task_deleted',?,?)""", (task_id,
                    json.dumps({"entity_id": entity["entity_id"], "history_preserved": True}), now))
            return dict(conn.execute("SELECT * FROM entity_catalog WHERE entity_id=?", (entity["entity_id"],)).fetchone())

    def restore_task(self, task_id: str) -> Dict[str, Any]:
        """Restore visibility only; scheduling remains off until explicitly enabled."""
        if not self.task_repo.get_task_status(task_id):
            raise NotFoundError("Task not found")
        now = datetime.now(timezone.utc).isoformat()
        with self.task_repo.db.transaction() as conn:
            entity = conn.execute("SELECT * FROM entity_catalog WHERE kind='task' AND legacy_key=?", (task_id,)).fetchone()
            if not entity:
                raise NotFoundError("Task catalog entry not found")
            if entity["deleted_at"] is None:
                return dict(entity)
            conn.execute("UPDATE tasks SET enabled=0,updated_at=? WHERE task_id=?", (now, task_id))
            conn.execute("UPDATE entity_catalog SET deleted_at=NULL,updated_at=? WHERE entity_id=?", (now, entity["entity_id"]))
            conn.execute("""INSERT INTO scheduler_watermarks(task_id,evaluated_through) VALUES(?,?)
                ON CONFLICT(task_id) DO UPDATE SET evaluated_through=excluded.evaluated_through""", (task_id, now))
            conn.execute("""INSERT INTO audit_events(entity_type,entity_id,event_type,details_json,occurred_at)
                VALUES('task',?,'task_restored',?,?)""", (task_id,
                    json.dumps({"entity_id": entity["entity_id"], "schedule_enabled": False}), now))
            return dict(conn.execute("SELECT * FROM entity_catalog WHERE entity_id=?", (entity["entity_id"],)).fetchone())

    def validate_task(self, task_id: str) -> Tuple[bool, List[str], List[str]]:
        active_version = self.task_repo.get_active_version(task_id)
        if not active_version:
            raise NotFoundError(f"Task '{task_id}' has no active version to validate")

        errors: List[str] = []
        warnings: List[str] = []
        try:
            warns = self.loader.validate_package(
                active_version.definition.to_dict(),
                active_version.package_files
            )
            warnings.extend(warns)
            return True, errors, warnings
        except ValidationError as ve:
            return False, ve.errors, ve.warnings
        except Exception as e:
            return False, [str(e)], warnings

    def set_task_enabled(self, task_id: str, enabled: bool) -> None:
        self._require_not_deleted(task_id)
        status = self.task_repo.get_task_status(task_id)
        if not status:
            raise NotFoundError(f"Task '{task_id}' not found")
        if enabled and not status.get("active_version_hash"):
            raise ValidationError("Activate a validated candidate before enabling its schedule")
        self.task_repo.set_task_enabled(task_id, enabled)
        self.state_repo.save_audit_event(
            AuditEvent(
                entity_type="task",
                entity_id=task_id,
                event_type="task_enabled" if enabled else "task_disabled",
                details={"enabled": enabled}
            )
        )

    def approve_delivery(self, task_id: str, approved: bool = True) -> None:
        self._require_not_deleted(task_id)
        status = self.task_repo.get_task_status(task_id)
        if not status:
            raise NotFoundError(f"Task '{task_id}' not found")
        if self.settings.environment == "production":
            version = self.task_repo.get_active_version(task_id)
            if not version:
                raise ValidationError("Publish an active task before changing delivery")
            if approved:
                self._validate_recipient_groups(version.definition)
            self.task_repo.set_production_delivery_enabled(task_id, version.version_hash, approved)
            self.state_repo.save_audit_event(AuditEvent(entity_type="task", entity_id=task_id,
                event_type="production_delivery_enabled" if approved else "production_delivery_disabled",
                details={"enabled": approved, "version_hash": version.version_hash}))
            return
        from researchops.delivery.smtp_config import delivery_revision, load_delivery_config
        version = self.task_repo.get_active_version(task_id)
        sender_id = version.definition.delivery.get("sender_profile_id", "default") if version else "default"
        revision = delivery_revision(load_delivery_config(self.settings.paths.delivery_config_file), sender_id,
                                     db=self.task_repo.db) if approved else None
        self.task_repo.set_delivery_approved(task_id, approved, delivery_revision=revision)
        self.state_repo.save_audit_event(
            AuditEvent(
                entity_type="task",
                entity_id=task_id,
                event_type="delivery_approved" if approved else "delivery_disapproved",
                details={"approved": approved}
            )
        )

    def activate_version(self, task_id: str, version_hash: str, *, schedule_enabled: bool = False) -> None:
        self._require_not_deleted(task_id)
        version = self.task_repo.get_version(version_hash)
        if not version or version.task_id != task_id:
            raise NotFoundError(f"Version '{version_hash}' not found for task '{task_id}'")
        self.loader.validate_package(version.definition.to_dict(),version.package_files)
        production = self.settings.environment == "production"
        if not production and not self.task_repo.has_dry_run_evidence(task_id,version_hash):
            raise ValidationError("Activation requires a successful dry-run of this exact candidate hash")
        mode = version.definition.delivery.get("mode", "dry_run") if production else "dry_run"
        if production and mode == "handoff":
            self._validate_recipient_groups(version.definition)
        self.task_repo.set_active_version(task_id, version_hash, delivery_mode=mode,
                                          schedule_enabled=schedule_enabled, require_idle=production)
        self.state_repo.save_audit_event(
            AuditEvent(
                entity_type="task",
                entity_id=task_id,
                event_type="active_version_updated",
                details={"version_hash": version_hash, "delivery_mode": mode,
                         "schedule_enabled": schedule_enabled}
            )
        )

    def _validate_recipient_groups(self, definition: TaskDefinition) -> None:
        from researchops.delivery.smtp_config import load_delivery_config
        config = load_delivery_config(self.settings.paths.delivery_config_file)
        self.catalog.bootstrap_delivery(config)
        from researchops.services.ownership import require_task_delivery_ownership, scoped_delivery_config
        owner_id = require_task_delivery_ownership(self.task_repo.db, definition)
        config = scoped_delivery_config(self.task_repo.db, config, owner_id)
        try:
            config.get_sender(definition.delivery.get("sender_profile_id", "default"))
        except Exception as exc:
            raise ValidationError("Select an existing sender account in Delivery settings") from exc
        groups = definition.delivery.get("allowed_recipient_group_ids", [])
        if definition.delivery.get("recipient_routing_mode", "legacy_ids") == "catalog_name":
            from researchops.delivery.recipient_routing import catalog_recipient_snapshot
            if not catalog_recipient_snapshot(self.task_repo.db, config, owner_user_id=owner_id):
                raise ValidationError("Create an active recipient group with at least one email address in Delivery settings")
            return
        if not groups or any(not config.recipient_groups.get(group) for group in groups):
            raise ValidationError("Create the selected recipient group with at least one email address in Delivery settings")
        for group in groups:
            self.catalog.require_active("recipient_group", group)

    def create_production_task(self, task_id: Optional[str] = None, name: str = "", instructions: str = "",
                               runner_type: str = "codex_exec", recipient_group_id: str = "",
                               cron: str = "0 9 * * *", schedule_enabled: bool = False,
                               model: Optional[str] = None, request_key: Optional[str] = None,
                               task_md: Optional[str] = None, email_spec_md: Optional[str] = None,
                               sender_profile_id: str = "default",
                               source_task_id: Optional[str] = None,
                               source_version_hash: Optional[str] = None,
                               recipient_routing_mode: Optional[str] = None,
                               stage_settings: Optional[dict] = None) -> TaskVersion:
        """Publish operator instructions directly for real execution and delivery.

        Repeating the exact task ID/package is idempotent; a conflicting existing
        task is never modified. Enqueueing a first run remains a separate command
        with its own request key, so this operation itself never sends mail.
        """
        if self.settings.environment != "production":
            raise ValidationError("Direct production publication requires the production environment")
        if request_key is not None and (not isinstance(request_key, str) or not 1 <= len(request_key) <= 128):
            raise ValidationError("request_key must contain 1-128 characters")
        if task_id is None or task_id == "":
            task_id = self.catalog.allocate("task", name, request_key=request_key)["legacy_key"]
        self._validate_task_id(task_id)
        self._require_not_deleted(task_id)
        from researchops.package.production_template import build_production_package
        files = build_production_package(task_id=task_id, name=name, instructions=instructions,
            runner_type=runner_type, recipient_group_id=recipient_group_id, cron=cron,
            schedule_enabled=schedule_enabled, model=model, task_md=task_md,
            email_spec_md=email_spec_md, sender_profile_id=sender_profile_id,
            recipient_routing_mode=recipient_routing_mode or ("legacy_ids" if recipient_group_id else "catalog_name"),
            stage_settings=self._normalize_stage_settings(stage_settings) if not source_task_id else None)
        if source_task_id:
            self._require_not_deleted(source_task_id)
            source = self.task_repo.get_active_version(source_task_id)
            if not source:
                raise NotFoundError("The source task has no active version to clone")
            if source_task_id == task_id:
                raise ValidationError("A clone needs a new task ID; use Edit to change this task")
            if source_version_hash and source.version_hash != source_version_hash:
                raise ValidationError("The source task changed; reopen Clone to use its current contents")
            files = self._edit_package(source, task_id=task_id, name=name,
                instructions=task_md if task_md is not None else instructions,
                email_spec_md=email_spec_md, runner_type=runner_type,
                recipient_group_id=recipient_group_id, sender_profile_id=sender_profile_id,
                cron=cron, schedule_enabled=schedule_enabled, model=model,
                recipient_routing_mode=recipient_routing_mode, stage_settings=stage_settings)
        definition_dict = yaml.safe_load(files["task.yaml"])
        self.loader.validate_package(definition_dict, files)
        definition = TaskDefinition(**definition_dict)
        self._validate_recipient_groups(definition)
        version_hash = compute_package_hash({name: text.encode("utf-8") for name, text in files.items()})
        status = self.task_repo.get_task_status(task_id)
        if status and status.get("active_version_hash") != version_hash:
            raise ValidationError("Task ID already exists; choose another ID or edit the existing task")
        version = TaskVersion(task_id=task_id, version_hash=version_hash,
            sealed_at=datetime.now(timezone.utc).isoformat(), definition=definition,
            package_files=files, is_active=True)
        publish_version(self.settings.paths.task_versions_dir, version)
        self.task_repo.publish_new_production_task(version, schedule_enabled=schedule_enabled)
        return self.task_repo.get_version(version_hash)

    def get_task_editor(self, task_id: str) -> Dict[str, Any]:
        """Read the actual active package without creating a saved working copy."""
        self._require_not_deleted(task_id)
        status = self.task_repo.get_task_status(task_id)
        if not status or not status.get("active_version_hash"):
            raise NotFoundError("Task has no published version to edit")
        version = self.task_repo.get_version(status["active_version_hash"])
        if not version:
            raise NotFoundError("Active task package was not found")
        task = version.definition
        from researchops.engine.execution_plan import resolve_task_stages
        entity = self.catalog.ensure("task", task_id, task.name)
        groups = list(task.delivery.get("allowed_recipient_group_ids", []))
        return {"task_id": task_id, "name": entity["display_name"], "display_name": entity["display_name"],
            "entity_id": entity["entity_id"], "deleted_at": entity["deleted_at"], "description": task.description or "",
            "task_md": version.package_files["task.md"],
            "instructions": version.package_files["task.md"],
            "email_spec_md": version.package_files.get("email_spec.md", ""),
            "runner_type": task.runner["type"], "model": task.runner.get("model") or "",
            "stage_settings": resolve_task_stages(task),
            "cron": (task.schedule or {}).get("cron", "0 9 * * *"),
            "enabled": bool(status["enabled"]), "schedule_enabled": bool(status["enabled"]),
            "delivery_mode": status["delivery_mode"],
            "recipient_routing_mode": task.delivery.get("recipient_routing_mode", "legacy_ids"),
            "recipient_group_id": groups[0] if groups else "", "recipient_group_ids": groups,
            "sender_profile_id": task.delivery.get("sender_profile_id", "default"),
            "expected_version_hash": version.version_hash, "expected_updated_at": status["updated_at"],
            "research_files": list(task.instructions.get("research_files", [])),
            "compose_files": list(task.instructions.get("compose_files", []))}

    def _edit_package(self, source: TaskVersion, *, task_id: str, name: str,
                      instructions: str, email_spec_md: Optional[str], runner_type: str,
                      recipient_group_id: str, sender_profile_id: str, cron: str,
                      schedule_enabled: bool, model: Optional[str],
                      recipient_routing_mode: Optional[str] = None,
                      stage_settings: Optional[dict] = None) -> Dict[str, str]:
        # Reuse input validation only, never the generated package/body template.
        from researchops.package.production_template import build_production_package
        old_mode = source.definition.delivery.get("recipient_routing_mode", "legacy_ids")
        routing_mode = old_mode if recipient_routing_mode is None else recipient_routing_mode
        build_production_package(task_id=task_id, name=name, instructions=instructions,
            runner_type=runner_type, recipient_group_id=recipient_group_id, cron=cron,
            schedule_enabled=schedule_enabled, model=model, email_spec_md=email_spec_md,
            sender_profile_id=sender_profile_id, recipient_routing_mode=routing_mode)
        files = dict(source.package_files)
        config = source.definition.to_dict()
        config.update(id=task_id, name=name.strip(), enabled=schedule_enabled)
        config.setdefault("schedule", {}).update(cron=cron, timezone="Asia/Seoul")
        config["schedule"].setdefault("misfire_policy", "enqueue_once")
        config["runner"].update(type=runner_type, model=model or None)
        stages = self._normalize_stage_settings(stage_settings, source)
        if stages is not None:
            config["runner"]["stages"] = stages
            config["runner"].update(stages["research"])
        config["delivery"]["sender_profile_id"] = sender_profile_id
        old_groups = config["delivery"].get("allowed_recipient_group_ids", [])
        # Existing multi-group rules remain intact if the UI's initial selection
        # is unchanged; a newly selected group updates only the routing enum.
        if routing_mode != old_mode:
            path = config["output"]["composition_schema"]
            composition_schema = json.loads(files[path])
            properties = composition_schema.setdefault("properties", {})
            old_field, new_field = ("recipient_group_id", "recipient_group_name") if routing_mode == "catalog_name" else ("recipient_group_name", "recipient_group_id")
            # Conditional/dependent routing rules cannot safely be translated by
            # replacing a label: retain them and require an explicit schema edit.
            remainder = {key: value for key, value in composition_schema.items() if key not in {"properties", "required"}}
            remainder["properties"] = {key: value for key, value in properties.items() if key != old_field}
            def references_old_field(value):
                if isinstance(value, dict):
                    return old_field in value or any(references_old_field(item) for item in value.values())
                if isinstance(value, list):
                    return any(references_old_field(item) for item in value)
                return isinstance(value, str) and (value == old_field or value.endswith("/" + old_field))
            if references_old_field(remainder):
                raise ValidationError("중첩된 수신자 스키마 규칙은 먼저 CLI 패키지의 스키마 파일에서 수정하세요. 기존 규칙은 유지했습니다.")
            properties.pop(old_field, None)
            properties[new_field] = ({"type": "string", "minLength": 1, "maxLength": 200}
                if routing_mode == "catalog_name" else {"enum": [recipient_group_id]})
            required = [field for field in composition_schema.get("required", []) if field != old_field]
            for field in (new_field, "recipient_group_reason"):
                if field not in required:
                    required.append(field)
            composition_schema["required"] = required
            properties.setdefault("recipient_group_reason", {"type": "string", "minLength": 1, "maxLength": 2000})
            files[path] = json.dumps(composition_schema, ensure_ascii=False, indent=2)
            config["delivery"]["recipient_routing_mode"] = routing_mode
            if routing_mode == "catalog_name":
                config["delivery"].pop("allowed_recipient_group_ids", None)
            else:
                config["delivery"]["allowed_recipient_group_ids"] = [recipient_group_id]
        elif routing_mode == "legacy_ids" and (not old_groups or recipient_group_id != old_groups[0]):
            config["delivery"]["allowed_recipient_group_ids"] = [recipient_group_id]
            path = config["output"]["composition_schema"]
            composition_schema = json.loads(files[path])
            composition_schema.setdefault("properties", {}).setdefault("recipient_group_id", {})["enum"] = [recipient_group_id]
            files[path] = json.dumps(composition_schema, ensure_ascii=False, indent=2)
        files["task.md"] = instructions
        if email_spec_md is not None:
            files["email_spec.md"] = email_spec_md
            compose = config["instructions"].setdefault("compose_files", ["task.md"])
            if "email_spec.md" not in compose:
                compose.append("email_spec.md")
        if config != source.definition.to_dict():
            files["task.yaml"] = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
        return files

    def update_production_task(self, task_id: str, expected_version_hash: str, *,
                               name: str, instructions: str, runner_type: str,
                               recipient_group_id: str = "", cron: str = "0 9 * * *",
                               schedule_enabled: Optional[bool] = None,
                               email_spec_md: Optional[str] = None, model: Optional[str] = None,
                               sender_profile_id: str = "default", task_md: Optional[str] = None,
                               expected_updated_at: Optional[str] = None,
                               recipient_routing_mode: Optional[str] = None,
                               stage_settings: Optional[dict] = None) -> TaskVersion:
        """Save a real task in place, retaining its identity and immutable history."""
        if self.settings.environment != "production":
            raise ValidationError("Direct task editing requires the production environment")
        self._require_not_deleted(task_id)
        source = self.task_repo.get_version(expected_version_hash)
        status = self.task_repo.get_task_status(task_id)
        if not source or source.task_id != task_id or not status:
            raise ValidationError("Task changed or no longer exists; reopen Edit")
        if schedule_enabled is None:
            schedule_enabled = bool(status["enabled"])
        files = self._edit_package(source, task_id=task_id, name=name,
            instructions=task_md if task_md is not None else instructions,
            email_spec_md=email_spec_md, runner_type=runner_type,
            recipient_group_id=recipient_group_id, sender_profile_id=sender_profile_id,
            cron=cron, schedule_enabled=schedule_enabled, model=model,
            recipient_routing_mode=recipient_routing_mode, stage_settings=stage_settings)
        config = yaml.safe_load(files["task.yaml"])
        self.loader.validate_package(config, files)
        definition = TaskDefinition(**config)
        self._validate_recipient_groups(definition)
        version_hash = compute_package_hash({key: value.encode("utf-8") for key, value in files.items()})
        version = TaskVersion(task_id=task_id, version_hash=version_hash,
            sealed_at=datetime.now(timezone.utc).isoformat(), definition=definition,
            package_files=files, is_active=True)
        publish_version(self.settings.paths.task_versions_dir, version)
        self.task_repo.publish_production_update(version, expected_version_hash=expected_version_hash,
            schedule_enabled=schedule_enabled, expected_delivery_mode=status["delivery_mode"],
            expected_updated_at=expected_updated_at)
        return self.task_repo.get_version(version_hash)

    def get_task_advanced_editor(self, task_id: str) -> Dict[str, Any]:
        """Read the active immutable package without saving intermediate state."""
        self._require_not_deleted(task_id)
        status = self.task_repo.get_task_status(task_id)
        version = self.task_repo.get_active_version(task_id)
        if not status or not version:
            raise NotFoundError("Task has no published version to edit")
        return {"task_id": task_id, "name": version.definition.name,
                "config_yaml": version.package_files["task.yaml"],
                "task_md": version.package_files["task.md"],
                "email_spec_md": version.package_files.get("email_spec.md", ""),
                "expected_version_hash": version.version_hash,
                "expected_updated_at": status["updated_at"],
                "schedule_enabled": bool(status["enabled"]),
                "supplemental_files": sorted(name for name in version.package_files
                    if name not in {"task.yaml", "task.md", "email_spec.md"})}

    def update_production_task_advanced(self, task_id: str, expected_version_hash: str, *,
                                        config_yaml: str, task_md: str, email_spec_md: str,
                                        expected_updated_at: str) -> TaskVersion:
        """Validate and publish submitted YAML/documents with the normal edit guards."""
        if self.settings.environment != "production":
            raise ValidationError("Direct task editing requires the production environment")
        self._require_not_deleted(task_id)
        if not isinstance(expected_updated_at, str) or not expected_updated_at:
            raise ValidationError("Task revision is required; reopen Advanced settings")
        source = self.task_repo.get_version(expected_version_hash)
        status = self.task_repo.get_task_status(task_id)
        if not source or source.task_id != task_id or not status:
            raise ValidationError("Task changed or no longer exists; reopen Advanced settings")
        for label, text in (("YAML", config_yaml), ("조사 지시", task_md), ("메일 작성 규격", email_spec_md)):
            if not isinstance(text, str) or len(text) > 100000 or "\x00" in text:
                raise ValidationError(f"{label}은 100,000자 이하의 텍스트로 입력하세요.")
        if not task_md.strip():
            raise ValidationError("조사 지시를 입력하세요.")
        try:
            config = yaml.safe_load(config_yaml)
        except yaml.YAMLError as exc:
            raise ValidationError("YAML 형식을 확인하세요.") from exc
        if not isinstance(config, dict) or config.get("id") != task_id:
            raise ValidationError("Task ID는 변경할 수 없습니다.")
        # Ownership is stored outside the package. Never accept a YAML field as
        # an ownership reassignment, including legacy/custom metadata fields.
        original = source.definition.to_dict()
        for key in ("owner", "owner_id", "owner_user_id"):
            if config.get(key) != original.get(key):
                raise ValidationError("Task 소유자는 고급 설정에서 변경할 수 없습니다.")
        if config.get("enabled") != original.get("enabled"):
            raise ValidationError("예약 상태는 일반 설정의 실행·일정에서 변경하세요.")
        if not isinstance(config.get("delivery"), dict) or config["delivery"].get("mode") != original["delivery"].get("mode"):
            raise ValidationError("메일 전달 방식은 고급 설정에서 변경할 수 없습니다.")
        files = dict(source.package_files)
        files.update({"task.yaml": config_yaml, "task.md": task_md})
        if "email_spec.md" in files or email_spec_md:
            files["email_spec.md"] = email_spec_md
        # Existing supplemental files are immutable inputs to this editor. A
        # changed path must still resolve inside this preserved package.
        self.loader.validate_package(config, files)
        definition = TaskDefinition(**config)
        self._validate_recipient_groups(definition)
        version_hash = compute_package_hash({name: text.encode("utf-8") for name, text in files.items()})
        version = TaskVersion(task_id=task_id, version_hash=version_hash,
            sealed_at=datetime.now(timezone.utc).isoformat(), definition=definition,
            package_files=files, is_active=True)
        publish_version(self.settings.paths.task_versions_dir, version)
        self.task_repo.publish_production_update(version, expected_version_hash=expected_version_hash,
            schedule_enabled=bool(status["enabled"]), expected_delivery_mode=status["delivery_mode"],
            expected_updated_at=expected_updated_at)
        return self.task_repo.get_version(version_hash)

    def list_templates(self) -> List[Dict[str, Any]]:
        templates = get_available_templates(self.settings)
        return [
            {
                "template_id": t.template_id,
                "name": t.name,
                "description": t.description,
            }
            for t in templates
        ]

    def list_versions(self, task_id: str) -> List[Dict[str, Any]]:
        versions = self.task_repo.list_versions(task_id)
        return [
            {
                "task_id": v.task_id,
                "version_hash": v.version_hash,
                "sealed_at": v.sealed_at,
                "is_active": v.is_active
            }
            for v in versions
        ]

    get_task = show_task
