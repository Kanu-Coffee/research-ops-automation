"""Global configuration management for ResearchOps."""

from dataclasses import dataclass, field
import ipaddress
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit
import yaml

from researchops.errors import ConfigError


@dataclass
class PathsConfig:
    repo_root: Path
    tasks_dir: Path
    data_dir: Path
    task_drafts_dir: Path
    task_versions_dir: Path
    task_workspaces_dir: Path
    run_archive_dir: Path
    delivery_outbox_dir: Path
    receipts_dir: Path
    database: Path
    schemas_dir: Path
    delivery_config_file: Optional[Path] = None


@dataclass
class RunnerConfig:
    codex_binary: str = "codex"
    antigravity_binary: str = "agy"
    default_type: str = "codex_exec"
    default_sandbox: str = "task-workspace"
    default_network_profile: str = "none"
    default_timeout_seconds: int = 7200
    default_session_mode: str = "fresh"
    global_concurrency: int = 2
    task_local_home: bool = True
    task_local_tmp: bool = True
    cgroup_root: Optional[Path] = None
    trace_max_bytes: int = 64 * 1024 * 1024
    trace_max_event_bytes: int = 8 * 1024 * 1024
    trace_max_events: int = 10_000
    trace_preview_bytes: int = 64 * 1024


@dataclass
class DeliveryConfig:
    global_handoff_kill_switch: bool = True
    default_mode: str = "dry_run"
    publisher: str = "filesystem_outbox"
    require_verified_receipt_for_success: bool = True
    max_message_bytes: int = 20000000
    smtp_auto_retry: bool = True
    smtp_max_attempts: int = 4
    smtp_retry_base_seconds: int = 1800
    smtp_retry_expiry_seconds: int = 86400


OFFICIAL_IMAGE_HOSTS = (
    "www.bccard.com", "www.hanacard.co.kr", "m.hanacard.co.kr",
    "www.hyundaicard.com", "img.hyundaicard.com",
    "card.kbcard.com", "img1.kbcard.com", "img2.kbcard.com",
    "www.lottecard.co.kr", "image.lottecard.co.kr",
    "www.samsungcard.com", "static11.samsungcard.com",
    "www.shinhancard.com", "pc.wooricard.com",
)


def validate_media_base_url(value: str) -> str:
    """A protected provider binds a literal loopback origin, never a task URL."""
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
        if (parsed.scheme != "http" or not address.is_loopback or
                getattr(address, "ipv4_mapped", None) is not None or
                parsed.path not in ("", "/") or parsed.query or parsed.fragment or
                "@" in parsed.netloc or "%" in parsed.netloc or
                any(ord(ch) <= 32 or ord(ch) == 127 for ch in value) or
                port is not None and not 1 <= port <= 65535):
            raise ValueError
        authority = f"[{address}]" if address.version == 6 else str(address)
        if parsed.netloc not in (authority, authority + f":{port}" if port else authority):
            raise ValueError
        return "http://" + authority + (f":{port}" if port else "")
    except (TypeError, ValueError, AttributeError):
        raise ConfigError("media provider base_url must be a literal loopback HTTP origin") from None


@dataclass(frozen=True)
class MediaProviderConfig:
    base_url: str
    bearer_token_file: Path


@dataclass
class MediaConfig:
    # These are protected application settings, never worker/task arguments.
    providers: Dict[str, MediaProviderConfig] = field(default_factory=dict)
    official_image_hosts: tuple[str, ...] = OFFICIAL_IMAGE_HOSTS
    max_total_bytes: int = 20_000_000
    max_files: int = 64
    max_image_bytes: int = 4 * 1024 * 1024
    file_timeout_seconds: int = 20
    phase_timeout_seconds: int = 120
    mime_reserve_bytes: int = 2_000_000
    mime_part_header_bytes: int = 4096


@dataclass
class WebConfig:
    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = 8765
    trusted_proxy_cidrs: List[str] = field(default_factory=lambda: ["127.0.0.1/32", "::1/128"])
    allowed_hosts: List[str] = field(default_factory=lambda: ["localhost", "127.0.0.1", "::1"])
    require_origin_check: bool = True
    csrf_protection: bool = True
    # Two 100k-character Korean Markdown fields need up to 1.8 MB when a
    # normal browser form percent-encodes their UTF-8 bytes.
    max_request_bytes: int = 2_000_000
    allow_remote_proxy: bool = False
    # Only isolated loopback browser tests may use production-mode HTTP auth.
    allow_insecure_local_auth: bool = False


