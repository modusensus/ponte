"""pytest tests for :mod:`ponte.config` (offline, uses tmp files)."""

from __future__ import annotations

import os

import pytest

from ponte.config import (
    ConfigNotFoundError,
    ConfigParseError,
    ConfigValidationError,
    TunnelConfig,
    get_config,
    load_config,
)


def _write_toml(tmp_path, body: str):
    cfg = tmp_path / "config.toml"
    cfg.write_text(body, encoding="utf-8")
    return str(cfg)


def _toml_str(path: object) -> str:
    """Render a path as a TOML basic string (escape backslashes)."""
    return str(path).replace("\\", "\\\\")


def _minimal(tmp_path) -> str:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
port = 22
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
known_hosts_file = "{_toml_str(tmp_path / 'known_hosts')}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222
"""
    return _write_toml(tmp_path, body)


def test_load_minimal(tmp_path) -> None:
    cfg = load_config(_minimal(tmp_path))
    assert isinstance(cfg, TunnelConfig)
    assert cfg.ssh.host == "example.com"
    assert cfg.ssh.destination == "testuser@example.com"
    assert len(cfg.tunnels) == 1
    assert cfg.tunnels[0].remote_port == 23334
    # 缺省 daemon 段 → 平台默认 pid/log 非空
    assert cfg.daemon.pid_file
    assert cfg.daemon.log_file


def test_missing_tunnels_rejected(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
known_hosts_file = "{_toml_str(tmp_path / 'known_hosts')}"
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_missing_required_field(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
known_hosts_file = "{_toml_str(tmp_path / 'known_hosts')}"

[[tunnels]]
remote_port = 9999
local_host = "localhost"
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_missing_identity_file_rejected(tmp_path) -> None:
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'nope')}"
known_hosts_file = "{_toml_str(tmp_path / 'known_hosts')}"

[[tunnels]]
remote_port = 9999
local_host = "localhost"
local_port = 2222
"""
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_get_config_caches(tmp_path) -> None:
    a = get_config(_minimal(tmp_path))
    b = get_config(_minimal(tmp_path))
    assert a is b


def test_ssh_options_extra_keys(tmp_path) -> None:
    """未知的 ssh.options 键应进入 extra，而不是报错。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
known_hosts_file = "{_toml_str(tmp_path / 'known_hosts')}"

[ssh.options]
ServerAliveInterval = 15
CustomFlag = true

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222
"""
    cfg = load_config(_write_toml(tmp_path, body))
    extra = dict(cfg.ssh.options.extra)
    assert extra == {"CustomFlag": "yes"}  # 布尔渲染成 yes/no
    assert cfg.ssh.options.ServerAliveInterval == 15


def test_tunnels_must_be_array(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
known_hosts_file = "{_toml_str(tmp_path / 'known_hosts')}"

[tunnels]
remote_port = 23334
local_host = "localhost"
local_port = 2222
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_daemon_custom_paths(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
known_hosts_file = "{_toml_str(tmp_path / 'known_hosts')}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222

[daemon]
pid_file = "/tmp/custom.pid"
log_file = "/tmp/custom.log"
log_backup_count = 5
"""
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.daemon.pid_file == "/tmp/custom.pid"
    assert cfg.daemon.log_file == "/tmp/custom.log"
    assert cfg.daemon.log_backup_count == 5


def test_service_section(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
known_hosts_file = "{_toml_str(tmp_path / 'known_hosts')}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222

[service]
name = "mytunnel"
autostart = false
kill_timeout = 3
"""
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.service.name == "mytunnel"
    assert cfg.service.autostart is False
    assert cfg.service.kill_timeout == 3.0


def test_expand_tilde(tmp_path, monkeypatch) -> None:
    # Windows 用 USERPROFILE，POSIX 用 HOME；两处都设，保证跨平台。
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "~/id_rsa"
known_hosts_file = "~/known_hosts"

[[tunnels]]
remote_port = 9999
local_host = "localhost"
local_port = 2222
"""
    cfg = load_config(_write_toml(tmp_path, body))
    assert os.path.normpath(cfg.ssh.identity_file) == os.path.normpath(tmp_path / "id_rsa")
    assert os.path.isfile(cfg.ssh.identity_file)


def test_config_file_not_found(tmp_path) -> None:
    with pytest.raises(ConfigNotFoundError):
        load_config(tmp_path / "missing.toml")


def test_config_invalid_toml(tmp_path) -> None:
    cfg = tmp_path / "bad.toml"
    cfg.write_text("[ssh\nhost = \"x\"", encoding="utf-8")
    with pytest.raises(ConfigParseError):
        load_config(cfg)


def test_ssh_section_as_string_rejected(tmp_path) -> None:
    """When [ssh] is omitted and ssh = "..." is a string, parsing fails."""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
ssh = "not a table"
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 9999
local_host = "localhost"
local_port = 2222
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_port_out_of_range_rejected(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
port = 99999

[[tunnels]]
remote_port = 9999
local_host = "localhost"
local_port = 2222
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_required_string_empty_rejected(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = """
[ssh]
host = ""
user = "testuser"
identity_file = "x"

[[tunnels]]
remote_port = 9999
local_host = "localhost"
local_port = 2222
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_optional_string_type_error(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
known_hosts_file = 123

[[tunnels]]
remote_port = 9999
local_host = "localhost"
local_port = 2222
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_retry_number_validation(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 9999
local_host = "localhost"
local_port = 2222

[retry]
base_delay = -1
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_health_boolean_validation(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 9999
local_host = "localhost"
local_port = 2222

[health]
remote_check_enabled = "yes"
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_coerce_unsupported_type() -> None:
    from ponte.config import _coerce
    with pytest.raises(ConfigValidationError):
        _coerce("x", float, "test")
