"""pytest tests for :mod:`ponte.health` (pure logic, no network, no SSH)."""

from __future__ import annotations

import threading
import time
from typing import Optional

import pytest

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
    # Use a healthy manager: with backoff enabled an unhealthy one would grow
    # the interval and could starve the ">= 3 checks" assertion below.
    hc = HealthChecker(_TM(alive=True, ports="list"), _hc())
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


def test_health_status_str() -> None:
    s = HealthStatus(
        process_alive=True,
        remote_ports={23334: True, 17897: False},
        all_healthy=False,
        timestamp=time.time(),
        error="probe failed",
    )
    text = str(s)
    assert "process=alive" in text
    assert "23334" in text
    assert "ok" in text
    assert "17897" in text
    assert "down" in text
    assert "healthy=False" in text
    assert "probe failed" in text


def test_run_loop_rejects_negative_interval() -> None:
    hc = HealthChecker(_TM(alive=True, ports="dict"), _hc())
    with pytest.raises(ValueError):
        hc.run_loop(interval=-1)


def test_check_remote_ports_type_error() -> None:
    hc = HealthChecker(_TM(alive=True, ports="bad"), _hc())
    s = hc.check()
    assert s.all_healthy is False
    assert "TypeError" in (s.error or "")


def test_backoff_interval_formula() -> None:
    """Pure exponential backoff grows by 2**failures and caps at the max."""
    assert HealthChecker._backoff_interval(60.0, 0, 300.0) == 60.0
    assert HealthChecker._backoff_interval(60.0, 1, 300.0) == 120.0
    assert HealthChecker._backoff_interval(60.0, 2, 300.0) == 240.0
    assert HealthChecker._backoff_interval(60.0, 3, 300.0) == 300.0  # capped
    assert HealthChecker._backoff_interval(60.0, 10, 300.0) == 300.0


def _wait_for_values(values: list, n: int, timeout: float = 3.0) -> None:
    """Busy-wait until ``values`` has at least ``n`` entries."""
    deadline = time.time() + timeout
    while len(values) < n and time.time() < deadline:
        time.sleep(0.01)
    assert len(values) >= n, f"captured only {len(values)} values, need {n}"


def _fake_wait_recorder(
    monkeypatch, waits: list[float], released: threading.Event
) -> None:
    """Replace ``Event.wait`` with a recorder that never stops until released.

    The recorder swallows the real sleep (so tests run fast) while capturing
    the timeout each call is invoked with — which is exactly the next check
    interval the backoff logic computed.
    """
    def fake_wait(self, timeout=None):
        if timeout is not None:
            waits.append(timeout)
        if released.is_set():
            return True
        time.sleep(0.001)
        return False

    monkeypatch.setattr(threading.Event, "wait", fake_wait)


def _stop_health_thread() -> None:
    """Stop + join the ``run_loop`` background thread, if still running."""
    for t in threading.enumerate():
        if t.name == "ponte-health-check":
            t.join(timeout=1)


def test_run_loop_backoff_after_failures(monkeypatch) -> None:
    """Unhealthy checks grow the interval exponentially, then cap at the max."""
    hc = HealthChecker(
        _TM(alive=True, ports="dict"),  # one remote port down -> unhealthy
        HealthConfig(
            check_interval=1.0,
            remote_check_enabled=True,
            remote_check_timeout=1,
            max_check_interval=8.0,
        ),
    )
    waits: list[float] = []
    released = threading.Event()
    _fake_wait_recorder(monkeypatch, waits, released)

    hc.run_loop(interval=1.0)
    _wait_for_values(waits, 6)
    released.set()
    _stop_health_thread()

    # Base interval 1.0: 1 failure -> 2.0, 2 -> 4.0, then capped at 8.0.
    assert waits[0] == 2.0
    assert waits[1] == 4.0
    assert waits[2] == 8.0
    assert all(w == 8.0 for w in waits[2:])


def test_run_loop_backoff_resets_after_recovery(monkeypatch) -> None:
    """A healthy check resets the counter, dropping the interval back to base."""

    class _FlakyTM:
        """TunnelManager stand-in whose health follows a scripted pattern."""

        def __init__(self, healthy_flags: list[bool]) -> None:
            self._proc = _Proc(True)
            self._flags = list(healthy_flags)
            self._i = 0

        @property
        def process(self) -> _Proc:
            return self._proc

        def check_remote_ports(self, **kw) -> dict[int, bool]:
            healthy = self._flags[self._i % len(self._flags)]
            self._i += 1
            return {23334: healthy}

    tm = _FlakyTM([False, False, False, True, True, False])
    hc = HealthChecker(
        tm,
        HealthConfig(
            check_interval=1.0,
            remote_check_enabled=True,
            remote_check_timeout=1,
            max_check_interval=8.0,
        ),
    )
    waits: list[float] = []
    released = threading.Event()
    _fake_wait_recorder(monkeypatch, waits, released)

    hc.run_loop(interval=1.0)
    _wait_for_values(waits, 7)
    released.set()
    _stop_health_thread()

    # Growing while failing...
    assert waits[0] == 2.0
    assert waits[1] == 4.0
    assert waits[2] == 8.0
    # ...back to the base interval once healthy...
    assert waits[3] == 1.0
    assert waits[4] == 1.0
    # ...and growing again on new consecutive failures.
    assert waits[5] == 2.0
    assert waits[6] == 4.0
