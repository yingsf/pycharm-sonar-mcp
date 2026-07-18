"""统一默认工具(4 个,README 与 Agent 指令默认推荐)

工具:
  * code_quality_status            —— 报告 JetBrains+Sonar 两个后端的完整状态。
  * code_quality_analyze_files     —— 统一分析 1..200 个文件(auto 模式默认)。
  * code_quality_analyze_git_changes —— 收集 git 变更并统一分析。
  * code_quality_clear_cache       —— 清除所有后端的内存缓存。

默认 backend_mode = auto:
  1. 尝试 JetBrains(可用则运行);
  2. 自动探测 SonarQube for IDE(可用则同时运行);
  3. 合并两个来源 → 自动去重(balanced)→ 返回 UnifiedFinding。

Sonar 未安装时,J JetBrains 单独成功 ⇒ success=true、notice 说明、不算 partial。
两个后端都不可用 ⇒ success=false + NO_ANALYSIS_BACKEND_AVAILABLE。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from .. import errors
from ..backends.sonar.discovery import get_global_cache
from ..core.git_changes import collect_changed_files
from ..core.path_utils import dedupe_and_sort, normalize_path
from ..logging_config import get_logger
from ..quality.deduplication import DeduplicationMode
from ..quality.orchestrator import (
    MODE_ALL,
    MODE_AUTO,
    MODE_JETBRAINS,
    MODE_SONAR,
    QualityOrchestrator,
)
from ._shared import (
    MAX_FILES,
    AnyContext,
    ensure_workspace_roots,
    error_dict,
    filter_valid_files,
    gather_workspace_roots,
)

_log = get_logger("quality_tools")

_VALID_BACKEND_MODES = frozenset({MODE_AUTO, MODE_JETBRAINS, MODE_SONAR, MODE_ALL})
_VALID_DEDUP_MODES = frozenset(
    {
        DeduplicationMode.CONSERVATIVE,
        DeduplicationMode.BALANCED,
        DeduplicationMode.AGGRESSIVE,
        DeduplicationMode.OFF,
    }
)


# ---------------------------------------------------------------------------
# 工具描述
# ---------------------------------------------------------------------------

STATUS_DESCRIPTION = (
    "Report availability and status of both analysis backends: the JetBrains MCP Server "
    "(PyCharm inspections, default) and SonarQube for IDE (optional). Use this first to "
    "see which backends are configured and ready."
)
ANALYZE_FILES_DESCRIPTION = (
    "Analyze 1 to 200 absolute file paths with the default backend strategy "
    "(backend_mode=auto: JetBrains first, Sonar auto-added if installed). "
    "Returns unified findings after deterministic cross-backend deduplication. "
    "Errors-only filtering and deduplication mode are configurable."
)
ANALYZE_GIT_DESCRIPTION = (
    "Collect changed files in the git repository under project_root (staged, unstaged, "
    "untracked; relative to base_ref) and analyze them with the unified backend strategy. "
    "Deleted files are excluded. Same deduplication and backend_mode options as "
    "code_quality_analyze_files."
)
CLEAR_CACHE_DESCRIPTION = (
    "Clear in-memory caches for all backends (Sonar port discovery, JetBrains session). "
    "Pass project_root to clear only one Sonar project's port mapping."
)


# ---------------------------------------------------------------------------
# Orchestrator 构造
# ---------------------------------------------------------------------------


def _build_orchestrator() -> QualityOrchestrator:
    """构造 orchestrator:JetBrains 按配置可用,Sonar 始终实例化(未安装时 is_available=False)"""
    # JetBrains:仅当已配置时才注入,否则保持 None(auto 模式下自动降级到 Sonar)。
    jetbrains = None
    try:
        from ..backends.jetbrains.analyzer import JetBrainsAnalysisBackend

        # 直接尝试加载配置;失败/未配置都视为 None。
        from ..backends.jetbrains.config import load_config

        cfg = load_config()
        if cfg is not None:
            jetbrains = JetBrainsAnalysisBackend(cfg)
    except errors.SonarMcpError as e:
        _log.debug("JetBrains backend not available: [%s] %s", e.code, e.user_message)
        jetbrains = None
    except Exception as e:  # pragma: no cover - 防御性
        _log.debug("JetBrains backend construction failed: %s", e)
        jetbrains = None

    # Sonar:复用旧 sonar_tools 的单例,确保端口缓存一致。
    from ._sonar_instances import get_sonar_backend

    sonar = get_sonar_backend()
    return QualityOrchestrator(jetbrains=jetbrains, sonar=sonar)


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


async def impl_status(ctx: AnyContext | None = None) -> dict[str, Any]:
    """code_quality_status:返回两个后端的完整状态"""
    _ = ctx
    try:
        orch = _build_orchestrator()
        return await orch.get_status()
    except errors.SonarMcpError as e:
        return error_dict(e)
    except Exception as e:  # pragma: no cover - 防御性
        _log.exception("code_quality_status failed")
        return error_dict(errors.internal_error(str(e)))


async def impl_analyze_files(
    file_absolute_paths: list[str],
    project_root: str | None = None,
    backend_mode: str = MODE_AUTO,
    errors_only: bool = False,
    deduplication_mode: str = DeduplicationMode.BALANCED,
    ctx: AnyContext | None = None,
) -> dict[str, Any]:
    """code_quality_analyze_files:统一分析 1..200 个文件"""
    start = time.monotonic()
    try:
        _validate_modes(backend_mode, deduplication_mode)

        if not isinstance(file_absolute_paths, list):
            raise errors.bad_request("file_absolute_paths must be a list of strings.")
        if not file_absolute_paths:
            raise errors.bad_request("file_absolute_paths is empty.")

        roots = await gather_workspace_roots(ctx)
        if not roots:
            raise errors.workspace_not_configured(
                "No workspace roots available. Configure MCP Roots or set SONAR_WORKSPACE_ROOTS."
            )

        if len(file_absolute_paths) > MAX_FILES:
            raise errors.too_many_files(
                f"Too many files: {len(file_absolute_paths)} > {MAX_FILES}."
            )

        accepted, skipped = filter_valid_files(file_absolute_paths, roots)
        if not accepted:
            raise errors.bad_request(
                "No valid files to analyze. "
                + ("; ".join(f"{p}: {c}" for p, c, _ in skipped[:5]))
                + ("..." if len(skipped) > 5 else "")
            )

        unique = dedupe_and_sort(accepted)
        # project_root 推断:优先用户传入;否则用首个文件所在工作区根(已校验)。
        pr = project_root or _infer_project_root(unique, roots)

        orch = _build_orchestrator()
        result = await orch.analyze_files(
            unique,
            backend_mode=backend_mode,
            errors_only=errors_only,
            deduplication_mode=deduplication_mode,
            project_root=pr,
            requested_file_count=len(file_absolute_paths),
        )
        payload = result.model_dump(by_alias=True, exclude_none=False)
        # 把分析前跳过项并到 fileSummaries / notices。
        if skipped:
            for path, code, msg in skipped:
                payload["fileSummaries"].append(
                    {
                        "filePath": path,
                        "status": "skipped",
                        "findingCount": 0,
                        "detail": f"{code}: {msg}",
                    }
                )
            payload["partialSuccess"] = True
            payload.setdefault("notices", []).append(
                f"{len(skipped)} file(s) skipped before analysis."
            )
        return payload
    except errors.SonarMcpError as e:
        _log.warning("code_quality_analyze_files failed: %s", e)
        return error_dict(e, partial=True)
    except Exception as e:
        _log.exception("Unexpected error in code_quality_analyze_files")
        return error_dict(errors.internal_error(str(e)), partial=True)
    finally:
        _ = start  # duration 由 orchestrator 内部计算


async def impl_analyze_git_changes(
    project_root: str,
    base_ref: str = "HEAD",
    include_untracked: bool = True,
    include_staged: bool = True,
    include_unstaged: bool = True,
    backend_mode: str = MODE_AUTO,
    errors_only: bool = False,
    deduplication_mode: str = DeduplicationMode.BALANCED,
    ctx: AnyContext | None = None,
) -> dict[str, Any]:
    """code_quality_analyze_git_changes:收集 git 变更并统一分析"""
    try:
        _validate_modes(backend_mode, deduplication_mode)

        if not isinstance(project_root, str) or not project_root.strip():
            raise errors.bad_request("project_root is required.")
        norm_root = normalize_path(project_root)
        if not os.path.isdir(norm_root):
            raise errors.git_invalid_repository(f"project_root is not a directory: {norm_root}")

        roots = await gather_workspace_roots(ctx)
        roots = ensure_workspace_roots(roots, norm_root)

        files = await asyncio.to_thread(
            lambda: collect_changed_files(
                norm_root,
                base_ref=base_ref,
                include_staged=include_staged,
                include_unstaged=include_unstaged,
                include_untracked=include_untracked,
                workspace_roots=roots,
            )
        )

        if not files:
            # 显式空结果(不作为错误)。
            return {
                "success": True,
                "partialSuccess": False,
                "degradedMode": False,
                "requestedFileCount": 0,
                "analyzedFileCount": 0,
                "rawFindingCount": 0,
                "uniqueFindingCount": 0,
                "duplicatesMerged": 0,
                "possibleDuplicateCount": 0,
                "deduplicationMode": deduplication_mode,
                "severityCounts": {},
                "backends": {},
                "fileSummaries": [],
                "findings": [],
                "deduplicationGroups": [],
                "possibleDuplicateGroups": [],
                "notices": ["No changed files to analyze."],
                "durationMs": 0,
                "projectRoot": norm_root,
                "baseRef": base_ref,
                "changedFileCount": 0,
            }

        if len(files) > MAX_FILES:
            raise errors.too_many_files(
                f"Too many changed files ({len(files)} > {MAX_FILES}). "
                "Analyze in smaller groups or commit some changes."
            )

        orch = _build_orchestrator()
        result = await orch.analyze_files(
            files,
            backend_mode=backend_mode,
            errors_only=errors_only,
            deduplication_mode=deduplication_mode,
            project_root=norm_root,
            requested_file_count=len(files),
        )
        payload = result.model_dump(by_alias=True, exclude_none=False)
        payload["projectRoot"] = norm_root
        payload["baseRef"] = base_ref
        payload["changedFileCount"] = len(files)
        return payload
    except errors.SonarMcpError as e:
        _log.warning("code_quality_analyze_git_changes failed: %s", e)
        return error_dict(e, partial=True)
    except Exception as e:
        _log.exception("Unexpected error in code_quality_analyze_git_changes")
        return error_dict(errors.internal_error(str(e)), partial=True)


def impl_clear_cache(project_root: str | None = None) -> dict[str, Any]:
    """code_quality_clear_cache:清除所有后端的内存缓存(同步,无 I/O)

    JetBrains 不持有持久 session(每次 analyze 都新建+关闭),这里只清 Sonar 端口缓存。
    """
    from ..backends.sonar.discovery import PortCache

    cache: PortCache = get_global_cache()
    if project_root:
        existed = cache.invalidate(project_root)
        return {
            "cleared": existed,
            "clearedPorts": [],
            "message": (
                f"Cleared cache for project_root={project_root}."
                if existed
                else f"No cache entry for project_root={project_root}."
            ),
            "backends": {
                "sonar": "cleared" if existed else "noop",
                "jetbrains": "no_persistent_cache",
            },
        }
    ports = cache.clear()
    return {
        "cleared": True,
        "clearedPorts": ports,
        "message": f"Cleared all {len(ports)} cached Sonar port(s); JetBrains has no persistent cache.",
        "backends": {"sonar": "cleared", "jetbrains": "no_persistent_cache"},
    }


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _validate_modes(backend_mode: str, deduplication_mode: str) -> None:
    if backend_mode not in _VALID_BACKEND_MODES:
        raise errors.bad_request(
            f"Invalid backend_mode {backend_mode!r}; expected one of {sorted(_VALID_BACKEND_MODES)}."
        )
    if deduplication_mode not in _VALID_DEDUP_MODES:
        raise errors.bad_request(
            f"Invalid deduplication_mode {deduplication_mode!r}; expected one of "
            f"{sorted(_VALID_DEDUP_MODES)}."
        )


def _infer_project_root(files: list[str], roots: list[str]) -> str | None:
    """从首个文件推断 project_root:取包含它的工作区根"""
    if not files or not roots:
        return None
    first = files[0]
    for r in roots:
        if _within(first, r):
            return r
    # 兜底:取首个文件所在目录。
    return os.path.dirname(first)


def _within(child: str, parent: str) -> bool:
    nc = os.path.normcase(os.path.normpath(child))
    np_ = os.path.normcase(os.path.normpath(parent)).rstrip(os.sep)
    return nc == np_ or nc.startswith(np_ + os.sep)


__all__ = [
    "ANALYZE_FILES_DESCRIPTION",
    "ANALYZE_GIT_DESCRIPTION",
    "CLEAR_CACHE_DESCRIPTION",
    "STATUS_DESCRIPTION",
    "impl_analyze_files",
    "impl_analyze_git_changes",
    "impl_clear_cache",
    "impl_status",
]
