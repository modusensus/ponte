"""pytest tests for :mod:`ponte.retry` (pure logic, no network, no SSH).

These mirror the standalone ``_smoke_test.py`` script but are written as
pytest functions using the *real* imported modules so coverage is attributed
to the actual source files.
"""

from __future__ import annotations

import threading
import time

from ponte.config import RetryConfig
from ponte.retry import RetryEvent, RetryRunner


class _FakeManager:
    """A TunnelManager stand-in that drives the retry loop by itself.

    ``connect()`` must return on its own (it cannot wait for ``runner.stop()``
    to be called from the event driver, that would deadlock). It stays "up"
    briefly, then returns so the runner observes a disconnection.
    """

    def __init__(self, runner, connect_result: int = 0) -> None:
        self._runner = runner
        self.connect_result = connect_result
        self.calls = 0

    def connect(self) -> int:
        self.calls += 1
        deadline = time.monotonic() + 0.5
        while not self._runner._stop.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.connect_result


def _cfg(**kw) -> RetryConfig:
    defaults = dict(
        max_retries=3,
        base_delay=0.1,
        max_delay=0.4,
        backoff_factor=2.0,
        jitter=False,
    )
    defaults.update(kw)
    return RetryConfig(**defaults)


def test_basic_connect_stop_flow() -> None:
    """Connect -> disconnect -> retry -> stop, without exhausting the budget."""
    runner = RetryRunner(_cfg())
    mgr = _FakeManager(runner, connect_result=0)
    events: list[tuple] = []

    def driver() -> None:
        for ev in runner.run(mgr):
            events.append((ev.type, ev.exit_code, ev.delay, ev.attempt, ev.error))
            if ev.type == RetryEvent.RETRYING:
                runner.stop()

    t = threading.Thread(target=driver)
    t.start()
    time.sleep(0.3)
    t.join()

    assert events[0][0] == RetryEvent.CONNECTING, events
    idx = [e[0] for e in events]
    assert RetryEvent.CONNECTED in idx
    assert RetryEvent.DISCONNECTED in idx
    assert RetryEvent.RETRYING in idx
    assert RetryEvent.MAX_RETRIES_REACHED not in idx, events


class _FailManager:
    """connect() always returns immediately (tunnel dies instantly)."""

    def __init__(self) -> None:
        self.calls = 0

    def connect(self) -> None:
        self.calls += 1
        return None


def test_max_retries_exhaustion() -> None:
    """max_retries=2 -> initial + 2 retries, then MAX_RETRIES_REACHED."""
    runner = RetryRunner(_cfg(max_retries=2, base_delay=0.01, max_delay=0.1))
    seq = [(e.type, e.attempt) for e in runner.run(_FailManager())]
    types_ = [s for s, _ in seq]
    assert types_.count(RetryEvent.DISCONNECTED) == 3  # initial + 2 retries
    assert types_.count(RetryEvent.CONNECTED) == 3
    assert seq[-1][0] == RetryEvent.MAX_RETRIES_REACHED


class _FlakyManager:
    """Fails on the first connect, then stays up until stop is requested."""

    def __init__(self, runner: RetryRunner) -> None:
        self._runner = runner
        self.calls = 0

    def connect(self) -> None:
        self.calls += 1
        if self.calls == 1:
            raise ChildProcessError("auth failed")
        while not self._runner._stop.is_set():
            time.sleep(0.01)
        return None


def test_max_retries_zero_retries_forever() -> None:
    """max_retries=0 -> infinite; a raised error is surfaced as DISCONNECTED."""
    runner = RetryRunner(_cfg(max_retries=0, base_delay=0.01, max_delay=0.05))
    mgr = _FlakyManager(runner)
    seq: list = []

    def driver() -> None:
        for ev in runner.run(mgr):
            seq.append(ev)
            if ev.type == RetryEvent.RETRYING and ev.attempt == 1:
                runner.stop()

    t = threading.Thread(target=driver)
    t.start()
    t.join(timeout=5)

    first_disc = seq[1]
    assert first_disc.type == RetryEvent.DISCONNECTED
    assert first_disc.error == "ChildProcessError: auth failed", first_disc
    assert RetryEvent.MAX_RETRIES_REACHED not in [e.type for e in seq], seq


def test_jitter_within_bounds() -> None:
    """Jittered delays land in [0, computed cap) with visible spread."""
    runner = RetryRunner(
        _cfg(max_retries=0, base_delay=5, max_delay=300, backoff_factor=2.0, jitter=True)
    )
    vals = [runner._backoff_delay(3) for _ in range(200)]
    cap = min(5 * 2.0 ** 3, 300)
    assert all(0 <= v < cap for v in vals), (min(vals), max(vals), cap)
    assert any(v > cap * 0.5 for v in vals), "expected spread"


def test_retry_event_repr() -> None:
    assert repr(RetryEvent.connecting()) == "RetryEvent('connecting')"
    assert "RetryEvent('disconnected'" in repr(RetryEvent.disconnected(1, error="boom"))
    assert "RetryEvent('retrying'" in repr(RetryEvent.retrying(1.5, 2))


def test_retry_event_equality() -> None:
    a = RetryEvent.connected()
    b = RetryEvent.connected()
    assert a == b
    assert a != RetryEvent.connecting()
    assert a != "not an event"


def test_sleep_interruptibly_zero_or_negative() -> None:
    runner = RetryRunner(_cfg())
    runner._sleep_interruptibly(0)
    runner._sleep_interruptibly(-1)
    assert not runner.stopped


def test_stop_idempotent() -> None:
    runner = RetryRunner(_cfg())
    runner.stop()
    assert runner.stopped
    runner.stop()
    assert runner.stopped
