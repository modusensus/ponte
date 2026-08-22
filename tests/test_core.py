"""pytest tests for :mod:`ponte.core` (SSH arg building + port probe parsing).

No real SSH is spawned: ``subprocess.run`` is patched and the probe/connect
command parsing is exercised directly.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from ponte.config import SSHConfig, SSHOptions, Tunnel, TunnelConfig, WindowsConfig
from ponte.core import TunnelManager, _find_ssh


def _cfg(*, host: str = "example.com", user: str = "testuser", port: int = 22,
         ssh_exe: str = "/usr/bin/ssh") -> TunnelConfig:
    return TunnelConfig(
        ssh=SSHConfig(
            host=host,
            user=user,
            identity_file="/keys/id_rsa",
            port=port,
            known_hosts_file="/keys/known_hosts",
            options=SSHOptions(),
        ),
        tunnels=[
            Tunnel(remote_port=23334, local_host="localhost", local_port=2222),
            Tunnel(remote_port=17897, local_host="localhost", local_port=7897),
        ],
        windows=WindowsConfig(ssh_exe=ssh_exe),
    )


def test_build_args_full(monkeypatch) -> None:
    monkeypatch.setattr("ponte.core._find_ssh", lambda: "/usr/bin/ssh")
    tm = TunnelManager(_cfg())
    args = tm.build_args()
    assert args[0] == "/usr/bin/ssh"
    assert "-i" in args
    assert args[args.index("-i") + 1] == "/keys/id_rsa"
    # 每条 -R 规则独立成对
    pairs = [args[i : i + 2] for i in range(len(args)) if args[i] == "-R"]
    assert ("-R", "23334:localhost:2222") in [tuple(p) for p in pairs]
    assert ("-R", "17897:localhost:7897") in [tuple(p) for p in pairs]
    # 默认 22 端口不加 -p
    assert "-p" not in args
    assert args[-1] == "testuser@example.com"


def test_build_args_custom_port(monkeypatch) -> None:
    monkeypatch.setattr("ponte.core._find_ssh", lambda: "/usr/bin/ssh")
    tm = TunnelManager(_cfg(port=2222))
    args = tm.build_args()
    assert args[args.index("-p") + 1] == "2222"


def test_find_ssh_uses_python_ssh(monkeypatch) -> None:
    # PATH 命中 ssh 时用 PATH（非 Windows 分支）
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("ponte.core.shutil.which", lambda _name: "/usr/local/bin/ssh")
    assert _find_ssh() == "/usr/local/bin/ssh"


def test_find_ssh_windows_config(monkeypatch) -> None:
    # Windows 且配置了 ssh_exe 且文件存在 → 用配置值
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("ponte.core.shutil.which", lambda _name: "/not/used")
    monkeypatch.setattr("ponte.core.os.path.isfile", lambda p: p == r"D:\Git\usr\bin\ssh.exe")
    cfg = _cfg(ssh_exe=r"D:\Git\usr\bin\ssh.exe")
    monkeypatch.setattr("ponte.core.get_config", lambda: cfg)
    assert _find_ssh() == r"D:\Git\usr\bin\ssh.exe"


def test_test_connection_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        "ponte.core.subprocess.run",
        lambda *a, **k: __import__("types").SimpleNamespace(returncode=0, stdout="OK"),
    )
    tm = TunnelManager(_cfg())
    assert tm.test_connection(timeout=5) is True


def test_test_connection_fail(monkeypatch) -> None:
    monkeypatch.setattr(
        "ponte.core.subprocess.run",
        lambda *a, **k: __import__("types").SimpleNamespace(returncode=1, stdout=""),
    )
    tm = TunnelManager(_cfg())
    assert tm.test_connection(timeout=5) is False


def test_check_remote_ports_python_probe(monkeypatch) -> None:
    # 服务器端 python3 分支：输出空格分隔的开放端口
    monkeypatch.setattr(
        "ponte.core.subprocess.run",
        lambda *a, **k: __import__("types").SimpleNamespace(returncode=0, stdout="23334\n"),
    )
    tm = TunnelManager(_cfg())
    assert tm.check_remote_ports(timeout=5) == {23334: True, 17897: False}


def test_check_remote_ports_tool_fallback(monkeypatch) -> None:
    # 回退分支：ss 风格输出 ``*:23334 `` token
    monkeypatch.setattr(
        "ponte.core.subprocess.run",
        lambda *a, **k: __import__("types").SimpleNamespace(
            returncode=0,
            stdout="tcp LISTEN 0 128 0.0.0.0:23334 users:(())\n",
        ),
    )
    tm = TunnelManager(_cfg())
    assert tm.check_remote_ports(timeout=5) == {23334: True, 17897: False}


def test_check_remote_ports_error(monkeypatch) -> None:
    monkeypatch.setattr(
        "ponte.core.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.SubprocessError("boom")),
    )
    tm = TunnelManager(_cfg())
    assert tm.check_remote_ports(timeout=5) == {23334: False, 17897: False}


def test_stop_no_process() -> None:
    tm = TunnelManager(_cfg())
    tm.process = None
    tm.stop()  # 无进程时安全返回
