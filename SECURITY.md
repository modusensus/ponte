# Security Policy

---

# English

## Reporting a vulnerability

If you believe you have found a security vulnerability in ponte, **do not
open a public issue** — report it privately so it can be fixed before it is
widely known.

Please email the details to:

**guanqishi26@gmail.com**

You can expect an initial acknowledgement within **72 hours**, and a plan for
a fix (or a reason it is not a vulnerability) as soon as the details are
confirmed.

### What to include

- Project name and version (see `ponte/__init__.py`).
- Description of the vulnerability and why it matters.
- Steps to reproduce, ideally with a minimal reproducer.
- Impact: what an attacker could do, and under what conditions.
- If known, suggested fixes or mitigations.

### What we do

1. Acknowledge the report within 72 hours.
2. Investigate and confirm the issue.
3. Prepare a fix / workaround and, if appropriate, a coordinated disclosure.
4. Credit the reporter (unless they prefer to stay anonymous).

## Supported versions

This is a small project maintained in a single branch (`main`). Security
fixes are released in the latest commit; there are no long-term-support
branches. If you rely on ponte in production, keep it up to date and pin what
you run.

## Security-relevant behaviour

Things worth knowing when running ponte:

- **The private key must never be committed.** `.gitignore` excludes
  `id_rsa` / `id_rsa.pub`; keys are read from the path in `config.toml`.
- **`config.toml` is real configuration, not a template.** It holds the SSH
  host/user and key paths. Commit placeholders, not real endpoints.
- **Remote-port probing runs commands on your SSH server.** Only configure
  servers you trust; the probe uses a Python socket connect first and falls
  back to `ss`/`lsof`/`netstat`.
- **The daemon runs as your user** (or SYSTEM via the Windows Scheduled
  Task). Protect access to the machine and to `config.toml`.

---

# 中文

## 报告漏洞

如果你认为在 ponte 中发现了安全漏洞，**请勿直接开公开 issue**——请私下
报告，以便在公开前完成修复。

请将详情发送至：

**guanqishi26@gmail.com**

收到后 **72 小时内**你会收到初步确认；确认详情后，会给出修复方案（或说明
为何不是漏洞）。

### 请附上

- 项目名称与版本（见 `ponte/__init__.py`）。
- 漏洞描述及为何重要。
- 复现步骤，最好附最小复现样例。
- 影响面：攻击者能在什么条件下做什么。
- 如已知，建议的修复或缓解方案。

### 我们会

1. 72 小时内确认收到。
2. 调查并核实问题。
3. 准备修复 / 规避方案，必要时协调披露时间。
4. 为报告者署名致谢（除非对方希望匿名）。

## 受支持版本

这是一个小项目，在单一分支（`main`）上维护。安全修复随最新提交发布；
没有长期支持（LTS）分支。若你在生产环境依赖 ponte，请保持更新并固定
所运行的版本。

## 与安全相关的行为

运行 ponte 时值得注意的事项：

- **私钥绝不可入库。** `.gitignore` 已排除 `id_rsa` / `id_rsa.pub`；密钥从
  `config.toml` 中指定的路径读取。
- **`config.toml` 是真实配置而非模板。** 它包含 SSH host/user 与密钥路径。
  请提交占位符，而非真实端点。
- **远程端口探测会在你的 SSH 服务器上执行命令。** 只配置你信任的服务器；
  探测优先用 Python socket 连接，回退到 `ss`/`lsof`/`netstat`。
- **守护进程以你的用户身份运行**（或经 Windows 计划任务以 SYSTEM 运行）。
  请保护好机器与 `config.toml` 的访问权限。
