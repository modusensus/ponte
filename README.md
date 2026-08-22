# ponte — SSH 反向隧道持久守护 CLI

一个 Python 写的持久 SSH 反向隧道守护工具：隧道掉线后由进程内无限次指数退避 + 抖动重连自动拉回，并有系统计划任务做开机自启与进程级兜底，解决"SSH 隧道断线后不再恢复"的老大难问题。

---

## 为什么需要它

旧方案是 `VBS → Batch → SSH + Scheduled Task(RestartCount=5)`：

- Scheduled Task 设置的 **RestartCount=5** 意味着 SSH 进程崩溃后最多自动重启 5 次，第 5 次之后计划任务直接放弃，隧道永远不会回来。
- Batch 里虽然写了个 `:loop`，但它是**无退避的死循环**，断线立刻重连，一旦服务器侧暂时不可达（网络抖动、安全组变更、服务器重启中），就会陷入毫无间隔的反复抢连。
- 还有一个隐蔽的坑：本机用户名是**非 ASCII 路径** `C:\Users\石晴`。OpenSSH 默认往 `~/.ssh` 写 `known_hosts`，在中文路径下会因为编码问题写入失败，导致连接直接被拒。

本次用 Python 分层重连彻底解决：

- **进程内无限重连**：`retry.py` 的 `max_retries=0` 表示"重试到天荒地老"，指数退避（5s → 10s → 20s …封顶 5 分钟）+ 全抖动，既不会风暴式抢连，也不会在 5 次后放弃。
- **显式 known_hosts 路径**：强制 `UserKnownHostsFile=C:\ssh-tunnel\known_hosts`，绕开中文用户目录的写入失败问题。
- **计划任务兜底**：`install` 创建 `SSH-Reverse-Tunnel` 计划任务（开机自启 + `RestartCount=999`），即便 Python 进程整体崩溃也能在 1 分钟内被系统拉活。
- **健康检查**：`health.py` 定时检查本地 SSH 进程是否存活、远程转发端口（`ss -tlnp`）是否在监听，异常时给出明确诊断而不是黑盒。

---

## 架构

```
┌──────────────────────────────────────────────────────────────────────┐
│                     ponte（本地守护进程，Python）                       │
│                                                                      │
│   main.py ──▶ daemon.py ──▶ retry.py  ──▶ core.py ──▶ ssh.exe -N -R  │
│    (typer     (生命周期     (无限退避       (纯 SSH       root@47.113.   │
│     CLI)      编排)         重连)          subprocess)   179.249)     │
│                                 │                                     │
│                                 ▼                                     │
│   health.py 周期检查：本地进程存活 + 远程端口 ss -tlnp                   │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
          掉线无限重连 / 进程崩溃兜底
                               ▼
  计划任务 SSH-Reverse-Tunnel（开机自启，RestartCount=999 * 1分钟）
```

### 分层说明

| 模块 | 职责 | 备注 |
|------|------|------|
| `main.py` | typer 命令行入口 | `start/stop/restart/status/logs/test/check/install/uninstall/config` |
| `daemon.py` | 生命周期编排 | 写 PID / 状态快照 / 停止标记，管计划任务，优雅停止 |
| `retry.py` | 重连策略 | 指数退避 + 全抖动；`max_retries=0` = 无限重试 |
| `core.py` | 纯 SSH 封装 | 拼 `-N -R` 参数，spawn/terminate ssh 子进程，远程端口探测 |
| `health.py` | 健康检查 | 周期检查本地进程存活 + 远程端口 `ss -tlnp` |
| `config.py` | 配置加载校验 | `tomllib`（Python 3.11+ 内置）→ dataclass，进程内缓存 |

---

## 目录结构

