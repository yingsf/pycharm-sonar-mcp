# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/yingsf/pycharm-sonar-mcp/releases/tag/v0.1.0
