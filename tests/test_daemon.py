"""pytest tests for :mod:`ponte.daemon` (offline helpers only).

Daemon lifecycle paths that need a real config / OS service are exercised
through their pure helper functions; nothing here spawns SSH or touches the
scheduled-task / systemd / launchd registries.
"""

from __future__ import annotations

import json
import os
import sys
import time

from ponte.config import SSHConfig, Tunnel, TunnelConfig
from ponte.daemon import (
    DaemonStatus,
    TunnelDaemon,
    _decode_console,
    _derive_status_file,
    _derive_stop_marker,
    _encode_ps,
)


def _cfg(tmp_path) -> TunnelConfig:
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
    assert _decode_console("正常".encode("utf-8")) == "正常"
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
