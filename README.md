# ponte — Persistent SSH Reverse Tunnel CLI

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-blue.svg)](#cross-platform-service-management)
[![CI](https://github.com/modusensus/ponte/actions/workflows/ci.yml/badge.svg)](https://github.com/modusensus/ponte/actions/workflows/ci.yml)
[![Codecov](https://codecov.io/gh/modusensus/ponte/branch/main/graph/badge.svg)](https://codecov.io/gh/modusensus/ponte)

---

# English

A persistent SSH **reverse** tunnel daemon in pure Python. It keeps `ssh -N -R`
alive across network drops and reboots, reconnecting with exponential backoff
+ jitter, and registers an OS-level auto-start service so it survives crashes.

## Features

- **Self-healing**: infinite reconnect with exponential backoff + full jitter
  (`max_retries=0` = retry forever), so a drop never becomes a dead tunnel.
- **Crash recovery**: `install` registers an OS auto-start service — Scheduled
  Task (Windows), systemd user unit (Linux), or launchd agent (macOS).
- **Health checks**: periodic local-process + remote-port probing, with clear
  diagnostics instead of a black box.
- **Cross-platform**: resolves `ssh` automatically, per-platform runtime
  paths, and portable remote-port probing (`socket` → `ss`/`lsof`/`netstat`).

## Quick start

```bash
pip install typer rich
# edit ponte/config.toml: set host/user and point identity_file at your key
ponte test              # verify SSH connectivity
ponte start             # run the daemon in the background
ponte status            # check process + remote ports
ponte install           # register auto-start + crash restart
```

## Commands

| Command | Purpose |
|---------|---------|
| `start` / `start --foreground` | start daemon in background / foreground (debug) |
| `stop` / `restart` | graceful stop / stop-then-start |
| `status` | local process + remote port + service state |
| `logs [-n N] [--follow]` | view / tail the daemon log |
| `test` | quick SSH connectivity check |
| `check` | verify configured remote ports are listening |
| `install` / `uninstall` | register / remove the OS auto-start service |
| `config` | print the effective configuration |

## Cross-platform service management

| Platform | Mechanism | Generated artifact |
|----------|-----------|--------------------|
| Windows | Scheduled Task | `Register-ScheduledTask` (`pythonw -m ponte.main start --foreground`) |
| Linux | systemd **user** unit | `~/.config/systemd/user/ponte.service` |
| macOS | launchd LaunchAgent | `~/Library/LaunchAgents/com.modusensus.ponte.plist` |

## Architecture

```
ponte (local daemon, Python)
  main.py ──▶ daemon.py ──▶ retry.py ──▶ core.py ──▶ ssh -N -R
  (typer     (lifecycle    (infinite    (pure SSH
   CLI)      orchestration) backoff)    subprocess)
                              │
                              ▼
  health.py ── periodic checks: process alive + remote ports
```

- `main.py` — typer CLI entry
- `daemon.py` — lifecycle orchestration, service install/uninstall, graceful stop
- `retry.py` — exponential backoff + jitter reconnect state machine
- `core.py` — SSH argument building, subprocess management, port probing
- `health.py` — periodic liveness + remote-port checks
- `config.py` — TOML load/validate (built-in `tomllib` on 3.11+)

## Configuration

Edit `ponte/config.toml` (all paths support `~` expansion):

- `[ssh]` — `host` / `port` / `user` / `identity_file` / `known_hosts_file`
- `[[tunnels]]` — reverse rules; the server opens `remote_port`, forwarding
  back to local `localhost:local_port` (`-R`)
- `[daemon]` — pid/log paths (default per-platform: `%LOCALAPPDATA%\ponte`,
  `~/.local/state/ponte`, `~/Library/Application Support/ponte`), log rotation
- `[retry]` — `max_retries` (0 = forever), backoff params, jitter
- `[health]` — check interval, remote probe toggle/timeout
- `[service]` — service name, autostart, POSIX kill grace
- `[windows]` — Windows-only knobs (`task_name`, `ssh_exe`)

## Troubleshooting

| Symptom | Where to look |
|---------|---------------|
| `Permission denied (publickey)` | public key on server `~/.ssh/authorized_keys`; on Windows strip inherited ACLs (`icacls id_rsa /inheritance:r /grant:r <user>:(R)`) |
| Connection rejected after key change | delete `known_hosts`, reconnect (`StrictHostKeyChecking=accept-new` default) |
| Process alive but remote port down | cloud security-group inbound rules; check server with `ss -tlnp` / `lsof -nP -iTCP -sTCP:LISTEN` |
| Logs | `ponte logs -n 100 --follow` |

## Development & testing

```bash
pip install -e ".[dev]"
pytest --cov=ponte --cov-report=term-missing   # 40+ tests, threshold in pyproject.toml
python _smoke_test.py                          # zero-dependency quick check
```

CI runs across Windows/Linux/macOS × Python 3.11/3.12 and reports coverage to
[Codecov](https://codecov.io/gh/modusensus/ponte). See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Notes

- **Never commit the private key**: `.gitignore` excludes `id_rsa` /
  `id_rsa.pub`; place your own keys on each machine.
- Runtime files (`ponte.pid` / `ponte.status.json` / `ponte.stop` /
  `ponte.log*`) are generated and not committed.
- Found a security issue? See [SECURITY.md](SECURITY.md) for how to report it
  privately.

---

# 中文

一个用纯 Python 写的持久 SSH **反向**隧道守护工具。它让 `ssh -N -R` 在网络
抖动与重启后依然存活：断线自动以指数退避 + 抖动重连，并注册系统级开机自启
服务，进程崩溃也能被拉活。

## 特性

- **自愈**：无限重连 + 指数退避 + 全抖动（`max_retries=0` = 永远重试），
  掉线不会变成死隧道。
- **崩溃兜底**：`install` 注册系统级开机自启服务——Windows 计划任务 /
  Linux systemd user / macOS launchd。
- **健康检查**：周期探测本地进程存活 + 远程端口，异常给出明确诊断。
- **跨平台**：自动查找 `ssh`、按平台落盘运行时文件、可移植的远程端口探测
  （`socket` → `ss`/`lsof`/`netstat`）。

## 快速开始

```bash
pip install typer rich
# 编辑 ponte/config.toml：填 host/user，identity_file 指向你的密钥
ponte test              # 验证 SSH 连通性
ponte start             # 后台启动守护进程
ponte status            # 查看进程 + 远程端口
ponte install           # 注册开机自启 + 崩溃重启
```

## 命令

| 命令 | 用途 |
|------|------|
| `start` / `start --foreground` | 后台启动 / 前台启动（调试） |
| `stop` / `restart` | 优雅停止 / 停旧起新 |
| `status` | 本地进程 + 远程端口 + 服务状态 |
| `logs [-n N] [--follow]` | 查看 / 跟读日志 |
| `test` | 快速测 SSH 连通性 |
| `check` | 检查各远程端口是否在监听 |
| `install` / `uninstall` | 注册 / 移除开机自启服务 |
| `config` | 打印当前生效配置 |

## 跨平台服务管理

| 平台 | 机制 | 生成物 |
|------|------|--------|
| Windows | 计划任务 | `Register-ScheduledTask`（`pythonw -m ponte.main start --foreground`） |
| Linux | systemd **user** 单元 | `~/.config/systemd/user/ponte.service` |
| macOS | launchd LaunchAgent | `~/Library/LaunchAgents/com.modusensus.ponte.plist` |

## 架构

```
ponte（本地守护进程，Python）
  main.py ──▶ daemon.py ──▶ retry.py ──▶ core.py ──▶ ssh -N -R
  (typer     (生命周期     (无限退避     (纯 SSH
   CLI)      编排)         重连)         subprocess)
                              │
                              ▼
  health.py ── 周期检查：进程存活 + 远程端口
```

- `main.py` — typer 命令行入口
- `daemon.py` — 生命周期编排、服务安装/卸载、优雅停止
- `retry.py` — 指数退避 + 抖动重连状态机
- `core.py` — SSH 参数构建、子进程管理、端口探测
- `health.py` — 周期存活 + 远程端口检查
- `config.py` — TOML 加载/校验（3.11+ 内置 `tomllib`）

## 配置

编辑 `ponte/config.toml`（所有路径支持 `~` 展开）：

- `[ssh]` — `host` / `port` / `user` / `identity_file` / `known_hosts_file`
- `[[tunnels]]` — 反向规则；服务器打开 `remote_port`，转发回本地
  `localhost:local_port`（`-R`）
- `[daemon]` — pid/log 路径（平台默认：`%LOCALAPPDATA%\ponte`、
  `~/.local/state/ponte`、`~/Library/Application Support/ponte`）、日志滚动
- `[retry]` — `max_retries`（0 = 无限）、退避参数、抖动
- `[health]` — 检查间隔、远程探测开关/超时
- `[service]` — 服务名、自启、POSIX 强杀等待
- `[windows]` — 仅 Windows 使用（`task_name`、`ssh_exe`）

## 排障

| 症状 | 排查方向 |
|------|----------|
| 「Permission denied (publickey)」 | 公钥是否加入服务器 `~/.ssh/authorized_keys`；Windows 下私钥去掉继承 ACL（`icacls id_rsa /inheritance:r /grant:r <用户名>:(R)`） |
| 换 key 后连接被拒 | 删除 `known_hosts` 重连（默认 `StrictHostKeyChecking=accept-new`） |
| 进程活着但远程端口不通 | 云安全组入方向规则；服务器上 `ss -tlnp` / `lsof -nP -iTCP -sTCP:LISTEN` 确认监听 |
| 排查日志 | `ponte logs -n 100 --follow` |

## 开发与测试

```bash
pip install -e ".[dev]"
pytest --cov=ponte --cov-report=term-missing   # 40+ 用例，阈值见 pyproject.toml
python _smoke_test.py                          # 零依赖快速自检
```

CI 在 Windows/Linux/macOS × Python 3.11/3.12 上运行，覆盖率上报到
[Codecov](https://codecov.io/gh/modusensus/ponte)。详见
[CONTRIBUTING.md](CONTRIBUTING.md)。

## 注意事项

- **私钥绝不计入仓库**：`.gitignore` 已排除 `id_rsa` / `id_rsa.pub`；
  各机器自行放置密钥。
- 运行时文件（`ponte.pid` / `ponte.status.json` / `ponte.stop` /
  `ponte.log*`）为生成物，不入库。
- 发现安全问题？见 [SECURITY.md](SECURITY.md)，请私下报告。
