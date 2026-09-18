"""Configuration loading and validation for the ponte SSH reverse tunnel tool.

Loads ``config.toml`` using :mod:`tomllib` and exposes validated dataclasses
through a process-wide cached accessor, :func:`get_config`.

``tomllib`` is used unconditionally: it is stdlib, and Python 3.11 is this
project's floor (see ``requires-python``), so the old ``tomli`` fallback was
dead code that only served to break ``mypy --platform linux``.

The configuration file lives *outside* the package so that ``pip install -U``
never overwrites it. Resolution order (first existing file wins):

1. an explicit path (the CLI's ``--config`` flag, or ``get_config(path)``);
2. ``$PONTE_CONFIG``;
3. the per-user config file (see :func:`user_config_path`);
4. the legacy ``<package>/config.toml`` — still honoured so an existing
   install keeps working, but reported as deprecated.

The current working directory is deliberately *not* searched: the daemon is
often started from a service manager with an arbitrary ``cwd``, and a stray
file silently redirecting the tunnel to another server is a real footgun.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, overload

__all__ = [
    "ConfigError",
    "ConfigNotFoundError",
    "ConfigParseError",
    "ConfigValidationError",
    "TunnelConfig",
    "SSHConfig",
    "SSHOptions",
    "Tunnel",
    "TUNNEL_FLAGS",
    "DEFAULT_BIND_HOST",
    "DaemonConfig",
    "RetryConfig",
    "HealthConfig",
    "WindowsConfig",
    "ServiceConfig",
    "get_config",
    "load_config",
    "set_config_path",
    "clear_config_cache",
    "config_search_paths",
    "user_config_path",
    "example_config_path",
    "init_config",
]

logger = logging.getLogger(__name__)

#: Environment variable that overrides the config file location.
CONFIG_ENV_VAR = "PONTE_CONFIG"

#: Name of the file inside the package directory (legacy location + template).
CONFIG_FILENAME = "config.toml"

#: Name of the shipped template used by ``ponte init``.
EXAMPLE_FILENAME = "config.example.toml"

#: Tunnel kinds mapped to the OpenSSH forwarding flag each one expands to.
#:
#: ``remote`` (``-R``)  — the *server* listens and forwards into the tunnel.
#: ``local``  (``-L``)  — *this* machine listens and forwards out through it.
#: ``dynamic``(``-D``)  — *this* machine serves a SOCKS5 proxy on that port.
TUNNEL_FLAGS: dict[str, str] = {"remote": "-R", "local": "-L", "dynamic": "-D"}

#: Bind address assumed for a ``-L``/``-D`` rule that does not name ``local_host``.
#: Loopback on purpose: a forwarding rule or SOCKS proxy reachable from the
#: whole LAN because a field was omitted is exactly the kind of accident a
#: tunnel manager should not enable.
DEFAULT_BIND_HOST = "127.0.0.1"

#: ``local_host`` values that mean "every interface" and cannot be connected to.
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "*"})

# Pathlike inputs accepted by get_config/load_config.
_Path = str | os.PathLike[str]


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
    """A single SSH forwarding rule.

    ``kind`` selects the OpenSSH flag and therefore which fields are
    meaningful. ``local_host:local_port`` is always the *client-side* end of the
    rule; ``remote_host:remote_port`` is always the *server-side* end:

    ``"remote"`` (``-R``, the default)
        The server listens on ``remote_port`` and forwards into the tunnel to
        ``local_host:local_port``. Setting ``remote_host`` turns it into the
        server-side bind address, which only matters with ``GatewayPorts=yes``;
        it is left unset by default so the emitted command line stays identical
        to what pre-``kind`` ponte versions produced.
    ``"local"`` (``-L``)
        *This* machine listens on ``local_host:local_port`` and forwards over
        the tunnel to ``remote_host:remote_port`` as reached from the server.
    ``"dynamic"`` (``-D``)
        *This* machine runs a SOCKS5 proxy on ``local_host:local_port``; the
        destination is chosen per connection, so both ``remote_*`` are unused.
    """

    remote_port: int | None
    """Server-side port: the port ``-R`` opens, or the ``-L`` destination port."""
    local_host: str
    """Client-side host: the ``-R`` destination, or the ``-L``/``-D`` bind address."""
    local_port: int
    """Client-side port: the ``-R`` destination, or the ``-L``/``-D`` listen port."""
    description: str = ""
    """Human readable description of what this tunnel is for."""
    kind: str = "remote"
    """One of ``remote`` (``-R``), ``local`` (``-L``) or ``dynamic`` (``-D``)."""
    remote_host: str | None = None
    """Server-side host: the ``-L`` destination host, or the ``-R`` bind address."""

    @property
    def flag(self) -> str:
        """The OpenSSH forwarding flag this tunnel expands to (``-R``/``-L``/``-D``)."""
        return TUNNEL_FLAGS.get(self.kind, "-R")

    @property
    def is_remote(self) -> bool:
        """``True`` for ``-R``: the *server* side owns the listening port."""
        return self.kind == "remote"

    @property
    def spec(self) -> str:
        """The argument OpenSSH expects immediately after :attr:`flag`."""
        if self.kind == "dynamic":
            return f"{self.local_host}:{self.local_port}"
        if self.kind == "local":
            return (
                f"{self.local_host}:{self.local_port}:"
                f"{self.remote_host or 'localhost'}:{self.remote_port}"
            )
        forward = f"{self.remote_port}:{self.local_host}:{self.local_port}"
        if self.remote_host:
            forward = f"{self.remote_host}:{forward}"
        return forward

    @property
    def summary(self) -> str:
        """One-line human readable rendering, used by ``ponte config``."""
        if self.kind == "dynamic":
            return f"-D {self.local_host}:{self.local_port}（SOCKS5 代理）"
        if self.kind == "local":
            return (
                f"-L {self.local_host}:{self.local_port} → "
                f"{self.remote_host or 'localhost'}:{self.remote_port}"
            )
        line = f"-R {self.remote_port} → {self.local_host}:{self.local_port}"
        if self.remote_host:
            line += f"（服务器绑定 {self.remote_host}）"
        return line


@dataclass(frozen=True)
class SSHConfig:
    """Connection parameters for the SSH endpoint."""

    host: str
    user: str
    identity_file: str
    port: int = 22
    known_hosts_file: str | None = None
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

    ``stable_after`` is the minimum session duration (seconds) that marks a
    connection as healthy. When :class:`~ponte.retry.RetryRunner` observes a
    session that ran at least this long, it resets its reconnect budget so a
    long-lived tunnel never permanently gives up after a flurry of earlier
    failures.
    """

    max_retries: int = 0
    base_delay: float = 5.0
    max_delay: float = 300.0
    backoff_factor: float = 2.0
    jitter: bool = True
    stable_after: float = 60.0


