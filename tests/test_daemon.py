"""pytest tests for :mod:`ponte.daemon` (offline helpers only).

Daemon lifecycle paths that need a real config / OS service are exercised
through their pure helper functions; nothing here spawns SSH or touches the
scheduled-task / systemd / launchd registries.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
import types

import pytest

from ponte.config import SSHConfig, Tunnel, TunnelConfig, WindowsConfig
from ponte.daemon import (
    DaemonStatus,
    TunnelDaemon,
    _decode_console,
    _derive_status_file,
    _derive_stop_marker,
    _encode_ps,
)
from ponte.health import HealthStatus


def _cfg(tmp_path, *, run_as: str = "system") -> TunnelConfig:
    return TunnelConfig(
        ssh=SSHConfig(
            host="example.com",
            user="testuser",
            identity_file="/keys/id_rsa",
            known_hosts_file="/keys/known_hosts",
        ),
        tunnels=[Tunnel(remote_port=23334, local_host="localhost", local_port=2222)],
        daemon=__import__("ponte.config", fromlist=["DaemonConfig"]).DaemonConfig(
            pid_file=str(tmp_path / "ponte.pid"),
            log_file=str(tmp_path / "ponte.log"),
        ),
        windows=WindowsConfig(run_as=run_as),
    )


def test_derive_status_and_stop_from_pid() -> None:
    pid = r"C:\x\ponte.pid"
    assert _derive_status_file(pid) == r"C:\x\ponte.status.json"
    assert _derive_stop_marker(pid) == r"C:\x\ponte.stop"


def test_encode_ps_roundtrip() -> None:
    script = "Write-Output 'installed'"
    encoded = _encode_ps(script)
    assert isinstance(encoded, str)
    decoded = encoded.encode("ascii")
    import base64
    assert base64.b64decode(decoded).decode("utf-16-le") == script


def test_decode_console_utf8_and_gbk() -> None:
    assert _decode_console(b"") == ""
    assert _decode_console("正常".encode()) == "正常"
    # GBK 字节在 UTF-8 下非法 → 回退 GBK 解码
    assert _decode_console("已注册".encode("gbk")) == "已注册"


def test_daemon_status_uptime() -> None:
    s = DaemonStatus(running=True, uptime_seconds=3661)
    assert s.uptime == "1h 1m 1s"


def test_write_read_pid(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    d.write_pid()
    assert d.read_pid() == os.getpid()


def test_read_pid_missing(tmp_path) -> None:
    d = TunnelDaemon(_cfg(tmp_path))
    assert d.read_pid() is None


def test_status_not_running(tmp_path) -> None:
    d = TunnelDaemon(_cfg(tmp_path))
    s = d.status()
    assert s.running is False


def test_status_json_parsing(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    # 伪造一个存活 pid 的 status 文件：用当前进程
    with open(cfg.daemon.pid_file, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    with open(d.status_file, "w", encoding="utf-8") as fh:
        json.dump(
            {"started_at": time.time(), "healthy": True, "remote_ports": {"23334": True}},
            fh,
        )
    s = d.status()
    assert s.running is True
    assert s.healthy is True
    assert s.remote_ports == {23334: True}


def test_status_malformed_pid_file(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    with open(cfg.daemon.pid_file, "w", encoding="utf-8") as fh:
        fh.write("not-a-number")
    assert d.read_pid() is None


def test_status_malformed_status_json(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    with open(cfg.daemon.pid_file, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    with open(d.status_file, "w", encoding="utf-8") as fh:
        fh.write("not json")
    s = d.status()
    assert s.running is True
    assert s.healthy is None


def test_safe_remove_missing_file(tmp_path) -> None:
    # 删除不存在的路径不应报错
    TunnelDaemon._safe_remove(str(tmp_path / "missing"))


def test_cleanup_removes_pid_and_marker(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    d.write_pid()
    with open(d.stop_marker, "w", encoding="utf-8") as fh:
        fh.write("x")
    d._cleanup()
    assert not os.path.exists(cfg.daemon.pid_file)
    assert not os.path.exists(d.stop_marker)


def test_pythonw_path_when_executable_is_pythonw(monkeypatch, tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    monkeypatch.setattr(sys, "executable", r"C:\Python\pythonw.exe")
    assert d._pythonw_path() == r"C:\Python\pythonw.exe"


def test_pythonw_path_falls_back_to_python(monkeypatch, tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    monkeypatch.setattr(sys, "executable", r"C:\Python\python.exe")
    # 模拟 Windows 分隔符语义（POSIX 上 os.path.join 用 / 会破坏匹配）：
    # split 拆出目录，join 用反斜杠拼接，pythonw.exe 存在。
    monkeypatch.setattr(
        "ponte.daemon.os.path.split",
        lambda _p: (r"C:\Python", "python.exe"),
    )
    monkeypatch.setattr(
        "ponte.daemon.os.path.join",
        lambda *parts: "\\".join(str(p).rstrip("\\/") for p in parts),
    )
    monkeypatch.setattr(
        "ponte.daemon.os.path.exists",
        lambda p: str(p).lower() == r"c:\python\pythonw.exe",
    )
    assert d._pythonw_path() == r"C:\Python\pythonw.exe"


def _capture_install_script(monkeypatch, tmp_path, *, run_as: str) -> str:
    """Mock the PowerShell layer and return the Scheduled-Task script."""
    cfg = _cfg(tmp_path, run_as=run_as)
    daemon = TunnelDaemon(cfg)
    captured: dict[str, str] = {}

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(daemon, "_pythonw_path", lambda: r"C:\Python\pythonw.exe")

    def _run(script: str) -> str:
        captured["script"] = script
        return "installed"

    monkeypatch.setattr(daemon, "_run_powershell", _run)

    assert daemon.install_scheduled_task() == "installed"
    return captured["script"]


def test_install_scheduled_task_run_as_system(monkeypatch, tmp_path) -> None:
    """Default run_as=system keeps a boot-time SYSTEM task (pre-login)."""
    script = _capture_install_script(monkeypatch, tmp_path, run_as="system")
    assert "New-ScheduledTaskTrigger -AtStartup" in script
    assert "-UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest" in script
    assert "-AtLogOn" not in script
    assert "Interactive" not in script


def test_install_scheduled_task_run_as_user(monkeypatch, tmp_path) -> None:
    """run_as=user uses a logon task that can read the installing user's keys."""
    script = _capture_install_script(monkeypatch, tmp_path, run_as="user")
    assert "$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name" in script
    assert "New-ScheduledTaskTrigger -AtLogOn -User $currentUser" in script
    assert "-UserId $currentUser -LogonType Interactive -RunLevel Limited" in script
    assert "-UserId 'SYSTEM'" not in script


