"""pytest tests for :mod:`ponte.health` (pure logic, no network, no SSH)."""

from __future__ import annotations

import threading
import time
from typing import Optional

from ponte.config import HealthConfig
from ponte.health import HealthChecker, HealthStatus


class _Proc:
    def __init__(self, alive: bool) -> None:
        self.alive = alive

    def poll(self) -> Optional[int]:
        return None if self.alive else 1


class _TM:
    """A TunnelManager stand-in exposing ``process`` + ``check_remote_ports``."""

    def __init__(self, alive: bool = True, ports: str = "dict", fail_ports: bool = False) -> None:
        self._proc = _Proc(alive)
        self.ports = ports
        self.fail_ports = fail_ports
        self._timeout: Optional[int] = None

    @property
    def process(self) -> _Proc:
        return self._proc

    def check_remote_ports(self, **kw) -> object:
        self._timeout = kw.get("timeout")
        if self.fail_ports:
            raise ConnectionError("refused")
        if self.ports == "dict":
            return {23334: True, 17897: False}
        if self.ports == "list":
            return [23334, 17897]
        return None


def _hc() -> HealthConfig:
    return HealthConfig(check_interval=60, remote_check_enabled=True, remote_check_timeout=10)


def test_dict_ports() -> None:
    hc = HealthChecker(_TM(alive=True, ports="dict"), _hc())
    s = hc.check()
    assert s.process_alive is True
    assert s.remote_ports == {23334: True, 17897: False}
    assert s.all_healthy is False  # one port down
    assert s.error is None
    assert s.timestamp > 0
    assert hc.manager._timeout == 10, "timeout passed through"


def test_list_normalization() -> None:
    hc = HealthChecker(_TM(alive=True, ports="list"), _hc())
    s = hc.check()
    assert s.remote_ports == {23334: True, 17897: True}
    assert s.all_healthy is True


def test_dead_process() -> None:
    hc = HealthChecker(_TM(alive=False, ports="dict"), _hc())
    s = hc.check()
    assert s.process_alive is False
    assert s.all_healthy is False
    assert s.remote_ports == {23334: True, 17897: False}


def test_port_check_failure() -> None:
    hc = HealthChecker(_TM(alive=True, ports="dict", fail_ports=True), _hc())
    s = hc.check()
    assert s.all_healthy is False
    assert s.error is not None and "ConnectionError" in s.error


def test_remote_check_disabled() -> None:
    cfg = HealthConfig(check_interval=60, remote_check_enabled=False, remote_check_timeout=10)
    hc = HealthChecker(_TM(alive=True, ports="bad"), cfg)
    s = hc.check()
    assert s.remote_ports == {}
    assert s.all_healthy is True


def test_run_loop_stops_cleanly() -> None:
    hc = HealthChecker(_TM(alive=True, ports="dict"), _hc())
    seen: list[HealthStatus] = []
    stop = hc.run_loop(interval=0.05, callback=lambda st: seen.append(st))
    assert isinstance(stop, threading.Event)
    time.sleep(0.3)
    stop.set()
    time.sleep(0.15)
    assert len(seen) >= 3, len(seen)
    assert all(isinstance(st, HealthStatus) for st in seen)
    n = len(seen)
    time.sleep(0.15)
    assert len(seen) == n, (len(seen), n)


def test_run_loop_callback_error_swallowed() -> None:
    hc = HealthChecker(_TM(alive=True, ports="dict"), _hc())

    def bad_cb(_st: HealthStatus) -> None:
        raise ValueError("cb boom")

    stop = hc.run_loop(interval=0.02, callback=bad_cb)
    time.sleep(0.15)
    stop.set()
    assert isinstance(hc.last_callback_error, ValueError)
