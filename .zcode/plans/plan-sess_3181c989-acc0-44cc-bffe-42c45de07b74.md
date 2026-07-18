# pycharm-code-quality-mcp v1.0.0 升级实施计划

## 已完成(零、一)
- ✅ GitHub 仓库改名:`yingsf/pycharm-sonar-mcp` → `yingsf/pycharm-code-quality-mcp`
- ✅ 本地 git remote 更新(SSH 风格),`git ls-remote origin HEAD` 通过
- ⏳ 本地目录名暂不改(标 LOCAL_DIRECTORY_RENAME_REQUIRED,后续手动 `mv`)

## 关键决策(已与你确认)
- 版本号:**v1.0.0**(主版本升级)
- 本地目录:不改(运行中无法安全 self-rename)
- 发版:push 后打 `v1.0.0` → CI 构建 arm64+Windows → `upload-macos-x64.sh` 本地补 Intel(复用现有脚本,脚本内 REPO/artifact 名会随改名更新)

## 新命名映射表
| 概念 | 旧 | 新 |
|---|---|---|
| GitHub 仓库 / 分发名 | pycharm-sonar-mcp | **pycharm-code-quality-mcp** |
| Python 导入包 | pycharm_sonar_mcp | **pycharm_code_quality_mcp** |
| 主命令 | pycharm-sonar-mcp | **pycharm-code-quality-mcp**(保留旧命令作兼容入口) |
| MCP server name | pycharm-sonar | **pycharm-code-quality** |
| 产品显示名 | PyCharm Sonar MCP | **PyCharm Code Quality MCP** |
| 日志级别 env | PYCHARM_SONAR_MCP_LOG_LEVEL | **PYCHARM_CODE_QUALITY_MCP_LOG_LEVEL**(旧名保留兼容) |

## 架构(目标)
```
src/pycharm_code_quality_mcp/
├── __init__.py / __main__.py / _pyi_entry.py
├── cli.py            # 新增 setup / jetbrains / sonar 子命令
├── server.py         # 只负责 FastMCP 构建+工具注册+stdio(瘦身后)
├── config.py         # platformdirs 配置目录 + config.json
├── errors.py         # 扩展 JetBrains/NO_ANALYSIS_BACKEND 错误码
├── logging_config.py
├── core/
│   ├── workspace.py / path_utils.py / git_changes.py / file_context.py
├── backends/
│   ├── base.py                          # AnalysisBackend 抽象基类
│   ├── jetbrains/{client,config,models,parser,analyzer}.py
│   └── sonar/{client,discovery,models,analyzer}.py   # 从现有代码迁入
├── quality/
│   ├── models.py        # UnifiedFinding/QualityAnalysisResult/BackendStatus...
│   ├── orchestrator.py  # 编排两个后端+合并
│   ├── severity.py / normalization.py / categorization.py
│   ├── deduplication.py / fingerprints.py
│   └── resources/rule_equivalences.json
└── tools/
    ├── sonar_tools.py       # 4 个旧工具(契约不变)
    ├── jetbrains_tools.py   # 3 个新 JetBrains 工具
    └── quality_tools.py     # 4 个统一工具(默认推荐)
```
旧包 `pycharm_sonar_mcp/` 作为**兼容包装层**(纯 re-export,不维护第二套业务逻辑),旧命令 `pycharm-sonar-mcp` 调用同一套新实现并输出迁移提示。

## 实施步骤(22 步,严格顺序)

### A. 包结构 + 改名(步骤 1-4)
1. 新建 `src/pycharm_code_quality_mcp/` 包结构(上述目录树),核心模块从旧位置**移动**(不是复制)到新位置:
   - `core/path_utils.py` ← 旧 path_utils.py
   - `core/workspace.py` ← 旧 workspace.py
   - `core/git_changes.py` ← 旧 git_changes.py
   - `core/file_context.py` ← 新建(代码锚点读取,3 行限)
   - `backends/sonar/{client,discovery}.py` ← 旧 sonar_client.py / ide_discovery.py(拆分)
   - `backends/sonar/models.py` ← 旧 models.py 的 Sonar 部分
   - `backends/sonar/analyzer.py` ← 新建(SonarBackend 包装)
   - `errors.py` / `logging_config.py` ← 移动 + 扩展
