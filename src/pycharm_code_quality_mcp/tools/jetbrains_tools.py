"""JetBrains 专用工具(3 个)

工具:
  * jetbrains_ide_status        —— 报告 JetBrains MCP 配置/可用性/索引状态。
  * jetbrains_inspect_files     —— 用 JetBrains inspections 分析 1..200 个文件。
  * jetbrains_inspect_git_changes —— 收集 git 变更并用 JetBrains inspections 分析。

这组工具直接走 JetBrains MCP,不做跨后端合并或去重;返回 JetBrains 原生问题列表。
统一编排(去重+合并)请用 ``quality_tools``。

环境变量:
  * ``JETBRAINS_INSPECTION_TIMEOUT_MS``:单文件 inspection 超时(默认 30000)。
  * ``JETBRAINS_MAX_FILES``:单次调用允许的最大文件数(上限仍是 200)。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from .. import errors
from ..backends.jetbrains.analyzer import JetBrainsAnalysisBackend
from ..backends.jetbrains.config import load_config
from ..backends.jetbrains.models import JetBrainsAnalysisResult
from ..core.git_changes import collect_changed_files
from ..core.path_utils import dedupe_and_sort, normalize_path
from ..logging_config import get_logger
from ._shared import (
    MAX_FILES,
    AnyContext,
    ensure_workspace_roots,
    error_dict,
    filter_valid_files,
    gather_workspace_roots,
    infer_single_project_root,
)

_log = get_logger("jetbrains_tools")

_ENV_MAX_FILES = "JETBRAINS_MAX_FILES"


def _effective_max_files() -> int:
    """读取 JETBRAINS_MAX_FILES;非法/未设置回退到 MAX_FILES(200)"""
    raw = os.environ.get(_ENV_MAX_FILES, "").strip()
    if not raw:
        return MAX_FILES
    try:
        v = int(raw)
    except ValueError:
        return MAX_FILES
    return max(1, min(v, MAX_FILES))


# ---------------------------------------------------------------------------
# 工具描述
# ---------------------------------------------------------------------------

STATUS_DESCRIPTION = (
    "Probe the locally-configured JetBrains MCP Server (PyCharm) and report whether "
    "it is configured, reachable, project-ready, and whether the required tool "
    "(get_file_problems) is exposed. get_project_status is reported as optional "
    "(absent on PyCharm 2026.1+). Use this for diagnostics."
)
INSPECT_FILES_DESCRIPTION = (
    "Inspect 1 to 200 absolute file paths with PyCharm's built-in JetBrains inspections "
    "via the local JetBrains MCP Server. Reuses one MCP session for all files; a single "
    "file failure does not abort the others. Returns inspection problems (1-based ranges)."
)
INSPECT_GIT_DESCRIPTION = (
    "Collect changed files in the git repository under project_root (staged, unstaged, "
    "untracked; relative to base_ref) and inspect them with JetBrains inspections. "
    "Deleted files are excluded."
)


# ---------------------------------------------------------------------------
# 后端实例化
# ---------------------------------------------------------------------------


def _load_config_or_raise() -> Any:
    """加载配置;未配置时抛 SonarMcpError(JETBRAINS_NOT_CONFIGURED)"""
    cfg = load_config()
    if cfg is None:
        raise errors.jetbrains_not_configured(
            "JetBrains MCP is not configured. Run: pycharm-code-quality-mcp jetbrains configure"
        )
    return cfg


def _make_backend() -> JetBrainsAnalysisBackend:
    """从配置加载并构造 JetBrainsAnalysisBackend"""
    cfg = _load_config_or_raise()
    return JetBrainsAnalysisBackend(cfg)


def _backend_or_error_dict() -> JetBrainsAnalysisBackend | dict[str, Any]:
    """构造后端;未配置/失败时返回错误 dict(供 *_status 工具使用)"""
    try:
        return _make_backend()
    except errors.SonarMcpError as e:
        return error_dict(e)
    except Exception as e:  # pragma: no cover - 防御性
        _log.exception("Failed to construct JetBrains backend")
        return error_dict(errors.internal_error(str(e)))


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


async def impl_ide_status() -> dict[str, Any]:
    """jetbrains_ide_status:报告 JetBrains MCP 配置/可用性/项目状态"""
    backend = _backend_or_error_dict()
    if isinstance(backend, dict):
        # 未配置或失败:返回明确状态而非裸错误。
        return {
            "available": False,
            "configured": False,
            "error": backend.get("errorMessage") or "JetBrains MCP not configured.",
            "errorCode": backend.get("errorCode"),
        }
    try:
        status = await backend.get_status()
        return status
    except errors.SonarMcpError as e:
        return {
            "available": False,
            "configured": True,
            "error": f"[{e.code}] {e.user_message}",
        }
    except Exception as e:  # pragma: no cover - 防御性
        _log.exception("jetbrains_ide_status failed")
        return {"available": False, "configured": True, "error": str(e)}


async def impl_inspect_files(
    file_absolute_paths: list[str],
    project_root: str | None = None,
    errors_only: bool = False,
    timeout_ms: int | None = None,
    ctx: AnyContext | None = None,
) -> dict[str, Any]:
    """jetbrains_inspect_files:用 JetBrains inspections 分析 1..200 个文件"""
    start = time.monotonic()
    try:
        if not isinstance(file_absolute_paths, list):
            raise errors.bad_request("file_absolute_paths must be a list of strings.")
        if not file_absolute_paths:
            raise errors.bad_request("file_absolute_paths is empty.")

        roots = await gather_workspace_roots(ctx)
        # 即便客户端没声明 Roots,只要调用方传了 project_root 就把它视为允许的工作区。
        roots = ensure_workspace_roots(roots, project_root)
        if not roots:
            raise errors.workspace_not_configured(
                "No workspace roots available. Configure MCP Roots, set SONAR_WORKSPACE_ROOTS, "
                "or pass project_root."
            )

        max_files = _effective_max_files()
        if len(file_absolute_paths) > max_files:
            raise errors.too_many_files(
                f"Too many files: {len(file_absolute_paths)} > {max_files}."
            )

        accepted, skipped = filter_valid_files(file_absolute_paths, roots)
        if not accepted:
            raise errors.bad_request(
                "No valid files to inspect. "
                + ("; ".join(f"{p}: {c}" for p, c, _ in skipped[:5]))
                + ("..." if len(skipped) > 5 else "")
            )

        unique = dedupe_and_sort(accepted)
        pr = infer_single_project_root(unique, roots, project_root)
        if pr:
            _log.debug("Effective project_root=%s", pr)

        # timeout_ms 入参若提供,临时覆盖环境变量(进程内本次调用生效)。
        if timeout_ms is not None:
            os.environ["JETBRAINS_INSPECTION_TIMEOUT_MS"] = str(max(1000, int(timeout_ms)))

        backend = _make_backend()
        result: JetBrainsAnalysisResult = await backend.backend.analyze_files(
            unique, errors_only=errors_only, project_root=pr
        )
        return _result_to_dict(result, skipped, start, pr)
    except errors.SonarMcpError as e:
        _log.warning("jetbrains_inspect_files failed: %s", e)
        return error_dict(e, partial=True)
    except Exception as e:
        _log.exception("Unexpected error in jetbrains_inspect_files")
        return error_dict(errors.internal_error(str(e)), partial=True)


async def impl_inspect_git_changes(
    project_root: str,
    base_ref: str = "HEAD",
    include_untracked: bool = True,
    include_staged: bool = True,
    include_unstaged: bool = True,
    errors_only: bool = False,
    ctx: AnyContext | None = None,
) -> dict[str, Any]:
    """jetbrains_inspect_git_changes:收集 git 变更并用 JetBrains inspections 分析"""
    start = time.monotonic()
    try:
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
            return {
                "success": True,
                "partialSuccess": False,
                "requestedFileCount": 0,
                "analyzedFileCount": 0,
                "problemCount": 0,
                "problems": [],
                "failedFiles": [],
                "projectIndexing": False,
                "projectRoot": norm_root,
                "baseRef": base_ref,
                "changedFileCount": 0,
                "notices": ["No changed files to analyze."],
                "durationMs": int((time.monotonic() - start) * 1000),
            }

        max_files = _effective_max_files()
        if len(files) > max_files:
            raise errors.too_many_files(
                f"Too many changed files ({len(files)} > {max_files}). "
                "Analyze in smaller groups or commit some changes."
            )

        backend = _make_backend()
        result = await backend.backend.analyze_files(
            files, errors_only=errors_only, project_root=norm_root
        )
        d = _result_to_dict(result, [], start, norm_root)
        d["baseRef"] = base_ref
        d["changedFileCount"] = len(files)
        return d
    except errors.SonarMcpError as e:
        _log.warning("jetbrains_inspect_git_changes failed: %s", e)
        return error_dict(e, partial=True)
    except Exception as e:
        _log.exception("Unexpected error in jetbrains_inspect_git_changes")
        return error_dict(errors.internal_error(str(e)), partial=True)


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _result_to_dict(
    result: JetBrainsAnalysisResult,
    skipped: list[tuple[str, str, str]],
    start: float,
    project_root: str | None,
) -> dict[str, Any]:
    """把 JetBrainsAnalysisResult 转成对外 dict,并把分析前跳过项并入"""
    payload = result.model_dump(by_alias=True)
    payload["requestedFileCount"] = len(result.problems) + len(result.failed_files) + len(skipped)
    payload["analyzedFileCount"] = payload["requestedFileCount"] - len(skipped)
    payload["problemCount"] = len(result.problems)
    payload["projectRoot"] = project_root
    if skipped:
        for path, code, msg in skipped:
            payload.setdefault("failedFiles", []).append(
                {"filePath": path, "errorCode": code, "errorMessage": msg}
            )
        payload["partialSuccess"] = True
        payload.setdefault("notices", []).append(
            f"{len(skipped)} file(s) skipped before inspection."
        )
    payload["durationMs"] = payload.get("durationMs") or int((time.monotonic() - start) * 1000)
    return payload


__all__ = [
    "INSPECT_FILES_DESCRIPTION",
    "INSPECT_GIT_DESCRIPTION",
    "STATUS_DESCRIPTION",
    "impl_ide_status",
    "impl_inspect_files",
    "impl_inspect_git_changes",
]
