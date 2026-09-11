"""Durable queue consumer with task-scoped fencing and conservative recovery."""

from datetime import datetime, timedelta, timezone
import logging
import os
import socket
import threading
import time
import uuid
from typing import Optional

from researchops.config import Settings
from researchops.domain.events import AuditEvent
from researchops.domain.models import ScheduledRun
from researchops.errors import ConcurrencyError, NotFoundError

logger = logging.getLogger(__name__)


class WorkerService:
    def __init__(self, settings: Settings, task_repo, run_repo, run_service,
                 state_repo, workspace_mgr, worker_id: Optional[str] = None,
                 lease_duration_seconds: int = 60, heartbeat_interval_seconds: int = 15):
        if lease_duration_seconds <= heartbeat_interval_seconds:
            raise ValueError("Lease duration must exceed heartbeat interval")
        self.settings, self.task_repo, self.run_repo = settings, task_repo, run_repo
        self.run_service, self.state_repo, self.workspace_mgr = run_service, state_repo, workspace_mgr
        self.worker_id = worker_id or f"worker-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.lease_duration_seconds = lease_duration_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def is_stopped(self):
        return self._stop_event.is_set()

    def recover_stale_leases(self) -> int:
        """Never remove another process's ownership based on a timeout alone."""
        recovered = 0
        for lease in self.run_repo.list_expired_leases(datetime.now(timezone.utc).isoformat()):
            run = self.run_repo.get_run(lease.run_id)
            if not run:
                continue
            controls = self.run_repo.get_execution_controls(run.run_id)
            if controls["child_cleanup_verified"]:
                if run.status == "running":
                    self.run_repo.mark_stale_attention(run.run_id,lease.fencing_token)
                self.workspace_mgr.release_workspace_lock(run.task_id,run.run_id,lease.fencing_token,child_cleanup_verified=True)
                self.run_repo.release_lease(run.run_id,lease.fencing_token)
                recovered += 1
            elif run.status == "running":
                self.run_repo.mark_stale_attention(run.run_id,lease.fencing_token)
                self.state_repo.save_audit_event(AuditEvent(entity_type="run",entity_id=run.run_id,event_type="stale_lease_blocked",details={"worker_id":lease.worker_id,"reason":"Descendant cleanup has not been verified; task claim retained"}))
                recovered += 1
        return recovered

    def _start_heartbeat(self, run_id, fencing_token, stop_event, cancel_event):
        def loop():
            next_renewal = time.monotonic()+self.heartbeat_interval_seconds
            while not stop_event.wait(min(1.0,self.heartbeat_interval_seconds)):
                try:
                    if self._stop_event.is_set() or self.run_repo.get_execution_controls(run_id)["cancel_requested"]:
                        cancel_event.set()
                    if time.monotonic() < next_renewal:
                        continue
                    expiry = (datetime.now(timezone.utc)+timedelta(seconds=self.lease_duration_seconds)).isoformat()
                    if not self.run_repo.heartbeat_lease(run_id,fencing_token,expiry):
                        cancel_event.set()
                        return
                    next_renewal = time.monotonic()+self.heartbeat_interval_seconds
                except Exception:
                    logger.exception("Lease monitoring failed for %s",run_id)
                    cancel_event.set()
                    return
        thread = threading.Thread(target=loop,daemon=True,name=f"heartbeat-{run_id}")
        thread.start()
        return thread

    def execute_next_run(self) -> Optional[ScheduledRun]:
        self.recover_stale_leases()
        claim = self.run_repo.claim_next_run(self.worker_id,self.lease_duration_seconds,max_running=self.settings.runner.global_concurrency)
        return self._execute_claim(claim) if claim else None

    def execute_run_id(self, run_id: str, force_dry_run: Optional[bool] = None) -> ScheduledRun:
        run = self.run_repo.get_run(run_id)
        if not run:
            raise NotFoundError(f"Run '{run_id}' not found")
        if run.status != "queued":
            return run
        if force_dry_run:
            self.run_repo.set_force_dry_run(run_id,True)
        claim = self.run_repo.claim_next_run(self.worker_id,self.lease_duration_seconds,max_running=self.settings.runner.global_concurrency,run_id=run_id)
        return self._execute_claim(claim) if claim else self.run_repo.get_run(run_id)

    def _execute_claim(self, claim) -> ScheduledRun:
        run,lease = claim
        hb_stop,cancel_event = threading.Event(),threading.Event()
        heartbeat = self._start_heartbeat(run.run_id,lease.fencing_token,hb_stop,cancel_event)
        try:
            self.state_repo.save_audit_event(AuditEvent(entity_type="run",entity_id=run.run_id,event_type="worker_claimed_lease",details={"worker_id":self.worker_id,"fencing_token":lease.fencing_token}))
            controls = self.run_repo.get_execution_controls(run.run_id)
            if controls["cancel_requested"]:
                cancel_event.set()
            return self.run_service.orchestrator.execute_run(run.run_id,force_dry_run=True if controls["force_dry_run"] else None,fencing_token=lease.fencing_token,cancel_event=cancel_event)
        except Exception as exc:
            # Preflight failures can occur before the orchestrator's own archive
            # try/finally. They still need a durable terminal state.
            current=self.run_repo.get_run(run.run_id)
            if current and current.status=="running":
                try:
                    self.run_repo.update_run_status(run.run_id,"failed","finalize",finished_at=datetime.now(timezone.utc).isoformat(),error_message=f"Worker preflight failed: {type(exc).__name__}",fencing_token=lease.fencing_token)
                except ConcurrencyError:
                    self.run_repo.mark_stale_attention(run.run_id,lease.fencing_token)
            raise
        finally:
            hb_stop.set()
            heartbeat.join(timeout=2)
            # Orchestrator releases this only after descendant cleanup succeeds.
            locked,_ = self.workspace_mgr.is_locked(run.task_id)
            if not locked:
                self.run_repo.mark_cleanup_verified(run.run_id,lease.fencing_token)
                self.run_repo.release_lease(run.run_id,lease.fencing_token)

    def run_worker(self, poll_interval: float = 2.0, once: bool = False,
                   max_runs: Optional[int] = None) -> int:
        completed = 0
        while not self.is_stopped():
            try:
                run = self.execute_next_run()
                if run:
                    completed += 1
                    if max_runs and completed >= max_runs:
                        break
                    continue
                if once:
                    break
            except Exception:
                logger.exception("Worker execution failed")
                if once:
                    break
            self._stop_event.wait(poll_interval)
        return completed
