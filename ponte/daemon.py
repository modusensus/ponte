"""Daemon / process-lifecycle management for the ponte tunnel manager.

This module orchestrates the three layers below it into a single long-running
process:

* :mod:`ponte.core`   — establish and tear down a single SSH session
* :mod:`ponte.retry`  — exponential-backoff reconnect state machine
* :mod:`ponte.health` — periodic liveness and remote-port checks

``TunnelDaemon.run()`` runs the retry loop in the *foreground* (blocking) so a
Scheduled Task, a service wrapper, or the CLI's background mode can all drive
the exact same loop. On Windows, background mode is implemented by re-spawning
this process detached (``CREATE_DETACHED_PROCESS``) rather than by Unix style
double-forking.

Graceful stop on Windows works via a *stop marker* file: the CLI writes a
marker into the package directory and a daemon-side watcher thread converts it
into a clean ``manager.stop()`` + ``runner.stop()`` shutdown. If the daemon
does not exit within a timeout the CLI escalates to ``taskkill /T /F``.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

from ponte.config import TunnelConfig, get_config
from ponte.core import TunnelManager
from ponte.health import HealthChecker, HealthStatus
from ponte.retry import RetryEvent, RetryRunner

__all__ = ["DaemonStatus", "TunnelDaemon"]

logger = logging.getLogger(__name__)

#: Seconds between stop-marker polls on Windows (see ``_watch_stop_marker``).
_STOP_POLL_INTERVAL = 0.5

#: ``GetExitCodeProcess``'s STILL_ACTIVE pseudo exit code.
_STILL_ACTIVE = 259


def _derive_status_file(pid_file: str) -> str:
    """Derive the JSON status path from a ``.pid`` file path."""
    base, _ext = os.path.splitext(pid_file)
    return base + ".status.json"


def _derive_stop_marker(pid_file: str) -> str:
    """Derive the stop-marker path from a ``.pid`` file path."""
    base, _ext = os.path.splitext(pid_file)
    return base + ".stop"


def _encode_ps(script: str) -> str:
    """Base64 UTF-16LE encode a PowerShell snippet for ``-EncodedCommand``.

    Using ``-EncodedCommand`` completely sidesteps Windows quoting and encoding
    mangling — the same class of problem that broke the earlier PowerShell
    attempts.
    """
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _decode_console(data: bytes) -> str:
    """Decode bytes from a Windows console process, tolerating GBK/ANSI output.

    PowerShell (and other console tools) on Chinese Windows emit the active
    code page (GBK) rather than UTF-8. Try UTF-8 first, then fall back to
    ``gbk`` with lossy replacement so a mis-decode never raises.
    """
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("gbk", errors="replace")


@dataclasses.dataclass
class DaemonStatus:
    """A snapshot of daemon state for the ``status``/``stop`` commands."""

    running: bool
    pid: Optional[int] = None
    started_at: Optional[float] = None
    uptime_seconds: float = 0.0
    healthy: Optional[bool] = None
    remote_ports: dict[int, bool] = dataclasses.field(default_factory=dict)
    health_error: Optional[str] = None
    message: str = ""

    @property
    def uptime(self) -> str:
        """Human readable uptime, e.g. ``1h 2m 3s``."""
        seconds = int(self.uptime_seconds)
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        return f"{hours}h {minutes}m {secs}s"


class TunnelDaemon:
    """Run and manage the persistent SSH reverse-tunnel process.

    The daemon is intentionally *stateless on disk*: everything it needs lives
    in ``config.toml``, and the only mutable artifacts are the PID file, the
    JSON status file (updated by the health loop) and the stop marker.
    """

    def __init__(self, config: Optional[TunnelConfig] = None) -> None:
        self.config = config if config is not None else get_config()
        self.pid_file = self.config.daemon.pid_file
        self.log_file = self.config.daemon.log_file
        self.status_file = _derive_status_file(self.pid_file)
        self.stop_marker = _derive_stop_marker(self.pid_file)
        self._shutdown = threading.Event()
        self._last_health: Optional[HealthStatus] = None

    # -- Paths -----------------------------------------------------------------

    @property
    def _package_dir(self) -> str:
        """Absolute path of this package's directory (``...\\ponte``)."""
        return os.path.dirname(os.path.abspath(__file__))

    @property
    def _root_dir(self) -> str:
        """Parent of the package dir — the ``C:\\ssh-tunnel`` project root."""
        return os.path.dirname(self._package_dir)

    # -- PID helpers -----------------------------------------------------------

    def write_pid(self) -> None:
        os.makedirs(os.path.dirname(self.pid_file), exist_ok=True)
        with open(self.pid_file, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))

    def read_pid(self) -> Optional[int]:
        try:
            with open(self.pid_file, "r", encoding="utf-8") as handle:
                return int(handle.read().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Return ``True`` if *pid* names a live process on this OS."""
        if sys.platform == "win32":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(code)
                )
                return bool(ok and code.value == _STILL_ACTIVE)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        # POSIX: signal 0 just probes for existence.
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    # -- Status JSON -----------------------------------------------------------

    def _read_status_json(self) -> dict:
        try:
            with open(self.status_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_status_json(self, payload: dict) -> None:
        try:
            with open(self.status_file, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.warning("could not write status file: %s", exc)

    # -- Health callback -------------------------------------------------------

    def _on_health(self, status: HealthStatus) -> None:
        """Store the latest health snapshot and mirror it to the JSON file."""
        self._last_health = status
        data = self._read_status_json()
        data.update(
            {
                "started_at": data.get("started_at", time.time()),
                "checked_at": time.time(),
                "process_alive": status.process_alive,
                "healthy": status.all_healthy,
                "remote_ports": {str(p): ok for p, ok in status.remote_ports.items()},
                "health_error": status.error,
            }
        )
        self._write_status_json(data)
        health = logger.warning if not status.all_healthy else logger.debug
        health("health: %s", status)

    # -- Foreground loop -------------------------------------------------------

    def run(self) -> int:
        """Block, keeping the tunnel up, until a stop is requested.

        Runs the retry generator and the health-monitor loop together. Returns
        the daemon exit code (``0`` for a clean, requested stop).
        """
        self._setup_logging()
        log = logging.getLogger("ponte.daemon")

        import ponte
        log.info(
            "ponte v%s daemon starting (pid %d)", ponte.__version__, os.getpid()
        )
        self.write_pid()
        self._safe_remove(self.stop_marker)

        manager = TunnelManager(self.config)
        runner = RetryRunner(self.config.retry)
        health = HealthChecker(manager, self.config.health)

        # Prime the status file with a start time before the first health tick.
        self._write_status_json({"started_at": time.time()})

        def request_stop(reason: str) -> None:
            """Request shutdown from any thread. Idempotent, never raises."""
            if self._shutdown.is_set():
                return
            log.info("shutdown requested: %s", reason)
            self._shutdown.set()
            runner.stop()   # abort any backoff sleep
            manager.stop()  # abort a blocked connect(), if any

        # SIGINT (Ctrl+C) and, where catchable, SIGTERM.
        try:
            signal.signal(signal.SIGINT, lambda *_a: request_stop("SIGINT"))
            signal.signal(signal.SIGTERM, lambda *_a: request_stop("SIGTERM"))
        except (ValueError, OSError):
            pass  # SIGTERM may be uncatchable on some Windows builds

        # Stop-marker watcher gives cross-process graceful stop on Windows.
        threading.Thread(
            target=self._watch_stop_marker,
            args=(request_stop,),
            daemon=True,
            name="ponte-stop-watch",
        ).start()

        # Health checks run once immediately, then every check_interval.
        health_stop = health.run_loop(
            interval=self.config.health.check_interval,
            callback=lambda st: self._on_health(st),
        )

        log.info(
            "starting SSH retry loop (max_retries=%s)",
            self.config.retry.max_retries,
        )
        try:
            for event in runner.run(manager):
                if event.type == RetryEvent.CONNECTING:
                    log.debug("connecting to %s ...", self.config.ssh.destination)
                elif event.type == RetryEvent.CONNECTED:
                    log.info("tunnel established")
                elif event.type == RetryEvent.DISCONNECTED:
                    log.warning(
                        "tunnel down (exit=%s error=%s)", event.exit_code, event.error
                    )
                elif event.type == RetryEvent.RETRYING:
                    log.warning(
                        "reconnecting attempt %d in %.1fs", event.attempt, event.delay
                    )
                elif event.type == RetryEvent.MAX_RETRIES_REACHED:
                    log.error("retry budget exhausted; giving up")
                if self._shutdown.is_set():
                    manager.stop()
        finally:
            health_stop.set()
            runner.stop()
            manager.stop()
            self._shutdown.set()
            self._cleanup()
        log.info("daemon exited cleanly")
        return 0

    def _watch_stop_marker(self, request_stop: Callable[[str], None]) -> None:
        """Watch for a stop marker file and request a graceful shutdown."""
        while not self._shutdown.is_set():
            if os.path.exists(self.stop_marker):
                request_stop("stop marker file present")
                return
            self._shutdown.wait(_STOP_POLL_INTERVAL)

    # -- Start / background ----------------------------------------------------

    def start(self, foreground: bool = False) -> int:
        """Start the daemon, optionally in the background.

        In background mode the daemon re-spawns itself detached and the parent
        returns the child PID once it writes its PID file.
        """
        if not foreground:
            return self._spawn_background()
        return self.run()

    def _spawn_background(self) -> int:
        """Re-launch this CLI as a detached background process."""
        cmd = [sys.executable, "-m", "ponte.main", "start", "--foreground"]
        if sys.platform == "win32":
            flags = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            )
            subprocess.Popen(
                cmd,
                cwd=self._root_dir,
                creationflags=flags,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            # POSIX: start a new session so the child detaches from the
            # controlling terminal and survives the parent shell exiting.
            subprocess.Popen(
                cmd,
                cwd=self._root_dir,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        # Wait for the child to write its PID file (up to 10 s).
        for _ in range(100):
            pid = self.read_pid()
            if pid is not None:
                return pid
            time.sleep(0.1)
        raise RuntimeError("daemon did not write its PID file within 10 s")

    # -- Stop ------------------------------------------------------------------

    def stop(self, timeout: float = 20.0) -> DaemonStatus:
        """Gracefully stop a running daemon, escalating to a force kill.

        Steps: stop the scheduled task (so it does not respawn us), drop the
        stop marker, wait for the PID to vanish, then ``taskkill /T /F`` if the
        daemon ignores the request.
        """
        status = self.status()
        if not status.running or status.pid is None:
            return status

        # 1. Stop the auto-start hook first so it cannot restart the daemon.
        self._stop_autostart()

        # 2. Drop the stop marker for a graceful shutdown.
        try:
            with open(self.stop_marker, "w", encoding="utf-8") as handle:
                handle.write(str(time.time()))
        except OSError as exc:
            logger.warning("could not write stop marker: %s", exc)

        # 3. Wait for the daemon to exit.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self._pid_alive(status.pid):
            time.sleep(0.2)

        # 4. Escalate if still alive.
        if self._pid_alive(status.pid):
            logger.warning(
                "daemon did not stop gracefully; force killing pid %d", status.pid
            )
            self._force_kill(status.pid)

        self._safe_remove(self.stop_marker)
        self._safe_remove(self.pid_file)
        return self.status()

    def _force_kill(self, pid: int) -> None:
        """Kill *pid* and its whole tree, regardless of platform.

        On Windows the whole process tree is killed. On POSIX we first send a
        graceful ``SIGTERM`` and only escalate to ``SIGKILL`` after the
        configured grace period, so the daemon gets a chance to clean up.
        """
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
            )
            return
        # POSIX: graceful SIGTERM, then escalate to SIGKILL.
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + self.config.service.kill_timeout
        while time.monotonic() < deadline and self._pid_alive(pid):
            time.sleep(0.2)
        if self._pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    # -- Status ----------------------------------------------------------------

    def status(self) -> DaemonStatus:
        pid = self.read_pid()
        if pid is None or not self._pid_alive(pid):
            return DaemonStatus(running=False, message="daemon is not running")
        info = self._read_status_json()
        started = info.get("started_at")
        uptime = (time.time() - float(started)) if started else 0.0
        return DaemonStatus(
            running=True,
            pid=pid,
            started_at=float(started) if started else None,
            uptime_seconds=max(0.0, uptime),
            healthy=info.get("healthy"),
            remote_ports={
                int(p): bool(ok) for p, ok in dict(info.get("remote_ports", {})).items()
            },
            health_error=info.get("health_error"),
        )

    # -- Diagnostics -----------------------------------------------------------

    def test_connection(self, timeout: int = 10) -> bool:
        """Run a one-off ``ssh ... echo OK`` against the configured endpoint."""
        return TunnelManager(self.config).test_connection(timeout=timeout)

    def check_remote_ports(self, timeout: int = 10) -> dict[int, bool]:
        """Probe which configured remote ports are currently listening."""
        return TunnelManager(self.config).check_remote_ports(timeout=timeout)

    # -- Cross-platform service install ----------------------------------------

    def install_service(self) -> str:
        """Register OS-level auto-start + crash-restart for the daemon.

        Dispatches to the platform backend: Windows Scheduled Task, a systemd
        *user* unit on Linux, or a launchd LaunchAgent on macOS.
        """
        if sys.platform == "win32":
            return self.install_scheduled_task()
        if sys.platform == "linux":
            return self._install_systemd()
        if sys.platform == "darwin":
            return self._install_launchd()
        raise RuntimeError(f"service install not supported on {sys.platform}")

    def uninstall_service(self) -> str:
        """Remove the auto-start service registered by :meth:`install_service`."""
        if sys.platform == "win32":
            return self.uninstall_scheduled_task()
        if sys.platform == "linux":
            return self._uninstall_systemd()
        if sys.platform == "darwin":
            return self._uninstall_launchd()
        raise RuntimeError(f"service uninstall not supported on {sys.platform}")

    def _stop_autostart(self) -> None:
        """Best-effort: stop the auto-start hook so it cannot respawn us."""
        try:
            if sys.platform == "win32":
                self._run_powershell_stop_task()
            elif sys.platform == "linux":
                name = self.config.service.name
                subprocess.run(
                    ["systemctl", "--user", "stop", f"{name}.service"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            elif sys.platform == "darwin":
                plist = self._launchd_plist()
                subprocess.run(
                    ["launchctl", "unload", plist],
                    capture_output=True,
                    text=True,
                    check=False,
                )
        except Exception as exc:  # noqa: BLE001 - best effort
            logger.debug("could not stop service autostart: %s", exc)

    def _launchd_plist(self) -> str:
        """Return the per-user LaunchAgent plist path (reverse-DNS label)."""
        label = f"com.modusensus.{self.config.service.name}"
        return os.path.join(
            os.path.expanduser("~/Library/LaunchAgents"), f"{label}.plist"
        )

    def _install_systemd(self) -> str:
        """Write a systemd *user* unit and enable+start it."""
        name = self.config.service.name
        unit_dir = os.path.expanduser("~/.config/systemd/user")
        os.makedirs(unit_dir, exist_ok=True)
        unit_path = os.path.join(unit_dir, f"{name}.service")
        unit = (
            "[Unit]\n"
            f"Description=ponte SSH reverse tunnel daemon ({name})\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={sys.executable} -m ponte.main start --foreground\n"
            f"WorkingDirectory={self._root_dir}\n"
            "Restart=always\n"
            "RestartSec=15\n"
            "Environment=PYTHONUNBUFFERED=1\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        with open(unit_path, "w", encoding="utf-8") as handle:
            handle.write(unit)
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", f"{name}.service"],
            check=True, capture_output=True, text=True,
        )
        return "installed"

    def _uninstall_systemd(self) -> str:
        """Disable+stop the systemd user unit and remove the file."""
        name = self.config.service.name
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", f"{name}.service"],
            capture_output=True, text=True, check=False,
        )
        unit_path = os.path.join(
            os.path.expanduser("~/.config/systemd/user"), f"{name}.service"
        )
        if os.path.exists(unit_path):
            os.remove(unit_path)
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True, text=True, check=False,
        )
        return "uninstalled"

    def _install_launchd(self) -> str:
        """Write a LaunchAgent plist and load it (auto-start + KeepAlive)."""
        plist_path = self._launchd_plist()
        os.makedirs(os.path.dirname(plist_path), exist_ok=True)
        label = f"com.modusensus.{self.config.service.name}"
        plist = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "<dict>\n"
            f"    <key>Label</key><string>{label}</string>\n"
            "    <key>ProgramArguments</key>\n"
            "    <array>\n"
            f"        <string>{sys.executable}</string>\n"
            "        <string>-m</string>\n"
            "        <string>ponte.main</string>\n"
            "        <string>start</string>\n"
            "        <string>--foreground</string>\n"
            "    </array>\n"
            f"    <key>WorkingDirectory</key><string>{self._root_dir}</string>\n"
            "    <key>RunAtLoad</key><true/>\n"
            "    <key>KeepAlive</key><true/>\n"
            "    <key>ProcessType</key><string>Background</string>\n"
            f"    <key>StandardOutPath</key><string>{self.log_file}</string>\n"
            f"    <key>StandardErrorPath</key><string>{self.log_file}</string>\n"
            "</dict>\n"
            "</plist>\n"
        )
        with open(plist_path, "w", encoding="utf-8") as handle:
            handle.write(plist)
        subprocess.run(
            ["launchctl", "load", "-w", plist_path],
            check=True, capture_output=True, text=True,
        )
        return "installed"

    def _uninstall_launchd(self) -> str:
        """Unload and remove the LaunchAgent plist."""
        plist_path = self._launchd_plist()
        subprocess.run(
            ["launchctl", "unload", "-w", plist_path],
            capture_output=True, text=True, check=False,
        )
        if os.path.exists(plist_path):
            os.remove(plist_path)
        return "uninstalled"

    # -- Windows Scheduled Task ------------------------------------------------

    def install_scheduled_task(self) -> str:
        """Register a Scheduled Task with OS-level auto-restart.

        The task runs ``pythonw -m ponte.main start --foreground`` in the
        project root.  The identity/timing depends on ``[windows] run_as``:

        * ``system`` (default) — boot-time task running as SYSTEM
          (``ServiceAccount``), so the tunnel is up before login; install
          requires elevation and the task cannot reach per-user SSH keys.
        * ``user`` — logon-time task running as the installing user
          (``Interactive`` + ``Limited``), can read the user's keys and needs
          no elevation, but runs only after an interactive logon.

        ``RestartCount`` (999, every minute) covers task-level restarts on top
        of the in-process retry loop.
        """
        if sys.platform != "win32":
            raise RuntimeError("Scheduled Tasks are only supported on Windows")
        exe = self._pythonw_path()
        task_name = self.config.windows.task_name
        run_as = self.config.windows.run_as

        if run_as == "user":
            trigger = "$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name\n$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser"
            principal = "$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited"
        else:
            trigger = "$trigger = New-ScheduledTaskTrigger -AtStartup"
            principal = "$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest"

        script = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute '{exe}' -Argument '-m ponte.main start --foreground' -WorkingDirectory '{self._root_dir}'
{trigger}
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
{principal}
Register-ScheduledTask -TaskName '{task_name}' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Output 'installed'
"""
        return self._run_powershell(script).strip()

    def uninstall_scheduled_task(self) -> str:
        """Remove the Scheduled Task registered by :meth:`install_scheduled_task`."""
        task_name = self.config.windows.task_name
        script = (
            f"Unregister-ScheduledTask -TaskName '{task_name}' -Confirm:$false; "
            "Write-Output 'uninstalled'"
        )
        try:
            return self._run_powershell(script).strip()
        except RuntimeError:
            return "uninstalled"

    def _run_powershell_stop_task(self) -> None:
        task_name = self.config.windows.task_name
        script = f"Stop-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue"
        self._run_powershell(script)

    def _pythonw_path(self) -> str:
        """Return a ``pythonw.exe`` path next to ``sys.executable``, if any."""
        base, name = os.path.split(sys.executable)
        if name.lower().startswith("pythonw"):
            return sys.executable
        candidate = os.path.join(base, "pythonw.exe")
        return candidate if os.path.exists(candidate) else sys.executable

    def _run_powershell(self, script: str, timeout: float = 90.0) -> str:
        """Run a PowerShell snippet via ``-EncodedCommand`` and return stdout."""
        cmd = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-EncodedCommand", _encode_ps(script),
        ]
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(
            cmd, capture_output=True, timeout=timeout, creationflags=flags
        )
        # PowerShell writes ANSI/GBK on Chinese systems; decode defensively so a
        # garbled line never masks the real exit code / stderr below.
        stdout = _decode_console(result.stdout)
        stderr = _decode_console(result.stderr)
        if result.returncode != 0:
            raise RuntimeError(
                f"PowerShell exited {result.returncode}: "
                f"{(stderr or stdout).strip()}"
            )
        return stdout

    # -- Logging ---------------------------------------------------------------

    def _setup_logging(self) -> None:
        """Configure rotating file logging. Idempotent."""
        root = logging.getLogger("ponte")
        if getattr(root, "_ponte_setup_ok", False):
            return
        root.setLevel(logging.INFO)
        max_bytes = self.config.daemon.log_max_bytes
        backups = self.config.daemon.log_backup_count
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            self.log_file,
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(handler)
        root._ponte_setup_ok = True  # type: ignore[attr-defined]

    # -- Cleanup helpers -------------------------------------------------------

    def _cleanup(self) -> None:
        try:
            self._safe_remove(self.pid_file)
            self._safe_remove(self.stop_marker)
        finally:
            self._shutdown.set()

    @staticmethod
    def _safe_remove(path: str) -> None:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
