"""pytest tests for the ponte CLI (typer) — no real daemon/SSH invoked."""

from __future__ import annotations

from typer.testing import CliRunner

from ponte.config import (
    HealthConfig,
    RetryConfig,
    SSHConfig,
    SSHOptions,
    Tunnel,
    TunnelConfig,
    WindowsConfig,
)
from ponte.daemon import DaemonStatus
from ponte.main import app


def _cfg() -> TunnelConfig:
    return TunnelConfig(
        ssh=SSHConfig(
            host="example.com",
            user="testuser",
            identity_file="/keys/id_rsa",
            known_hosts_file="/keys/known_hosts",
            options=SSHOptions(),
        ),
        tunnels=[Tunnel(remote_port=23334, local_host="localhost", local_port=2222)],
        retry=RetryConfig(max_retries=0, base_delay=5.0),
        health=HealthConfig(check_interval=60),
        windows=WindowsConfig(ssh_exe="/usr/bin/ssh"),
    )


class _FakeDaemon:
    def __init__(self, *, running: bool = False) -> None:
        self._running = running
        self.log_file = "/tmp/nonexistent-ponte.log"

    def status(self) -> DaemonStatus:
        return DaemonStatus(
            running=self._running,
            pid=1234 if self._running else None,
            uptime_seconds=10,
        )


def test_help(monkeypatch) -> None:
    monkeypatch.setattr("ponte.main.get_config", lambda: _cfg())
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("start", "stop", "status", "install", "uninstall", "config"):
        assert cmd in result.output


def test_config_command(monkeypatch) -> None:
    monkeypatch.setattr("ponte.main.get_config", lambda: _cfg())
    result = CliRunner().invoke(app, ["config"])
    assert result.exit_code == 0
    assert "example.com" in result.output
    assert "23334" in result.output
    assert "pid_file" in result.output


def test_start_already_running(monkeypatch) -> None:
    monkeypatch.setattr(
        "ponte.main._daemon", lambda: _FakeDaemon(running=True)
    )
    result = CliRunner().invoke(app, ["start"])
    assert result.exit_code == 0
    assert "已在运行" in result.output


def test_status_not_running(monkeypatch) -> None:
    monkeypatch.setattr(
        "ponte.main._daemon", lambda: _FakeDaemon(running=False)
    )
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0
    assert "未运行" in result.output


def test_logs_no_file(monkeypatch) -> None:
    monkeypatch.setattr(
        "ponte.main._daemon", lambda: _FakeDaemon(running=False)
    )
    # 无日志文件时给提示而不是崩溃
    result = CliRunner().invoke(app, ["logs"])
    assert result.exit_code == 0


class _FakeDaemonWithActions:
    def __init__(self, *, running: bool = False, test_ok: bool = True,
                 ports: dict[int, bool] | None = None,
                 install_result: str = "installed") -> None:
        self._running = running
        self._test_ok = test_ok
        self._ports = ports or {}
        self._install_result = install_result
        self.log_file = ""
        self.stopped = False
        self.started = False

    def status(self) -> DaemonStatus:
        return DaemonStatus(
            running=self._running,
            pid=1234 if self._running else None,
            uptime_seconds=10,
            healthy=True,
            remote_ports=self._ports,
        )

    def start(self, foreground: bool = False) -> int:
        self.started = True
        return 1234

    def run(self) -> int:
        self.started = True
        return 0

    def stop(self) -> DaemonStatus:
        self.stopped = True
        return DaemonStatus(running=False, message="killed")

    def test_connection(self, timeout: int = 10) -> bool:
        return self._test_ok

    def check_remote_ports(self, timeout: int = 10) -> dict[int, bool]:
        return self._ports

    def install_service(self) -> str:
        return self._install_result

    def uninstall_service(self) -> str:
        return "uninstalled"


def test_start_foreground(monkeypatch) -> None:
    fake = _FakeDaemonWithActions(running=False)
    monkeypatch.setattr("ponte.main._daemon", lambda: fake)
    result = CliRunner().invoke(app, ["start", "--foreground"])
    assert result.exit_code == 0
    assert fake.started


def test_stop_when_running(monkeypatch) -> None:
    fake = _FakeDaemonWithActions(running=True)
    monkeypatch.setattr("ponte.main._daemon", lambda: fake)
    result = CliRunner().invoke(app, ["stop"])
    assert result.exit_code == 0
    assert fake.stopped


def test_stop_when_not_running(monkeypatch) -> None:
    fake = _FakeDaemonWithActions(running=False)
    monkeypatch.setattr("ponte.main._daemon", lambda: fake)
    result = CliRunner().invoke(app, ["stop"])
    assert result.exit_code == 0
    assert "未运行" in result.output


def test_restart(monkeypatch) -> None:
    fake = _FakeDaemonWithActions(running=True)
    monkeypatch.setattr("ponte.main._daemon", lambda: fake)
    result = CliRunner().invoke(app, ["restart"])
    assert result.exit_code == 0
    assert fake.stopped
    assert fake.started


def test_test_command_ok(monkeypatch) -> None:
    fake = _FakeDaemonWithActions(test_ok=True)
    monkeypatch.setattr("ponte.main._daemon", lambda: fake)
    result = CliRunner().invoke(app, ["test"])
    assert result.exit_code == 0
    assert "OK" in result.output


def test_test_command_fail(monkeypatch) -> None:
    fake = _FakeDaemonWithActions(test_ok=False)
    monkeypatch.setattr("ponte.main._daemon", lambda: fake)
    result = CliRunner().invoke(app, ["test"])
    assert result.exit_code == 1


def test_check_command_with_ports(monkeypatch) -> None:
    fake = _FakeDaemonWithActions(ports={23334: True, 17897: False})
    monkeypatch.setattr("ponte.main._daemon", lambda: fake)
    result = CliRunner().invoke(app, ["check"])
    assert result.exit_code == 0
    assert "23334" in result.output
    assert "17897" in result.output


def test_check_command_no_ports(monkeypatch) -> None:
    fake = _FakeDaemonWithActions(ports={})
    monkeypatch.setattr("ponte.main._daemon", lambda: fake)
    result = CliRunner().invoke(app, ["check"])
    assert result.exit_code == 0


def test_install_command(monkeypatch) -> None:
    fake = _FakeDaemonWithActions(install_result="installed")
    monkeypatch.setattr("ponte.main._daemon", lambda: fake)
    result = CliRunner().invoke(app, ["install"])
    assert result.exit_code == 0
    assert "installed" in result.output


def test_uninstall_command(monkeypatch) -> None:
    fake = _FakeDaemonWithActions()
    monkeypatch.setattr("ponte.main._daemon", lambda: fake)
    result = CliRunner().invoke(app, ["uninstall"])
    assert result.exit_code == 0


def test_logs_command_tail_and_follow(monkeypatch, tmp_path) -> None:
    log = tmp_path / "ponte.log"
    log.write_text("line1\nline2\nline3\n", encoding="utf-8")
    fake = _FakeDaemonWithActions(running=False)
    fake.log_file = str(log)
    monkeypatch.setattr("ponte.main._daemon", lambda: fake)
    result = CliRunner().invoke(app, ["logs", "-n", "2"])
    assert result.exit_code == 0
    assert "line2" in result.output
    assert "line3" in result.output
    assert "line1" not in result.output


def test_force_kill_message_detection() -> None:
    from ponte.main import _force_kill_message
    assert "强制" in _force_kill_message(DaemonStatus(running=False, message="已强制 kill"))
    assert _force_kill_message(DaemonStatus(running=False, message="正常停止")) == ""
