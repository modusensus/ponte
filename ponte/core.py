"""Pure SSH tunnel logic — establish connection, forward traffic.

No retry, no daemon, no CLI. Just build the right SSH arguments and spawn
a subprocess. The caller is responsible for lifecycle management.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from typing import Optional

from ponte.config import TunnelConfig, get_config

__all__ = ["TunnelManager"]

logger = logging.getLogger(__name__)


def _creation_flags() -> int:
    """Return subprocess creation flags that suppress a console window.

    On Windows an SSH child spawned without ``CREATE_NO_WINDOW`` can pop a
    black console box (the same class of flicker this tool works hard to
    avoid). On POSIX the flag is meaningless, so return ``0``.

    ``create_no_window`` is resolved via ``getattr`` purely so that tests which
    monkeypatch ``sys.platform`` to ``"win32"`` on a POSIX host still work —
    the constant simply does not exist in ``subprocess`` there.
    """
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def _find_ssh(config: Optional[TunnelConfig] = None) -> str:
    """Return the path to the ``ssh`` executable.

    Resolution order:

    1. On Windows, the config may specify an explicit path
       (``[windows] ssh_exe``); honour it if it exists.
    2. ``ssh`` found on ``PATH`` (Linux/macOS almost always, Git-for-Windows
       often adds it too).
    3. Windows fallbacks to common Git installation paths.

    ``config`` is used to honour the Windows ``ssh_exe`` override. When not
    provided, the global config is loaded — callers that already hold a
    validated config (e.g. :class:`TunnelManager`) should pass it to avoid a
    second, possibly failing, config load.
    """
    cfg = config if config is not None else get_config()
    if sys.platform == "win32" and cfg.windows.ssh_exe:
        exe = cfg.windows.ssh_exe
        if os.path.isfile(exe):
            return exe
        logger.warning("Configured ssh_exe not found: %s, falling back to PATH", exe)

    found = shutil.which("ssh")
    if found:
        return found

    if sys.platform == "win32":
        for candidate in (
            r"D:\Git\usr\bin\ssh.exe",
            r"C:\Program Files\Git\usr\bin\ssh.exe",
            r"C:\Program Files (x86)\Git\usr\bin\ssh.exe",
        ):
            if os.path.isfile(candidate):
                return candidate
        logger.warning("ssh not found on PATH or Git fallback; trying 'ssh' verbatim")
    return "ssh"


class TunnelManager:
    """Manage a single SSH reverse-tunnel session.

    Parameters:
        config: A validated :class:`TunnelConfig` (from ``ponte.config``).
    """

    def __init__(self, config: TunnelConfig) -> None:
        self.config = config
        self.ssh_exe = _find_ssh(self.config)
        self.process: Optional[subprocess.Popen] = None

    # -- Connection ---------------------------------------------------------

    def connect(self) -> int:
        """Spawn SSH and block until the session ends.

        Returns the exit code of the SSH process. Raises
        :class:`subprocess.SubprocessError` (or a subclass) if the process
        cannot be launched.

        Call :meth:`stop` from another thread to terminate the session
        gracefully.
        """
        args = self.build_args()
        logger.info("Launching: %s", " ".join(args))
        self.process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            creationflags=_creation_flags(),
        )
        try:
            _, stderr = self.process.communicate()
            returncode: Optional[int] = self.process.returncode
        finally:
            self.process = None
        if stderr:
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            if stderr_text:
                logger.debug("SSH stderr: %s", stderr_text)
        return returncode

    def stop(self) -> None:
        """Terminate the running SSH process (if any)."""
        if self.process is not None and self.process.poll() is None:
            logger.info("Terminating SSH process (PID %d)", self.process.pid)
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("SSH did not exit, killing")
                self.process.kill()
                self.process.wait()

    def is_running(self) -> bool:
        """Return ``True`` if the SSH process is still alive."""
        if self.process is None:
            return False
        return self.process.poll() is None

    # -- Argument building --------------------------------------------------

    def build_args(self) -> list[str]:
        """Construct the full SSH command line as a list of strings.

        Example::

            ["ssh", "-o", "ServerAliveInterval=30", "-N",
             "-R", "23334:localhost:2222", "user@server-ip"]
        """
        cfg = self.config.ssh
        args = [self.ssh_exe]

        # SSH options
        for key, value in cfg.options.as_pairs():
            args.extend(["-o", f"{key}={value}"])

        # Known hosts file
        if cfg.known_hosts_file:
            args.extend(["-o", f"UserKnownHostsFile={cfg.known_hosts_file}"])

        # Identity file
        args.extend(["-i", cfg.identity_file])

        # Port
        if cfg.port != 22:
            args.extend(["-p", str(cfg.port)])

        # No shell, just forwarding
        args.append("-N")

        # Reverse tunnels
        for tunnel in self.config.tunnels:
            args.extend([
                "-R",
                f"{tunnel.remote_port}:{tunnel.local_host}:{tunnel.local_port}",
            ])

        # Destination
        args.append(cfg.destination)
        return args

    # -- Health / diagnostics -----------------------------------------------

    def test_connection(self, timeout: int = 10) -> bool:
        """Run a quick ``ssh … echo OK`` to verify connectivity.

        Returns ``True`` if the server responds with "OK".
        """
        cfg = self.config.ssh
        args = [self.ssh_exe]
        for key, value in cfg.options.as_pairs():
            args.extend(["-o", f"{key}={value}"])
        if cfg.known_hosts_file:
            args.extend(["-o", f"UserKnownHostsFile={cfg.known_hosts_file}"])
        args.extend(["-i", cfg.identity_file])
        if cfg.port != 22:
            args.extend(["-p", str(cfg.port)])
        args.extend([
            "-o", f"ConnectTimeout={timeout}",
            cfg.destination,
            "echo OK",
        ])
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout + 5,
                creationflags=_creation_flags(),
            )
            return result.returncode == 0 and "OK" in result.stdout
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("Connection test failed: %s", exc)
            return False

    def check_remote_ports(self, timeout: int = 10) -> dict[int, bool]:
        """Connect to the server and check which tunnel ports are listening.

        The probe runs *on the server*. It prefers a pure-Python socket check
        (no external tools), falling back to ``ss``/``lsof``/``netstat`` for
        servers without python3. Returns a ``{port: is_listening}`` mapping.
        """
        cfg = self.config.ssh
        ports = {t.remote_port for t in self.config.tunnels}
        if not ports:
            return {}

        port_literal = ", ".join(str(p) for p in sorted(ports))
        # python3 socket probe first (portable across server OSes), else
        # fall back to the common listener tools with `ss`-style output.
        remote_cmd = (
            "if command -v python3 >/dev/null 2>&1; then\n"
            "python3 - <<'PY'\n"
            "import socket\n"
            f"ports=[{port_literal}]\n"
            "open_ports=[]\n"
            "for p in ports:\n"
            "    s=socket.socket(); s.settimeout(1)\n"
            "    try:\n"
            "        s.connect(('127.0.0.1',p)); open_ports.append(p)\n"
            "    except OSError:\n"
            "        pass\n"
            "    finally:\n"
            "        s.close()\n"
            "print(' '.join(str(p) for p in open_ports))\n"
            "PY\n"
            "else\n"
            "(ss -tlnp 2>/dev/null || lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null || netstat -tln 2>/dev/null)\n"
            "fi"
        )

        args = [self.ssh_exe]
        for key, value in cfg.options.as_pairs():
            args.extend(["-o", f"{key}={value}"])
        if cfg.known_hosts_file:
            args.extend(["-o", f"UserKnownHostsFile={cfg.known_hosts_file}"])
        args.extend(["-i", cfg.identity_file])
        if cfg.port != 22:
            args.extend(["-p", str(cfg.port)])
        args.extend([
            "-o", f"ConnectTimeout={timeout}",
            cfg.destination,
            remote_cmd,
        ])
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout + 5,
                creationflags=_creation_flags(),
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("Remote port check failed: %s", exc)
            return {p: False for p in ports}

        output = result.stdout or ""
        # python3 branch prints the open ports space-separated on one line.
        python_ports = {int(p) for p in output.split() if p.isdigit()}
        status: dict[int, bool] = {}
        for port in ports:
            # Either the python3 line named the port, or a tool listing
            # contains a ``:<port> `` token (e.g. ``*:23334 ``).
            in_tool_output = f":{port} " in output or f":{port}\n" in output
            status[port] = port in python_ports or in_tool_output
        return status