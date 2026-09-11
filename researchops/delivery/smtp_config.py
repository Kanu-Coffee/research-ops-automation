"""Validated SMTP configuration kept outside worker and archive directories."""

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Dict, List, Optional

import yaml

from researchops.errors import DeliveryError


SMTP_PHASE_TIMEOUT_DEFAULTS = {
    "data_command_timeout_seconds": 120,
    "body_timeout_seconds": 180,
    "final_reply_timeout_seconds": 600,
}


def _boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise DeliveryError(f"{name} must be a boolean")
    return value


def validate_address(address: str) -> str:
    from email.headerregistry import Address
    if not isinstance(address, str) or any(c in address for c in "\r\n\x00"):
        raise DeliveryError("Invalid email address")
    try:
        parsed = Address(addr_spec=address)
        if not parsed.username or not parsed.domain or parsed.addr_spec != address:
            raise ValueError("not a bare mailbox")
    except (ValueError, IndexError) as exc:
        raise DeliveryError("Invalid email address") from exc
    return address


@dataclass
class SmtpSettings:
    host: str = "smtp.gmail.com"
    port: int = 587
    use_tls: bool = True
    use_ssl: bool = False
    username: str = ""
    password: str = field(default="", repr=False)
    sender_email: str = ""
    sender_name: str = "ResearchOps Notifications"
    timeout_seconds: int = 15
    password_configured: bool = False
    data_command_timeout_seconds: int = 120
    body_timeout_seconds: int = 180
    final_reply_timeout_seconds: int = 600

    def masked_password(self) -> str:
        return "****" if self.password or self.password_configured else ""

    def validate(self, *, sending: bool = False) -> None:
        for name in ("host", "username", "password", "sender_email", "sender_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
                raise DeliveryError(f"Invalid SMTP {name}")
        for name in ("use_tls", "use_ssl"):
            _boolean(getattr(self, name), name)
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise DeliveryError("SMTP port must be in 1..65535")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 120:
            raise DeliveryError("SMTP timeout must be in 1..120 seconds")
        for name in SMTP_PHASE_TIMEOUT_DEFAULTS:
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 3600:
                raise DeliveryError(f"SMTP {name} must be in 1..3600 seconds")
        if self.use_tls == self.use_ssl:
            raise DeliveryError("Exactly one of verified STARTTLS or implicit TLS is required")
        if sending:
            if not self.host:
                raise DeliveryError("SMTP host is required")
            validate_address(self.sender_email or self.username)
            if self.host.lower() == "smtp.gmail.com" and (not self.username or not self.password):
                raise DeliveryError("Enter the Gmail account and app password in Delivery settings before sending")


@dataclass
class BuiltinDeliveryConfig:
    enabled: bool = False
    auto_dispatch: bool = False
    smtp: SmtpSettings = field(default_factory=SmtpSettings)
    recipient_groups: Dict[str, List[str]] = field(default_factory=lambda: {
        "release-team": [], "release-stakeholders": [], "researchops-admins": []
    })
    sender_profiles: Dict[str, SmtpSettings] = field(default_factory=dict)

    def get_sender(self, sender_profile_id: str = "default") -> SmtpSettings:
        validate_sender_profile_id(sender_profile_id)
        if sender_profile_id == "default":
            return self.smtp
        if sender_profile_id not in self.sender_profiles:
            raise DeliveryError("Selected sender profile does not exist; choose a registered sender account")
        return self.sender_profiles[sender_profile_id]

    def all_senders(self) -> Dict[str, SmtpSettings]:
        return {"default": self.smtp, **self.sender_profiles}

    def validate(self) -> None:
        _boolean(self.enabled, "enabled")
        _boolean(self.auto_dispatch, "auto_dispatch")
        self.smtp.validate()
        if not isinstance(self.sender_profiles, dict):
            raise DeliveryError("sender_profiles must be an object")
        for profile_id, smtp in self.sender_profiles.items():
            validate_sender_profile_id(profile_id)
            if profile_id == "default":
                raise DeliveryError("The default sender is stored in smtp, not sender_profiles")
            if not isinstance(smtp, SmtpSettings):
                raise DeliveryError("Sender profiles must contain SMTP settings")
            smtp.validate()
        if not isinstance(self.recipient_groups, dict):
            raise DeliveryError("recipient_groups must be an object")
        for group, addresses in self.recipient_groups.items():
            if not isinstance(group, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", group):
                raise DeliveryError("Invalid recipient group ID")
            if not isinstance(addresses, list) or any(not isinstance(a, str) for a in addresses):
                raise DeliveryError("Recipient groups must contain mailbox lists")
            if len(addresses) != len(set(addresses)):
                raise DeliveryError("Duplicate recipient mailbox")
            for address in addresses:
                validate_address(address)

    def to_dict(self, *, include_secrets: bool = False) -> Dict[str, Any]:
        def public_smtp(settings):
            smtp = asdict(settings)
            smtp["password_configured"] = bool(settings.password or settings.password_configured)
            if not include_secrets:
                smtp["password"] = ""
            return smtp
        return {"version": 2, "enabled": self.enabled, "auto_dispatch": self.auto_dispatch,
                "smtp": public_smtp(self.smtp), "recipient_groups": self.recipient_groups,
                "sender_profiles": {key: public_smtp(smtp) for key, smtp in self.sender_profiles.items()}}


def validate_sender_profile_id(profile_id: str) -> str:
    if not isinstance(profile_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", profile_id):
        raise DeliveryError("Sender profile ID must be 2–64 lowercase letters, numbers or hyphens, starting with a letter")
    return profile_id


def preserve_sender_passwords(config: BuiltinDeliveryConfig, previous: BuiltinDeliveryConfig) -> None:
    """A blank UI password preserves only the same profile's credential."""
    prior = previous.all_senders()
    for profile_id, smtp in config.all_senders().items():
        if not smtp.password and profile_id in prior:
            smtp.password = prior[profile_id].password
        smtp.password_configured = bool(smtp.password)


def delivery_revision(config: BuiltinDeliveryConfig, sender_profile_id: str = "default", *, db=None) -> str:
    """Bind the selected sender/authentication and recipient settings, not other accounts.

    Default phase timeouts add no hash fields, preserving existing queued-message
    and approval revisions. Explicit nondefault phase budgets bind the revision;
    changing sender, authentication, recipients or legacy timeout still changes it.
    """
    if db is not None:
        from researchops.services.ownership import entity_owner, installation_owner, scoped_delivery_config
        from researchops.errors import NotFoundError
        try:
            owner = entity_owner(db, "sender", sender_profile_id)
        except NotFoundError:
            if installation_owner(db) is not None:
                raise DeliveryError("Selected sender has no owner") from None
            owner = None  # Standalone Core/CLI before initial account setup.
        config = scoped_delivery_config(db, config, owner)
    smtp = asdict(config.get_sender(sender_profile_id))
    smtp.pop("password_configured", None)
    for name, default in SMTP_PHASE_TIMEOUT_DEFAULTS.items():
        if smtp[name] == default:
            smtp.pop(name)
    payload = {"smtp": smtp, "recipient_groups": config.recipient_groups}
    if sender_profile_id != "default":
        payload["sender_profile_id"] = sender_profile_id
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_delivery_config(config_file: Optional[Path] = None) -> BuiltinDeliveryConfig:
    data: Dict[str, Any] = {}
    if config_file and config_file.exists():
        if config_file.is_symlink() or not stat.S_ISREG(config_file.stat().st_mode):
            raise DeliveryError("SMTP configuration must be a regular non-symlink file")
        if config_file.stat().st_mode & 0o077:
            raise DeliveryError("SMTP configuration must have permissions 0600")
        if config_file.parent.stat().st_mode & 0o077:
            raise DeliveryError("SMTP configuration directory must have permissions 0700")
        try:
            data = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise DeliveryError("Cannot read SMTP configuration") from exc
        if not isinstance(data, dict):
            raise DeliveryError("SMTP configuration must be an object")
        if set(data) - {"version", "enabled", "auto_dispatch", "smtp", "recipient_groups", "sender_profiles"}:
            raise DeliveryError("Unknown SMTP configuration field")
    smtp_data = data.get("smtp", {})
    if not isinstance(smtp_data, dict):
        raise DeliveryError("smtp must be an object")
    fields = SmtpSettings.__dataclass_fields__
    if set(smtp_data) - fields.keys():
        raise DeliveryError("Unknown SMTP settings field")
    smtp = SmtpSettings(**{key: value for key, value in smtp_data.items() if key in fields})
    profile_data = data.get("sender_profiles", {})
    if not isinstance(profile_data, dict):
        raise DeliveryError("sender_profiles must be an object")
    profiles = {}
    for profile_id, values in profile_data.items():
        validate_sender_profile_id(profile_id)
        if not isinstance(values, dict) or set(values) - fields.keys():
            raise DeliveryError("Invalid sender profile SMTP settings")
        profiles[profile_id] = SmtpSettings(**values)
        profiles[profile_id].password_configured = bool(profiles[profile_id].password)
    for env, name in (("SMTP_HOST", "host"), ("SMTP_USER", "username"),
                      ("SMTP_USERNAME", "username"), ("SMTP_PASS", "password"),
                      ("SMTP_PASSWORD", "password"), ("SMTP_SENDER_EMAIL", "sender_email"),
                      ("SMTP_SENDER_NAME", "sender_name")):
        if env in os.environ:
            setattr(smtp, name, os.environ[env])
    if "SMTP_PORT" in os.environ:
        try:
            smtp.port = int(os.environ["SMTP_PORT"])
        except ValueError as exc:
            raise DeliveryError("Invalid SMTP_PORT") from exc
    for env, name in (("SMTP_USE_TLS", "use_tls"), ("SMTP_USE_SSL", "use_ssl")):
        if env in os.environ:
            if os.environ[env].lower() not in ("true", "false", "1", "0"):
                raise DeliveryError(f"Invalid {env}")
            setattr(smtp, name, os.environ[env].lower() in ("true", "1"))
    smtp.password_configured = bool(smtp.password)
    cfg = BuiltinDeliveryConfig(enabled=data.get("enabled", False),
        auto_dispatch=data.get("auto_dispatch", False), smtp=smtp, sender_profiles=profiles,
        recipient_groups=data.get("recipient_groups", BuiltinDeliveryConfig().recipient_groups))
    cfg.validate()
    return cfg


def save_delivery_config(config: BuiltinDeliveryConfig, config_file: Path) -> None:
    config.validate()
    config_file = Path(config_file)
    if config_file.is_symlink() or config_file.parent.is_symlink():
        raise DeliveryError("SMTP configuration path must not be a symlink")
    config_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config_file.parent, 0o700)
    fd, temp_name = tempfile.mkstemp(prefix=".smtp-config-", dir=config_file.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            yaml.safe_dump(config.to_dict(include_secrets=True), stream, sort_keys=False, allow_unicode=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, config_file)
        parent_fd = os.open(config_file.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
