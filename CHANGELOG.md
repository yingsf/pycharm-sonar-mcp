# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.2] — 2026-07-23

### Fixed
- **Dynamic PyCharm project routing**: newer PyCharm MCP JSON may include
  `IJ_MCP_SERVER_PROJECT_PATH`. The CLI now strips that project-bound header from
  global config while using it temporarily for configure-time verification. Runtime
  JetBrains sessions dynamically set the header from the current `project_root` /
  MCP Roots / analyzed file paths, so one saved config can safely serve multiple
  projects.
- **JetBrains inspect project-root propagation**: `jetbrains_inspect_files` now passes
  the effective `project_root` into the backend instead of silently falling back to
  absolute paths or stale saved headers.
- **Auto-mode availability checks with project context**: `code_quality_*` auto mode
  now probes JetBrains availability with the same project root used for analysis.
- **Cross-project batch guard**: one `*_files` request spanning multiple workspace
  roots is rejected with a clear error instead of risking analysis against the wrong
  PyCharm project.
- **Friendlier JetBrains transport failures**: HTTP transport cancellations during
  initialize/tools/list/tool calls are mapped to stable JetBrains errors rather than
  leaking raw `CancelledError` tracebacks.

## [1.0.1] — 2026-07-21

### Fixed
- **doctor no longer appears to hang when run from `$HOME` during install**:
  the noqa style check now scans only explicit project roots (`--file`,
  `SONAR_WORKSPACE_ROOTS`, or a cwd with project markers), prunes cache/venv
  directories before descent, and caps the scan. The macOS installer now streams
  doctor output live instead of buffering it until completion.

## [1.0.0] — 2026-07-19

This release positions the project around **JetBrains inspections as the default
backend**, with SonarQube for IDE as an auto-detected optional enhancement.

### Added
- **JetBrains MCP backend** (`backends/jetbrains/`): connects to PyCharm's built-in
  MCP Server via the standard `mcp` Python SDK (Streamable HTTP + `ClientSession`).
  Strict read-only tool whitelist — only `get_project_status` and `get_file_problems`
  are ever called. No proxy/execute/refactor tools.
- **Unified orchestration** (`quality/orchestrator.py`): runs JetBrains + Sonar in
  parallel with backend isolation; one backend's failure never cancels the other;
  collects partial success and degraded mode transparently.
- **Deterministic cross-backend deduplication** (`quality/deduplication.py`):
  6-dimensional similarity scoring (location/message/rule/anchor/category/identifier),
  complete-link clustering to prevent transitive over-merge, 4 modes
  (conservative/balanced/aggressive/off), stable SHA-256 IDs, possible-duplicate
  groups for medium-confidence pairs.
- **Unified data model** (`quality/models.py`): `UnifiedFinding`, `SourceFinding`,
  `QualityAnalysisResult`, `BackendStatus`, severity normalization to
  BLOCKER/CRITICAL/MAJOR/MINOR/INFO/UNKNOWN, 18 stable categories.
- **4 unified default tools** (recommended in README/agent instructions):
  - `code_quality_status` — reports both backends' full status.
  - `code_quality_analyze_files` — 1–200 files, `backend_mode=auto` by default,
    configurable deduplication.
  - `code_quality_analyze_git_changes` — git-collected files through the unified flow.
  - `code_quality_clear_cache` — clears all in-memory caches.
- **3 JetBrains-specific tools**: `jetbrains_ide_status`, `jetbrains_inspect_files`,
  `jetbrains_inspect_git_changes` (no cross-backend merge, native problem shape).
- **CLI subcommands**: `setup`, `jetbrains configure|status|clear`, `sonar status`.
  The `configure` wizard parses the full "Copy HTTP Stream Config" JSON (flat /
  nested `transport` / `mcpServers` forms) and verifies the connection end-to-end
  (initialize + tools/list + required-tool presence).
- **Doctor in three sections** (General / JetBrains / Sonar) plus a Summary.
  Sonar-uninstalled no longer fails the doctor; degraded mode is reported when
  JetBrains is down but Sonar is up.
- **Code-anchor hashing** (`core/file_context.py`): reads ≤3 lines of context around
  a finding only to compute a SHA-256 hash; source text is never logged, returned,
  or persisted.
- **Architecture refactor** (`tools/` package): `server.py` is thin — it only
  builds the FastMCP app, registers all 8 tools, and runs stdio. Business logic
  lives in `tools/jetbrains_tools.py` and `tools/quality_tools.py`, with shared
  workspace/path helpers in `tools/_shared.py` and the Sonar backend singleton in
  `tools/sonar_tools.py` / `tools/_sonar_instances.py`.
- **`code_quality_analyze_project` tool**: scans the whole git repository under
  `project_root` (default: all `.py` files, tracked + untracked, .gitignore-respected)
  via `git ls-files`, then runs them through the unified backend strategy. Pass
  `extensions` to scan other file types. Fills the gap where users previously had
  to fake `base_ref` as the empty-tree SHA to scan the whole repo.
- **`project_root` fallback for `*_files` tools**: `code_quality_analyze_files` and
  `jetbrains_inspect_files` now treat an explicitly-passed `project_root` as an
  allowed workspace when the MCP client provides no Roots and `SONAR_WORKSPACE_ROOTS`
  is unset. Previously they returned `WORKSPACE_NOT_CONFIGURED`; now their behavior
  matches `*_git_changes` / `*_project`.
