<div align="center">

# ponte

**A persistent SSH reverse-tunnel daemon** · 持久 SSH 反向隧道守护工具

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-blue.svg)](#cross-platform-service-management)
[![CI](https://github.com/modusensus/ponte/actions/workflows/ci.yml/badge.svg)](https://github.com/modusensus/ponte/actions/workflows/ci.yml)
[![Codecov](https://codecov.io/gh/modusensus/ponte/branch/main/graph/badge.svg)](https://codecov.io/gh/modusensus/ponte)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**Language / 语言：** [English](#english) · [中文](#中文)

</div>

---

# English

> Keep `ssh -N -R` alive across network drops and reboots — reconnect with
> exponential backoff + jitter, and register an OS-level auto-start service so
> it survives crashes.

## ✦ Features

- 🔁 **Self-healing** — infinite reconnect with exponential backoff + full
  jitter (`max_retries=0` = retry forever), so a drop never becomes a dead
  tunnel. A session that stays up ≥ `stable_after` seconds resets the retry
  budget, so a long-running tunnel is never abandoned after a few flaky drops.
- 🛟 **Crash recovery** — `install` registers an OS auto-start service:
  boot-or-logon Scheduled Task (Windows), systemd user unit (Linux), launchd agent (macOS).
- 💚 **Health checks** — periodic local-process + remote-port probing, with
  clear diagnostics instead of a black box. A "zombie" SSH process (alive but
  ports down) is force-reconnected after 3 consecutive failed checks, and
  checks back off exponentially during outages so the server's `MaxStartups`
  is never hammered.
- 🖥️ **Cross-platform** — resolves `ssh` automatically, per-platform runtime
  paths, and portable remote-port probing (`socket` → `ss`/`lsof`/`netstat`).

## 🚀 Quick start

```bash
pipx install .          # or: pip install .   (pipx keeps it isolated)
ponte init              # create the config file and print its path
$EDITOR ~/.config/ponte/config.toml   # set host/user, point identity_file at your key
ponte test              # verify SSH connectivity
ponte start             # run the daemon in the background
ponte status            # check process + remote ports
ponte install           # register auto-start + crash restart
```

No config file yet? `ponte init` writes one from the shipped template. The
config lives **outside** the package, so `pip install -U ponte` never touches
it.

## ⌨️ Commands

| Command | Purpose |
|---------|---------|
| `init [--path P] [--force]` | write a config file from the template (never overwrites without `--force`) |
| `start` / `start --foreground` | start daemon in background / foreground (debug) |
| `stop` / `restart` | graceful stop / stop-then-start |
| `status` | local process + remote port + service state |
| `logs [-n N] [--follow]` | view / tail the daemon log |
| `test` | quick SSH connectivity check |
| `check` | verify configured remote ports are listening |
| `install` / `uninstall` | register / remove the OS auto-start service |
| `config` | print the effective configuration, its source file and any warnings |

Global options (before the command): `--config/-c PATH` pin a config file,
`--version/-V` print the version. Unknown/typo'd config keys are reported by
`ponte config` instead of being silently ignored.

## 🛠️ Cross-platform service management

| Platform | Mechanism | Generated artifact |
|----------|-----------|--------------------|
| Windows | Scheduled Task (boot or logon) | `Register-ScheduledTask` (`pythonw -m ponte.main --config <file> start --foreground`) |
| Linux | systemd **user** unit | `~/.config/systemd/user/ponte.service` |
| macOS | launchd LaunchAgent | `~/Library/LaunchAgents/com.modusensus.ponte.plist` |

The daemon is always launched with an explicit `--config`, so a service running
under a different account (e.g. a SYSTEM Scheduled Task) still reads *your*
config instead of silently falling back to another one.

## 🏗️ Architecture

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

## ⚙️ Configuration

Run `ponte init`, then edit the file it prints. All paths support `~` and
environment-variable expansion. Resolution order (first existing file wins):

1. `--config PATH`
2. `$PONTE_CONFIG`
3. user config dir — `%APPDATA%\ponte\config.toml` (Windows),
   `~/.config/ponte/config.toml` (Linux),
   `~/Library/Application Support/ponte/config.toml` (macOS)
4. `ponte/config.toml` inside the installed package — **legacy**, honoured only
   so pre-0.3 installs keep working; it is overwritten by `pip install -U`, so
   migrate with `ponte init`.

Sections:

- `[ssh]` — `host` / `port` / `user` / `identity_file` / `known_hosts_file` /
  `options` (any extra key there is passed through verbatim as `-o key=value`)
- `[[tunnels]]` — reverse rules; the server opens `remote_port`, forwarding
  back to local `localhost:local_port` (`-R`)
- `[daemon]` — pid/log paths (default per-platform: `%LOCALAPPDATA%\ponte`,
  `~/.local/state/ponte`, `~/Library/Application Support/ponte`), log rotation
- `[retry]` — `max_retries` (0 = forever), backoff params, `jitter`,
  `stable_after`
- `[health]` — check interval, remote probe toggle/timeout,
  `max_check_interval` (backoff ceiling while unhealthy)
- `[service]` — service name, autostart, POSIX kill grace
- `[windows]` — Windows-only knobs (`task_name`, `ssh_exe`, `run_as`).
  `run_as` is `user` (default: logon-time, runs as you, can read `~/.ssh`) or
  `system` (boot-time, survives login/reboot, needs elevation **and** an
  identity file SYSTEM can read).

## 🔍 Troubleshooting

| Symptom | Where to look |
|---------|---------------|
| `Permission denied (publickey)` | public key on server `~/.ssh/authorized_keys`; on Windows strip inherited ACLs (`icacls id_rsa /inheritance:r /grant:r <user>:(R)`) |
| Connection rejected after key change | delete `known_hosts`, reconnect (`StrictHostKeyChecking=accept-new` default) |
| Process alive but remote port down | cloud security-group inbound rules; check server with `ss -tlnp` / `lsof -nP -iTCP -sTCP:LISTEN` — the daemon now force-reconnects a "zombie" tunnel after 3 consecutive failed checks |
| Logs | `ponte logs -n 100 --follow` |

## 🧪 Development & testing

```bash
pip install -e ".[dev]"
pytest --cov=ponte --cov-report=term-missing   # threshold in pyproject.toml
ruff check .                                   # lint
mypy                                           # type check
python _smoke_test.py                          # zero-dependency quick check
```

CI runs lint + types on Linux, and the test suite across
Windows/Linux/macOS × Python 3.11/3.12, reporting coverage to
[Codecov](https://codecov.io/gh/modusensus/ponte). A `build` job also installs
the built wheel and runs `ponte init`, so a packaging regression cannot ship
again. See [CONTRIBUTING.md](CONTRIBUTING.md).

## 📝 Notes

- **Never commit the private key**: `.gitignore` excludes `id_rsa` /
  `id_rsa.pub`; place your own keys on each machine.
- Runtime files (`ponte.pid` / `ponte.status.json` / `ponte.stop` /
  `ponte.log*`) are generated and not committed.
- **Upgrading from ≤ 0.2.x**: the config file moved out of the package. Run
  `ponte init` (it migrates the in-package file if one exists), then
  `ponte install` again so the service picks up the new `--config` argument.
- Legacy pre-Python scripts (`tunnel.ps1`, `setup.ps1`, `ssh-tunnel.bat`,
  `ssh-tunnel.vbs`, `fix-wsl-tunnel.sh`) live in [`legacy/`](legacy/) and are
  **deprecated** — the CLI replaces them. `setup.ps1` copied your private key
  into the project directory; do not use it.
- Found a security issue? See [SECURITY.md](SECURITY.md) for how to report it
  privately.

---

# 中文

> 让 `ssh -N -R` 在网络抖动与重启后依然存活——断线自动以指数退避 + 抖动重连，
> 并注册系统级开机自启服务，进程崩溃也能被拉活。

## ✦ 特性

- 🔁 **自愈** — 无限重连 + 指数退避 + 全抖动（`max_retries=0` = 永远重试），
  掉线不会变成死隧道。会话稳定运行 ≥ `stable_after` 秒后重试预算归零，
  长跑隧道不会因前期几次抖动被永久放弃。
- 🛟 **崩溃兜底** — `install` 注册系统级开机自启服务：Windows 计划任务（开机或登录） /
  Linux systemd user / macOS launchd。
- 💚 **健康检查** — 周期探测本地进程存活 + 远程端口，异常给出明确诊断。
  SSH 进程假死（活着但端口全掉）时连续 3 次检查失败即强制重连；检查失败
  指数退避，不会高频新开 SSH 触发服务器 `MaxStartups`。
- 🖥️ **跨平台** — 自动查找 `ssh`、按平台落盘运行时文件、可移植的远程端口探测
  （`socket` → `ss`/`lsof`/`netstat`）。

## 🚀 快速开始

```bash
pipx install .          # 或 pip install .（pipx 会隔离安装）
ponte init              # 生成配置文件并打印路径
$EDITOR ~/.config/ponte/config.toml   # 填 host/user，identity_file 指向你的密钥
ponte test              # 验证 SSH 连通性
ponte start             # 后台启动守护进程
ponte status            # 查看进程 + 远程端口
ponte install           # 注册开机自启 + 崩溃重启
```

还没有配置文件？`ponte init` 会从内置模板生成一份。配置存放在**包外**，
`pip install -U ponte` 不会覆盖它。

## ⌨️ 命令

| 命令 | 用途 |
|------|------|
| `init [--path P] [--force]` | 从模板生成配置文件（不加 `--force` 不覆盖） |
| `start` / `start --foreground` | 后台启动 / 前台启动（调试） |
| `stop` / `restart` | 优雅停止 / 停旧起新 |
| `status` | 本地进程 + 远程端口 + 服务状态 |
| `logs [-n N] [--follow]` | 查看 / 跟读日志 |
| `test` | 快速测 SSH 连通性 |
| `check` | 检查各远程端口是否在监听 |
| `install` / `uninstall` | 注册 / 移除开机自启服务 |
| `config` | 打印生效配置、来源文件与配置告警 |

全局选项（写在子命令之前）：`--config/-c PATH` 指定配置文件，
`--version/-V` 打印版本。拼错/未知的配置项会由 `ponte config` 报出来，
不再被静默忽略。

## 🛠️ 跨平台服务管理

| 平台 | 机制 | 生成物 |
|------|------|--------|
| Windows | 计划任务（开机或登录） | `Register-ScheduledTask`（`pythonw -m ponte.main --config <文件> start --foreground`） |
| Linux | systemd **user** 单元 | `~/.config/systemd/user/ponte.service` |
| macOS | launchd LaunchAgent | `~/Library/LaunchAgents/com.modusensus.ponte.plist` |

守护进程始终带显式 `--config` 启动：即使服务以其它身份运行（例如 SYSTEM
计划任务），读到的仍是**你这份**配置，而不是静默回退到别处。

## 🏗️ 架构

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

## ⚙️ 配置

先运行 `ponte init`，再编辑它打印出的文件。所有路径支持 `~` 与环境变量展开。
查找顺序（先找到的生效）：

1. `--config PATH`
2. 环境变量 `$PONTE_CONFIG`
3. 用户配置目录 —— Windows `%APPDATA%\ponte\config.toml`、
   Linux `~/.config/ponte/config.toml`、
   macOS `~/Library/Application Support/ponte/config.toml`
4. 包内 `ponte/config.toml` —— **旧位置**，仅为兼容 0.3 之前的安装保留；
   它会被 `pip install -U` 覆盖，请用 `ponte init` 迁移。

各段含义：

- `[ssh]` — `host` / `port` / `user` / `identity_file` / `known_hosts_file` /
  `options`（该表内未列出的键会原样透传为 `-o key=value`）
- `[[tunnels]]` — 反向规则；服务器打开 `remote_port`，转发回本地
  `localhost:local_port`（`-R`）
- `[daemon]` — pid/log 路径（平台默认：`%LOCALAPPDATA%\ponte`、
  `~/.local/state/ponte`、`~/Library/Application Support/ponte`）、日志滚动
- `[retry]` — `max_retries`（0 = 无限）、退避参数、`jitter`、`stable_after`
- `[health]` — 检查间隔、远程探测开关/超时、`max_check_interval`
  （不健康期间的间隔退避上限）
- `[service]` — 服务名、自启、POSIX 强杀等待
- `[windows]` — 仅 Windows 使用（`task_name`、`ssh_exe`、`run_as`）。
  `run_as` 默认 `user`（登录后以你本人身份运行、能读 `~/.ssh`）或
  `system`（开机即起、重启也能拉起，但需提权，且 `identity_file`
  必须是 SYSTEM 能读到的文件）。

## 🔍 排障

| 症状 | 排查方向 |
|------|----------|
| 「Permission denied (publickey)」 | 公钥是否加入服务器 `~/.ssh/authorized_keys`；Windows 下私钥去掉继承 ACL（`icacls id_rsa /inheritance:r /grant:r <用户名>:(R)`） |
| 换 key 后连接被拒 | 删除 `known_hosts` 重连（默认 `StrictHostKeyChecking=accept-new`） |
| 进程活着但远程端口不通 | 云安全组入方向规则；服务器上 `ss -tlnp` / `lsof -nP -iTCP -sTCP:LISTEN` 确认监听 —— 守护进程已支持假死检测：连续 3 次检查失败自动强制重连 |
| 排查日志 | `ponte logs -n 100 --follow` |

## 🧪 开发与测试

```bash
pip install -e ".[dev]"
pytest --cov=ponte --cov-report=term-missing   # 阈值见 pyproject.toml
ruff check .                                   # 静态检查
mypy                                           # 类型检查
python _smoke_test.py                          # 零依赖快速自检
```

CI 在 Linux 上跑 lint + 类型检查，在 Windows/Linux/macOS × Python
3.11/3.12 上跑测试，覆盖率上报到
[Codecov](https://codecov.io/gh/modusensus/ponte)。另有一个 `build` 任务会
安装打好的 wheel 并执行 `ponte init`，避免打包问题再次溜进发布。详见
[CONTRIBUTING.md](CONTRIBUTING.md)。

## 📝 注意事项

- **私钥绝不计入仓库**：`.gitignore` 已排除 `id_rsa` / `id_rsa.pub`；
  各机器自行放置密钥。
- 运行时文件（`ponte.pid` / `ponte.status.json` / `ponte.stop` /
  `ponte.log*`）为生成物，不入库。
- **从 ≤ 0.2.x 升级**：配置文件已迁出包目录。请运行 `ponte init`
  （若存在包内旧配置会直接迁移），然后重新 `ponte install`，
  让服务带上新的 `--config` 参数。
- Python 化之前的遗留脚本（`tunnel.ps1`、`setup.ps1`、`ssh-tunnel.bat`、
  `ssh-tunnel.vbs`、`fix-wsl-tunnel.sh`）已移到 [`legacy/`](legacy/) 并标记
  为**废弃**，请改用 CLI。其中 `setup.ps1` 会把你的私钥复制进项目目录，
  不要再使用。
- 发现安全问题？见 [SECURITY.md](SECURITY.md)，请私下报告。
