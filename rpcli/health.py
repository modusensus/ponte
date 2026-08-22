"""Health monitoring for the rpcli SSH reverse tunnel manager.

Independently of the retry layer, this module periodically checks whether the
underlying SSH process is still alive and whether the remote forwarding ports
are listening. A monitor callback can then decide to restart a dead tunnel,
log a warning, or leave the retry loop to handle it.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from typing import Callable, Dict, Optional

from rpcli.config import HealthConfig
from rpcli.core import TunnelManager

__all__ = ["HealthChecker", "HealthStatus"]

#: Type of a user-supplied run-loop callback: ``Callable[[HealthStatus], None]``.
HealthCallback = Callable[["HealthStatus"], None]


@dataclasses.dataclass(frozen=True)
class HealthStatus:
    """A snapshot of tunnel health taken at ``timestamp``.

    Attributes:
        process_alive: True if the SSH process is still running.
        remote_ports: Mapping of remote port -> listening (True/False).
        all_healthy: Overall health — the process is alive, every checked
            remote port is listening, and the check did not error.
        timestamp: Unix time (``time.time()``) when the check was performed.
        error: Human-readable error message if the check partially failed,
            else ``None``.
    """

    process_alive: bool
    remote_ports: Dict[int, bool]
    all_healthy: bool
    timestamp: float
    error: Optional[str] = None

    def __str__(self) -> str:  # human-friendly one-liner for logs
        ports = {
            port: ("ok" if ok else "down") for port, ok in self.remote_ports.items()
        }
        return (
            f"process={'alive' if self.process_alive else 'dead'}, "
            f"remote_ports={ports}, "
            f"healthy={self.all_healthy}"
            + (f", error={self.error!r}" if self.error else "")
        )


class HealthChecker:
    """Periodic health checks for a :class:`TunnelManager`.

    ``config`` is a :class:`HealthConfig` carrying the ``[health]`` section of
    ``config.toml``. Used fields:

    * ``check_interval``       — default seconds between checks (``run_loop``
      accepts its own ``interval`` override).
    * ``remote_check_enabled`` — whether remote ports are probed at all.
    * ``remote_check_timeout`` — per-port probe timeout, seconds.
    """

    def __init__(self, manager: TunnelManager, config: HealthConfig) -> None:
        self.manager = manager
        self.config = config
        self.check_interval = config.check_interval
        self.remote_check_enabled = config.remote_check_enabled
        self.remote_check_timeout = config.remote_check_timeout
        # Last exception raised by a user callback in run_loop(), if any. The
        # loop swallows callback errors so one bad callback cannot kill the
        # monitor, but it records the most recent one here for diagnostics.
        self.last_callback_error: Optional[BaseException] = None

    # -- Checks ---------------------------------------------------------------

    def check(self) -> HealthStatus:
        """Perform one health check and return a :class:`HealthStatus` snapshot.

        Never raises: failures are reported through the ``error`` field and
        the ``all_healthy`` flag rather than propagating.
        """
        snapshot_time = time.time()
        error_messages: list[str] = []

        # 1. Is the SSH process still running?
        process_alive = False
        try:
            process_alive = self._is_process_alive()
        except Exception as exc:  # noqa: BLE001
            error_messages.append(f"process check failed: {type(exc).__name__}: {exc}")

        # 2. Are the remote forwarding ports listening?
        remote_ports: Dict[int, bool] = {}
        if self.remote_check_enabled:
            try:
                remote_ports = self.check_remote_ports()
            except Exception as exc:  # noqa: BLE001
                error_messages.append(
                    f"remote port check failed: {type(exc).__name__}: {exc}"
                )
        # When the remote check is disabled we leave remote_ports empty, so
        # ``all(remote_ports.values())`` is vacuously True and process_alive
        # alone determines health.

        # 3. Aggregate. An error on any sub-check makes the result unhealthy —
        # a failed probe is indistinguishable from a down tunnel, so be
        # conservative.
        error = "; ".join(error_messages) if error_messages else None
        all_healthy = (
            error is None
            and process_alive
            and all(remote_ports.values())
        )
        return HealthStatus(
            process_alive=process_alive,
            remote_ports=remote_ports,
            all_healthy=all_healthy,
            timestamp=snapshot_time,
            error=error,
        )

    def check_remote_ports(self) -> Dict[int, bool]:
        """Probe remote ports via ``manager.check_remote_ports()``.

        Returns a ``{port: bool}`` mapping of which configured remote ports are
        listening. Tolerates both a ``dict[int, bool]`` and a simple iterable of
        open ports as return values.
        """
        method = getattr(self.manager, "check_remote_ports", None)
        if not callable(method):
            raise RuntimeError(
                "TunnelManager.check_remote_ports() is not available"
            )
        try:
            result = method(timeout=self.remote_check_timeout)
        except TypeError:
            # The tunnel manager may not accept a timeout argument.
            result = method()

        if isinstance(result, dict):
            return {int(port): bool(ok) for port, ok in result.items()}
        if isinstance(result, (list, tuple, set, frozenset)):
            # An iterable of ports that are open — treat each as healthy.
            return {int(port): True for port in result}
        # Unknown shape: fail loudly rather than silently report health.
        raise TypeError(
            f"check_remote_ports() returned unsupported type {type(result).__name__}"
        )

    # -- Background loop ------------------------------------------------------

    def run_loop(
        self, interval: Optional[float] = None, callback: Optional[HealthCallback] = None
    ) -> threading.Event:
        """Run checks every ``interval`` seconds in a background daemon thread.

        Args:
            interval: Seconds between checks. Defaults to ``config.check_interval``.
            callback: Called with each :class:`HealthStatus`. A raising callback
                is caught and recorded in ``last_callback_error`` so it cannot
                kill the monitor thread.

        Returns:
            A :class:`threading.Event` that, when set, stops the loop. The loop
            performs one check immediately, then runs until the event is set.
        """
        if interval is None:
            interval = self.check_interval
        if interval < 0:
            raise ValueError(f"interval must be >= 0, got {interval}")
        if callback is None:
            callback = lambda status: None  # noqa: E731 - intentional no-op

        stop_event = threading.Event()

        def _loop() -> None:
            try:
                callback(self.check())
            except Exception as exc:  # noqa: BLE001
                self.last_callback_error = exc

            while not stop_event.is_set():
                # wait() returns True if the event was set -> stop.
                if stop_event.wait(interval):
                    return
                try:
                    status = self.check()
                except Exception as exc:  # noqa: BLE001 - check() should not
                    self.last_callback_error = exc  # raise, but be defensive
                    continue
                try:
                    callback(status)
                except Exception as exc:  # noqa: BLE001
                    self.last_callback_error = exc

        thread = threading.Thread(
            target=_loop, name="rpcli-health-check", daemon=True
        )
        thread.start()
        return stop_event

    # -- Internals -------------------------------------------------------------

    def _is_process_alive(self) -> bool:
        """Determines whether the underlying SSH process is still running.

        The tunnel manager stores its child process either as ``manager.process``
        or ``manager.proc`` (a ``subprocess.Popen``-like object), or exposes an
        ``is_running()`` method. ``poll()`` returning ``None`` means the process
        is still alive. If no process handle is available, the process is
        assumed alive so that healthy-teardown races do not produce false
        negatives.
        """
        is_running = getattr(self.manager, "is_running", None)
        if callable(is_running):
            try:
                return bool(is_running())
            except Exception:  # noqa: BLE001
                pass  # fall through to the process-handle check

        proc = getattr(self.manager, "process", None)
        if proc is None:
            proc = getattr(self.manager, "proc", None)
        if proc is None:
            return True  # no handle -> assume alive (conservative for liveness)

        poll = getattr(proc, "poll", None)
        if callable(poll):
            return poll() is None  # None return value == still running
        return True