"""Configuration loading and validation for the ponte SSH reverse tunnel tool.

Loads ``config.toml`` using :mod:`tomllib` (Python 3.11+) with a fallback to
the third-party ``tomli`` package on older interpreters, and exposes validated
dataclasses through a process-wide cached accessor, :func:`get_config`.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Union

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

__all__ = [
    "ConfigError",
    "ConfigNotFoundError",
    "ConfigParseError",
    "ConfigValidationError",
    "TunnelConfig",
    "SSHConfig",
    "SSHOptions",
    "Tunnel",
    "DaemonConfig",
    "RetryConfig",
    "HealthConfig",
    "WindowsConfig",
    "ServiceConfig",
    "get_config",
    "load_config",
]

# Pathlike inputs accepted by get_config/load_config.
_Path = Union[str, os.PathLike[str]]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class ConfigError(Exception):
    """Base exception for all configuration errors."""


class ConfigNotFoundError(ConfigError):
    """Raised when the configuration file cannot be found or read."""


class ConfigParseError(ConfigError):
    """Raised when the configuration file contains malformed TOML."""


class ConfigValidationError(ConfigError):
    """Raised when the configuration file is present but its contents are invalid."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SSHOptions:
    """SSH client options passed to OpenSSH as ``-o key=value`` flags.

    Any key present in the TOML ``[ssh.options]`` table that is not a named
    field here is preserved verbatim in :attr:`extra`, so user supplied
    ``-o`` flags that this module does not know about still work.
    """

    StrictHostKeyChecking: str = "accept-new"
    ServerAliveInterval: int = 30
    ServerAliveCountMax: int = 3
    ExitOnForwardFailure: str = "yes"
    TCPKeepAlive: str = "yes"
    extra: tuple[tuple[str, str], ...] = ()

    def as_pairs(self) -> list[tuple[str, str]]:
        """Return ``(key, value)`` pairs for every configured option.

        ``None`` fields are skipped and booleans are rendered as ``yes``/``no``
        (the spelling OpenSSH expects).
        """
        pairs: list[tuple[str, str]] = []
        for key, value in vars(self).items():
            if key == "extra":
                pairs.extend(self.extra)
            elif value is None:
                continue
            elif isinstance(value, bool):
                pairs.append((key, "yes" if value else "no"))
            else:
                pairs.append((key, str(value)))
        return pairs


@dataclass(frozen=True)
class Tunnel:
    """A single reverse-forward rule: remote_port -> local_host:local_port."""

    remote_port: int
    """Port opened on the SSH server, forwarding into the tunnel."""
    local_host: str
    """Destination host on the client side that receives forwarded traffic."""
    local_port: int
    """Destination port on the client side that receives forwarded traffic."""
    description: str = ""
    """Human readable description of what this tunnel is for."""


@dataclass(frozen=True)
class SSHConfig:
    """Connection parameters for the SSH endpoint."""

    host: str
    user: str
    identity_file: str
    port: int = 22
    known_hosts_file: Optional[str] = None
    options: SSHOptions = field(default_factory=SSHOptions)

    @property
    def destination(self) -> str:
        """The ``user@host`` target passed to ``ssh``."""
        return f"{self.user}@{self.host}"


@dataclass(frozen=True)
class DaemonConfig:
    """Settings for the background daemon / scheduled-task mode."""

    pid_file: str = ""
    log_file: str = ""
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 3


@dataclass(frozen=True)
class RetryConfig:
    """Reconnection backoff policy.

    ``max_retries`` of 0 means *retry forever*.
    """

    max_retries: int = 0
    base_delay: float = 5.0
    max_delay: float = 300.0
    backoff_factor: float = 2.0
    jitter: bool = True


@dataclass(frozen=True)
class HealthConfig:
    """Periodic connectivity checks while the daemon is running."""

    check_interval: int = 60
    remote_check_enabled: bool = True
    remote_check_timeout: int = 10


@dataclass(frozen=True)
class WindowsConfig:
    """Platform specific knobs used only on Windows."""

    task_name: str = "SSH-Reverse-Tunnel"
    ssh_exe: Optional[str] = None


@dataclass(frozen=True)
class ServiceConfig:
    """Cross-platform service identity / install knobs (systemd, launchd, …)."""

    name: str = "ponte"
    autostart: bool = True
    kill_timeout: float = 5.0


