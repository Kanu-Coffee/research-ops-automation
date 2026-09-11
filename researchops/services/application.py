"""Unified Application Service facade for CLI and Web adapters."""

from pathlib import Path
from typing import Optional

from researchops.config import Settings, ensure_directories, load_settings
from researchops.workspace.manager import WorkspaceManager
from researchops.runners.base import BaseRunner
from researchops.engine.orchestrator import Orchestrator
from researchops.delivery.handoff import HandoffPublisher
from researchops.delivery.receipt import ReceiptConsumer
from researchops.services.task_service import TaskService
from researchops.services.run_service import RunService
from researchops.services.workspace_service import WorkspaceService
from researchops.services.delivery_service import DeliveryService
from researchops.services.catalog_service import CatalogService
from researchops.services.doctor import DoctorService
from researchops.services.scheduler import SchedulerService
from researchops.services.worker import WorkerService
from researchops.storage.db import Database
from researchops.storage.repositories import (
    DeliveryRepository, RunRepository, StateRepository, TaskRepository
)


class ApplicationService:
    def __init__(self, settings: Optional[Settings] = None, custom_runner: Optional[BaseRunner] = None):
        self.settings = settings or load_settings()
        ensure_directories(self.settings)

        self.db = Database(self.settings.paths.database)
        self.db.init_schema()

        from researchops.services.auth_service import AuthService
        self.auth = AuthService(self.settings, self.db)

        self.task_repo = TaskRepository(self.db)
        self.run_repo = RunRepository(self.db)
        self.delivery_repo = DeliveryRepository(self.db)
        self.state_repo = StateRepository(self.db)
        self.catalog = CatalogService(self.settings, self.db)
        self.catalog.bootstrap()

        self.workspace_mgr = WorkspaceManager(self.settings, self.db)
        self.handoff_publisher = HandoffPublisher(self.settings, self.delivery_repo, self.state_repo)
        self.receipt_consumer = ReceiptConsumer(
            self.settings, self.delivery_repo, self.run_repo, self.state_repo, self.task_repo
        )
        from researchops.delivery.smtp_dispatcher import SmtpDispatcher
        self.smtp_dispatcher = SmtpDispatcher(
            settings=self.settings,
            receipt_consumer=self.receipt_consumer,
            delivery_repo=self.delivery_repo,
            state_repo=self.state_repo,
            config_file=self.settings.paths.delivery_config_file
        )

        self.orchestrator = Orchestrator(
            settings=self.settings,
            task_repo=self.task_repo,
            run_repo=self.run_repo,
            delivery_repo=self.delivery_repo,
            state_repo=self.state_repo,
            workspace_mgr=self.workspace_mgr,
            custom_runner=custom_runner,
            smtp_dispatcher=self.smtp_dispatcher
        )

        from researchops.services.model_catalog import ModelCatalogService
        self.model_catalog = ModelCatalogService(self.settings)
        self.tasks = TaskService(self.settings, self.task_repo, self.state_repo,
                                 model_catalog=self.model_catalog)
        self.runs = RunService(
            self.settings, self.task_repo, self.run_repo,
            self.delivery_repo, self.state_repo, self.orchestrator, model_catalog=self.model_catalog
        )
        self.workspaces = WorkspaceService(self.settings, self.workspace_mgr, self.state_repo)
        self.delivery = DeliveryService(
            self.settings, self.delivery_repo, self.state_repo,
            self.handoff_publisher, self.receipt_consumer,
            self.smtp_dispatcher, catalog=self.catalog
        )
        self.doctor = DoctorService(self.settings, self.db)
        self.scheduler = SchedulerService(
            settings=self.settings,
            task_repo=self.task_repo,
            run_repo=self.run_repo,
            run_service=self.runs,
            state_repo=self.state_repo
        )
        self.worker = WorkerService(
            settings=self.settings,
            task_repo=self.task_repo,
            run_repo=self.run_repo,
            run_service=self.runs,
            state_repo=self.state_repo,
            workspace_mgr=self.workspace_mgr
        )



def create_application_service(config_path: Optional[str or Path] = None) -> ApplicationService:
    settings = load_settings(config_path)
    return ApplicationService(settings)
