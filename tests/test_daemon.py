"""pytest tests for :mod:`ponte.daemon` (offline helpers only).

Daemon lifecycle paths that need a real config / OS service are exercised
through their pure helper functions; nothing here spawns SSH or touches the
scheduled-task / systemd / launchd registries.
"""

from __future__ import annotations

import json
import os
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