@dataclass(frozen=True)
class HealthConfig:
    """Periodic connectivity checks while the daemon is running."""

    check_interval: int = 60
    remote_check_enabled: bool = True
    remote_check_timeout: int = 10
    max_check_interval: float = 300.0
    """Ceiling (seconds) on the health-check interval under exponential backoff.

    When a check fails, the next interval grows as
    ``check_interval * 2 ** consecutive_failures`` but is capped at this value
    so a prolonged outage never stops probing the SSH server entirely (and
    never hammers it hard enough to trip ``MaxStartups``).
    """


@dataclass(frozen=True)
class WindowsConfig:
    """Platform specific knobs used only on Windows.

    ``run_as`` selects the Scheduled-Task identity/timing:

    * ``"user"`` (default) — logon-time task running as the installing user
      (``Interactive`` + ``Limited``), so it can read the user's keys and does
      not require elevation, but only runs after an interactive logon. This is
      the default because the shipped ``identity_file`` points at ``~/.ssh``,
      which a SYSTEM task cannot read — the "safer looking" default was broken
      out of the box.
    * ``"system"`` — boot-time task running as SYSTEM, surviving login/reboot,
      so the tunnel is up before anyone logs in; requires elevation and an
      identity file that SYSTEM can read (i.e. *not* ``~/.ssh``).

    ``pythonw_exe`` pins the windowless interpreter the task runs. ponte refuses
    to install a task that would fall back to ``python.exe`` (an interactive task
    running a console program flashes a console window at every logon), so set
    this only when ``pythonw.exe`` does not sit next to ``sys.executable``.
    """

    task_name: str = "SSH-Reverse-Tunnel"
    ssh_exe: str | None = None
    pythonw_exe: str | None = None
    run_as: str = "user"


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


