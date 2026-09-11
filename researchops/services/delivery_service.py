"""Delivery handoff and receipt application service."""

from dataclasses import asdict
from contextlib import contextmanager
import fcntl
from functools import wraps
import os
from pathlib import Path
import stat
import threading
from typing import Any, Dict, List, Optional, Tuple

from researchops.config import Settings
from researchops.domain.models import DeliveryHandoff, DeliveryReceipt
from researchops.errors import DeliveryError, NotFoundError, ResearchOpsError, ValidationError
from researchops.delivery.handoff import HandoffPublisher
from researchops.delivery.receipt import ReceiptConsumer
from researchops.storage.repositories import DeliveryRepository, StateRepository


from researchops.delivery.smtp_dispatcher import SmtpDispatcher
from researchops.delivery.smtp_config import (BuiltinDeliveryConfig, SmtpSettings,
    preserve_sender_passwords, validate_sender_profile_id)

_SENDER_OWNER = object()


def _serialize_config_edit(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._config_edit_lock():
            return method(self, *args, **kwargs)
    return wrapped


class DeliveryService:
    def __init__(
        self,
        settings: Settings,
        delivery_repo: DeliveryRepository,
        state_repo: StateRepository,
        handoff_publisher: HandoffPublisher,
        receipt_consumer: ReceiptConsumer,
        smtp_dispatcher: Optional[SmtpDispatcher] = None,
        catalog=None,
    ):
        self.settings = settings
        self.delivery_repo = delivery_repo
        self.state_repo = state_repo
        self.handoff_publisher = handoff_publisher
        self.receipt_consumer = receipt_consumer
        self.smtp_dispatcher = smtp_dispatcher
        if catalog is None:
            from researchops.services.catalog_service import CatalogService
            catalog = CatalogService(settings, delivery_repo.db)
        self.catalog = catalog
        self._config_mutex = threading.RLock()
        self._config_edit_depth = 0

    @contextmanager
    def _config_edit_lock(self):
        """Serialize whole read/modify/write operations across Web processes."""
        with self._config_mutex:
            if self._config_edit_depth:
                yield
                return
            directory = self.settings.paths.delivery_config_file.parent
            if directory.is_symlink():
                raise DeliveryError("SMTP configuration directory must not be a symlink")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / ".delivery-config.lock"
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                        info.st_uid != os.getuid() or info.st_mode & 0o077):
                    raise DeliveryError("SMTP configuration lock must be a private regular file")
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._config_edit_depth = 1
                try:
                    yield
                finally:
                    self._config_edit_depth = 0
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def list_handoffs(self, task_id: Optional[str] = None, status: Optional[str] = None) -> List[DeliveryHandoff]:
        return self.delivery_repo.list_handoffs(task_id=task_id, status=status)

    def show_handoff(self, handoff_id: str) -> Dict[str, Any]:
        handoff = self.delivery_repo.get_handoff(handoff_id)
        if not handoff:
            raise NotFoundError(f"Handoff '{handoff_id}' not found")
        receipt = None
        if handoff.external_receipt_id:
            receipt = self.delivery_repo.get_receipt(handoff.external_receipt_id)

        return {
            "handoff": {
                "handoff_id": handoff.handoff_id,
                "idempotency_key": handoff.idempotency_key,
                "run_id": handoff.run_id,
                "task_id": handoff.task_id,
                "mode": handoff.mode,
                "status": handoff.status,
                "recipient_group_id": handoff.recipient_group_id,
                "published_at": handoff.published_at,
                "acknowledged_at": handoff.acknowledged_at,
                "external_delivery_status": handoff.external_delivery_status,
                "delivery_request_sha256": handoff.delivery_request_sha256
            },
            "delivery_request": handoff.delivery_request,
            "receipt": receipt.to_dict() if receipt else None,
            "smtp_attempt": self.show_smtp_job(handoff_id)
        }

    def show_smtp_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Safe diagnostic/status view excludes credentials, MIME and envelope addresses."""
        from researchops.delivery.queue import SmtpQueue
        job = SmtpQueue(self.delivery_repo.db).get(job_id)
        if not job:
            return None
        fields = ("job_id", "handoff_id", "status", "phase", "message_id", "mime_sha256",
                  "created_at", "updated_at", "error", "server_reply")
        import json
        return {**{field:job[field] for field in fields},
                "sender_profile_id":json.loads(job["envelope_json"]).get("sender_profile_id", "default")}

    def republish_handoff(self, handoff_id: str) -> DeliveryHandoff:
        return self.handoff_publisher.republish_handoff(handoff_id)

    def email_retry_status(self, handoff_id: str) -> Dict[str, Any]:
        """Read retry eligibility and safe attempt history without SMTP access."""
        if not self.smtp_dispatcher:
            return {"eligible": False, "uncertain": False, "can_retry_uncertain": False,
                    "can_republish": False, "block_reason": "SMTP_DISPATCHER_UNAVAILABLE",
                    "attempts": [], "next_attempt_at": None}
        return self.smtp_dispatcher.retry_status(handoff_id)

    def retry_email(self, handoff_id: str, *, request_key: str,
                    allow_uncertain: bool = False, reason: str = "") -> Dict[str, Any]:
        """Queue a new SMTP attempt for the preserved message; never run a model."""
        if not self.smtp_dispatcher:
            raise DeliveryError("SMTP Dispatcher is not initialized")
        return self.smtp_dispatcher.retry_email(handoff_id, request_key=request_key,
            allow_uncertain=allow_uncertain, reason=reason)

    def import_receipt(self, receipt_data_or_file: str or Path) -> Tuple[DeliveryReceipt, DeliveryHandoff]:
        return self.receipt_consumer.import_and_verify_receipt(receipt_data_or_file)

    def reconcile_receipt(self, handoff_id: str) -> Optional[DeliveryHandoff]:
        # SMTP attempts finalize atomically; imported JSON cannot reconcile run state.
        return self.delivery_repo.get_handoff(handoff_id)

    def get_delivery_config(self) -> BuiltinDeliveryConfig:
        if self.smtp_dispatcher:
            config = self.smtp_dispatcher.get_config()
        else:
            from researchops.delivery.smtp_config import load_delivery_config
            config = load_delivery_config(self.settings.paths.delivery_config_file)
        for smtp in config.all_senders().values():
            smtp.password_configured = bool(smtp.password)
            smtp.password = ""
        self.catalog.bootstrap_delivery(config)
        return config

    def operating_status(self, sender_profile_id: str = "default", *, owner_user_id=_SENDER_OWNER) -> Dict[str, Any]:
        """Configuration readiness, without credentials, addresses or network calls."""
        config = self.get_delivery_config()
        entry = self.catalog.require_active("sender", sender_profile_id)
        owner = entry["owner_user_id"] if owner_user_id is _SENDER_OWNER else owner_user_id
        if entry["owner_user_id"] != owner:
            raise NotFoundError("발신 계정을 찾을 수 없습니다.")
        from researchops.services.ownership import scoped_delivery_config
        config = scoped_delivery_config(self.delivery_repo.db, config, owner)
        smtp = config.get_sender(sender_profile_id)
        gmail = smtp.host.lower() == "smtp.gmail.com"
        missing = []
        if self.settings.delivery.global_handoff_kill_switch:
            missing.append("Global email sending is disabled")
        if not config.enabled:
            missing.append("SMTP delivery is disabled")
        if not smtp.host:
            missing.append("SMTP server is required")
        if not (smtp.sender_email or smtp.username):
            missing.append("Sender email is required")
        if gmail and not smtp.username:
            missing.append("Gmail account address is required")
        if (smtp.username or gmail) and not smtp.password_configured:
            missing.append("Gmail app password is required" if gmail else "SMTP password is required")
        active_groups = self.catalog.names("recipient_group", owner_user_id=owner)
        nonempty = sum(bool(addresses) for key, addresses in config.recipient_groups.items() if key in active_groups)
        if not nonempty:
            missing.append("Create a recipient group with at least one email address")
        return {
            "production": self.settings.environment == "production",
            "delivery_enabled": config.enabled,
            "global_send_blocked": self.settings.delivery.global_handoff_kill_switch,
            "smtp_configured": bool(smtp.host and (smtp.sender_email or smtp.username)
                                    and (not (gmail or smtp.username) or smtp.password_configured)),
            "sender_profile_id": sender_profile_id,
            "sender_profile_count": sum(key in self.catalog.names("sender", owner_user_id=owner) for key in config.all_senders()),
            "gmail": gmail,
            "scheduler_enabled": self.settings.raw_config.get("scheduler", {}).get("enabled", True),
            "recipient_group_count": sum(key in active_groups for key in config.recipient_groups),
            "nonempty_group_count": nonempty,
            "missing": missing,
        }

    @_serialize_config_edit
    def save_delivery_config(self, config: BuiltinDeliveryConfig) -> None:
        if self.smtp_dispatcher:
            self.smtp_dispatcher.save_config(config)
        else:
            from researchops.delivery.smtp_config import save_delivery_config, load_delivery_config
            if self.settings.paths.delivery_config_file.exists():
                preserve_sender_passwords(config, load_delivery_config(self.settings.paths.delivery_config_file))
            save_delivery_config(config, self.settings.paths.delivery_config_file)
        self.catalog.bootstrap_delivery(config)

    @_serialize_config_edit
    def save_sender_profile(self, profile_id: str, smtp_settings: SmtpSettings, *, create: bool = False,
                            enabled: Optional[bool] = None) -> None:
        """Create/update one protected sender without changing any other account.

        An empty password preserves this profile's existing password. New profiles
        never inherit the default account's credential.
        """
        validate_sender_profile_id(profile_id)
        if not isinstance(smtp_settings, SmtpSettings):
            raise DeliveryError("Sender profile requires SMTP settings")
        smtp_settings.validate()
        config = self.get_delivery_config()
        exists = profile_id in config.all_senders()
        if create and exists:
            raise DeliveryError("Sender profile already exists; choose another ID or edit the existing account")
        if not create and not exists:
            raise DeliveryError("Sender profile does not exist; register a new account first")
        if exists:
            self.catalog.require_active("sender", profile_id)
        if profile_id == "default":
            config.smtp = smtp_settings
        else:
            config.sender_profiles[profile_id] = smtp_settings
        if enabled is not None:
            if type(enabled) is not bool:
                raise DeliveryError("SMTP delivery enabled must be a boolean")
            config.enabled = enabled
        self.save_delivery_config(config)

    @_serialize_config_edit
    def update_sender_account(self, profile_id: str, smtp_settings: SmtpSettings, *,
                              display_name: str = "", enabled: Optional[bool] = None) -> None:
        """Keep a cosmetic edit out of the protected delivery configuration."""
        if display_name:
            self.catalog.validate_name(display_name)
        self.catalog.require_active("sender", profile_id)
        config = self.get_delivery_config()
        previous = config.get_sender(profile_id)
        fields = ("host", "port", "use_tls", "use_ssl", "username", "sender_email", "sender_name")
        changed = bool(smtp_settings.password) or any(
            getattr(smtp_settings, key) != getattr(previous, key) for key in fields)
        if changed or (enabled is not None and enabled != config.enabled):
            # This normalization belongs to a delivery change, not a name edit.
            smtp_settings.sender_email = smtp_settings.sender_email or smtp_settings.username
            self.save_sender_profile(profile_id, smtp_settings, enabled=enabled)
        if display_name:
            self.catalog.rename("sender", profile_id, display_name)

    @_serialize_config_edit
    def create_sender_account(self, display_name: str, smtp_settings: SmtpSettings, *,
                              enabled: Optional[bool] = None, request_key: Optional[str] = None) -> str:
        """Allocate a routing token; the editable label is never a credential key."""
        self.catalog.validate_name(display_name)
        if not isinstance(smtp_settings, SmtpSettings):
            raise DeliveryError("Sender account requires SMTP settings")
        smtp_settings.validate()
        if enabled is not None and type(enabled) is not bool:
            raise DeliveryError("SMTP delivery enabled must be a boolean")
        config = self.get_delivery_config()
        entry = self.catalog.allocate("sender", display_name, request_key=request_key)
        key = entry["legacy_key"]
        if key not in config.all_senders():
            self.save_sender_profile(key, smtp_settings, create=True, enabled=enabled)
        else:
            from researchops.delivery.smtp_config import load_delivery_config
            # Compare in the protected process without putting passwords (or
            # password-derived hashes) in the catalog/request database.
            previous = load_delivery_config(self.settings.paths.delivery_config_file)
            existing = asdict(previous.get_sender(key))
            requested = asdict(smtp_settings)
            existing.pop("password_configured", None)
            requested.pop("password_configured", None)
            if existing != requested or (enabled is not None and enabled != previous.enabled):
                raise ValidationError("This creation request was already used with different SMTP settings; open the existing account or start a new creation")
        return key

    @_serialize_config_edit
    def create_recipient_group(self, display_name: str, addresses: List[str], *,
                               request_key: Optional[str] = None) -> str:
        self.catalog.validate_name(display_name)
        # Validate the complete payload before reserving an identity.
        BuiltinDeliveryConfig(recipient_groups={"validation-group": addresses}).validate()
        config = self.get_delivery_config()
        entry = self.catalog.allocate("recipient_group", display_name, request_key=request_key)
        key = entry["legacy_key"]
        if key not in config.recipient_groups:
            config.recipient_groups[key] = addresses
            self.save_delivery_config(config)
        elif config.recipient_groups[key] != addresses:
            raise ValidationError("This creation request was already used with different recipients; open the existing group or start a new creation")
        return key

    @_serialize_config_edit
    def save_recipient_group(self, group_id: str, addresses: List[str]) -> None:
        config = self.get_delivery_config()
        self.catalog.require_active("recipient_group", group_id)
        if group_id not in config.recipient_groups:
            raise DeliveryError("Recipient group does not exist; create a group first")
        config.recipient_groups[group_id] = addresses
        self.save_delivery_config(config)

    def rename_recipient_group(self, group_id: str, display_name: str):
        return self.catalog.rename("recipient_group", group_id, display_name)

    def rename_sender_account(self, profile_id: str, display_name: str):
        return self.catalog.rename("sender", profile_id, display_name)

    def delete_recipient_group(self, group_id: str):
        return self.catalog.delete("recipient_group", group_id)

    def delete_sender_account(self, profile_id: str):
        return self.catalog.delete("sender", profile_id)

    def test_smtp_connection(self, smtp_settings: Optional[SmtpSettings] = None, *, sender_profile_id: str = "default") -> Tuple[bool, str]:
        try:
            if smtp_settings is None:
                self.get_delivery_config()
                self.catalog.require_active("sender", sender_profile_id)
        except ResearchOpsError as exc:
            return False, str(exc)
        if not self.smtp_dispatcher:
            return False, "SMTP Dispatcher is not initialized"
        return self.smtp_dispatcher.test_connection(smtp_settings, sender_profile_id=sender_profile_id)

    def send_test_email(self, to_email: str, smtp_settings: Optional[SmtpSettings] = None, *, sender_profile_id: str = "default") -> Tuple[bool, str]:
        try:
            self.get_delivery_config()
            self.catalog.require_active("sender", sender_profile_id)
        except ResearchOpsError as exc:
            return False, str(exc)
        if not self.smtp_dispatcher:
            return False, "SMTP Dispatcher is not initialized"
        return self.smtp_dispatcher.send_test_email(to_email, smtp_settings, sender_profile_id=sender_profile_id)

    def dispatch_handoff(self, handoff_id: str) -> Tuple[bool, Optional[DeliveryReceipt], str]:
        if not self.smtp_dispatcher:
            return False, None, "SMTP Dispatcher is not initialized"
        return self.smtp_dispatcher.dispatch_handoff(handoff_id)

    def dispatch_all_pending(self) -> List[Tuple[str, bool, str]]:
        if not self.smtp_dispatcher:
            return []
        return self.smtp_dispatcher.dispatch_all_pending()
