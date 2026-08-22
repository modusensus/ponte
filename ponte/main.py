"""Command-line interface for ponte — an SSH reverse tunnel manager.

Typical usage::

    ponte start          # launch the tunnel daemon in the background
    ponte status         # inspect daemon health and remote ports
    ponte logs -f        # follow the daemon log
    ponte install        # register a Windows scheduled task (auto-start)
"""

from __future__ import annotations

import os
import sys
import time
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from ponte.config import get_config

__all__ = ["app"]


def _configure_utf8_stdio() -> None:
    """Force UTF-8 on stdio so Chinese output renders in UTF-8 terminals.

    On Windows, Python defaults stdout encoding to the ANSI code page (e.g.
    GBK on Chinese systems), which garbles Chinese text in UTF-8 terminals
    such as Windows Terminal, mintty and VS Code.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


_configure_utf8_stdio()


app = typer.Typer(
    no_args_is_help=True,
    help="管理 SSH 反向隧道守护进程的命令行工具",
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)

#: Tokens inside a DaemonStatus.message that indicate a force kill was needed.
_FORCE_KILL_TOKENS = ("kill", "force", "强制", "强杀")

#: Poll interval (seconds) used by --follow when no new log data arrives.
_FOLLOW_POLL_INTERVAL = 0.25


def _daemon() -> "TunnelDaemon":
    """Return a daemon instance bound to the effective configuration.

    The ``ponte.daemon`` module is imported lazily so that config-only
    commands (``config``, ``--help``) keep working even when the daemon
    module is unavailable or out of date.
    """
    from ponte.daemon import TunnelDaemon

    return TunnelDaemon(config=get_config())


def _fail(message: str) -> None:
    """Print a red ``错误：`` message to stderr and exit with status 1."""
    err_console.print(f"[bold red]错误：{escape(message)}[/bold red]")
    raise typer.Exit(code=1)


def _force_kill_message(status: object) -> str:
    """Return ``status.message`` if it mentions a force kill, otherwise ''."""
    message = getattr(status, "message", None) or ""
    lowered = message.lower()
    if any(token in lowered for token in _FORCE_KILL_TOKENS):
        return message
    return ""


# ---------------------------------------------------------------------------
# start / stop / restart
# ---------------------------------------------------------------------------


@app.command()
def start(
    foreground: bool = typer.Option(
        False, "--foreground", "-f", help="前台运行（阻塞，Ctrl+C 停止）"
    ),
) -> None:
    """启动反向隧道守护进程。"""
    try:
        daemon = _daemon()
        status = daemon.status()
        if status.running:
            pid = status.pid if status.pid is not None else "?"
            console.print(f"[yellow]已在运行 (pid {pid})[/yellow]")
            raise typer.Exit(code=0)

        if foreground:
            try:
                code = daemon.run()
            except (KeyboardInterrupt, typer.Abort):
                console.print("\n[yellow]已停止[/yellow]")
                raise typer.Exit(code=0)
            raise typer.Exit(code=code or 0)

        pid = daemon.start()
        console.print(f"[green]已启动，pid {pid}[/green]")
        console.print("[dim]可运行 ponte status 查看健康[/dim]")
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


@app.command()
def stop() -> None:
    """停止反向隧道守护进程。"""
    try:
        daemon = _daemon()
        if not daemon.status().running:
            console.print("[grey]未运行[/grey]")
            raise typer.Exit(code=0)
        result = daemon.stop()
        console.print("[green]已停止[/green]")
        kill_msg = _force_kill_message(result)
        if kill_msg:
            console.print(f"[yellow]{escape(kill_msg)}[/yellow]")
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


@app.command()
def restart() -> None:
    """重启反向隧道守护进程（先停止，再以后台模式启动）。"""
    try:
        daemon = _daemon()
        if daemon.status().running:
            result = daemon.stop()
            kill_msg = _force_kill_message(result)
            if kill_msg:
                console.print(f"[yellow]{escape(kill_msg)}[/yellow]")
            else:
                console.print("[grey]已停止旧进程[/grey]")
        pid = daemon.start()
        console.print(f"[green]已重启，pid {pid}[/green]")
        console.print("[dim]可运行 ponte status 查看健康[/dim]")
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


# ---------------------------------------------------------------------------
# status / logs
# ---------------------------------------------------------------------------


@app.command()
def status() -> None:
    """查看守护进程与隧道健康状态。"""
    try:
        s = _daemon().status()
        if not s.running:
            console.print("[grey]未运行（可 ponte start 启动）[/grey]")
            raise typer.Exit(code=0)

        table = Table(title="ponte 状态", header_style="bold cyan")
        table.add_column("项目", no_wrap=True, style="cyan")
        table.add_column("值")

        table.add_row("PID", str(s.pid) if s.pid is not None else "—")
        table.add_row("运行时长", s.uptime)

        if s.healthy is True:
            health_markup = "[green]健康[/green]"
        elif s.healthy is None:
            health_markup = "[yellow]未知[/yellow]"
        else:
            detail = escape(s.health_error or "")
            health_markup = f"[red]异常[/red]" + (f"（{detail}）" if detail else "")
        table.add_row("健康状态", health_markup)

        if s.remote_ports:
            for port in sorted(s.remote_ports):
                mark = "[green]监听中[/green]" if s.remote_ports[port] else "[red]未监听[/red]"
                table.add_row(f"远程端口 {port}", mark)
        if s.message:
            table.add_row("备注", escape(s.message))

        console.print(table)
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


def _follow_log(path: str, start_offset: int) -> None:
    """Poll *path* for new content starting at *start_offset* until stopped."""
    offset = start_offset
    while True:
        try:
            size = os.path.getsize(path)
        except OSError:
            console.print("[yellow]日志文件已消失[/yellow]")
            break
        if size < offset:
            # The log was rotated/truncated: tail the new file from the start.
            offset = 0
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
        if data:
            offset += len(data)
            console.print(data.decode("utf-8", errors="replace"), end="", markup=False)
        else:
            time.sleep(_FOLLOW_POLL_INTERVAL)


@app.command()
def logs(
    lines: int = typer.Option(20, "--lines", "-n", min=1, help="显示日志最后 N 行"),
    follow: bool = typer.Option(False, "--follow", "-f", help="持续跟随输出新增日志"),
) -> None:
    """查看守护进程日志（默认只看尾部，-f 跟随）。"""
    try:
        log_file = _daemon().log_file
        if not os.path.isfile(log_file):
            console.print("[yellow]尚无日志（daemon 从未启动？）[/yellow]")
            raise typer.Exit(code=0)

        with open(log_file, "rb") as fh:
            raw = fh.read()
        offset = len(raw)
        content = raw.decode("utf-8", errors="replace")
        text_lines = content.splitlines()
        for line in text_lines[-lines:]:
            console.print(line, markup=False)
        if not follow:
            raise typer.Exit(code=0)

        try:
            _follow_log(log_file, offset)
        except KeyboardInterrupt:
            console.print("\n[yellow]已停止跟随[/yellow]")
            raise typer.Exit(code=0)
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


# ---------------------------------------------------------------------------
# test / check
# ---------------------------------------------------------------------------


@app.command()
def test(
    timeout: int = typer.Option(10, "--timeout", help="连接测试超时（秒）"),
) -> None:
    """测试到 SSH 服务器的连接是否正常。"""
    try:
        ok = _daemon().test_connection(timeout=timeout)
        if ok:
            console.print("[green]连接正常 OK[/green]")
        else:
            console.print("[red]连接失败[/red]")
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


@app.command()
def check(
    timeout: int = typer.Option(10, "--timeout", help="端口检查超时（秒）"),
) -> None:
    """检查各反向隧道的远程端口是否在监听。"""
    try:
        ports = _daemon().check_remote_ports(timeout=timeout)
        if not ports:
            console.print("[yellow]没有任何配置的隧道端口[/yellow]")
            raise typer.Exit(code=0)
        for port in sorted(ports):
            mark = "[green]监听中[/green]" if ports[port] else "[red]未监听[/red]"
            console.print(f"端口 {port}: {mark}")
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


# ---------------------------------------------------------------------------
# scheduled task / config
# ---------------------------------------------------------------------------


@app.command()
def install() -> None:
    """注册 Windows 计划任务（开机自启 + 崩溃重启）。"""
    if sys.platform != "win32":
        err_console.print("[bold red]错误：仅支持 Windows（开机自启）[/bold red]")
        raise typer.Exit(code=1)
    try:
        daemon = _daemon()
        message = daemon.install_scheduled_task()
        console.print("[green]已注册计划任务（开机自启 + 崩溃重启）[/green]")
        if message:
            console.print(f"[dim]{escape(message)}[/dim]")
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


@app.command()
def uninstall() -> None:
    """移除已注册的 Windows 计划任务。"""
    try:
        daemon = _daemon()
        message = daemon.uninstall_scheduled_task()
        console.print("[green]已移除计划任务[/green]")
        if message:
            console.print(f"[dim]{escape(message)}[/dim]")
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


@app.command()
def config() -> None:
    """打印当前生效的配置关键项。"""
    try:
        cfg = get_config()
        retry = cfg.retry
        health = cfg.health

        table = Table(title="生效配置", header_style="bold cyan")
        table.add_column("键", no_wrap=True, style="cyan")
        table.add_column("值")

        table.add_row("服务器", f"{cfg.ssh.user}@{cfg.ssh.host}")
        table.add_row("SSH 端口", str(cfg.ssh.port))
        tunnel_lines = [
            f"- 远程 {t.remote_port} → {t.local_host}:{t.local_port}"
            + (f"  ({escape(t.description)})" if t.description else "")
            for t in cfg.tunnels
        ]
        table.add_row("反向隧道", "\n".join(tunnel_lines) or "（无）")
        table.add_row(
            "retry",
            f"max_retries={retry.max_retries}, base_delay={retry.base_delay}s, "
            f"max_delay={retry.max_delay}s, backoff_factor={retry.backoff_factor}, "
            f"jitter={'on' if retry.jitter else 'off'}",
        )
        table.add_row(
            "health",
            f"check_interval={health.check_interval}s, "
            f"remote_check={'on' if health.remote_check_enabled else 'off'}, "
            f"remote_check_timeout={health.remote_check_timeout}s",
        )
        table.add_row("pid_file", cfg.daemon.pid_file or "（默认）")
        table.add_row("log_file", cfg.daemon.log_file or "（默认）")
        table.add_row("ssh_exe", cfg.windows.ssh_exe or "ssh（PATH）")
        table.add_row("配置文件", cfg.source_path)

        console.print(table)
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


if __name__ == "__main__":
    app()