def _default_config_dir() -> str:
    """Return a per-user directory for ``config.toml``.

    Separate from :func:`_default_state_dir` because configuration and mutable
    runtime state follow different conventions (``%APPDATA%`` vs
    ``%LOCALAPPDATA%``, ``XDG_CONFIG_HOME`` vs ``XDG_STATE_HOME``).
    """
    if sys.platform == "win32":
        return os.path.join(
            os.environ.get("APPDATA") or os.path.expanduser("~"),
            "ponte",
        )
    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~"), "Library", "Application Support", "ponte"
        )
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(xdg, "ponte")


def user_config_path() -> str:
    """Absolute path of the per-user ``config.toml`` (may not exist yet)."""
    return os.path.join(_default_config_dir(), CONFIG_FILENAME)


def package_dir() -> str:
    """Absolute path of the installed ``ponte`` package directory."""
    return os.path.dirname(os.path.abspath(__file__))


def example_config_path() -> str:
    """Absolute path of the shipped configuration template."""
    return os.path.join(package_dir(), EXAMPLE_FILENAME)


def legacy_config_path() -> str:
    """Absolute path of the pre-0.3 config file *inside* the package.

    Kept only so installations created before the move keep working; the file
    is overwritten by every ``pip install -U`` and reported as deprecated.
    """
    return os.path.join(package_dir(), CONFIG_FILENAME)


#: Process-wide override installed by the CLI's ``--config`` flag.
_EXPLICIT_PATH: str | None = None


def set_config_path(path: _Path | None) -> None:
    """Pin the configuration file for this process (``--config`` support).

    Passing ``None`` clears the override. Raises nothing: a non-existent path
    is reported later by :func:`get_config` together with the search list.
    """
    global _EXPLICIT_PATH
    _EXPLICIT_PATH = os.path.abspath(os.fspath(path)) if path else None


def clear_config_cache() -> None:
    """Drop the cached :class:`TunnelConfig` instances (mainly for tests)."""
    _CACHE.clear()


def config_search_paths(explicit: _Path | None = None) -> list[str]:
    """Return the candidate config paths, in resolution order.

    Duplicates are removed while preserving order. The list is meant to be
    shown to the user when nothing is found, so it includes paths that do not
    exist yet.
    """
    candidates: list[str | None] = [
        os.fspath(explicit) if explicit is not None else None,
        _EXPLICIT_PATH,
        os.environ.get(CONFIG_ENV_VAR),
        user_config_path(),
        legacy_config_path(),
    ]
    paths: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        resolved = os.path.abspath(os.path.expanduser(os.path.expandvars(candidate)))
        if resolved not in paths:
            paths.append(resolved)
    return paths


def _resolve_config_path(explicit: _Path | None = None) -> str:
    """Return the first existing candidate, or raise with the full search list."""
    searched = config_search_paths(explicit)
    for candidate in searched:
        if os.path.isfile(candidate):
            if candidate == legacy_config_path():
                logger.warning(
                    "使用的仍是包内的旧配置 %s（pip 升级会覆盖它）；"
                    "请运行 `ponte init` 迁移到用户配置目录",
                    candidate,
                )
            return candidate
    raise ConfigNotFoundError(
        "找不到配置文件。已查找：\n  - "
        + "\n  - ".join(searched)
        + "\n运行 `ponte init` 生成一份，或用 --config / PONTE_CONFIG 指定路径。"
    )


