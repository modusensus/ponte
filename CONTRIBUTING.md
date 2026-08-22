# Contributing

Thanks for your interest in ponte. This document covers how to set up a dev
environment, run tests, and open a clean change.

---

# English

## Getting started

Requires **Python 3.11+** (uses the built-in `tomllib`).

```bash
git clone git@github.com:modusensus/ponte.git
cd ponte
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Running tests

```bash
pytest                          # full suite (config, core, daemon, health, main, retry)
pytest --cov=ponte --cov-report=term-missing   # with coverage report
python _smoke_test.py           # zero-dependency smoke check
```

Coverage has a `fail_under` threshold in `pyproject.toml`; don't let it drop.
CI runs the same suite on Windows / Linux / macOS × Python 3.11 / 3.12 and
uploads coverage to Codecov.

## Project layout

```
ponte/
  __init__.py   # version
  main.py       # typer CLI
  daemon.py     # lifecycle, service install/uninstall, graceful stop
  retry.py      # reconnect state machine (backoff + jitter)
  core.py       # SSH args / subprocess / port probing
  health.py     # periodic checks
  config.py     # TOML load/validate
  config.toml   # configuration
tests/          # pytest suite
_smoke_test.py  # offline smoke script
```

## Conventions

- **Follow the existing style**: plain Python with `from __future__ import
  annotations`, detailed docstrings on public methods.
- **Keep it offline-testable**: the test suite must not need a network
  connection or a real SSH server. If you touch `core.py` / `daemon.py`,
  add/adjust tests that mock `subprocess` or use fake configs.
- **Cross-platform awareness**: don't hardcode Windows paths or Linux-only
  commands. Platform-specific branches go behind `sys.platform` checks, and
  runtime files use the per-platform defaults from `config.py`.
- **No secrets in the repo**: never commit keys, tokens, `.env`, or real
  server addresses. `config.toml` ships with placeholders on purpose.

## Commit messages

Keep them concise and conventional; describe *why*, not just *what*:

```
type(scope): short summary

e.g. fix(daemon): fail loudly when service install is rejected
     feat(core): probe remote ports via python socket
     docs(readme): bilingual quick-start
```

Types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`.

## Before you open a change

1. `pytest` passes locally.
2. Coverage stays at/above the `fail_under` threshold.
3. No private keys or real endpoints in the diff.
4. If behaviour changed on a specific OS, say so in the description.

## Code of conduct

Be constructive. This is a small project — small, focused changes are easier
to review and land than large rewrites.

---

# 中文

## 环境准备

需要 **Python 3.11+**（使用内置 `tomllib`）。

```bash
git clone git@github.com:modusensus/ponte.git
cd ponte
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## 运行测试

```bash
pytest                          # 完整测试套件（config/core/daemon/health/main/retry）
pytest --cov=ponte --cov-report=term-missing   # 带覆盖率报告
python _smoke_test.py           # 零依赖冒烟检查
```

覆盖率在 `pyproject.toml` 里有 `fail_under` 阈值，请勿让它回落。CI 会在
Windows / Linux / macOS × Python 3.11 / 3.12 上跑同一套测试，并把覆盖率
上报到 Codecov。

## 项目结构

```
ponte/
  __init__.py   # 版本号
  main.py       # typer CLI
  daemon.py     # 生命周期、服务安装/卸载、优雅停止
  retry.py      # 重连状态机（退避 + 抖动）
  core.py       # SSH 参数 / 子进程 / 端口探测
  health.py     # 周期检查
  config.py     # TOML 加载/校验
  config.toml   # 配置
tests/          # pytest 测试套件
_smoke_test.py  # 离线冒烟脚本
```

## 约定

- **沿用现有风格**：纯 Python，公开方法带详细 docstring，文件头加
  `from __future__ import annotations`。
- **保持可离线测试**：测试套件不得依赖外网或真实 SSH 服务器。改动
  `core.py` / `daemon.py` 时，请用 mock `subprocess` 或假配置补/改测试。
- **跨平台意识**：不要硬编码 Windows 路径或 Linux 专属命令。平台差异走
  `sys.platform` 分支，运行时文件用 `config.py` 里的平台默认路径。
- **仓库不留密钥**：绝不提交 key、token、`.env` 或真实服务器地址。
  `config.toml` 里的占位符是有意保留的。

## 提交信息

简洁、符合常规格式，说明**为什么**而不只是**改了什么**：

```
type(scope): short summary

例如：fix(daemon): fail loudly when service install is rejected
     feat(core): probe remote ports via python socket
     docs(readme): bilingual quick-start
```

类型：`feat` / `fix` / `docs` / `test` / `refactor` / `chore`。

## 提交前检查

1. `pytest` 本地通过。
2. 覆盖率不低于 `fail_under` 阈值。
3. diff 里没有私钥或真实端点。
4. 若某个 OS 上行为有变化，请在描述里说明。

## 行为准则

请保持建设性。这是个小项目——小而聚焦的改动比大重写更容易评审与合入。