- **doctor: noqa style check**: scans the current working directory's Python files
  and warns on ruff-style `# noqa: Sxxx` comments (e.g. `# noqa: S3776`). SonarQube
  for IDE ignores these — it only honors `# NOSONAR` — so such comments silently fail
  to suppress the intended Sonar rule. The doctor report points at the first conflict
  and suggests the correct form.

### Fixed
- **JetBrains backend never actually connected** (5 bugs found via dogfooding):
  1. `REQUIRED_TOOLS` demanded `get_project_status`, which PyCharm 2026.1+ no longer
     exposes — now only `get_file_problems` is required; `get_project_status` degrades
     to `{"isIndexing": False, "projectStatusAvailable": False}` when absent.
  2. `get_file_problems` was called with `fileAbsolutePath` (wrong); the PyCharm MCP
     schema requires `filePath` (project-relative). Fixed the parameter name and added
     a `_to_relative_path` helper.
  3. `projectPath` was never passed, so multi-project setups failed with
     "Unable to determine the target project". Now threaded from `project_root`
     through `JetBrainsAnalysisBackend` → `JetBrainsBackend` → `JetBrainsClient`.
  4. Parser only recognized `problems` / `findings` / `diagnostics` / `issues` field
     names; PyCharm actually returns `errors`. Added.
  5. Parser only recognized `startLine` / `startColumn`; PyCharm returns `line` /
     `column`. Added the alias mapping.
- **`_utc_time` returned a float instead of `struct_time`**, causing
  `TypeError: Tuple or struct_time argument required` on every log line. Fixed the
  return type and made the unused parameter explicit.
- **`get_logger` PyCharm type-narrowing false positive**: split the combined
  `if name is None or name == _LOGGER_NAME:` into two separate checks so PyCharm's
  flow analysis correctly narrows `name` to `str` before `name.startswith(...)`.
- **doctor `tools` field** previously listed `ALLOWED_TOOLS` (the whitelist) even
  when the server didn't expose all of them; now reports the actual exposed subset.
- **Install scripts now prompt for JetBrains configuration**: `install-macos.sh`
  and `install-windows.ps1` run `doctor` at the end and, if JetBrains MCP is not
  configured, print an explicit next-steps banner (with sample JSON shapes and
  the `jetbrains configure` command). Previously the install silently left users
  in degraded mode with only an `[INFO]` line that was easy to miss.

### Changed
- **Default backend strategy is `auto`**: JetBrains first, Sonar auto-added when
  installed. Sonar-uninstalled is **not** a partial failure (JetBrains-only success
  is still `success=true`).
- **Config directory** via `platformdirs`:
  macOS `~/Library/Application Support/pycharm-code-quality-mcp/`,
  Windows `%LOCALAPPDATA%\pycharm-code-quality-mcp\`. POSIX config files are
  saved `0600` since they may carry auth headers.
- **JetBrains URL hardening**: only `localhost` / `127.0.0.1` / `::1` are accepted;
  remote IPs, LAN addresses and public domains are refused. Headers never enter logs.
- **Severity normalization**: JetBrains `ERROR`→`CRITICAL`, `WARNING`→`MAJOR`,
  `WEAK WARNING`/`TYPO`→`MINOR`, `INFORMATION`→`INFO`, `SERVER PROBLEM`→`MAJOR`.
- **Custom IPv4-loopback HTTP transport**: TCP dials `127.0.0.1` while the HTTP
  `Host`/`Origin` authority stays `localhost`, defeating HTTP 421 and IPv6-first
  `localhost` resolution.
- **Cross-platform path safety**: normalization, drive-letter/UNC/case handling,
  symlink and Windows junction escape detection, workspace containment enforcement.
- **Git change collection** via NUL-separated (`-z`) git output (no `shell=True`),
  with deletion/directory/out-of-workspace filtering and dedup.
- **Partial-failure semantics**: failed batches never drop successful findings;
  `partialSuccess`, `failedFiles`, `batchErrors` are reported explicitly.
- **Cross-platform install/uninstall scripts** (macOS Bash 3.2, Windows PowerShell 5.1+)
  with SHA-256 verification, atomic replacement, and best-effort Codex/Claude registration.
- **PyInstaller spec** producing standalone binaries.
- **GitHub Actions CI** (test matrix across Ubuntu/macOS/Windows × Python 3.11–3.13)
  and a release workflow building per-platform binaries with smoke tests.

### Tests
- Comprehensive suites: orchestrator (backend selection / partial success /
  degraded mode / severity merging), deduplication (merge conditions / forbidden
  pairs / transitive-chain protection / 4 modes / stable ordering), JetBrains
  parser (structuredContent / JSON TextContent / plain-text fallback / unknown
  fields), JetBrains client (config / loopback / whitelist / timeout / session
  lifecycle), CLI commands (setup / configure JSON parsing / clear / status /
  doctor sections), quality tools (status / analyze / clear-cache / git-changes
  empty case), severity / categorization / normalization / fingerprints.

[1.0.1]: https://github.com/yingsf/pycharm-code-quality-mcp/releases/tag/v1.0.1
[1.0.0]: https://github.com/yingsf/pycharm-code-quality-mcp/releases/tag/v1.0.0