2. 旧包 `pycharm_sonar_mcp/` 改写为纯兼容层:每个模块 `from pycharm_code_quality_mcp.* import *`,加迁移提示。保留 `__version__`。
3. 全仓库字符串替换(分四类精确替换,避免误伤):
   - `pycharm-sonar-mcp` → `pycharm-code-quality-mcp`(分发名/命令/仓库 URL)
   - `pycharm_sonar_mcp` → `pycharm_code_quality_mcp`(导入包)
   - `pycharm-sonar`(MCP name) → `pycharm-code-quality`
   - `github.com/yingsf/pycharm-sonar-mcp` → `github.com/yingsf/pycharm-code-quality-mcp`
   - **保留旧名的位置**:兼容层模块、CHANGELOG 迁移历史、README 升级说明、兼容测试、`PYCHARM_SONAR_MCP_LOG_LEVEL`(旧 env 兼容)
4. `pyproject.toml`:name/version(0.1.0→1.0.0)/scripts(加新命令入口,保留旧)/packages 指向新包/hatch wheel 包含两个包目录/新增依赖 `platformdirs`/urls 更新。

### B. JetBrains backend(步骤 5-8)
5. `backends/jetbrains/config.py`:用 platformdirs 读 `~/Library/Application Support/pycharm-code-quality-mcp/config.json`(macOS)/ `%LOCALAPPDATA%\pycharm-code-quality-mcp\config.json`(Windows)。config.json 结构:`{jetbrains:{transport,url,headers}}`。支持 `JETBRAINS_MCP_URL`/`JETBRAINS_MCP_HEADERS_JSON` env 覆盖。文件权限 0600(POSIX)。
6. `backends/jetbrains/client.py`:基于 `mcp.client.streamable_http.streamable_http_client` + `mcp.ClientSession`。**只允许 loopback**(localhost/127.0.0.1/::1,拒绝远程/局域网/公网)。一次 Session 内顺序调用 `get_project_status` + 多次 `get_file_problems`,Session 用完即关。**白名单**:只允许 `get_project_status`/`get_file_problems`,禁止代理任何修改型工具。
7. `backends/jetbrains/parser.py`:解析 `CallToolResult`(优先 structuredContent → JSON TextContent → 文本兼容解析)。产出 `JetBrainsProblem` 模型。保留 raw。
8. `backends/jetbrains/analyzer.py` + `models.py`:`JetBrainsBackend(AnalysisBackend)`,实现 `analyze_files(paths)` 顺序调用,单文件失败不丢其他结果。

### C. 统一模型 + 去重引擎(步骤 9-13)
9. `quality/models.py`:`SourceFinding`/`UnifiedRange`/`UnifiedFinding`(含 id/sources/ruleIds/severity/severityRank/message/category/range/duplicateCount/deduplication/sourceFindings)/`BackendStatus`/`QualityAnalysisResult`/`DeduplicationInfo`/`DuplicateGroup`。
10. `quality/severity.py`:归一化等级(0-5)+ Sonar/JetBrains → 统一 severity 映射表。
11. `quality/categorization.py` + `resources/rule_equivalences.json`:18 个稳定 category;规则等价表(Sonar ruleKey ↔ JetBrains inspectionId ↔ canonical category);确定性 message pattern 归类。
12. `quality/normalization.py`:路径/范围(统一 1-based)/消息(Unicode NFKC+lowercase+trim+合并空格+去结尾标点+统一引号+`<str>`/`<num>` 替换+identifier 提取,兼容中文)。
13. `quality/deduplication.py` + `fingerprints.py`:候选分组(同文件+范围相交/起始行≤2/anchor 相同/规则等价);6 维相似度特征(balanced 权重);4 个自动合并条件(A/B/C/D);中置信度 possibleDuplicateGroups;受约束聚类(complete-link,防传递闭包错误合并);代表问题选择;稳定 SHA-256 ID(不用 hash())。4 种 deduplication_mode。

### D. 编排 + 工具 + CLI(步骤 14-16)
14. `quality/orchestrator.py`:`QualityOrchestrator`,backend_mode(auto/jetbrains/sonar/all);后端隔离并行执行(anyio task group,单后端异常不取消另一个);合并+去重;success/partialSuccess/degradedMode 语义。
15. `tools/`:
   - `sonar_tools.py`:4 个旧工具(契约完全不变,内部转发到 sonar analyzer)
   - `jetbrains_tools.py`:`jetbrains_ide_status`/`jetbrains_inspect_files`/`jetbrains_inspect_git_changes`
   - `quality_tools.py`:`code_quality_status`/`code_quality_analyze_files`/`code_quality_analyze_git_changes`/`code_quality_clear_cache`(默认推荐)
   - server.py 注册全部 11 个工具
16. `cli.py`:新增 `setup` / `jetbrains configure|status|clear` / `sonar status` 子命令。configure 向导接收完整 JSON、自动拆 URL+headers、initialize、tools/list、校验两个必需工具存在、保存。doctor 扩展为分三段(General/JetBrains/Sonar),Sonar 未装不算失败。

