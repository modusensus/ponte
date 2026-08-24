"""Retry and resilience layer for the ponte SSH reverse tunnel manager.

This module wraps :meth:`TunnelManager.connect` with reconnect logic:
exponential backoff, optional jitter, and a configurable retry budget.

The runner is a *generator-driven* state machine. The driving code (e.g. a
CLI ``for`` loop or an event pump) iterates over :meth:`RetryRunner.run` and
receives one :class:`RetryEvent` at a time::

    from ponte.retry import RetryRunner, RetryEvent

    runner = RetryRunner(get_config().retry)
    for event in runner.run(manager):
        if event.type == RetryEvent.CONNECTED:
            print("tunnel up")
        elif event.type == RetryEvent.DISCONNECTED:
            print(f"tunnel down: exit={event.exit_code} error={event.error}")
        elif event.type == RetryEvent.RETRYING:
            print(f"retrying #{event.attempt} in {event.delay:.1f}s")

Because the runner runs inside a generator, long backoff sleeps do not block
other threads; :meth:`RetryRunner.stop` may be called from a signal handler or
any thread to request a graceful halt.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Iterator, Optional

from ponte.config import RetryConfig
from ponte.core import TunnelManager

__all__ = ["RetryEvent", "RetryRunner"]

# How often (seconds) to re-check the stop flag during a backoff sleep. Keeps
# stop() responsive without hammering the CPU.
_SLEEP_GRANULARITY = 0.25


class RetryEvent:
    """An event emitted by :meth:`RetryRunner.run`.

    Every event carries a ``type`` attribute that should be compared against
    the class-level constants. Payload attributes are only meaningful for the
    events that carry them:

    ========================  ================================================
    Constant                  Payload
    ========================  ================================================
    ``CONNECTING``            (none)
    ``CONNECTED``             (none)
    ``DISCONNECTED``          ``exit_code`` (int | None), ``error`` (str | None)
    ``RETRYING``              ``delay`` (float), ``attempt`` (int)
    ``MAX_RETRIES_REACHED``   (none)
    ========================  ================================================
    """

    #: A connection attempt is about to be made.
    CONNECTING = "connecting"
    #: The tunnel was established (an SSH session ran to completion).
    CONNECTED = "connected"
    #: The connection terminated. Carries ``exit_code`` and ``error``.
    DISCONNECTED = "disconnected"
    #: Waiting for the next reconnect attempt. Carries ``delay`` and ``attempt``.
    RETRYING = "retrying"
    #: The retry budget has been exhausted; the runner is giving up.
    MAX_RETRIES_REACHED = "max_retries_reached"

    __slots__ = ("type", "exit_code", "delay", "attempt", "error")

    def __init__(
        self,
        type: str,
        exit_code: Optional[int] = None,
        delay: float = 0.0,
        attempt: int = 0,
        error: Optional[str] = None,
    ) -> None:
        self.type = type
        self.exit_code = exit_code
        self.delay = delay
        self.attempt = attempt
        self.error = error

    # -- Factory helpers -----------------------------------------------------

    @classmethod
    def connecting(cls) -> "RetryEvent":
        return cls(cls.CONNECTING)

    @classmethod
    def connected(cls) -> "RetryEvent":
        return cls(cls.CONNECTED)

    @classmethod
    def disconnected(
        cls, exit_code: Optional[int], error: Optional[str] = None
    ) -> "RetryEvent":
        return cls(cls.DISCONNECTED, exit_code=exit_code, error=error)

    @classmethod
    def retrying(cls, delay: float, attempt: int) -> "RetryEvent":
        return cls(cls.RETRYING, delay=delay, attempt=attempt)

    @classmethod
    def max_retries_reached(cls) -> "RetryEvent":
        return cls(cls.MAX_RETRIES_REACHED)

    # -- dunder ---------------------------------------------------------------

    def __repr__(self) -> str:
        if self.type == self.DISCONNECTED:
            return (
                f"RetryEvent({self.type!r}, exit_code={self.exit_code!r}, "
                f"error={self.error!r})"
            )
        if self.type == self.RETRYING:
            return (
                f"RetryEvent({self.type!r}, delay={self.delay!r}, "
                f"attempt={self.attempt!r})"
            )
        return f"RetryEvent({self.type!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RetryEvent):
            return NotImplemented
        return (
            self.type == other.type
            and self.exit_code == other.exit_code
            and self.delay == other.delay
            and self.attempt == other.attempt
            and self.error == other.error
        )


class RetryRunner:
    """Wrap a :class:`TunnelManager` with reconnect and backoff behaviour.

    The ``config`` argument is a :class:`RetryConfig` (from ``ponte.config``)
    carrying the ``[retry]`` section of ``config.toml``. Used fields:

    * ``max_retries``    — how many times to reconnect after the initial
      connection drops. ``0`` means retry forever.
    * ``base_delay``     — initial backoff delay, seconds.
    * ``max_delay``      — cap on the backoff delay, seconds.
    * ``backoff_factor`` — multiplier applied per retry attempt.
    * ``jitter``         — if true, add random full jitter to each delay.
    * ``stable_after``   — minimum session duration (seconds) that counts as a
      healthy connection. When a session runs at least this long, the retry
      budget is reset to zero, so a tunnel that stays up for hours does not
      permanently give up because of a few earlier flaky connections.

    Backoff for attempt ``n``::

        min(base_delay * backoff_factor ** n, max_delay)

    with full jitter (a uniformly random value between 0 and that delay) applied
    when ``jitter`` is enabled.
    """

    def __init__(self, config: RetryConfig) -> None:
        self.config = config
        self.max_retries = config.max_retries
        self.base_delay = config.base_delay
        self.max_delay = config.max_delay
        self.backoff_factor = config.backoff_factor
        self.jitter_enabled = config.jitter
        self.stable_after = config.stable_after
        self._stop = threading.Event()
        # Last exception raised by TunnelManager.connect(), if any. Useful for
        # diagnostics after the run completes.
        self.last_error: Optional[BaseException] = None

    # -- Public API -----------------------------------------------------------

    def stop(self) -> None:
        """Request a graceful stop.

        The running generator exits after the *current* connection ends (or,
        if it is currently in a backoff sleep, after at most
        ``_SLEEP_GRANULARITY`` seconds). Safe to call from any thread — for
        example from a ``SIGINT``/``SIGTERM`` handler.
        """
        self._stop.set()

    @property
    def stopped(self) -> bool:
        """True if :meth:`stop` has been requested for the current run."""
        return self._stop.is_set()

    def run(self, manager: TunnelManager) -> Iterator[RetryEvent]:
        """Keep the tunnel connected, yielding :class:`RetryEvent` objects.

        The connection lifecycle against ``manager.connect()``:

        1. yield ``CONNECTING`` and call ``manager.connect()``. It is expected
           to block while the tunnel is up and return once it terminates
           (exit code), or raise on failure to launch.
        2. A clean return means a session ran to completion — yield
           ``CONNECTED`` followed by ``DISCONNECTED(exit_code)``. A raise
           means the tunnel never came up — yield ``DISCONNECTED(None, error)``.
           After a clean return, if ``manager.last_session_duration`` is at
           least ``stable_after``, the session is considered healthy and the
           retry budget (``retries_used``) is reset to zero.
        3. Unless :meth:`stop` was requested, compute an exponential-backoff
           delay and yield ``RETRYING(delay, attempt)``, then sleep (actually
           wait -- interruptibly).
        4. Repeat until the retry budget is exhausted (``max_retries > 0`` and
           that many reconnects have been attempted), at which point
           ``MAX_RETRIES_REACHED`` is yielded and the generator returns.
        """
        self._stop.clear()
        self.last_error = None
        retries_used = 0

        while True:
            if self._stop.is_set():
                return

            yield RetryEvent.connecting()
            exit_code: Optional[int]
            error: Optional[str]
            try:
                exit_code = manager.connect()
            except Exception as exc:  # noqa: BLE001 - must survive any failure
                self.last_error = exc
                exit_code = None
                error = f"{type(exc).__name__}: {exc}"
                yield RetryEvent.disconnected(exit_code, error=error)
            else:
                # connect() returned: the tunnel stayed up for a while and then
                # terminated, or exited immediately. Either way a session ran.
                yield RetryEvent.connected()
                yield RetryEvent.disconnected(exit_code)

                # A session that stayed up for at least ``stable_after``
                # seconds is a sign of a healthy tunnel. Reset the retry budget
                # so a long-running tunnel is not permanently abandoned just
                # because it had a few flaky connections earlier in the day.
                # ``getattr`` keeps the runner compatible with manager objects
                # (e.g. test fakes) that predate last_session_duration.
                session_duration = getattr(manager, "last_session_duration", None)
                if session_duration is not None and session_duration >= self.stable_after:
                    retries_used = 0

            if self._stop.is_set():
                return

            if self.max_retries > 0 and retries_used >= self.max_retries:
                yield RetryEvent.max_retries_reached()
                return

            retries_used += 1
            delay = self._backoff_delay(retries_used)
            yield RetryEvent.retrying(delay, retries_used)
            self._sleep_interruptibly(delay)

    # -- Internals -------------------------------------------------------------

    def _backoff_delay(self, attempt: int) -> float:
        """Compute the backoff delay for the given (1-based) attempt number."""
        delay = min(
            self.base_delay * (self.backoff_factor**attempt),
            self.max_delay,
        )
        if self.jitter_enabled:
            # Full jitter: uniform random value in [0, delay). Spreads retries
            # to avoid a thundering herd while never exceeding the cap.
            delay = random.uniform(0.0, delay)
        return delay

    def _sleep_interruptibly(self, seconds: float) -> None:
        """Sleep for ``seconds`` while remaining responsive to :meth:`stop`."""
        if seconds <= 0:
            return
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(remaining, _SLEEP_GRANULARITY))