```
C:\ssh-tunnel\
├─ ponte\                 # ponte 包源码
│  ├─ __init__.py         # 版本号
│  ├─ main.py             # typer CLI 入口
│  ├─ daemon.py           # 生命周期编排
│  ├─ retry.py            # 无限重连（退避 + 抖动）
│  ├─ core.py             # 纯 SSH subprocess（-N -R）
│  ├─ health.py           # 周期健康检查（进程 + 远程端口）
│  ├─ config.py           # TOML 加载 / 校验
│  └─ config.toml         # 配置文件
├─ _smoke_test.py         # 冒烟测试（不连外网）
├─ id_rsa / id_rsa.pub    # SSH 身份密钥（⚠️ 不入库，自备）
├─ known_hosts            # 显式 UserKnownHostsFile
├─ ponte.bat              # Windows 启动入口（可选，放入 PATH）
└─ ponte.log / ponte.pid / ponte.status.json / ponte.stop
                          # daemon 运行时文件（⚠️ 不入库）
```

---

## 环境要求与安装

- **Python 3.11+**（`tomllib` 为内置库，无需额外安装 TOML 解析器）
- 第三方依赖：`typer`、`rich`
- SSH 客户端：Git for Windows 自带的 `D:\Git\usr\bin\ssh.exe`（通过 `windows.ssh_exe` 指定），或任意 OpenSSH

```bash
pip install typer rich
```

> 计划任务若以 **SYSTEM** 账户运行，建议尾部启动命令用 `pythonw`（无控制台窗口版），避免弹出黑框。

---

## 配置说明（`ponte/config.toml`）

所有路径支持 `~` 展开；identity 文件与 known_hosts 文件必须在配置校验时真实存在，否则启动即报错。

| 段 | 字段 | 含义 |
|----|------|------|
| `[ssh]` | `host` / `port` / `user` | 服务器 `47.113.179.249`，阿里云 ECS，`root` |
| `[ssh]` | `identity_file` | `C:\ssh-tunnel\id_rsa`，SSH 私钥 |
| `[ssh]` | `known_hosts_file` | `C:\ssh-tunnel\known_hosts`，显式指定（绕开中文用户目录失败问题） |
| `[ssh.options]` | `StrictHostKeyChecking="accept-new"` | 首次连接自动接受服务器指纹并写入 known_hosts |
| `[ssh.options]` | `ServerAliveInterval=30` / `ServerAliveCountMax=3` | 每 30s 发 keepalive，3 次无应答即判定断线，保证隧道断线能被尽快感知 |
| `[ssh.options]` | `ExitOnForwardFailure="yes"` | 远程端口绑定失败立即退出（而不是伪成功） |
| `[ssh.options]` | `TCPKeepAlive="yes"` | TCP 层 keepalive |
| `[[tunnels]]` | 反向隧道规则 | **方向注意**：服务器上打开 `remote_port`，转发回本地 `localhost:local_port`（`-R` 的反向转发） |
| `[daemon]` | `pid_file` / `log_file` / `log_max_bytes` / `log_backup_count` | 运行时文件；日志单文件 10MB、滚动保留 3 份 |
| `[retry]` | `max_retries=0` | **0 = 无限重试**，永不言弃 |
| `[retry]` | `base_delay=5` / `max_delay=300` / `backoff_factor=2.0` / `jitter=true` | 退避 `min(5×2^attempt, 300)` 秒，全抖动取 `[0, 计算值)` 均匀随机，避免惊群 |
| `[health]` | `check_interval=60` / `remote_check_enabled=true` / `remote_check_timeout=10` | 每 60s 检查一次；远程检查通过再连一台 SSH 跑 `ss -tlnp` 看端口 |
| `[windows]` | `task_name` | 计划任务名 `SSH-Reverse-Tunnel` |
| `[windows]` | `ssh_exe` | `D:\Git\usr\bin\ssh.exe` |

当前的两种反向隧道：

```toml
[[tunnels]]
remote_port = 23334   # 服务器上 23334 端口 → 本地 localhost:2222（WSL SSH，经 netsh portproxy）
local_host = "localhost"
local_port = 2222

[[tunnels]]
remote_port = 17897   # 服务器上 17897 端口 → 本地 localhost:7897（Windows 代理）
local_host = "localhost"
local_port = 7897
```

---

## 使用