### E. 脚本 + CI + 打包(步骤 17)
17. 全部脚本改名:`install-macos.sh`/`install-windows.ps1` 的 PROG_NAME/下载 URL/REPO 更新;新增迁移逻辑(检测旧 `pycharm-sonar-mcp` 二进制和旧 MCP 配置 `pycharm-sonar`,迁移到新名,不破坏其他 MCP);`configure-codex.sh`/`configure-claude.sh` 的 MCP_NAME 改 `pycharm-code-quality`;`upload-macos-x64.sh` 的 REPO/ASSET_NAME 更新;`.github/workflows/test.yml`+`release.yml` 的 artifact 名/路径更新;`pyinstaller/pycharm-code-quality-mcp.spec`(重命名)+hiddenimports 更新。

### F. 测试 + 文档(步骤 18-19)
18. 测试(全量新增/改写):
   - JetBrains:配置有效/无效、远程 URL 拒绝、loopback 允许、headers 解析、initialize 失败、tools/list、缺 get_file_problems/缺 get_project_status、project indexing、structuredContent/JSON TextContent/文本解析、bad response、timeout、单文件失败、多文件部分失败、Session 只创建一次/必须关闭、不调白名单外工具(Mock ClientSession)
   - 编排:jetbrains only / sonar 降级 / 两者 / 都不可用 / auto / all / 一后端失败 / partialSuccess / degradedMode
   - 去重:完全相同/相同规则/同行不同列/anchor 相同/unused parameter 两来源合并/同名变量不同问题不合并/不同文件不合并/category 冲突不合并/高/中/低置信度/severity 取最高/sourceFindings 保留/stable ID/顺序稳定/无传递链错误扩大/4 种 mode/统计字段
   - 路径/Git:现有全部测试改导入路径后继续通过
   - stdio:stdout 无日志/握手/旧命令入口/新命令入口
19. README 完整重写(以 JetBrains 为默认)+ CHANGELOG v1.0.0 + 迁移说明。

### G. 质量门禁 + 发版(步骤 20-22)
20. `uv sync` / `ruff check` / `ruff format --check` / `mypy src` / `pytest` 全绿;修复全部失败。
21. 本地构建冻结二进制(`uv run --with pyinstaller pyinstaller ...`)冒烟:`--version`/`doctor`/MCP stdio initialize 握手/旧命令兼容/新命令。
22. 提交 push → 等 test.yml 三平台绿 → 打 `v1.0.0` tag → CI 出 arm64+Windows Release → `upload-macos-x64.sh v1.0.0` 本地补 Intel。凭证泄露扫描后完成。

## 关键技术约束(贯穿全程)
- **stdout 纯净**:serve 模式下 stdout 只允许 JSON-RPC
- **Git 安全**:参数数组、不 shell=True、NUL 分隔(复用现有)
- **JetBrains 只读**:只调 get_project_status/get_file_problems,白名单常量
- **loopback only**:JetBrains URL + Sonar 都只连 127.0.0.1/localhost/::1
- **代码锚点**:最多 3 行、只算 hash、不输出/不保存源码
- **确定性去重**:不联网、不下载模型、相同输入相同输出
- **兼容**:旧命令/旧 MCP name/旧 env 不立即失效,输出迁移提示
- **凭证**:GH_TOKEN 只从环境读,不写入任何文件/commit/日志

## 验收标准
- GitHub 仓库已是 `pycharm-code-quality-mcp`(✅ 已完成)
- 本地 origin 指向新仓库(✅ 已完成)
- 主包名/主命令/主 MCP name 全部更新,旧名只存于兼容层
- 11 个 MCP 工具(4 旧 + 3 JetBrains + 4 统一)全部注册且契约正确
- JetBrains 为默认后端,Sonar 未装时工具正常工作
- 去重引擎 4 种 mode + 统计字段 + 稳定 ID
- ruff/mypy/pytest 全绿(三平台 CI)
- v1.0.0 Release 含三平台二进制
- 无凭证泄露

## 风险与权衡
- **工作量大**:约 30+ 新文件、20+ 测试文件。我会分批提交(架构骨架→JetBrains→去重→工具→测试→文档→发版),每批跑质量门禁。
- **JetBrains MCP 真实端点**:我无法在 CI 里连真实 PyCharm,所以 JetBrains 测试全部 Mock ClientSession(和现有 Sonar 测试同策略)。doctor 的 JetBrains 检查在你本机能真实跑。
- **旧 MCP name 迁移**:已注册 `pycharm-sonar` 的 codex/claude 配置不会自动改名,安装脚本会检测并提示迁移,但不强制(避免破坏)。
- **本地目录名**:运行中不改,最终报告标 LOCAL_DIRECTORY_RENAME_REQUIRED,你手动 `mv` 即可。