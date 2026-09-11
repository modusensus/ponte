"""pytest tests for :mod:`ponte.config` (offline, uses tmp files)."""

from __future__ import annotations

import os

import pytest

from ponte.config import (
    ConfigError,
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
    # 缺省 health 段 → max_check_interval 取默认值
    assert cfg.health.max_check_interval == 300.0


def test_health_max_check_interval_default() -> None:
    """``HealthConfig.max_check_interval`` defaults to 300s (backoff ceiling)."""
    from ponte.config import HealthConfig

    assert HealthConfig().max_check_interval == 300.0


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


def test_windows_run_as_user(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222

[windows]
run_as = "user"
"""
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.windows.run_as == "user"


def test_windows_run_as_invalid_rejected(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222

[windows]
run_as = "root"
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_windows_run_as_default_is_user(tmp_path) -> None:
    """默认 run_as=user：SYSTEM 计划任务读不到 ~/.ssh 密钥，不能做默认值。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222
"""
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.windows.run_as == "user"


def test_expand_tilde(tmp_path, monkeypatch) -> None:
    # Windows 用 USERPROFILE，POSIX 用 HOME；两处都设，保证跨平台。
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = """
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


# ---------------------------------------------------------------------------
# 文档承诺过的可调项（此前被静默忽略）
# ---------------------------------------------------------------------------


def test_retry_stable_after_parsed(tmp_path) -> None:
    """``[retry] stable_after`` 必须真的生效，而不是被解析器丢掉。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222

[retry]
stable_after = 15
"""
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.retry.stable_after == 15.0


def test_health_max_check_interval_parsed(tmp_path) -> None:
    """``[health] max_check_interval`` 同上。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222

[health]
max_check_interval = 45
"""
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.health.max_check_interval == 45.0


def test_unknown_key_is_reported_not_ignored(tmp_path) -> None:
    """拼错的键要产生警告，而不是无声无息。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222
parsel_port = 9999

[retry]
base_dely = 3
"""
    cfg = load_config(_write_toml(tmp_path, body))
    joined = " ".join(cfg.warnings)
    assert "retry.base_dely" in joined
    assert "tunnels[0].parsel_port" in joined
    # 未知键只是警告，不应让加载失败
    assert cfg.retry.base_delay == 5.0


def test_ssh_options_extras_are_not_warned(tmp_path) -> None:
    """``[ssh.options]`` 故意支持任意键，不应产生警告。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[ssh.options]
Compression = "yes"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222
"""
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.warnings == ()


# ---------------------------------------------------------------------------
# 配置文件位置：--config / PONTE_CONFIG / 用户目录 / 包内旧位置
# ---------------------------------------------------------------------------


def test_search_paths_order(monkeypatch, tmp_path) -> None:
    from ponte.config import config_search_paths, user_config_path

    monkeypatch.setenv("PONTE_CONFIG", str(tmp_path / "env.toml"))
    explicit = tmp_path / "explicit.toml"
    paths = config_search_paths(explicit)
    assert paths[0] == os.path.abspath(explicit)
    assert paths[1] == os.path.abspath(tmp_path / "env.toml")
    assert paths[2] == os.path.abspath(user_config_path())


def test_set_config_path_overrides_env(monkeypatch, tmp_path) -> None:
    from ponte.config import config_search_paths, set_config_path

    set_config_path(tmp_path / "pinned.toml")
    monkeypatch.setenv("PONTE_CONFIG", str(tmp_path / "env.toml"))
    assert config_search_paths()[0] == os.path.abspath(tmp_path / "pinned.toml")
    set_config_path(None)
    assert config_search_paths()[0] == os.path.abspath(tmp_path / "env.toml")


def test_get_config_uses_env_var(monkeypatch, tmp_path) -> None:
    """不传路径时按 PONTE_CONFIG 找到文件。"""
    target = _minimal(tmp_path)
    monkeypatch.setenv("PONTE_CONFIG", target)
    cfg = get_config()
    assert cfg.source_path == os.path.abspath(target)
    assert cfg.ssh.host == "example.com"


def test_get_config_missing_lists_searched_paths(monkeypatch, tmp_path) -> None:
    """找不到文件时给出可执行的提示，而不是一句 'not found'。"""
    monkeypatch.setenv("PONTE_CONFIG", str(tmp_path / "nope.toml"))
    with pytest.raises(ConfigNotFoundError) as excinfo:
        get_config()
    message = str(excinfo.value)
    assert "ponte init" in message
    assert "nope.toml" in message


def test_init_config_writes_and_refuses_overwrite(monkeypatch, tmp_path) -> None:
    from ponte.config import init_config

    target = tmp_path / "cfg" / "config.toml"
    written = init_config(target)
    assert os.path.isfile(written)
    # 模板必须能被解析（占位符除外：identity_file 指向不存在的密钥）
    assert "YOUR_SERVER_IP" in open(written, encoding="utf-8").read()

    with pytest.raises(ConfigError):
        init_config(target)

    init_config(target, force=True)  # --force 覆盖不报错


def test_init_config_prefers_legacy_file(monkeypatch, tmp_path) -> None:
    """已存在的包内旧配置应被迁移，而不是用占位模板覆盖。"""
    from ponte import config as config_module
    from ponte.config import init_config

    legacy = tmp_path / "config.toml"
    legacy.write_text("# legacy\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "legacy_config_path", lambda: str(legacy))
    monkeypatch.setattr(
        config_module, "example_config_path", lambda: str(tmp_path / "template.toml")
    )

    target = tmp_path / "out" / "config.toml"
    init_config(target)
    assert target.read_text(encoding="utf-8") == "# legacy\n"


def test_user_config_path_is_absolute_and_named_config_toml() -> None:
    from ponte.config import user_config_path

    path = user_config_path()
    assert os.path.isabs(path)
    assert os.path.basename(path) == "config.toml"


def test_example_config_ships_with_package() -> None:
    """模板必须真正随包发布（曾经 config.toml 不在 wheel 里）。"""
    from ponte.config import example_config_path

    assert os.path.isfile(example_config_path())