def init_config(path: _Path | None = None, *, force: bool = False) -> str:
    """Write a configuration file from the shipped template.

    Args:
        path: Target file. Defaults to :func:`user_config_path`.
        force: Overwrite an existing file.

    Returns:
        The absolute path written.

    Raises:
        ConfigError: if the target exists and *force* is false, or if no
            template is available.
    """
    target = os.path.abspath(
        os.path.expanduser(os.fspath(path)) if path is not None else user_config_path()
    )
    if os.path.exists(target) and not force:
        raise ConfigError(
            f"配置文件已存在：{target}（如需覆盖请加 --force）"
        )

    # Prefer migrating a legacy in-package config (real settings) over the
    # placeholder template.
    legacy = legacy_config_path()
    template = example_config_path()
    if os.path.isfile(legacy):
        source = legacy
    elif os.path.isfile(template):
        source = template
    else:  # pragma: no cover - packaging regression guard
        raise ConfigError(
            f"未找到配置模板（{template}）；请重新安装 ponte 或手动创建 {target}"
        )

    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    try:
        shutil.copyfile(source, target)
    except OSError as exc:
        raise ConfigError(f"无法写入 {target}: {exc}") from exc
    return target


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
    warnings: tuple[str, ...] = ()
    """Non-fatal problems found while parsing (unknown keys, deprecated names).

    Shown by ``ponte config`` and logged by :func:`load_config` instead of
    being silently dropped — a typo in a config key used to have no effect and
    no diagnostic.
    """


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


def get_config(path: _Path | None = None) -> TunnelConfig:
    """Return the validated :class:`TunnelConfig` (cached per resolved path).

    When *path* is omitted the first existing candidate from
    :func:`config_search_paths` is used; see the module docstring for the
    order. The first load per resolved path is parsed, validated and cached;
    later calls return the same instance.
    """
    if path is None:
        config_path = _resolve_config_path()
    else:
        config_path = os.path.abspath(os.path.expanduser(os.fspath(path)))

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
    warnings: list[str] = []
    _warn_unknown_keys(data, _KNOWN_TOP_LEVEL, "", warnings)

    ssh = _parse_ssh(data.get("ssh", {}), warnings)
    tunnels = _parse_tunnels(data.get("tunnels", []), warnings)
    daemon = _parse_daemon(data.get("daemon", {}), warnings)
    retry = _parse_retry(data.get("retry", {}), warnings)
    health = _parse_health(data.get("health", {}), warnings)
    windows = _parse_windows(data.get("windows", {}), warnings)
    service = _parse_service(data.get("service", {}), warnings)

    cfg = TunnelConfig(
        ssh=ssh,
        tunnels=tunnels,
        daemon=daemon,
        retry=retry,
        health=health,
        windows=windows,
        service=service,
        source_path=config_path,
        warnings=tuple(warnings),
    )
    for message in warnings:
        logger.warning("%s: %s", config_path, message)
    _validate(cfg)
    return cfg


#: Recognised keys per section. Used to report typos instead of silently
#: ignoring them; ``ssh.options`` is intentionally open-ended.
_KNOWN_TOP_LEVEL = frozenset(
    {"ssh", "tunnels", "daemon", "retry", "health", "windows", "service"}
)
_KNOWN_SSH = frozenset(
    {"host", "port", "user", "identity_file", "known_hosts_file", "options"}
)
_KNOWN_TUNNEL = frozenset(
    {"kind", "remote_port", "remote_host", "local_host", "local_port", "description"}
)
_KNOWN_DAEMON = frozenset(
    {"pid_file", "log_file", "log_max_bytes", "log_backup_count"}
)
_KNOWN_RETRY = frozenset(
    {"max_retries", "base_delay", "max_delay", "backoff_factor", "jitter", "stable_after"}
)
_KNOWN_HEALTH = frozenset(
    {"check_interval", "remote_check_enabled", "remote_check_timeout", "max_check_interval"}
)
_KNOWN_WINDOWS = frozenset({"task_name", "ssh_exe", "pythonw_exe", "run_as"})
_KNOWN_SERVICE = frozenset({"name", "autostart", "kill_timeout"})


