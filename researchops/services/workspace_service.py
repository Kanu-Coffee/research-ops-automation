"""Workspace inspection and maintenance application service."""

from typing import Any, Dict

from researchops.config import Settings
from researchops.domain.events import AuditEvent
from researchops.workspace.manager import WorkspaceManager
from researchops.storage.repositories import StateRepository


class WorkspaceService:
    def __init__(self, settings: Settings, workspace_mgr: WorkspaceManager, state_repo: StateRepository):
        self.settings = settings
        self.workspace_mgr = workspace_mgr
        self.state_repo = state_repo

    def inspect(self, task_id: str) -> Dict[str, Any]:
        return self.workspace_mgr.inspect_workspace(task_id)

    def reset(self, task_id: str) -> None:
        self.workspace_mgr.reset_workspace(task_id)
        self.state_repo.save_audit_event(
            AuditEvent(
                entity_type="workspace",
                entity_id=task_id,
                event_type="workspace_reset",
                details={}
            )
        )

    def purge(self, task_id: str) -> None:
        self.workspace_mgr.purge_workspace(task_id)
        self.state_repo.save_audit_event(
            AuditEvent(
                entity_type="workspace",
                entity_id=task_id,
                event_type="workspace_purged",
                details={}
            )
        )

    def snapshot(self, task_id: str) -> Dict[str, Any]:
        snapshot_path = self.workspace_mgr.snapshot_workspace(task_id)
        self.state_repo.save_audit_event(
            AuditEvent(
                entity_type="workspace",
                entity_id=task_id,
                event_type="workspace_snapshot_created",
                details={"snapshot_path": str(snapshot_path)}
            )
        )
        return {
            "task_id": task_id,
            "snapshot_path": str(snapshot_path),
            "size_bytes": snapshot_path.stat().st_size
        }