def validate_web_bind(config: WebConfig, bind: str) -> None:
    """Default loopback; explicit routed proxy mode requires exact socket peers.

    This is an application allowlist, not a firewall or proof of a VPN tunnel.
    The external proxy provides TLS; Web owns account authentication.
    """
    try:
        address = ipaddress.ip_address(bind)
        peers = [ipaddress.ip_network(cidr, strict=True) for cidr in config.trusted_proxy_cidrs]
    except (ValueError, TypeError) as exc:
        raise ConfigError("Invalid Web bind/proxy address") from exc
    if type(config.allow_remote_proxy) is not bool:
        raise ConfigError("web.allow_remote_proxy must be boolean")
    if type(config.allow_insecure_local_auth) is not bool:
        raise ConfigError("web.allow_insecure_local_auth must be boolean")
    if config.allow_insecure_local_auth and (not address.is_loopback or config.allow_remote_proxy):
        raise ConfigError("Insecure local auth requires loopback bind without remote proxy mode")
    if config.allow_remote_proxy:
        if not peers or len(peers) > 8 or any(
            peer.prefixlen != peer.max_prefixlen or peer.network_address.is_unspecified
            or peer.network_address.is_multicast or peer.network_address.is_link_local
            for peer in peers
        ):
            raise ConfigError("Remote proxy mode requires at most 8 exact /32 or /128 peers")
    if address.is_loopback:
        return
    private_ranges = [ipaddress.ip_network(value) for value in
                      ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")]
    if not config.allow_remote_proxy or not any(address in network for network in private_ranges):
        raise ConfigError("Remote Web requires explicit proxy mode and a private interface address")
    if not any(not peer.network_address.is_loopback for peer in peers):
        raise ConfigError("Remote Web requires the actual non-loopback proxy socket peer")
    if not config.require_origin_check or not config.csrf_protection:
        raise ConfigError("Remote Web requires Origin and CSRF protection")


@dataclass
class Settings:
    environment: str = "development"
    timezone: str = "Asia/Seoul"
    paths: PathsConfig = field(default_factory=lambda: None)  # type: ignore
    runner: RunnerConfig = field(default_factory=RunnerConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    web: WebConfig = field(default_factory=WebConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    raw_config: Dict[str, Any] = field(default_factory=dict)


def find_repo_root() -> Path:
    """Find repository root by looking for START_HERE.md or AGENTS.md."""
    override = os.environ.get("RESEARCHOPS_ROOT")
    if override:
        return Path(override).resolve()

    current = Path(__file__).resolve().parent
    for parent in [current, *current.parents]:
        if (parent / "START_HERE.md").exists() or (parent / "AGENTS.md").exists():
            return parent
    return Path.cwd().resolve()


def bundled_path(repo_root: Path, relative: str) -> Path:
    """Source checkout assets take precedence over installed wheel data."""
    source = repo_root / relative
    if source.exists():
        return source.resolve()
    return (Path(sys.prefix) / "share/researchops" / relative).resolve()


def load_settings(config_path: Optional[str | Path] = None) -> Settings:
    repo_root = find_repo_root()

    if config_path is None:
        env_path = os.environ.get("RESEARCHOPS_CONFIG")
        if env_path:
            config_path = Path(env_path)
        else:
            default_config = repo_root / "examples/global/settings.example.yaml"
            if default_config.exists():
                config_path = default_config

    data: Dict[str, Any] = {}
    if config_path:
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"Cannot load settings: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("Settings must be a mapping")
    for section in ("paths", "runner", "delivery", "media", "web", "scheduler", "worker", "network_profiles", "resource_profiles"):
        if section in data and not isinstance(data[section], dict):
            raise ConfigError(f"{section} must be a mapping")
    if data.get("timezone", "Asia/Seoul") != "Asia/Seoul":
        raise ConfigError("timezone must be Asia/Seoul")

    def boolean(section, key, default):
        value = data.get(section, {}).get(key, default)
        if type(value) is not bool:
            raise ConfigError(f"{section}.{key} must be a boolean")
        return value

    def positive(section, key, default):
        value = data.get(section, {}).get(key, default)
        if type(value) is not int or value <= 0:
            raise ConfigError(f"{section}.{key} must be a positive integer")
        return value

    def enum(section, key, default, allowed):
        value = data.get(section, {}).get(key, default)
        if not isinstance(value, str) or value not in allowed:
            raise ConfigError(f"Unsupported {section}.{key}: {value!r}")
        return value

    def resolve_path(p: str, default: str) -> Path:
        raw = data.get("paths", {}).get(p, default)
        if not isinstance(raw, str) or not raw.strip():
            raise ConfigError(f"paths.{p} must be a nonempty path string")
        path = Path(raw)
        if not path.is_absolute():
            path = repo_root / path
        return path.resolve()

    data_dir = resolve_path("data_dir", "./var")
    paths = PathsConfig(
        repo_root=repo_root,
        tasks_dir=resolve_path("tasks_dir", "./tasks"),
        data_dir=data_dir,
        task_drafts_dir=resolve_path("task_drafts_dir", str(data_dir / "task-drafts")),
        task_versions_dir=resolve_path("task_versions_dir", str(data_dir / "task-versions")),
        task_workspaces_dir=resolve_path("task_workspaces_dir", str(data_dir / "task-workspaces")),
        run_archive_dir=resolve_path("run_archive_dir", str(data_dir / "run-archive")),
        delivery_outbox_dir=resolve_path("delivery_outbox_dir", str(data_dir / "delivery-outbox")),
        receipts_dir=resolve_path("receipts_dir", str(data_dir / "receipts")),
        database=resolve_path("database", str(data_dir / "researchops.db")),
        schemas_dir=resolve_path("schemas_dir", str(bundled_path(repo_root, "schemas"))),
        delivery_config_file=resolve_path("delivery_config_file", str(data_dir / "delivery_config.yaml")),
    )


    # Writable execution space must never contain the application audit/secret roots.
    if paths.data_dir in (repo_root, Path("/"), Path.home().resolve()):
        raise ConfigError("data_dir must be a dedicated runtime directory")
    if paths.database == paths.delivery_config_file:
        raise ConfigError("Database and SMTP configuration must be distinct files")
    boundaries = [paths.task_workspaces_dir, paths.run_archive_dir, paths.delivery_outbox_dir,
                  paths.receipts_dir, paths.task_drafts_dir, paths.task_versions_dir, paths.tasks_dir]
    for index, boundary in enumerate(boundaries):
        if boundary == repo_root or boundary == Path("/"):
            raise ConfigError("Runtime/task path must not be repository or filesystem root")
        for other in boundaries[index + 1:]:
            if boundary == other or boundary in other.parents or other in boundary.parents:
                raise ConfigError(f"Task/runtime boundaries overlap: {boundary}, {other}")
    for secret_path in (paths.database, paths.delivery_config_file):
        if any(root == secret_path or root in secret_path.parents for root in boundaries):
            raise ConfigError("Database and delivery secrets must be outside task/output boundaries")
    if any(root == paths.schemas_dir or root in paths.schemas_dir.parents or paths.schemas_dir in root.parents
           for root in boundaries):
        raise ConfigError("Trusted schemas must be outside mutable task/runtime boundaries")

    runner_data = data.get("runner", {})
    for binary_key in ("codex_binary", "antigravity_binary"):
        value = runner_data.get(binary_key, "codex" if binary_key == "codex_binary" else "agy")
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ConfigError(f"runner.{binary_key} must be a nonempty executable name")
    runner = RunnerConfig(
        codex_binary=runner_data.get("codex_binary", "codex"),
        antigravity_binary=runner_data.get("antigravity_binary", "agy"),
        default_type=enum("runner", "default_type", "codex_exec", {"codex_exec", "antigravity_exec", "fake"}),
        default_sandbox=enum("runner", "default_sandbox", "task-workspace", {"task-workspace"}),
        default_network_profile=runner_data.get("default_network_profile", "none"),
        default_timeout_seconds=positive("runner", "default_timeout_seconds", 7200),
        default_session_mode=enum("runner", "default_session_mode", "fresh", {"fresh"}),
        global_concurrency=positive("runner", "global_concurrency", 2),
        task_local_home=boolean("runner", "task_local_home", True),
        task_local_tmp=boolean("runner", "task_local_tmp", True),
        trace_max_bytes=positive("runner", "trace_max_bytes", 64 * 1024 * 1024),
        trace_max_event_bytes=positive("runner", "trace_max_event_bytes", 8 * 1024 * 1024),
        trace_max_events=positive("runner", "trace_max_events", 10_000),
        trace_preview_bytes=positive("runner", "trace_preview_bytes", 64 * 1024),
    )
    if (runner.trace_max_event_bytes > runner.trace_max_bytes or
            runner.trace_preview_bytes > runner.trace_max_bytes):
        raise ConfigError("Trace event/preview limits must not exceed the total trace limit")
    if not runner.task_local_home or not runner.task_local_tmp:
        raise ConfigError("task-local HOME and TMP are mandatory")
    if runner_data.get("cgroup_root") is not None:
        group_root = runner_data["cgroup_root"]
        if not isinstance(group_root, str) or not Path(group_root).is_absolute():
            raise ConfigError("runner.cgroup_root must be an absolute delegated cgroup v2 path")
        runner.cgroup_root = Path(group_root)
    production = data.get("environment") == "production"
    network_profiles = data.setdefault("network_profiles", {
        "none": {"mode": "none"},
        **({"public-research": {"mode": "native"}} if production else {}),
    })
    if runner.default_network_profile not in network_profiles:
        raise ConfigError("runner.default_network_profile is not declared")
    for name, profile in network_profiles.items():
        if not isinstance(profile, dict) or profile.get("mode") not in ("none", "mediated", "native"):
            raise ConfigError(f"Unsupported network profile: {name}")
        if profile.get("mode") == "mediated" and profile.get("block_private_networks") is not True:
            raise ConfigError(f"Mediated profile must block private networks: {name}")
    for name, profile in data.get("resource_profiles", {}).items():
        if not isinstance(profile, dict):
            raise ConfigError(f"Invalid resource profile: {name}")
        for field_name in ("workspace_bytes", "workspace_inodes", "memory_bytes", "max_pids"):
            if type(profile.get(field_name)) is not int or profile[field_name] <= 0:
                raise ConfigError(f"Invalid resource profile limit: {name}.{field_name}")

    delivery_data = data.get("delivery", {})
    delivery = DeliveryConfig(
        global_handoff_kill_switch=boolean("delivery", "global_handoff_kill_switch", not production),
        default_mode=enum("delivery", "default_mode", "handoff" if production else "dry_run", {"dry_run", "handoff"}),
        publisher=enum("delivery", "publisher", "builtin_smtp" if production else "filesystem_outbox", {"filesystem_outbox", "builtin_smtp"}),
        require_verified_receipt_for_success=boolean("delivery", "require_verified_receipt_for_success", True),
        max_message_bytes=positive("delivery", "max_message_bytes", 20000000),
        smtp_auto_retry=boolean("delivery", "smtp_auto_retry", True),
        smtp_max_attempts=positive("delivery", "smtp_max_attempts", 4),
        smtp_retry_base_seconds=positive("delivery", "smtp_retry_base_seconds", 1800),
        smtp_retry_expiry_seconds=positive("delivery", "smtp_retry_expiry_seconds", 86400),
    )
    if delivery.smtp_max_attempts > 10 or delivery.smtp_retry_expiry_seconds > 604800:
        raise ConfigError("SMTP retry limits exceed 10 attempts or 7 days")
    if not delivery.require_verified_receipt_for_success:
        raise ConfigError("Verified dispatcher success evidence is mandatory")
    media_data = data.get("media", {})
    media = MediaConfig()
    for key, maximum in (("max_total_bytes", 20_000_000), ("max_files", 64),
                         ("max_image_bytes", 4 * 1024 * 1024), ("file_timeout_seconds", 30),
                         ("phase_timeout_seconds", 600)):
        value = positive("media", key, getattr(media, key))
        if value > maximum:
            raise ConfigError(f"media.{key} exceeds the supported bound")
        setattr(media, key, value)
    for key in ("mime_reserve_bytes", "mime_part_header_bytes"):
        value = positive("media", key, getattr(media, key))
        if value < getattr(media, key):
            raise ConfigError(f"media.{key} cannot reduce the conservative MIME allowance")
        setattr(media, key, value)
    if "retries" in media_data and (type(media_data["retries"]) is not int or media_data["retries"] != 0):
        raise ConfigError("Media acquisition does not retry requests")
    hosts = media_data.get("official_image_hosts", list(OFFICIAL_IMAGE_HOSTS))
    if (not isinstance(hosts, list) or len(hosts) > 128 or len(set(str(h) for h in hosts)) != len(hosts) or
            any(not isinstance(h, str) or len(h) > 253 or "." not in h or
                h != h.lower() or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                                      for part in h.split(".")) for h in hosts)):
        raise ConfigError("media.official_image_hosts must be exact DNS hostnames")
    media.official_image_hosts = tuple(hosts)
    providers = media_data.get("providers", {})
    if not isinstance(providers, dict) or len(providers) > 16:
        raise ConfigError("media.providers must be a bounded mapping")
    for name, provider in providers.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name) or
                not isinstance(provider, dict) or set(provider) != {"base_url", "bearer_token_file"}):
            raise ConfigError("Invalid protected media provider")
        origin = validate_media_base_url(provider["base_url"])
        token_ref = provider["bearer_token_file"]
        if not isinstance(token_ref, str) or not Path(token_ref).is_absolute() or "\x00" in token_ref:
            raise ConfigError("media bearer_token_file must be an absolute protected file reference")
        token_path = Path(os.path.abspath(token_ref))
        if any(boundary == token_path or boundary in token_path.parents for boundary in boundaries):
            raise ConfigError("Media credentials must be outside task/output boundaries")
        media.providers[name] = MediaProviderConfig(origin, token_path)
    web_data = data.get("web", {})
    web = WebConfig(enabled=boolean("web", "enabled", False),
                    allow_remote_proxy=boolean("web", "allow_remote_proxy", False),
                    allow_insecure_local_auth=boolean("web", "allow_insecure_local_auth", False),
                    bind=web_data.get("bind", "127.0.0.1"), port=positive("web", "port", 8765),
                    trusted_proxy_cidrs=web_data.get("trusted_proxy_cidrs", ["127.0.0.1/32", "::1/128"]),
                    allowed_hosts=web_data.get("allowed_hosts", ["localhost", "127.0.0.1", "::1"]),
                    require_origin_check=boolean("web", "require_origin_check", True),
                    csrf_protection=boolean("web", "csrf_protection", True),
                    max_request_bytes=positive("web", "max_request_bytes", 2_000_000))
    if not web.require_origin_check or not web.csrf_protection or web.port > 65535:
        raise ConfigError("Web requires Origin/CSRF checks and a valid TCP port")
    for field_name in ("trusted_proxy_cidrs", "allowed_hosts"):
        value = getattr(web, field_name)
        if not isinstance(value, list) or not value or any(not isinstance(v, str) or not v for v in value):
            raise ConfigError(f"web.{field_name} must be a nonempty list of strings")
    validate_web_bind(web, web.bind)
    if any(any(ch in host for ch in ("/", "*", "@", "\r", "\n")) for host in web.allowed_hosts):
        raise ConfigError("web.allowed_hosts must contain literal hostnames")
    boolean("scheduler", "enabled", True)
    for key, default in (("tick_seconds", 60), ("tick_timeout_seconds", 30),
                         ("stale_lease_seconds", 300), ("max_queued_runs_per_task", 1)):
        positive("scheduler", key, default)
    if data.get("scheduler", {}).get("max_queued_runs_per_task", 1) != 1:
        raise ConfigError("Only one queued run per task is supported")
    for key, default in (("poll_interval_seconds", 2), ("error_backoff_initial_seconds", 5),
                         ("error_backoff_max_seconds", 60), ("max_consecutive_errors", 5)):
        positive("worker", key, default)

    return Settings(
        environment=data.get("environment", "development"),
        timezone=data.get("timezone", "Asia/Seoul"),
        paths=paths,
        runner=runner,
        delivery=delivery,
        web=web,
        media=media,
        raw_config=data,
    )


def ensure_directories(settings: Settings) -> None:
    """Ensure all runtime directories exist."""
    p = settings.paths
    for d in [
        p.tasks_dir,
        p.data_dir,
        p.task_drafts_dir,
        p.task_versions_dir,
        p.task_workspaces_dir,
        p.run_archive_dir,
        p.delivery_outbox_dir,
        p.receipts_dir,
        p.database.parent,
    ]:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