def _warn_unknown_keys(
    section: Mapping[str, Any],
    known: frozenset[str],
    where: str,
    warnings: list[str] | None,
) -> None:
    """Append a warning for every key in *section* that is not in *known*.

    A ``None`` collector means "the caller parses this section outside a full
    config load" (e.g. a unit test) and simply discards the diagnostics.
    """
    if warnings is None:
        return
    for key in section:
        if key not in known:
            prefix = f"{where}." if where else ""
            warnings.append(f"未知配置项 '{prefix}{key}' 已忽略（请检查拼写）")


def _parse_ssh(section: Any, warnings: list[str] | None = None) -> SSHConfig:
    _expect_table(section, "ssh")
    _warn_unknown_keys(section, _KNOWN_SSH, "ssh", warnings)
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


def _parse_tunnels(section: Any, warnings: list[str] | None = None) -> list[Tunnel]:
    if not section:
        return []
    if not isinstance(section, list):
        raise ConfigValidationError("'tunnels' must be an array of tables")
    tunnels: list[Tunnel] = []
    for index, item in enumerate(section):
        where = f"tunnels[{index}]"
        _expect_table(item, where)
        _warn_unknown_keys(item, _KNOWN_TUNNEL, where, warnings)
        kind = _optional_str(item, "kind", default="remote")
        if kind not in TUNNEL_FLAGS:
            raise ConfigValidationError(
                f"Field '{where}.kind' must be one of "
                f"{', '.join(sorted(TUNNEL_FLAGS))}, got {kind!r}"
            )
        local_port = _required_int(
            item, "local_port", minimum=1, maximum=65535, where=where
        )
        description = _optional_str(item, "description", default="")

        if kind == "dynamic":
            # A SOCKS proxy picks its destination per connection: anything
            # remote-shaped here is either a misunderstanding or a copy-paste
            # leftover, so say so instead of silently dropping it.
            ignored = [key for key in ("remote_host", "remote_port") if key in item]
            if ignored and warnings is not None:
                warnings.append(
                    f"{where}.kind = 'dynamic' 不使用 "
                    + " / ".join(ignored)
                    + "，已忽略"
                )
            remote_port: int | None = None
            remote_host: str | None = None
            local_host = _optional_str(item, "local_host", default=None) or DEFAULT_BIND_HOST
        else:
            remote_port = _required_int(
                item, "remote_port", minimum=1, maximum=65535, where=where
            )
            remote_host = _optional_str(item, "remote_host", default=None)
            if kind == "local":
                local_host = (
                    _optional_str(item, "local_host", default=None) or DEFAULT_BIND_HOST
                )
                if remote_host is None:
                    raise ConfigValidationError(
                        f"Missing required field '{where}.remote_host'"
                        " (a 'local' tunnel needs the host the server forwards to)"
                    )
            else:
                # -R destination: required, and has no sane default.
                local_host = _require_str(item, "local_host", where)

        tunnels.append(
            Tunnel(
                remote_port=remote_port,
                local_host=local_host,
                local_port=local_port,
                description=description,
                kind=kind,
                remote_host=remote_host,
            )
        )
    return tunnels


def _parse_daemon(section: Any, warnings: list[str] | None = None) -> DaemonConfig:
    if not section:
        section = {}
    _expect_table(section, "daemon")
    _warn_unknown_keys(section, _KNOWN_DAEMON, "daemon", warnings)
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


def _parse_retry(section: Any, warnings: list[str] | None = None) -> RetryConfig:
    if not section:
        return RetryConfig()
    _expect_table(section, "retry")
    _warn_unknown_keys(section, _KNOWN_RETRY, "retry", warnings)
    dft = RetryConfig()
    return RetryConfig(
        max_retries=_optional_int(section, "max_retries", default=dft.max_retries, minimum=0, where="retry"),
        base_delay=_optional_number(section, "base_delay", default=dft.base_delay, minimum=0.0, where="retry"),
        max_delay=_optional_number(section, "max_delay", default=dft.max_delay, minimum=0.0, where="retry"),
        backoff_factor=_optional_number(
            section, "backoff_factor", default=dft.backoff_factor, minimum=1.0, where="retry"
        ),
        jitter=_optional_bool(section, "jitter", default=dft.jitter, where="retry"),
        stable_after=_optional_number(
            section, "stable_after", default=dft.stable_after, minimum=0.0, where="retry"
        ),
    )


