"""旧四个 sonar_* 工具的实现(契约保持不变)

工具:
  * sonar_ide_status          —— 扫描端口并报告实例。
  * sonar_analyze_files       —— 分析 1..200 个文件(自动分批)。
  * sonar_analyze_git_changes —— 收集 git 变更并分析。
  * sonar_clear_cache         —— 清除 project→port 缓存。

旧工具继续返回原有契约(AnalysisResult / ClearCacheResult / IdeStatusResult)。
只有 code_quality_* 工具才会返回新的 UnifiedFinding 模型。

实现从原 server.py 原样搬迁而来;helper 走 ``tools._shared``。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .. import errors
from ..backends.sonar.client import SonarClient
from ..backends.sonar.discovery import IdeDiscovery
from ..backends.sonar.models import (
    AnalysisResult,
    ClearCacheResult,
    FailedFile,
    IdeStatusResult,
)
from ..backends.sonar.result_summary import assert_single_project_root, build_result
from ..core.git_changes import collect_changed_files
from ..core.path_utils import dedupe_and_sort, normalize_path
from ..logging_config import get_logger
from ._shared import (
    MAX_FILES,
    SONAR_BATCH_SIZE,
    AnyContext,
    ensure_workspace_roots,
    error_dict,
    filter_valid_files,
    gather_workspace_roots,
    make_skipped_summary,
    within,
)

_log = get_logger("sonar_tools")

# ---------------------------------------------------------------------------
# 模块级单例(server.py 启动时复用,测试可注入)
# ---------------------------------------------------------------------------

_SONAR_CLIENT: SonarClient | None = None
_DISCOVERY: IdeDiscovery | None = None


def get_sonar_client() -> SonarClient:
    global _SONAR_CLIENT
    if _SONAR_CLIENT is None:
        _SONAR_CLIENT = SonarClient()
    return _SONAR_CLIENT


def get_discovery() -> IdeDiscovery:
    global _DISCOVERY
    if _DISCOVERY is None:
        _DISCOVERY = IdeDiscovery(get_sonar_client())
    return _DISCOVERY


def reset_singletons(
    client: SonarClient | None = None, discovery: IdeDiscovery | None = None
) -> None:
    """替换模块级单例(主要用于测试注入)"""
    global _SONAR_CLIENT, _DISCOVERY
    _SONAR_CLIENT = client
    _DISCOVERY = discovery


# ---------------------------------------------------------------------------
# 工具描述(对外契约的一部分)
# ---------------------------------------------------------------------------

STATUS_DESCRIPTION = (
    "Scan ports 64120-64130 on localhost and report how many SonarQube for IDE "
    "instances are reachable, with their ports and status. Use this for diagnostics."
)
ANALYZE_DESCRIPTION = (
    "Analyze 1 to 200 absolute file paths with the user's local SonarQube for IDE. "
    "Files are auto-batched (50/batch). Returns findings with ruleKey, message, "
    "severity, filePath and textRange, plus per-file status and severity counts."
)
GIT_DESCRIPTION = (
    "Collect changed files in the git repository under project_root (staged, unstaged, "
    "untracked; relative to base_ref) and analyze them with Sonar. Deleted files are excluded."
)
CLEAR_DESCRIPTION = (
    "Clear the in-memory port discovery cache. Pass project_root to clear only one project."
)


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


async def impl_ide_status() -> dict[str, Any]:
    """sonar_ide_status:扫描端口 64120..64130,报告可用实例"""
    discovery = get_discovery()
    try:
        instances = await asyncio.to_thread(discovery.discover_all_instances)
    except errors.SonarMcpError as e:
        return error_dict(e)
    except Exception as e:  # pragma: no cover - defensive
        _log.exception("Unexpected error during ide_status")
        return error_dict(errors.internal_error(str(e)))

    result = IdeStatusResult(
        available=bool(instances),
        instanceCount=len(instances),
        instances=instances,
    )
    return result.model_dump(by_alias=True)


async def impl_analyze_files(
    file_absolute_paths: list[str],
    project_root: str | None,
    ctx: AnyContext | None = None,
) -> dict[str, Any]:
    """sonar_analyze_files:分析 1..200 个绝对路径文件"""
    start = time.monotonic()
    try:
        if not isinstance(file_absolute_paths, list):  # 防御性:模型可能传入非列表
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
                f"Too many files: {len(file_absolute_paths)} > {MAX_FILES}. "
                "Split into multiple calls or use sonar_analyze_git_changes."
            )

        accepted, skipped = filter_valid_files(file_absolute_paths, roots)
        if not accepted:
            raise errors.bad_request(
                "No valid files to analyze. "
                + ("; ".join(f"{p}: {c}" for p, c, _ in skipped[:5]))
                + ("..." if len(skipped) > 5 else "")
            )

        unique = dedupe_and_sort(accepted)
        pr = assert_single_project_root(unique, roots)
        if project_root:
            _log.debug("Model-supplied project_root=%s; detected=%s", project_root, pr)

        discovery = get_discovery()
        port = await asyncio.to_thread(discovery.discover_for_project, pr)

        sonar = get_sonar_client()
        outcomes = await asyncio.to_thread(
            sonar.analyze_files_batched,
            port,
            unique,
            batch_size=SONAR_BATCH_SIZE,
            per_batch_timeout=60.0,
        )

        result = build_result(
            requested_files=unique,
            batch_outcomes=outcomes,
            ide_port=port,
            start_time=start,
        )
        _merge_skips_into_result(result, skipped)

        return result.model_dump(by_alias=True)
    except errors.SonarMcpError as e:
        _log.warning("analyze_files failed: %s", e)
        return error_dict(e, partial=True)
    except Exception as e:
        _log.exception("Unexpected error in analyze_files")
        return error_dict(errors.internal_error(str(e)), partial=True)


async def impl_analyze_git_changes(
    project_root: str,
    base_ref: str,
    include_untracked: bool,
    include_staged: bool,
    include_unstaged: bool,
    ctx: AnyContext | None = None,
) -> dict[str, Any]:
    """sonar_analyze_git_changes:收集 project_root 下的 git 变更并分析"""
    start = time.monotonic()
    try:
        if not isinstance(project_root, str) or not project_root.strip():
            raise errors.bad_request("project_root is required.")
        norm_root = normalize_path(project_root)
        if not _is_dir(norm_root):
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
            # 无变更时返回显式空结果(不作为错误)。
            result = AnalysisResult(
                success=True,
                partialSuccess=False,
                idePort=None,
                requestedFileCount=0,
                analyzedFileCount=0,
                skippedFileCount=0,
                failedFileCount=0,
                findingCount=0,
                durationMs=int((time.monotonic() - start) * 1000),
                projectRoot=norm_root,
                baseRef=base_ref,
                changedFileCount=0,
            )
            result.notices.append("No changed files to analyze.")
            return result.model_dump(by_alias=True)

        if len(files) > MAX_FILES:
            raise errors.too_many_files(
                f"Too many changed files ({len(files)} > {MAX_FILES}). "
                "Analyze in smaller groups or commit some changes."
            )

        # 复用 analyze_files 的后续流程(git 已产出经过工作区过滤的绝对存在文件)。
        pr = assert_single_project_root(files, roots)
        discovery = get_discovery()
        port = await asyncio.to_thread(discovery.discover_for_project, pr)
        sonar = get_sonar_client()
        outcomes = await asyncio.to_thread(
            sonar.analyze_files_batched,
            port,
            files,
            batch_size=SONAR_BATCH_SIZE,
            per_batch_timeout=60.0,
        )
        result = build_result(
            requested_files=files,
            batch_outcomes=outcomes,
            ide_port=port,
            start_time=start,
        )
        result.project_root = norm_root
        result.base_ref = base_ref
        result.changed_file_count = len(files)
        return result.model_dump(by_alias=True)
    except errors.SonarMcpError as e:
        _log.warning("analyze_git_changes failed: %s", e)
        return error_dict(e, partial=True)
    except Exception as e:
        _log.exception("Unexpected error in analyze_git_changes")
        return error_dict(errors.internal_error(str(e)), partial=True)


def impl_clear_cache(project_root: str | None) -> dict[str, Any]:
    """sonar_clear_cache:清除内存中的 project→port 缓存(同步,无 I/O)"""
    from ..backends.sonar.discovery import get_global_cache

    cache = get_global_cache()
    if project_root:
        existed = cache.invalidate(project_root)
        msg = (
            f"Cleared cache for project_root={project_root}."
            if existed
            else f"No cache entry for project_root={project_root}."
        )
        result = ClearCacheResult(cleared=existed, clearedPorts=[], message=msg)
    else:
        ports = cache.clear()
        result = ClearCacheResult(
            cleared=True,
            clearedPorts=ports,
            message=f"Cleared all {len(ports)} cached port(s).",
        )
    return result.model_dump(by_alias=True)


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _merge_skips_into_result(result: AnalysisResult, skipped: list[tuple[str, str, str]]) -> None:
    """把分析前的跳过项就地附加到 AnalysisResult"""
    if not skipped:
        return
    for path, code, msg in skipped:
        result.skipped_files.append(FailedFile(filePath=path, errorCode=code, errorMessage=msg))
        result.file_summaries.append(make_skipped_summary(path, code, msg))
    result.skipped_file_count = len(result.skipped_files)
    result.partial_success = True
    result.notices.append(f"{len(skipped)} file(s) skipped before analysis.")


def _is_dir(path: str) -> bool:
    import os

    return os.path.isdir(path)


# 导出供 server.py / 测试 / 旧兼容层访问。
_ = within  # 保留引用(避免被 lint 删除,部分旧测试会从 server 模块转引)

__all__ = [
    "ANALYZE_DESCRIPTION",
    "CLEAR_DESCRIPTION",
    "GIT_DESCRIPTION",
    "STATUS_DESCRIPTION",
    "get_discovery",
    "get_sonar_client",
    "impl_analyze_files",
    "impl_analyze_git_changes",
    "impl_clear_cache",
    "impl_ide_status",
    "reset_singletons",
]
