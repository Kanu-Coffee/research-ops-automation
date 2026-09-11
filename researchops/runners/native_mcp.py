"""Read-only native MCP metadata and the production CLI control environment.

The CLI owns configuration merging, MCP startup and authentication. This module
never opens credential stores, starts a server, or treats configured metadata as
a successful connection. Secret-bearing configuration values stay out of its
inventory, errors and revision fingerprint.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tomllib

from researchops.errors import WorkspaceError
from researchops.runners.development_runner import control_environment
from researchops.strict_json import strict_json_loads
from researchops.workspace.security import assert_path_contained, read_safe_bytes


MAX_CONFIG_BYTES = 1_000_000
MAX_SERVERS = 128
MAX_ENV_REFERENCES = 128
MAX_ENV_VALUE_BYTES = 32_768
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,127}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_BASE_ENV = {"HOME", "CODEX_HOME", "PATH", "LANG", "TZ", "TMPDIR", "USER", "LOGNAME",
             "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
_PROTECTED_ENV = _BASE_ENV | {"SHELL", "IFS", "ENV", "BASH_ENV", "CDPATH", "SHELLOPTS", "PS4"}
_PROTECTED_PREFIXES = ("SMTP", "RESEARCHOPS", "SSH", "LD_", "DYLD_", "BASH", "ZSH", "PYTHON", "NODE_", "GIT_")
_APPROVAL_MODES = {"auto", "prompt", "writes", "approve"}


def _provider(provider):
    aliases = {"codex": "codex_exec", "codex_exec": "codex_exec",
               "antigravity": "antigravity_exec", "antigravity_exec": "antigravity_exec"}
    if provider not in aliases:
        raise ValueError("Unsupported native MCP provider")
    return aliases[provider]


def _absolute_path(value):
    if not isinstance(value, (str, Path)):
        raise ValueError("Native configuration root must be an absolute path")
    value = str(value)
    if not value or len(value) > 4096 or any(ord(char) < 32 for char in value) or not Path(value).is_absolute():
        raise ValueError("Native configuration root must be an absolute path")
    return Path(os.path.abspath(value))


def _home(environ):
    return _absolute_path(environ.get("HOME", str(Path.home())))


def _production_path(binary, home):
    # Never inherit PATH from a task or arbitrary control-process environment.
    candidates = []
    if isinstance(binary, str) and Path(binary).is_absolute() and "\x00" not in binary:
        candidates.append(Path(binary).parent)
    candidates += [home / ".local/bin", home / ".npm-global/bin", Path("/usr/local/bin"),
                   Path("/usr/bin"), Path("/bin")]
    return ":".join(dict.fromkeys(str(path) for path in candidates if path.is_dir()))


def _protected_env(name):
    upper = name.upper()
    return upper in _PROTECTED_ENV or upper.startswith(_PROTECTED_PREFIXES)


def _usable_value(value):
    return (isinstance(value, str) and bool(value) and "\x00" not in value
            and len(value.encode("utf-8", errors="replace")) <= MAX_ENV_VALUE_BYTES)


def _issue(issues, code, source, server=None):
    issue = {"code": code, "source": source}
    if server is not None:
        issue["server"] = server
    if issue not in issues:
        issues.append(issue)


def _read_config(path, source, scope, kind, issues):
    info = {"scope": scope, "source": source, "status": "missing"}
    try:
        # Rooting at / makes every ancestor descriptor-relative and no-follow.
        assert_path_contained(path, Path(path.anchor))
        metadata = path.lstat()
    except FileNotFoundError:
        return info, None, None
    except (OSError, WorkspaceError):
        info["status"] = "unsafe"
        _issue(issues, "config_unreadable_or_unsafe", source)
        return info, None, None
    try:
        raw = read_safe_bytes(path, Path(path.anchor), MAX_CONFIG_BYTES)
    except WorkspaceError:
        info["status"] = "unsafe"
        _issue(issues, "config_unreadable_or_unsafe", source)
        return info, None, None
    try:
        document = (tomllib.loads(raw.decode("utf-8")) if kind == "toml"
                    else strict_json_loads(raw, max_bytes=MAX_CONFIG_BYTES))
        if not isinstance(document, dict):
            raise ValueError("Expected native configuration object")
    except (ValueError, UnicodeError, RecursionError):
        info["status"] = "invalid"
        _issue(issues, "config_invalid", source)
        return info, None, None
    info["status"] = "loaded"
    # Fingerprint public metadata, not raw config or credential values. A touch
    # can change this revision; it is not a cryptographic seal of the config.
    stamp = (metadata.st_mtime_ns, metadata.st_size)
    return info, document, stamp


def _names(value, pattern, issues, source, server, *, limit=MAX_ENV_REFERENCES):
    if not isinstance(value, list) or len(value) > limit:
        _issue(issues, "server_metadata_invalid", source, server)
        return []
    accepted = []
    for name in value:
        if isinstance(name, str) and pattern.fullmatch(name):
            if name not in accepted:
                accepted.append(name)
        else:
            _issue(issues, "server_metadata_invalid", source, server)
    return sorted(accepted)


def _env_references(server, provider, issues, source, name):
    if provider != "codex_exec":
        return [], []
    references, remote = [], []
    variables = server.get("env_vars", [])
    if not isinstance(variables, list) or len(variables) > MAX_ENV_REFERENCES:
        _issue(issues, "server_metadata_invalid", source, name)
        variables = []
    for variable in variables:
        if isinstance(variable, str):
            references += _names([variable], _ENV_NAME, issues, source, name)
        elif (isinstance(variable, dict) and set(variable) <= {"name", "source"}
              and isinstance(variable.get("source"), str)
              and variable.get("source") in {"local", "remote"}):
            found = _names([variable.get("name")], _ENV_NAME, issues, source, name)
            references += found
            if variable["source"] == "remote":
                remote += found
        else:
            _issue(issues, "server_metadata_invalid", source, name)
    if "bearer_token_env_var" in server:
        references += _names([server["bearer_token_env_var"]], _ENV_NAME, issues, source, name)
    headers = server.get("env_http_headers", {})
    if not isinstance(headers, dict):
        _issue(issues, "server_metadata_invalid", source, name)
    else:
        references += _names(list(headers.values()), _ENV_NAME, issues, source, name)
    if len(set(references)) > MAX_ENV_REFERENCES:
        _issue(issues, "server_metadata_invalid", source, name)
        return [], []
    return sorted(set(references)), sorted(set(remote))


def _server_metadata(name, server, provider, source, environ, path, issues):
    enabled_key = "enabled" if provider == "codex_exec" else "disabled"
    enabled_value = server.get(enabled_key, provider == "codex_exec")
    if type(enabled_value) is not bool:
        _issue(issues, "server_metadata_invalid", source, name)
        enabled = False
    else:
        enabled = enabled_value if provider == "codex_exec" else not enabled_value
    required = server.get("required", False) if provider == "codex_exec" else False
    if type(required) is not bool:
        required = False
        _issue(issues, "server_metadata_invalid", source, name)
    url_key = "url" if provider == "codex_exec" else "serverUrl"
    has_command = isinstance(server.get("command"), str) and bool(server["command"])
    has_url = isinstance(server.get(url_key), str) and bool(server[url_key])
    transport = "stdio" if has_command and not has_url else "http" if has_url and not has_command else "unknown"
    if transport == "unknown":
        _issue(issues, "server_transport_invalid", source, name)
    references, remote_env = _env_references(server, provider, issues, source, name)
    # HTTP authentication is read from the CLI environment. A server's inline
    # env applies only to its STDIO process and cannot satisfy HTTP token refs.
    inline_env = server.get("env", {}) if transport == "stdio" else {}
    if not isinstance(inline_env, dict):
        inline_env = {}
        _issue(issues, "server_metadata_invalid", source, name)
    blocked = [key for key in references if _protected_env(key)]
    missing = [key for key in references if key not in remote_env and not _protected_env(key)
               and not _usable_value(inline_env.get(key)) and not _usable_value(environ.get(key))]
    if enabled and remote_env:
        _issue(issues, "remote_environment_reference_not_forwarded", source, name)
    if enabled and blocked:
        _issue(issues, "protected_environment_reference", source, name)
    if enabled and missing:
        _issue(issues, "required_environment_missing", source, name)
    executable_ready = None
    if has_command:
        command = server["command"]
        if "\x00" in command or len(command) > 4096:
            executable_ready = False
        elif Path(command).is_absolute() or "/" not in command:
            executable_ready = shutil.which(command, path=path) is not None
        else:
            # Relative command/cwd resolution belongs to the native loader.
            _issue(issues, "relative_executable_not_checked", source, name)
        if enabled and executable_ready is False:
            _issue(issues, "executable_unavailable", source, name)
    allow_key = "enabled_tools" if provider == "codex_exec" else None
    deny_key = "disabled_tools" if provider == "codex_exec" else "disabledTools"
    allowed = (_names(server[allow_key], _TOOL_NAME, issues, source, name)
               if allow_key is not None and allow_key in server else None)
    denied = _names(server.get(deny_key, []), _TOOL_NAME, issues, source, name)
    mode = server.get("default_tools_approval_mode") if provider == "codex_exec" else None
    if mode is not None and (not isinstance(mode, str) or mode not in _APPROVAL_MODES):
        mode = None
        _issue(issues, "server_metadata_invalid", source, name)
    auth_keys = ("bearer_token_env_var", "http_headers", "env_http_headers", "oauth", "experimental_bearer_token") if provider == "codex_exec" else ("headers", "oauth", "authProviderType")
    return {"name": name, "source": source, "transport": transport, "enabled": enabled,
            "required": required, "authentication_configured": any(bool(server.get(key)) for key in auth_keys),
            "required_env_names": references, "missing_env_names": missing, "blocked_env_names": blocked,
            "remote_env_names": remote_env,
            "executable": {"configured": has_command, "available": executable_ready},
            "tool_policy": {"enabled_tools": allowed, "disabled_tools": denied, "default_approval_mode": mode}}


def _inventory(provider, binary, project_dir, environ):
    provider = _provider(provider)
    issues, sources, servers, stamps, forward_names = [], [], [], [], set()
    limitations = ["Metadata only: no MCP connection, authentication or tool call was attempted.",
                   "Native CLI resolves effective configuration, permissions and cached authentication.",
                   "Revision covers sanitized metadata and file timestamps, not configuration or credential contents."]
    try:
        home = _home(environ)
        path = _production_path(binary, home)
        if provider == "codex_exec":
            root = _absolute_path(environ.get("CODEX_HOME", str(home / ".codex")))
            definitions = [(root / "config.toml", "<CODEX_HOME>/config.toml", "user", "toml")]
            limitations += ["System, profile and trusted project configuration are not merged by this inventory.",
                            "Plugin MCP servers and Apps are resolved by Codex; they are not enumerated here."]
        else:
            definitions = [(home / ".gemini/config/mcp_config.json", "~/.gemini/config/mcp_config.json", "user", "json")]
            limitations += ["Plugin MCP servers are resolved by Antigravity; they are not enumerated here.",
                            "Antigravity helper environment and configuration reload are not verified here.",
                            "Additional token environment is not forwarded to Antigravity; use native per-server configuration."]
        if project_dir is not None:
            project = _absolute_path(project_dir)
            if provider == "antigravity_exec":
                definitions.append((project / ".agents/mcp_config.json", "<project>/.agents/mcp_config.json", "project", "json"))
                limitations.append("Project and global entries are separate metadata; native precedence is not inferred.")
            else:
                sources.append({"scope": "project", "source": "<project>/.codex/config.toml", "status": "not_loaded"})
    except ValueError:
        _issue(issues, "configuration_root_invalid", "native")
        definitions, path = [], "/usr/local/bin:/usr/bin:/bin"
    for file, source, scope, kind in definitions:
        info, document, stamp = _read_config(file, source, scope, kind, issues)
        sources.append(info)
        if stamp is not None:
            stamps.append((source, stamp))
        if document is None:
            continue
        configured = document.get("mcp_servers" if provider == "codex_exec" else "mcpServers", {})
        if not isinstance(configured, dict):
            info["status"] = "invalid"
            _issue(issues, "config_invalid", source)
            continue
        if len(configured) > MAX_SERVERS:
            _issue(issues, "server_count_exceeded", source)
            continue
        for name, server in sorted(configured.items()):
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                _issue(issues, "server_name_invalid", source)
                continue
            if not isinstance(server, dict):
                _issue(issues, "server_metadata_invalid", source, name)
                continue
            entry = _server_metadata(name, server, provider, source, environ, path, issues)
            servers.append(entry)
            if provider == "codex_exec" and entry["enabled"]:
                # Inline values are the native server's responsibility, and must
                # never be promoted into the CLI control environment.
                inline = server.get("env", {}) if entry["transport"] == "stdio" else {}
                forward_names.update(key for key in entry["required_env_names"]
                                     if key not in entry["remote_env_names"] and not _protected_env(key)
                                     and not (isinstance(inline, dict) and key in inline))
    revision = hashlib.sha256(json.dumps({"sources": sources, "servers": servers, "stamps": stamps},
                                        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"provider": provider, "config_sources": sources, "servers": servers, "issues": issues,
            "connections_checked": False, "inventory_complete": False, "config_revision": revision,
            "limitations": limitations}, forward_names


def inspect_native_mcp(provider, binary, project_dir=None, environ=None):
    """Return bounded, secret-free metadata; do not open auth files or run CLIs."""
    inventory, _ = _inventory(provider, binary, project_dir, os.environ if environ is None else environ)
    return inventory


def production_control_environment(provider, binary, control_dir, *, project_dir=None, environ=None,
                                   include_mcp_env=True):
    """Keep native login and a known PATH; selectively supply Codex MCP env refs.

    The Codex caller must separately enforce shell_environment_policy.inherit=
    none. Agy has no verified equivalent, so extra token env is not forwarded.
    Inline MCP env/headers and credential stores remain the native CLI's concern.
    """
    provider = _provider(provider)
    source = os.environ if environ is None else environ
    home = _home(source)
    if environ is None:
        env = control_environment(provider, binary, control_dir)
    else:
        env = {"HOME": str(home), "LANG": "C.UTF-8", "TZ": "Asia/Seoul", "TMPDIR": str(control_dir)}
        for key in ("USER", "LOGNAME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
            if _usable_value(source.get(key)):
                env[key] = source[key]
    env["PATH"] = _production_path(binary, home)
    if provider == "codex_exec":
        env["CODEX_HOME"] = str(_absolute_path(source.get("CODEX_HOME", str(home / ".codex"))))
        references = _inventory(provider, binary, project_dir, source)[1] if include_mcp_env else ()
        for name in references:
            if _usable_value(source.get(name)):
                env[name] = source[name]
    return env