def _parse_health(section: Any, warnings: list[str] | None = None) -> HealthConfig:
    if not section:
        return HealthConfig()
    _expect_table(section, "health")
    _warn_unknown_keys(section, _KNOWN_HEALTH, "health", warnings)
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
        max_check_interval=_optional_number(
            section,
            "max_check_interval",
            default=dft.max_check_interval,
            minimum=1.0,
            where="health",
        ),
    )


def _parse_windows(section: Any, warnings: list[str] | None = None) -> WindowsConfig:
    if not section:
        return WindowsConfig()
    _expect_table(section, "windows")
    _warn_unknown_keys(section, _KNOWN_WINDOWS, "windows", warnings)
    dft = WindowsConfig()
    task_name = _optional_str(section, "task_name", default=dft.task_name)
    ssh_exe = _optional_str(section, "ssh_exe", default=None)
    pythonw_exe = _optional_str(section, "pythonw_exe", default=None)
    run_as = _optional_str(section, "run_as", default=dft.run_as)
    if run_as not in ("system", "user"):
        raise ConfigValidationError(
            f"Field 'windows.run_as' must be 'system' or 'user', got {run_as!r}"
        )
    return WindowsConfig(
        task_name=task_name,
        ssh_exe=_expand(ssh_exe) if ssh_exe else None,
        pythonw_exe=_expand(pythonw_exe) if pythonw_exe else None,
        run_as=run_as or dft.run_as,
    )


def _parse_service(section: Any, warnings: list[str] | None = None) -> ServiceConfig:
    if not section:
        return ServiceConfig()
    _expect_table(section, "service")
    _warn_unknown_keys(section, _KNOWN_SERVICE, "service", warnings)
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

    _reject_duplicate_listeners(cfg.tunnels)

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


def _reject_duplicate_listeners(tunnels: list[Tunnel]) -> None:
    """Reject two rules that would try to listen on the same port.

    Every forward is established with ``ExitOnForwardFailure=yes``, so a
    duplicate listen request makes OpenSSH drop the *whole* connection — the
    tunnel then reconnects forever with a cryptic ``remote port forwarding
    failed`` in the log. Catching it at config-load time turns that loop into
    one clear message.

    ``-R`` ports live on the server, ``-L``/``-D`` ports live on this machine,
    so the two namespaces are checked separately; ``-L`` and ``-D`` do compete
    with each other because both bind locally.
    """
    remote_seen: dict[int, int] = {}
    local_seen: dict[tuple[str, int], int] = {}
    for index, tunnel in enumerate(tunnels):
        if tunnel.is_remote:
            port = int(tunnel.remote_port or 0)
            previous = remote_seen.get(port)
            if previous is not None:
                raise ConfigValidationError(
                    f"tunnels[{index}] and tunnels[{previous}] both request remote "
                    f"port {port} on the server"
                )
            remote_seen[port] = index
            continue
        key = (tunnel.local_host, tunnel.local_port)
        previous_index = local_seen.get(key)
        if previous_index is not None:
            raise ConfigValidationError(
                f"tunnels[{index}] and tunnels[{previous_index}] both listen on "
                f"{tunnel.local_host}:{tunnel.local_port} locally"
            )
        local_seen[key] = index


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


@overload
def _optional_str(data: Mapping[str, Any], key: str, default: str) -> str: ...


@overload
def _optional_str(
    data: Mapping[str, Any], key: str, default: None
) -> str | None: ...


def _optional_str(
    data: Mapping[str, Any], key: str, default: str | None
) -> str | None:
    """Return an optional string field, falling back to *default*.

    Overloaded so callers passing a non-``None`` default get a ``str`` back
    instead of having to narrow ``str | None`` themselves.
    """
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
    minimum: int | None,
    maximum: int | None,
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
    minimum: int | None = None,
    maximum: int | None = None,
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
    minimum: int | None,
    maximum: int | None,
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
    minimum: float | None = None,
    maximum: float | None = None,
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