def _default_state_dir() -> str:
    """Return a per-user directory for pid/log/status files.

    … location varies by OS so the tool works out of the box on
    Windows, Linux and macOS without hardcoding ``C:\\ssh-tunnel``.
    """
    if sys.platform == "win32":
        return os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
            "ponte",
        )
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "ponte")
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(xdg, "ponte")


@dataclass(frozen=True)
class TunnelConfig:
    """Top-level validated configuration for the tool."""

    ssh: SSHConfig
    tunnels: list[Tunnel]
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    windows: WindowsConfig = field(default_factory=WindowsConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)
    source_path: str = ""
    """Absolute path of the TOML file this configuration was loaded from."""


# ---------------------------------------------------------------------------
# Loading / parsing
# ---------------------------------------------------------------------------
_CACHE: dict[str, TunnelConfig] = {}

# Known SSH option fields and the Python type TOML values are coerced to.
_FIELD_TYPES: dict[str, type] = {
    "StrictHostKeyChecking": str,
    "ServerAliveInterval": int,
    "ServerAliveCountMax": int,
    "ExitOnForwardFailure": str,
    "TCPKeepAlive": str,
}


def get_config(path: Optional[_Path] = None) -> TunnelConfig:
    """Return the validated :class:`TunnelConfig` for *path* (cached singleton).

    When *path* is omitted the file ``config.toml`` living next to this module
    is used. The first load per resolved path is parsed, validated and cached;
    later calls return the same instance.
    """
    if path is None:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml")
    else:
        config_path = os.fspath(path)
    config_path = os.path.abspath(config_path)

    cached = _CACHE.get(config_path)
    if cached is not None:
        return cached

    cfg = load_config(config_path)
    _CACHE[config_path] = cfg
    return cfg