假设 `C:\ssh-tunnel` 已在 `PATH`（或直接用 `python -m ponte.main ...`）：

```bash
ponte start                 # 后台启动守护进程（写 PID，脱离终端）
ponte start --foreground    # 前台启动，日志直接打到终端（调试用）
ponte stop                  # 优雅停止：先停计划任务，再标记退出；20s 无响应则 taskkill /T /F
ponte restart               # 停旧起新
ponte status                # 查看本地进程 / 远程端口 / 计划任务状态
ponte logs -n 50            # 查看最近 50 行日志
ponte logs --follow         # 跟读日志（类似 tail -f）
ponte test                  # 快速测 SSH 连通性（ssh … echo OK）
ponte check                 # 本地进程 + 远程端口健康检查
ponte install               # 注册计划任务：开机自启 + RestartCount=999
ponte uninstall             # 卸载计划任务并停止隧道
ponte config                # 打印当前生效配置
```

---

## 高可用设计（三层兜底）

1. **进程内无限重连（主防线）**：`retry.py` 以 `max_retries=0` 无限重试，SSH 掉线即按指数退避 + 全抖动等待后重连；退避期间仍响应停止请求（0.25s 粒度轮询）。
2. **健康检查（观察层）**：`health.py` 每 60s 检查本地 SSH 进程存活 + 远程转发端口监听，异常可触发重启决策并写日志诊断。
3. **计划任务兜底（进程级）**：`install` 注册 `SSH-Reverse-Tunnel`，开机自启、`RestartCount=999`、1 分钟重启间隔、`MultipleInstances=IgnoreNew` —— 即便 Python 守护进程整体崩溃，系统也会在一分钟内把它拉回来。

### 停止流程

`stop` 依序执行：先停掉计划任务（防止兜底把进程再拉起）→ 写 `ponte.stop` 停止标记 → 优雅终止 SSH 子进程 → 20 秒无响应则 `taskkill /T /F` 连进程树强杀。

---

## 排障

| 症状 | 排查方向 |
|------|----------|
| 「Permission denied (publickey) 身份认证失败」 | 私钥 `C:\ssh-tunnel\id_rsa` 是否已有对应公钥在服务器 `~/.ssh/authorized_keys`；Windows 下私钥文件权限不要存在"继承给其他用户"，必要时 `icacls id_rsa /inheritance:r /grant:r 石晴:(R)` |
| 首次/换 key 后连接被拒 | 删除 `C:\ssh-tunnel\known_hosts`，用 `StrictHostKeyChecking=accept-new`（配置默认）重新连接，重新写入指纹 |
| 隧道进程活着但远程端口不通 | 阿里云 **安全组** 是否放行了入方向的 `23334` / `17897`；服务器上 `ss -tlnp` 确认端口确实在监听 |
| 排查日志 | 主日志 `C:\ssh-tunnel\ponte.log`（10MB × 3 滚动）；`ponte logs -n 100 --follow` 实时跟看 |

---

## 开发与测试

`C:\ssh-tunnel\_smoke_test.py` 是纯本地冒烟测试：在导入真实模块前用临时 stub 替换 `ponte.config` 与 `ponte.core`，只测 `retry.py` 与 `health.py` 的纯逻辑（退避序列、抖动范围、`max_retries=0` 无限重试、`MAX_RETRIES_REACHED` 终止、`run_loop` 停止与回调异常吞并等），**不连外网、不 spawn SSH**。

```bash
python C:\ssh-tunnel\_smoke_test.py
```

全部断言通过时输出 `ALL SMOKE TESTS PASSED`。

---

## 注意事项

- **`id_rsa` 私钥绝不计入仓库**：`.gitignore` 已排除 `id_rsa` / `id_rsa.pub`，新环境需自行放置密钥文件。
- `known_hosts`、`config.toml`、`ponte.bat`、`ponte/` 源码、`_smoke_test.py` 均入库保留；daemon 运行时文件（`ponte.pid` / `ponte.status.json` / `ponte.stop` / `ponte.log*`）不入库。