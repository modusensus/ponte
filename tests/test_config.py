"""pytest tests for :mod:`ponte.config` (offline, uses tmp files)."""

from __future__ import annotations

import os

import pytest

from ponte.config import (
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
