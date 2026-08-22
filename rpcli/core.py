"""Pure SSH tunnel logic — establish connection, forward traffic.

No retry, no daemon, no CLI. Just build the right SSH arguments and spawn
a subprocess. The caller is responsible for lifecycle management.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Optional

from rpcli.config import TunnelConfig, get_config

__all__ = ["TunnelManager"]

logger = logging.getLogger(__name__)


def _find_ssh() -> str:
    """Return the path to the ``ssh`` executable.

    On Windows the config may specify an explicit path; otherwise ``ssh`` is
    resolved from ``PATH``.
    """
    cfg = get_config()
    if sys.platform == "win32" and cfg.windows.ssh_exe:
        exe = cfg.windows.ssh_exe
        if os.path.isfile(exe):
            return exe
        logger.warning("Configured ssh_exe not found: %s, falling back to PATH", exe)
    return "ssh"


class TunnelManager:
    """Manage a single SSH reverse-tunnel session.

    Parameters:
        config: A validated :class:`TunnelConfig` (from ``rpcli.config``).
    """

    def __init__(self, config: TunnelConfig) -> None:
        self.config = config
        self.ssh_exe = _find_ssh()
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
             "-R", "23334:localhost:2222", "root@47.113.179.249"]
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
            )
            return result.returncode == 0 and "OK" in result.stdout
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("Connection test failed: %s", exc)
            return False

    def check_remote_ports(self, timeout: int = 10) -> dict[int, bool]:
        """Connect to the server and check which tunnel ports are listening.

        Returns a ``{port: is_listening}`` mapping.
        """
        cfg = self.config.ssh
        ports = {t.remote_port for t in self.config.tunnels}
        if not ports:
            return {}

        grep_pattern = "|".join(str(p) for p in ports)
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
            f"ss -tlnp | grep -E '{grep_pattern}'",
        ])
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout + 5,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("Remote port check failed: %s", exc)
            return {p: False for p in ports}

        output = result.stdout
        status: dict[int, bool] = {}
        for port in ports:
            status[port] = f":{port} " in output
        return status