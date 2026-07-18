# pycharm-sonar-mcp

一个**本地**的 Model Context Protocol (MCP) 服务,用于桥接
[Codex App](https://github.com/openai/codex) / Codex CLI / [Claude Code](https://www.anthropic.com/claude-code)
与本机 PyCharm 中安装的 **SonarQube for IDE** 插件。

它通过调用 SonarQube for IDE 在你本机已开放的本地 HTTP 接口,把 Sonar 代码分析结果暴露给你的 AI 编程助手,让助手能看到与 PyCharm 中完全相同的 findings,从而对自身改动进行验证。

```
Codex App / Codex CLI / Claude Code
                │
                │  stdio (MCP)
                ▼
       pycharm-sonar-mcp
                │
                │  localhost HTTP (端口 64120–64130)
                ▼
   PyCharm + SonarQube for IDE 插件
```

> **本项目不是 SonarSource 官方产品。** 本项目不包含、不分发、不修改任何
> SonarSource 分析器、插件、规则包或二进制组件。它仅调用用户本机 PyCharm 中
> SonarQube for IDE 已开放的本地 HTTP 接口。它从不上传源代码,也不调用任何云端分析服务。

---

## 目录

- [为什么必须在每位开发者本机安装](#为什么必须在每位开发者本机安装)
- [工作原理](#工作原理)
- [命名规范](#命名规范)
- [前置条件](#前置条件)
- [macOS — 安装](#macos--安装)
- [macOS — 配置 Codex](#macos--配置-codex)
- [macOS — 配置 Claude Code](#macos--配置-claude-code)
- [macOS — doctor](#macos--doctor)
- [macOS — 更新](#macos--更新)
- [macOS — 卸载](#macos--卸载)
- [macOS — 常见问题](#macos--常见问题)
- [Windows — 安装](#windows--安装)
- [Windows — 配置 Codex](#windows--配置-codex)
- [Windows — 配置 Claude Code](#windows--配置-claude-code)
- [Windows — doctor](#windows--doctor)
- [Windows — 更新](#windows--更新)
- [Windows — 卸载](#windows--卸载)
- [Windows — 常见问题](#windows--常见问题)
- [Sonar 端口发现机制](#sonar-端口发现机制)
- [localhost 与 127.0.0.1、HTTP 421 与 IPv6](#localhost-与-127001http-421-与-ipv6)
- [单文件、多文件与 Git 变更分析](#单文件多文件与-git-变更分析)
- [分批策略与部分失败语义](#分批策略与部分失败语义)
- [工作区安全与多项目限制](#工作区安全与多项目限制)
- [代理干扰](#代理干扰)
- [推荐的 Agent 提示词](#推荐的-agent-提示词)
- [本地开发](#本地开发)
- [已知限制](#已知限制)

---

## 为什么必须在每位开发者本机安装

- **SonarQube for IDE 运行在你的 PyCharm 内。** 它的嵌入式 HTTP 服务只绑定本机的回环地址(`127.0.0.1`,端口 `64120`–`64130`),无法从其他机器或共享服务器访问。
- **源代码永远不会离开你的机器。** MCP 服务只与你本机的 PyCharm 通信,不上传代码,也不调用任何云端分析服务。
- **每位开发者的 PyCharm 跟踪不同的工作树。** 单一远程服务器无法知道你打开了哪个项目、哪些文件已被索引。桥接层必须与编辑器同处一台机器。

因此**不能**把它部署为团队共享的单一远程服务。

## 工作原理

1. 带有 SonarQube for IDE 的 PyCharm 在回环端口的 `64120`–`64130` 范围内开放 `GET /sonarlint/api/status` 与 `POST /sonarlint/api/analysis/files` 两个接口。
2. `pycharm-sonar-mcp` 扫描该端口范围,验证每个响应服务确实属于 Sonar,根据目标文件匹配到正确的 PyCharm 实例,然后调用分析接口并把 findings 返回给 MCP 客户端(Codex/Claude)。
3. findings 保留 Sonar 的原始字段:`ruleKey`、`message`、`severity`、`filePath`、`textRange`。未来插件版本可能返回的未知字段会被兼容保留。

服务仅使用 **stdio** 传输,绝不绑定公网或局域网地址;`stdout` 专门用于 MCP JSON-RPC 通信,所有日志写入 `stderr`。

## 命名规范

| 概念 | 名称 |
| --- | --- |
| Git 仓库名 | `pycharm-sonar-mcp` |
| Python 分发名 | `pycharm-sonar-mcp` |
| Python 导入包名 | `pycharm_sonar_mcp` |
| 命令行程序名 | `pycharm-sonar-mcp` |
| MCP 服务配置名 | `pycharm-sonar` |

## 前置条件

- **PyCharm** 处于运行状态,已安装 **SonarQube for IDE** 插件,并已打开你的项目。
- 插件后端必须已启动:在 PyCharm 中至少打开过一个源文件,触发 Sonar 的首次分析(本地 HTTP 服务只有在此之后才会出现)。
- 已安装 **Codex App / Codex CLI** 和/或 **Claude Code**(可选,但通常是预期场景)。
- 第一版正式支持平台:**macOS 13+(Apple Silicon 或 Intel)** 与 **Windows 11 x64**。Linux 源码级兼容,但不是第一版正式发布平台。

---

## macOS — 安装

安装位置:`~/.local/bin/pycharm-sonar-mcp`(无需 `sudo`,不向家目录外写入任何内容)。

```bash
curl -fsSL https://raw.githubusercontent.com/yingsf/pycharm-sonar-mcp/main/scripts/install-macos.sh \
  | bash
```

或在克隆仓库后:

```bash
bash scripts/install-macos.sh
```

安装脚本会:

- 检测 `arm64` 还是 `x64`,
- 下载匹配的二进制文件和 `SHA256SUMS`,
- 校验 SHA-256(校验失败时保留已有旧版本不动),
- 原子替换旧二进制,
- 尽力注册 Codex 和 Claude Code(未安装时仅告警,不阻断),
- 运行 `doctor`。

需要 Bash 3.2+(系统自带的 `/bin/bash` 即可)和 `curl` 或 `wget`。

## macOS — 配置 Codex

如果安装脚本未能发现 Codex,可手动用二进制的绝对路径注册:

```bash
codex mcp add pycharm-sonar -- "$HOME/.local/bin/pycharm-sonar-mcp"
```

重新执行 `scripts/configure-codex.sh --force` 可更新注册项。该脚本幂等,使用二进制绝对路径(不依赖 `PATH`)。注册完成后需**重启 Codex App** 或重载 Codex CLI MCP。

验证:

```bash
codex mcp list
```

## macOS — 配置 Claude Code

```bash
claude mcp add \
  --transport stdio \
  --scope user \
  pycharm-sonar \
  -- "$HOME/.local/bin/pycharm-sonar-mcp"
```

或重新执行 `scripts/configure-claude.sh --force`。注册完成后需在 Claude Code 中**重载 MCP**。

验证:

```bash
claude mcp list
```

## macOS — doctor

```bash
pycharm-sonar-mcp doctor
pycharm-sonar-mcp doctor --file /absolute/path/to/your/file.py
```

`doctor` 会打印操作系统/架构/版本、localhost IPv4 状态、`64120`–`64130` 上的 Sonar 实例、HTTP authority 行为(421 检查)、代理干扰、Codex/Claude 是否存在、Git 是否存在以及工作区配置。任一硬性检查失败即返回非零退出码。它**不会**启动 MCP 服务。

## macOS — 更新

重新执行安装脚本即可:它会下载最新发布、校验 checksum 并原子替换二进制:

```bash
bash scripts/install-macos.sh
```

如需固定版本:

```bash
PYCHARM_SONAR_MCP_VERSION=v0.1.0 bash scripts/install-macos.sh
```

## macOS — 卸载

```bash
bash scripts/uninstall-macos.sh                 # 仅删除二进制
bash scripts/uninstall-macos.sh --purge         # 同时移除 Codex 与 Claude 注册项
```

卸载绝不会删除 PyCharm、SonarQube for IDE 插件或其他 MCP 服务。

## macOS — 常见问题

- **`pycharm-sonar-mcp doctor` 提示找不到 Sonar 实例。** 请在 PyCharm 中打开你的项目,并至少打开一个源文件以触发 Sonar 后端启动。
- **`IDE_MULTIPLE_MATCHES`。** 同时打开了多个 PyCharm 项目窗口。请关闭重复窗口,或为排障设置 `SONAR_IDE_PORT=6412X`。
- **HTTP 421 / Misdirected Request。** 正常情况下不应出现 — 自定义 transport 始终发送 `Host: localhost:<port>`。如遇到请提 issue,这通常意味着 transport 出现回归。
- **编辑器看不到工具。** 注册完成后请重启 Codex App 或重载 Claude Code MCP。
- **需要在防火墙开放端口吗?** 不需要。`64120`–`64130` 仅属于本机回环服务,**不应**对外开放。

---

## Windows — 安装

安装位置:`%LOCALAPPDATA%\pycharm-sonar-mcp\pycharm-sonar-mcp.exe`
(无需管理员权限、不写入 `Program Files`、不修改系统级 `PATH`)。

**PowerShell**(默认方式;需要 Windows PowerShell 5.1+ 或 PowerShell 7+):

```powershell
irm https://raw.githubusercontent.com/yingsf/pycharm-sonar-mcp/main/scripts/install-windows.ps1 | iex
```

或在克隆仓库后:

```powershell
pwsh -File scripts\install-windows.ps1
```

安装脚本会:

- 下载 `pycharm-sonar-mcp-windows-x64.exe` 与 `SHA256SUMS`,
- 校验 SHA-256(`Get-FileHash`;校验失败时保留旧版本不动),
- 原子替换旧 `.exe`,
- 尽力注册 Codex 和 Claude Code(未安装时仅告警),
- 运行 `doctor`。

**ExecutionPolicy:** 上面的单行命令不会永久修改你的 `ExecutionPolicy`。若直接运行 `.ps1` 被策略拦截,可使用:

```powershell
pwsh -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```

**Windows 路径格式:** 安装脚本与工具同时接受 `C:\Project\src\a.py` 与混合分隔符(`C:/Project/src/a.py`)。盘符大小写(`c:` 与 `C:`)以及大小写不敏感比较已正确处理。

## Windows — 配置 Codex

```powershell
codex mcp add pycharm-sonar -- "$env:LOCALAPPDATA\pycharm-sonar-mcp\pycharm-sonar-mcp.exe"
```

或重新执行 `scripts\configure-codex.ps1 -Force`。使用 `.exe` 的绝对路径(不依赖 `PATH`)。注册后需**重启 Codex App**。

验证:

```powershell
codex mcp list
```

## Windows — 配置 Claude Code

```powershell
claude mcp add `
  --transport stdio `
  --scope user `
  pycharm-sonar `
  -- "$env:LOCALAPPDATA\pycharm-sonar-mcp\pycharm-sonar-mcp.exe"
```

或重新执行 `scripts\configure-claude.ps1 -Force`。注册后需在 Claude Code 中**重载 MCP**。

验证:

```powershell
claude mcp list
```

## Windows — doctor

```powershell
& "$env:LOCALAPPDATA\pycharm-sonar-mcp\pycharm-sonar-mcp.exe" doctor
& "$env:LOCALAPPDATA\pycharm-sonar-mcp\pycharm-sonar-mcp.exe" doctor --file C:\Path\To\file.py
```

输出与检查项与 macOS 的 `doctor` 完全对等(操作系统行会显示 `Windows ... x64`)。任一硬性检查失败即返回非零退出码。它**不会**启动 MCP 服务。

## Windows — 更新

```powershell
pwsh -File scripts\install-windows.ps1 -Force
```

如需固定版本:

```powershell
pwsh -File scripts\install-windows.ps1 -Version v0.1.0
```

## Windows — 卸载

```powershell
pwsh -File scripts\uninstall-windows.ps1                 # 仅删除二进制
pwsh -File scripts\uninstall-windows.ps1 -Purge          # 同时移除 Codex 与 Claude 注册项
```

绝不会删除 PyCharm、SonarQube for IDE 插件或其他 MCP 服务。

## Windows — 常见问题

- **需要哪个 PowerShell 版本?** Windows PowerShell 5.1(Windows 10/11 自带)或 PowerShell 7+。无论哪种,安装脚本都会打印其各项检查结果。
- **需要永久修改 ExecutionPolicy 吗?** 不需要。使用 `irm | iex` 单行命令,或对单次调用使用 `-ExecutionPolicy Bypass`。
- **没有安装 Python 能用吗?** 能。发布的产物是 PyInstaller 产出的独立 `.exe`,不需要 Python、Git Bash 或 WSL。
- **长路径/空格/中文用户名?** 全部支持。盘符大小写已归一;Junction/符号链接逃逸会被拒绝。
- **需要在 Windows Defender 防火墙开放 64120–64130 端口吗?** 不需要。它们只属于本机回环,对外开放属于安全风险且无必要。

---

## Sonar 端口发现机制

端口**绝不是**写死的 `64120`。发现策略按以下顺序进行:

1. **显式端口** — 若设置了 `SONAR_IDE_PORT`,它必须是 `64120..64130` 内的整数,且必须是有效的 Sonar 服务。否则调用会明确报错(不静默切换到其他端口)。该变量仅用于排障。
2. **项目端口缓存** — 内存中的 `project_root → port` 映射(从不持久化到磁盘)。
3. **扫描** `64120..64130`,对每个响应者调用 `GET /sonarlint/api/status` 进行校验。仅当返回 HTTP 200、可解析为 JSON、是 JSON 对象,且对象看起来属于 Sonar 状态响应(含 `ideName`、`version`、`connectedMode` 等可识别字段)时才视为有效。仅凭 TCP 端口可连接**不算**有效。
4. **匹配** — 若存在多个实例,用单个目标文件对每个实例探针调用(`POST /sonarlint/api/analysis/files`),只保留能够分析该文件的实例。返回"文件未索引"的实例会被排除。
5. **缓存** 解析得到的 `project_root → port`。
6. **清缓存并重试一次** — 仅在连接错误、超时、404、421、非 JSON 或非 Sonar 响应时触发。从不进行超过一次的重试。对于"文件未索引"、"文件类型不支持"、"正在 Indexing"、"被限流"等错误,不视为端口变化。

当**多个实例都能索引该文件**时,工具返回 `IDE_MULTIPLE_MATCHES`,建议关闭重复项目窗口或设置 `SONAR_IDE_PORT`。

## localhost 与 127.0.0.1、HTTP 421 与 IPv6

SonarQube for IDE 会校验 HTTP `Host`/authority:

- 携带 `Host: 127.0.0.1:<port>` 的请求会返回 **HTTP 421 Misdirected Request**。只有 `Host: localhost:<port>` 才被接受。
- 部分系统中 `localhost` 会优先解析为 `::1`(IPv6),而插件可能只绑定 `127.0.0.1`(IPv4)。朴素客户端会因此走 IPv6 而失败。

`pycharm-sonar-mcp` 通过一个**自定义 HTTP transport** 同时解决这两个问题:

- 向 `127.0.0.1:<port>` 打开 TCP 套接字(始终走 IPv4 回环),
- 请求中发送 `Host: localhost:<port>` 与 `Origin: http://localhost`,
- 从不把 URL 永久改写为 `http://127.0.0.1`,
- 忽略 `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`(`trust_env=False`)。

因此网络层走 IPv4 回环,应用层 authority 保持 `localhost`。IPv4-only 绑定、IPv6-first 解析以及 421 行为均有对应单测覆盖。

## 单文件、多文件与 Git 变更分析

- **`sonar_analyze_files`** — 传入 1–200 个**绝对**路径。路径必须真实存在、是普通文件,且位于配置的工作区内。它们会被规范化、去重并排序。同一调用中包含来自多个项目根目录的文件会被拒绝(`MULTIPLE_PROJECT_ROOTS`)。
- **`sonar_analyze_git_changes`** — 传入 `project_root`(以及可选的 `base_ref`、`include_untracked`、`include_staged`、`include_unstaged`)。它使用 NUL 分隔的 git 输出(`git diff --name-only -z`、`git ls-files -z` 等)收集变更文件,从不使用 `shell=True`,从不使用 `splitlines()`。已删除文件、目录和不存在路径会被排除;结果去重并按工作区过滤。

## 分批策略与部分失败语义

文件按**每批 50 个顺序执行**(不并发 — 不能压垮 PyCharm 的分析进程)。对外仍表现为一次 MCP 调用。

- 所有批次的 findings 会被合并。
- 某批次失败(超时、5xx、非 JSON 等)时,保留已成功的结果并返回 `partialSuccess: true`,同时在 `batchErrors` 与按文件的 `failedFiles` 中说明失败原因。
- 空 findings **不等于**分析成功 — 分析干净无问题的文件会被报告为 `status: "analyzed"`、`findingCount: 0`,与"未索引"明确区分。

结果结构包含 `requestedFileCount`、`analyzedFileCount`、`skippedFileCount`、`failedFileCount`、`findingCount`、`severityCounts`、`fileSummaries`、`findings`、`durationMs`。Git 分析还会附加 `projectRoot`、`baseRef`、`changedFileCount`。

## 工作区安全与多项目限制

服务只分析允许工作区内的文件。工作区根目录来源于:

1. **MCP 客户端 Roots**(首选),或
2. `SONAR_WORKSPACE_ROOTS` — 一个或多个绝对路径,使用操作系统路径分隔符(macOS 用 `:`,Windows 用 `;`)分隔。

示例:

```bash
# macOS
export SONAR_WORKSPACE_ROOTS="/Users/me/project1:/Users/me/project2"
# Windows (PowerShell)
$env:SONAR_WORKSPACE_ROOTS = "C:\Project1;D:\Project2"
```

如果两者都不可用,分析会被拒绝并返回 `WORKSPACE_NOT_CONFIGURED`。

工作区归属检查基于**真实**解析路径,因此符号链接与 Windows Junction 逃逸工作区会被拒绝(`SYMLINK_ESCAPE`)。第一版要求同一调用中的所有文件属于同一个项目根目录。

**worktree 不一致:** 若 Codex/Claude 工作在与 PyCharm 打开的项目不同的 worktree 中,Sonar 会把文件报告为未索引。请在两者中打开同一个物理工作目录。

## 代理干扰

如果你的 shell 设置了 `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`,这些变量对所有回环调用都会被**忽略**(`trust_env=False`)。`doctor` 在检测到代理变量时会输出告警,让你知道它们存在于环境中但未被使用。

---

## 推荐的 Agent 提示词

把以下内容放入你的助手任务说明或提交信息检查清单:

```text
完成代码修改后,使用 PyCharm Sonar MCP 分析本次所有改动文件。
修复能够安全处理的 CRITICAL 和 MAJOR 问题,然后重新分析。
不要通过关闭规则、添加 noqa、排除文件或忽略注释来规避问题。
如果仍有问题,报告规则编号、严重程度、文件和行号。
```

你也可以在 `AGENTS.md` / `CLAUDE.md` 中加入 `## Sonar verification` 段落:

```markdown
## Sonar verification

After modifying production source code:

1. Analyze all changed source files with the PyCharm Sonar MCP.
2. Fix CRITICAL and MAJOR findings when the fix is safe.
3. Re-run analysis after fixes.
4. Do not suppress, disable or exclude rules merely to pass analysis.
5. Report all remaining findings with rule key, severity, file and line.
6. Do not claim that Sonar passed unless the MCP tool was actually called.
```

---

## 本地开发

需要 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
uv run pycharm-sonar-mcp doctor
uv run pycharm-sonar-mcp serve
```

本地构建独立二进制:

```bash
uv pip install pyinstaller
uv run pyinstaller --noconfirm --clean pyinstaller/pycharm-sonar-mcp.spec
```

`.github/workflows/test.yml` 中的 CI 会跑完整矩阵(Ubuntu/macOS/Windows × Python 3.11–3.13),外加 ShellCheck/PowerShell 语法检查,以及每个平台的 PyInstaller 构建 + 冒烟测试。

## 已知限制

- **PyCharm 必须运行**,且项目已打开;SonarQube for IDE 必须已安装。
- Sonar 后端必须已启动(先在 PyCharm 中打开过一个源文件)。
- 文件必须已被当前 IDE 实例**索引**。
- Codex/Claude 与 PyCharm 必须操作**同一个物理工作目录**。
- **第一版:** 一次 `sonar_analyze_files` 调用只支持单一项目根目录(请分别分析每个项目)。
- 本工具**不提供** SonarQube Server 的 Quality Gate、历史趋势或覆盖率管理功能。
- **平台状态:** macOS(arm64、x64)与 Windows 11 x64 是第一版正式发布目标,并在 CI 中实际执行。Linux 源码级兼容(Ubuntu CI 跑通测试),但不发布预编译二进制。任何未在此列出的平台均为 **Experimental**。