def load_config(path: _Path) -> TunnelConfig:
    """Parse and validate the TOML file at *path* without caching."""
    config_path = os.path.abspath(os.fspath(path))
    if not os.path.isfile(config_path):
        raise ConfigNotFoundError(f"Configuration file not found: {config_path}")

    try:
        with open(config_path, "rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigParseError(f"Invalid TOML in {config_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigNotFoundError(f"Could not read {config_path}: {exc}") from exc

    if not isinstance(data, Mapping):
        raise ConfigValidationError(f"Top level of {config_path} must be a TOML table")

    return _parse_config(data, config_path)


def _parse_config(data: Mapping[str, Any], config_path: str) -> TunnelConfig:
    ssh = _parse_ssh(data.get("ssh", {}))
    tunnels = _parse_tunnels(data.get("tunnels", []))
    daemon = _parse_daemon(data.get("daemon", {}))
    retry = _parse_retry(data.get("retry", {}))
    health = _parse_health(data.get("health", {}))
    windows = _parse_windows(data.get("windows", {}))
    service = _parse_service(data.get("service", {}))

    cfg = TunnelConfig(
        ssh=ssh,
        tunnels=tunnels,
        daemon=daemon,
        retry=retry,
        health=health,
        windows=windows,
        service=service,
        source_path=config_path,
    )
    _validate(cfg)
    return cfg


def _parse_ssh(section: Any) -> SSHConfig:
    _expect_table(section, "ssh")
    host = _require_str(section, "host", "ssh")
    user = _require_str(section, "user", "ssh")
    identity_file = _require_str(section, "identity_file", "ssh")
    port = _optional_int(section, "port", default=22, minimum=1, maximum=65535, where="ssh")
    known_hosts = _optional_str(section, "known_hosts_file", None)
    options = _parse_ssh_options(section.get("options", {}))
    return SSHConfig(
        host=host,
        user=user,
        identity_file=_expand(identity_file),
        port=port,
        known_hosts_file=_expand(known_hosts) if known_hosts else None,
        options=options,
    )


def _parse_ssh_options(section: Any) -> SSHOptions:
    dft = SSHOptions()
    if not section:
        return dft
    _expect_table(section, "ssh.options")
    kwargs: dict[str, Any] = {}
    extra: list[tuple[str, str]] = []
    for key, raw_value in section.items():
        expected = _FIELD_TYPES.get(key)
        if expected is not None:
            kwargs[key] = _coerce(raw_value, expected, f"ssh.options.{key}")
        else:
            extra.append((key, _option_str(raw_value)))
    merged = {**vars(dft), **kwargs, "extra": tuple(extra)}
    return SSHOptions(**merged)


def _parse_tunnels(section: Any) -> list[Tunnel]:
    if not section:
        return []
    if not isinstance(section, list):
        raise ConfigValidationError("'tunnels' must be an array of tables")
    tunnels: list[Tunnel] = []
    for index, item in enumerate(section):
        where = f"tunnels[{index}]"
        _expect_table(item, where)
        remote_port = _required_int(item, "remote_port", minimum=1, maximum=65535, where=where)
        local_port = _required_int(item, "local_port", minimum=1, maximum=65535, where=where)
        local_host = _require_str(item, "local_host", where)
        description = _optional_str(item, "description", default="")
        tunnels.append(
            Tunnel(
                remote_port=remote_port,
                local_host=local_host,
                local_port=local_port,
                description=description,
            )
        )
    return tunnels


def _parse_daemon(section: Any) -> DaemonConfig:
    if not section:
        section = {}
    _expect_table(section, "daemon")
    state_dir = _default_state_dir()
    pid_file = _optional_str(section, "pid_file", default="")
    log_file = _optional_str(section, "log_file", default="")
    log_max_bytes = _optional_int(
        section, "log_max_bytes", default=DaemonConfig().log_max_bytes, minimum=1, where="daemon"
    )
    log_backup_count = _optional_int(
        section, "log_backup_count", default=DaemonConfig().log_backup_count, minimum=0, where="daemon"
    )
    return DaemonConfig(
        pid_file=_expand(pid_file) if pid_file else os.path.join(state_dir, "ponte.pid"),
        log_file=_expand(log_file) if log_file else os.path.join(state_dir, "ponte.log"),
        log_max_bytes=log_max_bytes,
        log_backup_count=log_backup_count,
    )


def _parse_retry(section: Any) -> RetryConfig:
    if not section:
        return RetryConfig()
    _expect_table(section, "retry")
    dft = RetryConfig()
    return RetryConfig(
        max_retries=_optional_int(section, "max_retries", default=dft.max_retries, minimum=0, where="retry"),
        base_delay=_optional_number(section, "base_delay", default=dft.base_delay, minimum=0.0, where="retry"),
        max_delay=_optional_number(section, "max_delay", default=dft.max_delay, minimum=0.0, where="retry"),
        backoff_factor=_optional_number(
            section, "backoff_factor", default=dft.backoff_factor, minimum=1.0, where="retry"
        ),
        jitter=_optional_bool(section, "jitter", default=dft.jitter, where="retry"),
    )


def _parse_health(section: Any) -> HealthConfig:
    if not section:
        return HealthConfig()
    _expect_table(section, "health")
    dft = HealthConfig()
    return HealthConfig(
        check_interval=_optional_int(
            section, "check_interval", default=dft.check_interval, minimum=1, where="health"
        ),
        remote_check_enabled=_optional_bool(
            section, "remote_check_enabled", default=dft.remote_check_enabled, where="health"
        ),
        remote_check_timeout=_optional_int(
            section, "remote_check_timeout", default=dft.remote_check_timeout, minimum=1, where="health"
        ),
    )


def _parse_windows(section: Any) -> WindowsConfig:
    if not section:
        return WindowsConfig()
    _expect_table(section, "windows")
    task_name = _optional_str(section, "task_name", default="SSH-Reverse-Tunnel")
    ssh_exe = _optional_str(section, "ssh_exe", default=None)
    return WindowsConfig(
        task_name=task_name,
        ssh_exe=_expand(ssh_exe) if ssh_exe else None,
    )


def _parse_service(section: Any) -> ServiceConfig:
    if not section:
        return ServiceConfig()
    _expect_table(section, "service")
    dft = ServiceConfig()
    return ServiceConfig(
        name=_optional_str(section, "name", default=dft.name),
        autostart=_optional_bool(section, "autostart", default=dft.autostart, where="service"),
        kill_timeout=_optional_number(
            section, "kill_timeout", default=dft.kill_timeout, minimum=0.0, where="service"
        ),
    )


def _validate(cfg: TunnelConfig) -> None:
    """Cross-field validation that runs after every section is parsed."""
    if not cfg.tunnels:
        raise ConfigValidationError("At least one tunnel must be configured under 'tunnels'")

    # The identity file is essential for non-interactive operation; fail fast
    # with a clear message rather than letting SSH fail later.
    if cfg.ssh.identity_file and not os.path.isfile(cfg.ssh.identity_file):
        raise ConfigValidationError(
            f"SSH identity file does not exist: {cfg.ssh.identity_file}"
        )
    if cfg.ssh.known_hosts_file and not os.path.isfile(cfg.ssh.known_hosts_file):
        raise ConfigValidationError(
            f"Known-hosts file does not exist: {cfg.ssh.known_hosts_file}"
        )


# ---------------------------------------------------------------------------
# Low level coercion helpers
# ---------------------------------------------------------------------------
def _expect_table(value: Any, name: str) -> None:
    if not isinstance(value, Mapping):
        raise ConfigValidationError(f"'{name}' must be a TOML table, got {type(value).__name__}")


def _expand(path: str) -> str:
    """Expand ``~`` and environment variables in a path."""
    return os.path.expanduser(os.path.expandvars(path))


def _option_str(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _coerce(value: Any, expected: type, name: str) -> Any:
    if expected is str:
        if isinstance(value, bool):
            return "yes" if value else "no"
        if not isinstance(value, str):
            raise ConfigValidationError(
                f"Field '{name}' must be a string, got {type(value).__name__}"
            )
        return value
    if expected is int:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ConfigValidationError(f"Field '{name}' must be an integer, got {value!r}") from None
    raise ConfigValidationError(f"Unsupported coercion target for '{name}'")


def _require_str(data: Mapping[str, Any], key: str, where: str) -> str:
    """Return a non-empty string field, raising for missing/invalid values."""
    value = data.get(key)
    if value is None:
        raise ConfigValidationError(f"Missing required field '{where}.{key}'")
    if isinstance(value, bool) or not isinstance(value, str):
        raise ConfigValidationError(
            f"Field '{where}.{key}' must be a string, got {type(value).__name__}"
        )
    if not value.strip():
        raise ConfigValidationError(f"Field '{where}.{key}' must not be empty")
    return value.strip()


def _optional_str(data: Mapping[str, Any], key: str, default: Optional[str]) -> Optional[str]:
    """Return an optional string field, falling back to *default*."""
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, str):
        raise ConfigValidationError(f"Field '{key}' must be a string, got {type(value).__name__}")
    stripped = value.strip()
    return stripped if stripped else default


def _required_int(
    data: Mapping[str, Any],
    key: str,
    *,
    minimum: Optional[int],
    maximum: Optional[int],
    where: str,
) -> int:
    """Return a required integer field, validating its range."""
    if key not in data or data[key] is None:
        raise ConfigValidationError(f"Missing required field '{where}.{key}'")
    return _check_int(data[key], key, minimum=minimum, maximum=maximum, where=where)


def _optional_int(
    data: Mapping[str, Any],
    key: str,
    *,
    default: int,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
    where: str,
) -> int:
    """Return an optional integer field, falling back to *default*."""
    if key not in data or data[key] is None:
        return default
    return _check_int(data[key], key, minimum=minimum, maximum=maximum, where=where)


def _check_int(
    value: Any,
    key: str,
    *,
    minimum: Optional[int],
    maximum: Optional[int],
    where: str,
) -> int:
    """Coerce *value* to ``int`` and enforce an inclusive range."""
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        raise ConfigValidationError(f"Field '{where}.{key}' must be an integer, got {value!r}") from None
    if isinstance(value, bool):
        raise ConfigValidationError(f"Field '{where}.{key}' must be an integer, got {value!r}")
    if minimum is not None and coerced < minimum:
        raise ConfigValidationError(
            f"Field '{where}.{key}' must be >= {minimum}, got {coerced}"
        )
    if maximum is not None and coerced > maximum:
        raise ConfigValidationError(
            f"Field '{where}.{key}' must be <= {maximum}, got {coerced}"
        )
    return coerced


def _optional_number(
    data: Mapping[str, Any],
    key: str,
    *,
    default: float,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    where: str,
) -> float:
    """Return an optional numeric field (``int`` or ``float``), falling back to *default*."""
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if isinstance(value, bool):
        raise ConfigValidationError(f"Field '{where}.{key}' must be a number, got {value!r}")
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        raise ConfigValidationError(f"Field '{where}.{key}' must be a number, got {value!r}") from None
    if minimum is not None and coerced < minimum:
        raise ConfigValidationError(
            f"Field '{where}.{key}' must be >= {minimum}, got {coerced}"
        )
    if maximum is not None and coerced > maximum:
        raise ConfigValidationError(
            f"Field '{where}.{key}' must be <= {maximum}, got {coerced}"
        )
    return coerced


def _optional_bool(data: Mapping[str, Any], key: str, *, default: bool, where: str) -> bool:
    """Return an optional boolean field, falling back to *default*."""
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if not isinstance(value, bool):
        raise ConfigValidationError(f"Field '{where}.{key}' must be a boolean, got {value!r}")
    return value