def test_setup_logging_idempotent(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    d._setup_logging()
    root = __import__("logging").getLogger("ponte")
    assert getattr(root, "_ponte_setup_ok", False) is True
    # 第二次调用不应重复添加 handler
    before = len(root.handlers)
    d._setup_logging()
    assert len(root.handlers) == before


def test_on_health_writes_status(tmp_path) -> None:
    from ponte.health import HealthStatus

    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    d._on_health(
        HealthStatus(
            process_alive=True,
            remote_ports={23334: True},
            all_healthy=True,
            timestamp=time.time(),
        )
    )
    data = d._read_status_json()
    assert data["process_alive"] is True
    assert data["healthy"] is True
    assert data["remote_ports"] == {"23334": True}


def test_read_status_json_missing_returns_empty(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    assert d._read_status_json() == {}


def test_read_status_json_invalid_returns_empty(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    os.makedirs(os.path.dirname(d.status_file), exist_ok=True)
    with open(d.status_file, "w", encoding="utf-8") as fh:
        fh.write("not json")
    assert d._read_status_json() == {}


def test_write_status_json_failure_silent(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)

    def _bad_open(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", _bad_open)
    d._write_status_json({"x": 1})  # 不应抛出


class _FakeManager:
    """A TunnelManager stand-in that only records ``stop()`` calls."""

    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


def _unhealthy_status(process_alive: bool = True) -> HealthStatus:
    return HealthStatus(
        process_alive=process_alive,
        remote_ports={23334: False},
        all_healthy=False,
        timestamp=time.time(),
    )


def _healthy_status() -> HealthStatus:
    return HealthStatus(
        process_alive=True,
        remote_ports={23334: True},
        all_healthy=True,
        timestamp=time.time(),
    )


def test_on_health_forces_reconnect_after_threshold(tmp_path) -> None:
    """连续 N 次 unhealthy 且进程活着 → manager.stop() 被调用，触发后计数归零。"""
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    manager = _FakeManager()

    # 连续两次假死还不够。
    d._on_health(_unhealthy_status(), manager)
    d._on_health(_unhealthy_status(), manager)
    assert manager.stop_calls == 0

    # 第三次达到阈值 → 强制重连一次。
    d._on_health(_unhealthy_status(), manager)
    assert manager.stop_calls == 1

    # 触发后计数归零，需重新累积才能再次触发。
    d._on_health(_unhealthy_status(), manager)
    d._on_health(_unhealthy_status(), manager)
    assert manager.stop_calls == 1
    d._on_health(_unhealthy_status(), manager)
    assert manager.stop_calls == 2


def test_on_health_resets_failures_on_recovery(tmp_path) -> None:
    """健康恢复 → 计数归零，不触发 stop。"""
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    manager = _FakeManager()

    d._on_health(_unhealthy_status(), manager)
    d._on_health(_unhealthy_status(), manager)
    d._on_health(_healthy_status(), manager)  # 恢复 → 计数归零

    # 恢复后重新累计，两次未达阈值 → 不触发。
    d._on_health(_unhealthy_status(), manager)
    d._on_health(_unhealthy_status(), manager)
    assert manager.stop_calls == 0
    # 第三次才触发。
    d._on_health(_unhealthy_status(), manager)
    assert manager.stop_calls == 1


def test_on_health_does_not_force_reconnect_when_process_dead(tmp_path) -> None:
    """进程已死（非假死）不触发强制重连，交给 retry 的 backoff 处理。"""
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    manager = _FakeManager()

    for _ in range(5):
        d._on_health(_unhealthy_status(process_alive=False), manager)
    assert manager.stop_calls == 0


# ---------------------------------------------------------------------------
# 进程参数 / 工作目录 / 强制终止提示
# ---------------------------------------------------------------------------


def test_daemon_args_pass_config_explicitly(tmp_path) -> None:
    """服务方式启动时必须带上 --config，否则可能解析到另一份配置。"""
    cfg = _cfg(tmp_path)
    cfg = dataclasses.replace(cfg, source_path=str(tmp_path / "config.toml"))
    args = TunnelDaemon(cfg)._daemon_args()
    assert args[:2] == ["-m", "ponte.main"]
    assert "--config" in args
    assert args[args.index("--config") + 1] == str(tmp_path / "config.toml")
    assert args[-2:] == ["start", "--foreground"]


def test_daemon_args_string_quotes_paths_with_spaces(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    cfg = dataclasses.replace(
        cfg, source_path=str(tmp_path / "my config" / "config.toml")
    )
    rendered = TunnelDaemon(cfg)._daemon_args_string()
    assert '"' in rendered
    assert rendered.startswith("-m ponte.main")


def test_work_dir_is_config_directory_not_package_parent(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    cfg = dataclasses.replace(cfg, source_path=str(tmp_path / "config.toml"))
    assert TunnelDaemon(cfg).work_dir == str(tmp_path)


def test_work_dir_falls_back_to_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cfg = _cfg(tmp_path)
    cfg = dataclasses.replace(cfg, source_path="/definitely/missing/config.toml")
    assert TunnelDaemon(cfg).work_dir == os.path.expanduser("~")


def test_stop_reports_force_kill_in_message(monkeypatch, tmp_path) -> None:
    """优雅停止失败时必须告诉用户用了强杀（此前提示永远不会出现）。"""
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    d.write_pid()
    monkeypatch.setattr(d, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(d, "_stop_autostart", lambda: None)
    monkeypatch.setattr(d, "_force_kill", lambda _pid: None)

    status = d.stop(timeout=0.1)
    assert status.running is False
    assert "强制" in status.message


def test_stop_graceful_has_no_kill_message(monkeypatch, tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    d.write_pid()
    alive = {"n": 0}

    def _pid_alive(_pid: int) -> bool:
        alive["n"] += 1
        return alive["n"] == 1  # 第一次（status）活着，之后已退出

    monkeypatch.setattr(d, "_pid_alive", _pid_alive)
    monkeypatch.setattr(d, "_stop_autostart", lambda: None)
    status = d.stop(timeout=0.1)
    assert "强制" not in status.message
    assert "kill" not in status.message.lower()


# ---------------------------------------------------------------------------
# 服务安装 / 卸载（subprocess 全部 mock，跨平台可跑）
# ---------------------------------------------------------------------------


def _record_run(record: list[list[str]]):
    def _run(args, **_kwargs):
        record.append(list(args))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    return _run


def test_install_systemd_writes_unit_with_config(monkeypatch, tmp_path) -> None:
    cfg = _cfg(tmp_path)
    cfg = dataclasses.replace(cfg, source_path=str(tmp_path / "config.toml"))
    d = TunnelDaemon(cfg)
    home = tmp_path / "home"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        "ponte.daemon.os.path.expanduser", lambda p: str(home) + p[1:]
    )
    record: list[list[str]] = []
    monkeypatch.setattr("ponte.daemon.subprocess.run", _record_run(record))

    assert d._install_systemd() == "installed"

    unit_path = home / ".config" / "systemd" / "user" / "ponte.service"
    unit = unit_path.read_text(encoding="utf-8")
    assert "Restart=always" in unit
    assert "--config" in unit
    assert f'WorkingDirectory={tmp_path}' in unit
    assert ["systemctl", "--user", "enable", "--now", "ponte.service"] in record


def test_uninstall_systemd_removes_unit(monkeypatch, tmp_path) -> None:
    d = TunnelDaemon(_cfg(tmp_path))
    home = tmp_path / "home"
    unit_path = home / ".config" / "systemd" / "user" / "ponte.service"
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        "ponte.daemon.os.path.expanduser", lambda p: str(home) + p[1:]
    )
    monkeypatch.setattr("ponte.daemon.subprocess.run", _record_run([]))

    assert d._uninstall_systemd() == "uninstalled"
    assert not unit_path.exists()


def test_install_launchd_escapes_and_removes(monkeypatch, tmp_path) -> None:
    cfg = _cfg(tmp_path)
    cfg = dataclasses.replace(cfg, source_path=str(tmp_path / "a&b.toml"))
    d = TunnelDaemon(cfg)
    home = tmp_path / "home"
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "ponte.daemon.os.path.expanduser", lambda p: str(home) + p[1:]
    )
    record: list[list[str]] = []
    monkeypatch.setattr("ponte.daemon.subprocess.run", _record_run(record))

    assert d._install_launchd() == "installed"
    plist_path = home / "Library" / "LaunchAgents" / "com.modusensus.ponte.plist"
    plist = plist_path.read_text(encoding="utf-8")
    assert "KeepAlive" in plist
    assert "a&amp;b.toml" in plist  # XML 转义，避免畸形 plist
    loaded = [cmd for cmd in record if cmd[:3] == ["launchctl", "load", "-w"]]
    assert loaded, record
    assert os.path.basename(loaded[0][3]) == "com.modusensus.ponte.plist"

    assert d._uninstall_launchd() == "uninstalled"
    assert not plist_path.exists()


def test_spawn_background_returns_child_pid(monkeypatch, tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)

    class _Popen:
        def __init__(self, cmd, **_kwargs) -> None:
            self.cmd = cmd

    def _popen(cmd, **kwargs):
        proc = _Popen(cmd, **kwargs)
        # 子进程“启动后”写下 PID
        d.write_pid()
        return proc

    monkeypatch.setattr("ponte.daemon.subprocess.Popen", _popen)
    assert d._spawn_background() == os.getpid()


def test_install_service_dispatch_rejects_unknown_platform(monkeypatch, tmp_path) -> None:
    d = TunnelDaemon(_cfg(tmp_path))
    monkeypatch.setattr(sys, "platform", "aix")

    with pytest.raises(RuntimeError):
        d.install_service()
    with pytest.raises(RuntimeError):
        d.uninstall_service()
