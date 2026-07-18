# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-07-19

This release repositions the project around **JetBrains inspections as the default
backend**, with SonarQube for IDE as an auto-detected optional enhancement. The
GitHub repository has been renamed to `pycharm-code-quality-mcp`; old names keep
working through compatibility shims.

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
- **Architecture refactor** (`tools/` package): `server.py` is now thin — it only
  builds the FastMCP app, registers all 11 tools, and runs stdio. Business logic
  lives in `tools/sonar_tools.py`, `tools/jetbrains_tools.py`, `tools/quality_tools.py`,
  with shared workspace/path helpers in `tools/_shared.py`.

### Changed
- **Default backend strategy is `auto`**: JetBrains first, Sonar auto-added when
  installed. Sonar-uninstalled is **not** a partial failure (JetBrains-only success
  is still `success=true`).
- **Package renamed** `pycharm_sonar_mcp` → `pycharm_code_quality_mcp`;
  distribution name → `pycharm-code-quality-mcp`; main command →
  `pycharm-code-quality-mcp`; MCP server name → `pycharm-code-quality`.
- **Config directory** via `platformdirs`:
  macOS `~/Library/Application Support/pycharm-code-quality-mcp/`,
  Windows `%LOCALAPPDATA%\pycharm-code-quality-mcp\`. POSIX config files are
  saved `0600` since they may carry auth headers.
- **JetBrains URL hardening**: only `localhost` / `127.0.0.1` / `::1` are accepted;
  remote IPs, LAN addresses and public domains are refused. Headers never enter logs.
- **Severity normalization**: JetBrains `ERROR`→`CRITICAL`, `WARNING`→`MAJOR`,
  `WEAK WARNING`/`TYPO`→`MINOR`, `INFORMATION`→`INFO`, `SERVER PROBLEM`→`MAJOR`.

### Preserved (backward compatibility)
- The four legacy `sonar_*` tools return their **original contract** unchanged
  (`AnalysisResult` / `IdeStatusResult` / `ClearCacheResult`).
- Old command `pycharm-sonar-mcp`, old MCP name `pycharm-sonar`, old package
  `pycharm_sonar_mcp`, old env vars (`SONAR_IDE_PORT`, `SONAR_WORKSPACE_ROOTS`,
  `PYCHARM_SONAR_MCP_LOG_LEVEL`) all keep working through a thin shim that delegates
  to the new implementation. A migration notice is printed.
- Legacy `sonar_*` tools and new `code_quality_*` tools share the same Sonar
  port-discovery cache and HTTP transport — no second business implementation.

### Tests
- Added comprehensive suites: orchestrator (backend selection / partial success /
  degraded mode / severity merging), deduplication (merge conditions / forbidden
  pairs / transitive-chain protection / 4 modes / stable ordering), JetBrains
  parser (structuredContent / JSON TextContent / plain-text fallback / unknown
  fields), JetBrains client (config / loopback / whitelist / timeout / session
  lifecycle), CLI new commands (setup / configure JSON parsing / clear / status /
  doctor sections), quality tools (status / analyze / clear-cache / git-changes
  empty case), severity / categorization / normalization / fingerprints.
- The suite now has 334 passing tests (5 platform-skipped on non-Windows).

## [0.1.0] — 2026-07-18

### Added
- First release. Local MCP server bridging Codex App / Codex CLI / Claude Code to the
  SonarQube for IDE plugin running inside PyCharm on the developer's own machine.
- Four MCP tools:
  - `sonar_ide_status` — scans ports `64120..64130` and reports SonarQube for IDE instances.
  - `sonar_analyze_files` — analyzes 1–200 absolute file paths (auto-batched 50/batch).
  - `sonar_analyze_git_changes` — collects git changes (staged/unstaged/untracked vs `base_ref`) and analyzes them.
  - `sonar_clear_cache` — clears the in-memory project→port discovery cache.
- Custom IPv4-loopback HTTP transport: TCP dials `127.0.0.1` while the HTTP
  `Host`/`Origin` authority stays `localhost`, defeating HTTP 421 and IPv6-first
  `localhost` resolution.
- Port discovery with heuristic status validation, in-memory project→port cache,
  multi-instance file-ownership probing, and single-retry cache invalidation.
- Cross-platform path safety: normalization, drive-letter/UNC/case handling, symlink
  and Windows junction escape detection, workspace containment enforcement.
- Git change collection via NUL-separated (`-z`) git output (no `shell=True`), with
  deletion/directory/out-of-workspace filtering and dedup.
- Partial-failure semantics: failed batches never drop successful findings;
  `partialSuccess`, `failedFiles`, `batchErrors` are reported explicitly.
- `doctor` diagnostics without `lsof`/`grep`/`sed`/`awk`/`netstat`/PowerShell external calls.
- Cross-platform install/uninstall scripts (macOS Bash 3.2, Windows PowerShell 5.1+)
  with SHA-256 verification, atomic replacement, and best-effort Codex/Claude registration.
- PyInstaller spec producing standalone binaries.
- GitHub Actions CI (test matrix across Ubuntu/macOS/Windows × Python 3.11–3.13) and a
  release workflow building per-platform binaries with smoke tests.

[1.0.0]: https://github.com/yingsf/pycharm-code-quality-mcp/releases/tag/v1.0.0
[0.1.0]: https://github.com/yingsf/pycharm-code-quality-mcp/releases/tag/v0.1.0
