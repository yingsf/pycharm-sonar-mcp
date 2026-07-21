# pycharm-code-quality-mcp

一个**本地**的 Model Context Protocol (MCP) 服务,把
[Codex App](https://github.com/openai/codex) / Codex CLI / [Claude Code](https://www.anthropic.com/claude-code)
连接到 PyCharm 的代码质量检查能力。

**默认后端是 PyCharm 内置的 JetBrains MCP Server(inspections)。**
**SonarQube for IDE 是自动探测的可选增强后端** —— 没有安装 Sonar 插件时,
工具仍然可以正常运行;安装了则同时执行两个后端,自动合并并去重。

```
Codex App / Codex CLI / Claude Code
                │
                │  stdio (MCP)
                ▼
      pycharm-code-quality-mcp
                │
        ┌───────┴────────┐
        │                │
        ▼                ▼
  JetBrains MCP     SonarQube for IDE
  (PyCharm 内置)     (可选增强插件)
  localhost HTTP    localhost HTTP
  (Streamable HTTP) (端口 64120–64130)
        │                │
        └───────┬────────┘
                ▼
      合并 + 确定性自动去重
                │
                ▼
        UnifiedFinding 列表
```

> **本项目不是 SonarSource 或 JetBrains 官方产品。** 它不包含、不分发、不修改
> 任何 SonarSource 分析器或 JetBrains 二进制组件。它只通过标准 MCP 协议调用
> 用户本机已开放的本地接口。它从不上传源代码,也不调用任何云端分析服务。

---

## 目录

- [产品定位](#产品定位)
- [命名规范](#命名规范)
- [两种后端策略](#两种后端策略)
- [MCP 工具一览(8 个)](#mcp-工具一览8-个)
- [JetBrains MCP Server 启用方式](#jetbrains-mcp-server-启用方式)
- [快速开始(macOS)](#快速开始macos)
- [快速开始(Windows)](#快速开始windows)
- [注册到 MCP 客户端(Codex/Claude)](#注册到-mcp-客户端codexclaude)
- [CLI 子命令](#cli-子命令)
- [doctor 三段诊断](#doctor-三段诊断)
- [自动去重机制](#自动去重机制)
- [严重程度归一化](#严重程度归一化)
- [单文件、多文件与 Git 变更分析](#单文件多文件与-git-变更分析)
- [工作区安全与多项目限制](#工作区安全与多项目限制)
- [Sonar 端口发现机制](#sonar-端口发现机制)
- [localhost 与 127.0.0.1、HTTP 421 与 IPv6](#localhost-与-127001http-421-与-ipv6)
- [推荐的 Agent 提示词](#推荐的-agent-提示词)
- [安全模型](#安全模型)
- [本地开发](#本地开发)
- [已知限制](#已知限制)

---

## 产品定位

- **JetBrains inspections 是基础能力。** 通过 PyCharm 自带的 MCP Server
  (`Tools → MCP Server`)暴露的 `get_file_problems`(必需)/ `get_project_status`(可选),
  获得与 IDE 完全一致的 inspection 结果。
- **SonarQube for IDE 是可选增强。** 若你安装了该插件,工具会自动探测并在
  `auto` 模式下同时运行 Sonar;两个来源的 findings 会经过确定性自动去重后返回。
- **源代码永不离开你的机器。** MCP 服务只与本机的 PyCharm 通信,不上传代码,
  也不调用任何云端分析服务。
- **每位开发者的 PyCharm 跟踪不同的工作树。** 因此**不能**把它部署为团队共享的
  单一远程服务 —— 桥接层必须与编辑器同处一台机器。

## 命名规范

| 概念 | 名称 |
| --- | --- |
| Git 仓库名 | `pycharm-code-quality-mcp` |
| Python 分发名 | `pycharm-code-quality-mcp` |
| Python 导入包 | `pycharm_code_quality_mcp` |
| 主命令 | `pycharm-code-quality-mcp` |
| MCP 服务名 | `pycharm-code-quality` |
| 显示名 | PyCharm Code Quality MCP |

## 两种后端策略

统一工具的 `backend_mode` 参数控制后端选择:

| `backend_mode` | 行为 |
| --- | --- |
| `auto`(默认) | JetBrains 优先;Sonar 可用时自动加入;合并 + 去重 |
| `jetbrains` | 只运行 JetBrains inspections |
| `sonar` | 只运行 SonarQube for IDE(降级模式,旧用户兼容) |
| `all` | 两个后端都必须尝试;一个失败时返回 `partialSuccess=true` |

**`auto` 模式判定逻辑**:

- JetBrains 可用 + Sonar 可用 → 两者并行,合并去重。
- JetBrains 可用 + Sonar 未安装 → JetBrains 单独成功,`success=true`,
  Sonar 状态 `unavailable`,添加 notice,**不算** partial failure。
- JetBrains 不可用 + Sonar 可用 → Sonar 降级运行,`degradedMode=true`。
- 两者都不可用 → `success=false`,错误码 `NO_ANALYSIS_BACKEND_AVAILABLE`。

## MCP 工具一览(8 个)

### 统一默认工具(5 个,README 与 Agent 指令推荐)

| 工具 | 作用 |
| --- | --- |
| `code_quality_status` | 报告两个后端的完整状态(configured / available / projectReady / indexing / instances) |
| `code_quality_analyze_files` | 分析 1–200 个绝对路径文件,默认 `backend_mode=auto`,确定性去重 |
| `code_quality_analyze_git_changes` | 收集 git 变更(staged/unstaged/untracked,相对 `base_ref`)并统一分析 |
| `code_quality_analyze_project` | 扫描整个仓库(默认所有 `.py`,tracked + untracked,尊重 `.gitignore`)并统一分析 |
| `code_quality_clear_cache` | 清除所有后端的内存缓存 |

返回 **`UnifiedFinding`** 模型:`id` / `sources` / `ruleIds` / `severity` /
`category` / `filePath` / `range` / `duplicateCount` / `deduplication` /
`sourceFindings`(完整保留所有原始来源)。

### JetBrains 专用工具(3 个)

| 工具 | 作用 |
| --- | --- |
| `jetbrains_ide_status` | 探测 JetBrains MCP 配置/连接/项目状态/必需工具是否暴露 |
| `jetbrains_inspect_files` | 用 JetBrains inspections 分析 1–200 个文件(单 session 复用,单文件失败不影响其他) |
| `jetbrains_inspect_git_changes` | 收集 git 变更并用 JetBrains inspections 分析 |

返回 JetBrains 原生 problem 列表(1-based 行列号),不做跨后端合并。

## JetBrains MCP Server 启用方式

1. 打开 PyCharm → **Settings → Tools → MCP Server**。
2. 勾选 **Enable MCP Server**。
3. 在 **Exposed Tools** 中启用:
   - `get_file_problems`(**必需** —— 分析能力的最小依赖)
   - `get_project_status`(**可选** —— PyCharm 2026.1+ 已不再暴露,缺失时
     `indexing` 状态降级为"未知",不影响 analyze)
4. 点击 **Copy HTTP Stream Config**(会复制一段 JSON 到剪贴板)。
5. 把这段 JSON 粘贴给配置命令:

   ```bash
   pycharm-code-quality-mcp jetbrains configure --json '<paste config here>'
   ```

   向导会自动识别 URL 和 headers(支持 flat / `transport` 嵌套 / `mcpServers`
   三种 JSON 形态),保存配置,然后真实连接做 `initialize` + `tools/list`
   校验,确认 `get_file_problems` 已暴露。

**多项目场景**:如果你在 PyCharm 里同时开了多个项目,`get_file_problems` 调用
必须带 `projectPath` 参数来消歧。本工具的 `*_files` / `*_git_changes` /
`*_project` 工具会自动把传入的 `project_root` 透传过去,无需手动配置。

**安全约束**:JetBrains URL 只允许 `localhost` / `127.0.0.1` / `::1`,
远程 IP、局域网地址、公网域名一律拒绝。headers 不会进入日志。POSIX 上配置文件
以 `0600` 权限保存。

## 快速开始(macOS)

```bash
# 1) 下载并安装二进制 + 自动注册到 Codex/Claude
curl -fsSL https://github.com/yingsf/pycharm-code-quality-mcp/releases/latest/download/install-macos.sh \
  | bash
```

安装脚本会在最后跑 `doctor`,如果检测到 JetBrains 还没配置,会**显式打印**
配置引导(含 JSON 样例)。接着做第 2 步:

```bash
# 2) 在 PyCharm 里:
#    Settings → Tools → MCP Server → 勾选 Enable MCP Server
#    在 Exposed Tools 里启用 get_file_problems(必需)
#    点击 "Copy HTTP Stream Config"(复制 JSON 到剪贴板)

# 3) 把 JSON 喂给 configure(保存配置 + 真实连接校验)
pycharm-code-quality-mcp jetbrains configure --json '<粘贴刚复制的 JSON>'
```

**PyCharm 复制的 JSON 三种常见形态**(configure 命令都支持):

```jsonc
// 形态 1: flat(最常见)
{"url":"http://127.0.0.1:64342/stream","headers":{}}

// 形态 2: transport 嵌套
{"transport":{"type":"streamable-http","url":"http://127.0.0.1:64342/stream","headers":{}}}

// 形态 3: mcpServers 嵌套
{"mcpServers":{"pycharm":{"url":"http://127.0.0.1:64342/stream","headers":{}}}}
```

> 端口号 `64342` 只是示例,实际值在 PyCharm 的 MCP Server 设置面板里显示。
> 如果你的 PyCharm 要求 token,会被放进 `headers` 字段(不会进入日志)。

```bash
# 4) 验证双后端都接通
pycharm-code-quality-mcp doctor
```

`doctor` 应显示 `[OK] JetBrains MCP configured` + `[OK] Project ready` +
`[OK] Code quality analysis available through JetBrains inspections`。
若仍报 `[WARN] Degraded mode`,说明 JetBrains 没接通,工具会退化为只用 Sonar。

Intel macOS 用 `upload-macos-x64.sh` 产物(架构会自动识别,无需手动选)。

## 快速开始(Windows)

```powershell
# 1) 下载并执行安装脚本
iex (irm https://github.com/yingsf/pycharm-code-quality-mcp/releases/latest/download/install-windows.ps1)

# 2) 在 PyCharm 里启用 MCP Server 并 Copy HTTP Stream Config(同上)

# 3) 配置 JetBrains
pycharm-code-quality-mcp jetbrains configure --json '<粘贴 JSON>'

# 4) 运行 doctor
pycharm-code-quality-mcp doctor
```

Windows 不需要 Python、Git Bash、WSL 或管理员权限。

## 注册到 MCP 客户端(Codex/Claude)

安装脚本(`install-macos.sh` / `install-windows.ps1`)会在装完二进制后**自动**
把本工具注册到 Codex 和 Claude Code(若已安装)。注册名统一为
`pycharm-code-quality`,传输方式 `stdio`。

正常情况下你**不需要手动跑这一节**——除非:

- 安装时跳过了自动注册(Codex/Claude 当时不在 PATH 上)
- 你想换 scope(user / project / local)
- 想写入团队共享的配置文件
- 自动注册失败,需要手动排查

### Codex CLI / Codex App

```bash
# 注册(用二进制的绝对路径,避免 PATH 依赖)
codex mcp add pycharm-code-quality -- "$HOME/.local/bin/pycharm-code-quality-mcp"

# 列出已注册的 MCP
codex mcp list

# 删除
codex mcp remove pycharm-code-quality
```

### Claude Code

```bash
# 注册到 user scope(对所有项目生效)
claude mcp add --transport stdio --scope user pycharm-code-quality \
  -- "$HOME/.local/bin/pycharm-code-quality-mcp"

# 列出
claude mcp list

# 删除
claude mcp remove pycharm-code-quality
```

### ⚠️ 重要:别和 PyCharm 自带的 MCP 混淆

PyCharm 2024.3+ 自带一个 **MCP Server**(URL 形式,直连 `http://127.0.0.1:port`),
如果你在 Codex/Claude 里手动加过名为 `pycharm` 的 URL 注册,那是**直连 PyCharm**,
**不是本工具**。两者区别:

| 注册名 | 类型 | 作用 |
| --- | --- | --- |
| `pycharm-code-quality` | **stdio** + 本工具二进制 | 双后端(JetBrains + Sonar)+ 去重 + 8 个 `code_quality_*` / `jetbrains_*` 工具 |
| `pycharm` (URL 形式) | HTTP/SSE 直连 PyCharm | PyCharm 暴露的 38 个原生工具(含执行类、改写类) |

本工具走 stdio + 白名单只读调用(`get_file_problems`),**不会**触碰 PyCharm 的
执行/改写工具。两个注册可以共存:Codex/Claude 既可以用本工具的 `code_quality_*`
做只读分析,也可以直接调 `pycharm` 的原生工具做执行。

### 配置文件位置(供团队推送 / 备份)

- **Codex**:`~/.codex/config.toml`(或 `config.json`,看版本),MCP 段在 `mcp_servers` 下
- **Claude Code**:`~/.claude.json`(user scope)或 项目 `.mcp.json`(project scope)

### 单独重跑自动注册

```bash
# macOS/Linux
./scripts/configure-codex.sh --force
./scripts/configure-claude.sh --force

# Windows
powershell -File scripts\configure-codex.ps1 -Force
powershell -File scripts\configure-claude.ps1 -Force
```

`--force` 会先 `remove` 旧的同名注册再 `add`,所以是幂等的。

## CLI 子命令

```
pycharm-code-quality-mcp                       # 等价于 serve
pycharm-code-quality-mcp serve                 # 运行 stdio MCP 服务(默认)
pycharm-code-quality-mcp --version
pycharm-code-quality-mcp doctor [--file PATH]  # 三段诊断,不启动 MCP
pycharm-code-quality-mcp setup [--json '...']  # 非交互式首次配置向导

pycharm-code-quality-mcp jetbrains configure [--json '...']
pycharm-code-quality-mcp jetbrains status
pycharm-code-quality-mcp jetbrains clear

pycharm-code-quality-mcp sonar status          # 扫描 Sonar 实例
```

环境变量:

| 变量 | 作用 |
| --- | --- |
| `JETBRAINS_MCP_URL` | JetBrains MCP 端点(覆盖配置文件) |
| `JETBRAINS_MCP_HEADERS_JSON` | 附加 headers(JSON) |
| `JETBRAINS_INSPECTION_TIMEOUT_MS` | 单文件 inspection 超时(默认 30000) |
| `JETBRAINS_MAX_FILES` | 单次调用最大文件数(上限 200) |
| `SONAR_IDE_PORT` | 显式指定 Sonar 端口 |
| `SONAR_WORKSPACE_ROOTS` | 允许的工作区根(os.pathsep 分隔) |
| `PYCHARM_CODE_QUALITY_MCP_LOG_LEVEL` | 日志级别(DEBUG/INFO/WARNING/ERROR) |

## doctor 三段诊断

```
PyCharm Sonar MCP Doctor

== General ==
[OK] Operating system: macOS 26.5 x64
[OK] MCP version: 1.0.1
...

== JetBrains ==
[OK] JetBrains MCP configured: http://localhost:63342/mcp
[OK] URL is loopback
[OK] Tools exposed: get_file_problems, get_project_status
[OK] Project ready

== Sonar ==
[OK] SonarQube for IDE found: ports 64120
[OK] HTTP authority (localhost): status OK (PyCharm)

== Summary ==
[OK] Code quality analysis available through JetBrains inspections

Result: 0 failure(s), 0 warning(s)
```

- **Sonar 未安装时 doctor 不整体失败**(只报 INFO/WARN)。
- **JetBrains 未配置但 Sonar 可用**:doctor 报 degraded 警告,Sonar 仍可作后端。
- **noqa 风格检查**:doctor 只扫描明确的项目范围: `--file` 所在目录、
  `SONAR_WORKSPACE_ROOTS`,或带有 `pyproject.toml` / `.git` 等项目标记的 cwd。
  非项目目录(例如 `$HOME`)会跳过,避免安装时递归扫描大目录。该检查会检测
  ruff 风格的 `# noqa: Sxxx` 注释(例如 `# noqa: S3776`)。**SonarQube for IDE
  不识别这种 ruff 风格的 noqa**,只认 `# NOSONAR`(全大写、整行抑制)。如果你写
  `# noqa: Sxxx` 想抑制某条 Sonar 规则,它会**静默失效**。doctor 会列出冲突位置并提示改用
  `# NOSONAR`。

## 自动去重机制

`code_quality_*` 工具默认 `deduplication_mode=balanced`。去重是**确定性**的
(相同输入永远得到相同输出,不联网、不调用模型),支持 4 种模式:
`conservative` / `balanced`(默认)/ `aggressive` / `off`。

**6 维相似度评分**(权重相加为 1.00):

| 维度 | 权重 | 信号来源 |
| --- | --- | --- |
| location | 0.30 | 同文件 + 范围交并比 / 起始行距离 |
| message | 0.25 | token Jaccard + SequenceMatcher + 标识符重合 |
| ruleEquivalence | 0.20 | 同规则或已知跨后端等价规则对 |
| anchor | 0.15 | 问题行±1 行代码的 SHA-256(不输出源码) |
| category | 0.07 | 18 个稳定 category 是否一致 |
| identifier | 0.03 | 消息中标识符集合的 Jaccard |

**4 个自动合并条件**(满足任一即合并):

- **A**:同文件 + 范围相交 + 规范化消息完全相同。
- **B**:同文件 + 显式规则等价 + 起始行距离 ≤ 2 + messageScore ≥ 0.55。
- **C**:同文件 + 代码锚点相同 + category 相同 + messageScore ≥ 0.82。
- **D**:综合得分 ≥ 阈值(balanced=0.86)+ 至少两个强信号成立。

**禁止合并**:不同文件、category 冲突(spelling↔type_mismatch、security↔style、
syntax_error↔documentation)、距离过远且无 anchor/规则等价、仅严重程度相同、
仅消息中出现同一变量名。

**聚类保护**:用 complete-link(组内最远两成员仍需满足合并条件),避免
"A~B、B~C 但 A!~C" 的传递链错误扩大分组。

**不丢失证据**:合并后的 `UnifiedFinding.sourceFindings` 完整保留所有原始来源;
中置信度(0.70–0.86)的对子进入 `possibleDuplicateGroups`,不自动合并。

## 严重程度归一化

两个后端的不同严重级别词汇表统一映射到 6 个稳定等级:

| 统一等级 | rank | Sonar | JetBrains |
| --- | --- | --- | --- |
| BLOCKER | 5 | BLOCKER | — |
| CRITICAL | 4 | CRITICAL | ERROR |
| MAJOR | 3 | MAJOR | WARNING、SERVER PROBLEM |
| MINOR | 2 | MINOR | WEAK WARNING、TYPO |
| INFO | 1 | INFO | INFORMATION |
| UNKNOWN | 0 | — | 其他 |

合并后问题的最终 severity 取所有来源中的**最高等级**。原始 severity 在
`sourceFindings.raw` 中完整保留。

## 单文件、多文件与 Git 变更分析

- **`*_analyze_files`**:1–200 个绝对路径,自动去重 + 稳定排序。
- **`*_analyze_git_changes`**:用 `git diff -z` / `git ls-files -z --others`
  收集变更(staged/unstaged/untracked,相对 `base_ref`),排除已删除文件、
  目录、不存在路径、工作区外路径。
- Git 调用一律使用参数数组(`shell=False`)、NUL 分隔、UTF-8 解码,正确处理
  空格、CJK、制表符与特殊字符。

## 工作区安全与多项目限制

- 路径必须在工作区根内(MCP Roots 或 `SONAR_WORKSPACE_ROOTS`)。
- **所有 `*_files` / `*_project` 工具都接受 `project_root` 参数作兜底**:即便 MCP
  客户端没声明 Roots、也没设环境变量,只要调用方传了 `project_root`,就会把它视为
  允许的工作区。这样在脚本/CLI 场景下也能直接用。
- 解析符号链接 / Windows junction 后检测逃逸。
- 跨平台路径规范化:Windows 盘符大小写、UNC、长路径;macOS 大小写敏感/不敏感 APFS。
- 单次调用所有文件必须归属同一项目根,否则返回 `MULTIPLE_PROJECT_ROOTS`。

## Sonar 端口发现机制

SonarQube for IDE 内嵌 HTTP 服务绑定 `127.0.0.1`,端口范围 `64120`–`64130`。
发现顺序:`SONAR_IDE_PORT` → project→port 内存缓存 → 端口扫描 + 文件探针匹配 →
缓存失效重试一次。

## localhost 与 127.0.0.1、HTTP 421 与 IPv6

Sonar 校验 HTTP `Host`/`authority`:`127.0.0.1:<port>` 会返回 HTTP 421
Misdirected Request,只接受 `localhost`。但某些系统上 `localhost` 会先解析为
`::1`(IPv6),而 IDE 只监听 IPv4。本项目用自定义 httpx transport:TCP 连到
`127.0.0.1`,同时保持 `Host: localhost` 头,完美解耦网络层与应用层权威。

## 推荐的 Agent 提示词

```
完成代码修改后,调用 code_quality_analyze_git_changes。

默认使用 JetBrains inspections;如果检测到 SonarQube for IDE,则同时执行 Sonar。

自动合并两个检查器报告的高置信度重复问题,但必须保留所有来源原始记录。

优先修复 CRITICAL 和 MAJOR 问题,然后重新检查。

不要通过关闭规则、添加 noqa、排除文件或忽略注释规避问题。

最终报告:
- 原始问题数
- 去重后问题数
- 合并重复数
- JetBrains 问题数
- Sonar 问题数
- 剩余问题
- 未修复原因
```

## 安全模型

- JetBrains URL 仅允许 loopback(`localhost` / `127.0.0.1` / `::1`)。
- Sonar 仅连接 `127.0.0.1`。
- 不上传代码、不保存源代码、不记录源码。
- 不代理任意 JetBrains 工具(白名单只有 `get_file_problems` /
  `get_project_status`),绝不开启 brave mode。
- 不执行 shell、不修改文件、不修改 PyCharm 设置、不修改 Sonar 规则、
  不自动添加 noqa、不自动忽略规则。
- 配置 header 不写日志。
- 去重读取的代码上下文:仅限已验证的允许文件、最多 3 行、只计算 hash、立即释放、不输出。
- 文件仍限制在 MCP Roots 或允许工作区内。

## 本地开发

```bash
git clone https://github.com/yingsf/pycharm-code-quality-mcp.git
cd pycharm-code-quality-mcp
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

架构(`src/pycharm_code_quality_mcp/`):

```
cli.py            setup / jetbrains / sonar / doctor / serve
server.py         注册 8 个工具 + stdio(瘦入口)
doctor.py         三段诊断
core/             workspace / path_utils / git_changes / file_context
backends/
  base.py         AnalysisBackend 抽象基类
  jetbrains/      client / config / parser / analyzer / models
  sonar/          client / discovery / transport / analyzer / models / result_summary
quality/
  models.py       UnifiedFinding / SourceFinding / QualityAnalysisResult / ...
  orchestrator.py 并行编排 + 合并 + 去重
  severity.py     6 级归一化
  categorization.py 18 个稳定 category
  normalization.py 路径 / 范围 / 消息规范化
  deduplication.py 6 维评分 + complete-link 聚类
  fingerprints.py 稳定 SHA-256 id
tools/
  _shared.py      workspace/roots/校验 helper
  _sonar_instances.py  把 sonar_tools 单例包装成 SonarBackend
  sonar_tools.py  Sonar 后端单例管理(client / discovery)
  jetbrains_tools.py 3 个 jetbrains_* 工具
  quality_tools.py 4 个 code_quality_* 工具(默认推荐)
```

## 已知限制

- 单次调用所有文件必须归属同一项目根。
- JetBrains inspection 一次只接收一个文件;本工具在一个 MCP session 内顺序调用,
  默认不并发(`JETBRAINS_MAX_CONCURRENCY=1`)。
- 去重是确定性启发式,不是语义级;中置信度对子会进入 `possibleDuplicateGroups`
  等待人工确认,不会自动合并。
- macOS 与 Windows 对等支持;Linux 未官方打包(可从源码运行)。

## License

MIT —— 见 [LICENSE](LICENSE)。
