"""MCP 服务:四个桥接 Codex/Claude Code 与 SonarQube for IDE 的工具

工具:
  * sonar_ide_status          —— 扫描端口并报告实例。
  * sonar_analyze_files       —— 分析 1..200 个文件(自动分批)。
  * sonar_analyze_git_changes —— 收集 git 变更并分析。
  * sonar_clear_cache         —— 清除 project→port 缓存。

仅使用 stdio 传输;stdout 专用于 JSON-RPC,所有日志写入 stderr。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from . import errors
from .git_changes import collect_changed_files
from .ide_discovery import IdeDiscovery
from .logging_config import get_logger
from .models import AnalysisResult, ClearCacheResult, IdeStatusResult
from .path_utils import (
    check_symlink_escape,
    dedupe_and_sort,
    normalize_path,
    validate_regular_file,
)
from .result_summary import assert_single_project_root, build_result
from .sonar_client import SonarClient
from .workspace import resolve_workspace_roots

# FastMCP 的 Context 是泛型,参数化为 session/lifespan/request;我们不依赖具体 session 形态,统一用 Any。
AnyContext = Context[Any, Any, Any]

_log = get_logger("server")

MAX_FILES = 200
BATCH_SIZE = 50

# 模块级单例,可注入用于测试。
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
# FastMCP 应用构建
# ---------------------------------------------------------------------------


def build_app() -> FastMCP:
    """构建 FastMCP 应用并注册全部四个工具"""
    app = FastMCP(
        name="pycharm-sonar",
        instructions=(
            "Local bridge to PyCharm's SonarQube for IDE. "
            "Use sonar_ide_status to check availability, sonar_analyze_files to analyze "
            "absolute file paths, sonar_analyze_git_changes to analyze the working tree, "
            "and sonar_clear_cache to reset port discovery."
        ),
    )

    @app.tool(name="sonar_ide_status", description=_STATUS_DESCRIPTION)
    async def sonar_ide_status() -> dict[str, Any]:
        """扫描端口 64120..64130 并报告 SonarQube for IDE 实例"""
        return await _impl_ide_status()

    @app.tool(name="sonar_analyze_files", description=_ANALYZE_DESCRIPTION)
    async def sonar_analyze_files(
        file_absolute_paths: list[str],
        project_root: str | None = None,
        ctx: AnyContext | None = None,
    ) -> dict[str, Any]:
        """用 Sonar 分析一个或多个绝对路径文件"""
        return await _impl_analyze_files(file_absolute_paths, project_root, ctx)

    @app.tool(name="sonar_analyze_git_changes", description=_GIT_DESCRIPTION)
    async def sonar_analyze_git_changes(
        project_root: str,
        base_ref: str = "HEAD",
        include_untracked: bool = True,
        include_staged: bool = True,
        include_unstaged: bool = True,
        ctx: AnyContext | None = None,
    ) -> dict[str, Any]:
        """收集 project_root 下的 git 变更并用 Sonar 分析"""
        return await _impl_analyze_git_changes(
            project_root, base_ref, include_untracked, include_staged, include_unstaged, ctx
        )

    @app.tool(name="sonar_clear_cache", description=_CLEAR_DESCRIPTION)
    async def sonar_clear_cache(
        project_root: str | None = None,
    ) -> dict[str, Any]:
        """清除内存中的 project→port 缓存"""
        return _impl_clear_cache(project_root)

    return app


_STATUS_DESCRIPTION = (
    "Scan ports 64120-64130 on localhost and report how many SonarQube for IDE "
    "instances are reachable, with their ports and status. Use this for diagnostics."
)
_ANALYZE_DESCRIPTION = (
    "Analyze 1 to 200 absolute file paths with the user's local SonarQube for IDE. "
    "Files are auto-batched (50/batch). Returns findings with ruleKey, message, "
    "severity, filePath and textRange, plus per-file status and severity counts."
)
_GIT_DESCRIPTION = (
    "Collect changed files in the git repository under project_root (staged, unstaged, "
    "untracked; relative to base_ref) and analyze them with Sonar. Deleted files are excluded."
)
_CLEAR_DESCRIPTION = (
    "Clear the in-memory port discovery cache. Pass project_root to clear only one project."
)


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


async def _impl_ide_status() -> dict[str, Any]:
    discovery = get_discovery()
    try:
        instances = await asyncio.to_thread(discovery.discover_all_instances)
    except errors.SonarMcpError as e:
        return _error_dict(e)
    except Exception as e:  # pragma: no cover - defensive
        _log.exception("Unexpected error during ide_status")
        return _error_dict(errors.internal_error(str(e)))

    result = IdeStatusResult(
        available=bool(instances),
        instanceCount=len(instances),
        instances=instances,
    )
    return result.model_dump(by_alias=True)


def _filter_valid_files(
    raw_paths: list[Any],
    roots: list[str],
) -> tuple[list[str], list[tuple[str, str, str]]]:
    """把输入路径划分为已接受的绝对路径与 (path, code, msg) 跳过项

    每个被接受的路径都经过校验:存在、是普通文件、位于工作区内,且无 symlink/junction 逃逸。
    """
    from .path_utils import is_within_workspace

    accepted: list[str] = []
    skipped: list[tuple[str, str, str]] = []
    for raw in raw_paths:
        if not isinstance(raw, str):
            skipped.append((str(raw), errors.BAD_REQUEST, "Path is not a string."))
            continue
        if not os.path.isabs(raw):
            skipped.append((raw, errors.BAD_REQUEST, "Path must be absolute."))
            continue
        try:
            norm = validate_regular_file(raw)
        except errors.SonarMcpError as e:
            skipped.append((raw, e.code, e.user_message))
            continue
        if not is_within_workspace(norm, roots):
            skipped.append((norm, errors.WORKSPACE_VIOLATION, "File outside workspace roots."))
            continue
        if check_symlink_escape(norm, roots):
            skipped.append((norm, errors.SYMLINK_ESCAPE, "Symlink/junction escapes workspace."))
            continue
        accepted.append(norm)
    return accepted, skipped


def _merge_skips_into_result(result: AnalysisResult, skipped: list[tuple[str, str, str]]) -> None:
    """把分析前的跳过项就地附加到 AnalysisResult"""
    if not skipped:
        return
    from .models import FailedFile

    for path, code, msg in skipped:
        result.skipped_files.append(FailedFile(filePath=path, errorCode=code, errorMessage=msg))
        result.file_summaries.append(_make_skipped_summary(path, code, msg))
    result.skipped_file_count = len(result.skipped_files)
    result.partial_success = True
    result.notices.append(f"{len(skipped)} file(s) skipped before analysis.")


async def _impl_analyze_files(
    file_absolute_paths: list[str],
    project_root: str | None,
    ctx: AnyContext | None = None,
) -> dict[str, Any]:
    start = time.monotonic()
    try:
        if not isinstance(file_absolute_paths, list):  # 防御性:模型可能传入非列表
            raise errors.bad_request("file_absolute_paths must be a list of strings.")
        if not file_absolute_paths:
            raise errors.bad_request("file_absolute_paths is empty.")

        roots = await _gather_workspace_roots(ctx)
        if not roots:
            raise errors.workspace_not_configured(
                "No workspace roots available. Configure MCP Roots or set SONAR_WORKSPACE_ROOTS."
            )

        if len(file_absolute_paths) > MAX_FILES:
            raise errors.too_many_files(
                f"Too many files: {len(file_absolute_paths)} > {MAX_FILES}. "
                "Split into multiple calls or use sonar_analyze_git_changes."
            )

        accepted, skipped = _filter_valid_files(file_absolute_paths, roots)
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
            batch_size=BATCH_SIZE,
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
        return _error_dict(e, partial=True)
    except Exception as e:
        _log.exception("Unexpected error in analyze_files")
        return _error_dict(errors.internal_error(str(e)), partial=True)


async def _impl_analyze_git_changes(
    project_root: str,
    base_ref: str,
    include_untracked: bool,
    include_staged: bool,
    include_unstaged: bool,
    ctx: AnyContext | None = None,
) -> dict[str, Any]:
    start = time.monotonic()
    try:
        if not isinstance(project_root, str) or not project_root.strip():
            raise errors.bad_request("project_root is required.")
        norm_root = normalize_path(project_root)
        if not os.path.isdir(norm_root):
            raise errors.git_invalid_repository(f"project_root is not a directory: {norm_root}")

        roots = await _gather_workspace_roots(ctx)
        if not roots:
            # git 调用若无任何工作区配置,把 project_root 自动加入允许工作区。
            roots = [norm_root]
        elif not any(_within(norm_root, r) for r in roots):
            roots = [*roots, norm_root]

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

        # 复用 analyze_files 的后续流程(git 已产出经过工作区过滤的绝对存在文件,无需重复校验)。
        pr = assert_single_project_root(files, roots)
        discovery = get_discovery()
        port = await asyncio.to_thread(discovery.discover_for_project, pr)
        sonar = get_sonar_client()
        outcomes = await asyncio.to_thread(
            sonar.analyze_files_batched,
            port,
            files,
            batch_size=BATCH_SIZE,
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
        return _error_dict(e, partial=True)
    except Exception as e:
        _log.exception("Unexpected error in analyze_git_changes")
        return _error_dict(errors.internal_error(str(e)), partial=True)


def _impl_clear_cache(project_root: str | None) -> dict[str, Any]:
    """清除内存中的 project→port 缓存,同步函数无 I/O"""
    from .ide_discovery import get_global_cache

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
# 辅助函数
# ---------------------------------------------------------------------------


def _root_uri_to_path(uri: str) -> str | None:
    """把 root URI 转换为本地文件系统路径,非文件路径返回 None"""
    if uri.startswith("file://"):
        from urllib.request import url2pathname

        return url2pathname(uri[7:])
    if uri.startswith("/"):
        return uri
    return None


def _client_supports_roots(session: Any) -> bool:
    """判断 MCP 客户端是否声明了 Roots 能力"""
    check = getattr(session, "check_client_capability", None)
    if not callable(check):
        return False
    try:
        from mcp import types

        cap = types.ClientCapabilities(roots=types.RootsCapability())
        return bool(check(cap))
    except Exception:
        return False


async def _collect_roots_from_session(session: Any) -> list[str]:
    """从客户端 session 拉取 roots 并把 URI 转换为本地路径

    给 ``list_roots()`` 加 5 秒超时:有些客户端声明了 Roots 能力却不响应
    ``roots/list`` 请求,没有超时保护会让整个工具调用永久挂起。超时后回退到
    环境变量,工作区仍可用。
    """
    list_roots = getattr(session, "list_roots", None)
    if not callable(list_roots):
        return []
    try:
        roots_resp = await asyncio.wait_for(list_roots(), timeout=5.0)
    except TimeoutError:
        _log.warning(
            "Client declared Roots capability but did not respond to roots/list within 5s; falling back to SONAR_WORKSPACE_ROOTS"
        )
        return []
    except Exception as e:
        _log.debug("list_roots failed: %s", e)
        return []
    out: list[str] = []
    for r in getattr(roots_resp, "roots", []):
        uri = str(getattr(r, "uri", ""))
        path = _root_uri_to_path(uri)
        if path is not None:
            out.append(path)
    return out


async def _gather_workspace_roots(ctx: AnyContext | None = None) -> list[str]:
    """解析工作区根目录,优先取 MCP Roots,回退到环境变量

    若提供了 FastMCP Context 且客户端声明了 Roots 能力,则拉取 roots 列表并把
    file:// URI 转换为本地路径。
    """
    mcp_roots: list[str] = []
    if ctx is not None:
        try:
            request_ctx = getattr(ctx, "request_context", None)
            session = getattr(request_ctx, "session", None) if request_ctx is not None else None
            if session is not None and _client_supports_roots(session):
                mcp_roots = await _collect_roots_from_session(session)
        except Exception as e:  # pragma: no cover - defensive
            _log.debug("Could not read MCP roots: %s", e)

    return resolve_workspace_roots(mcp_roots or None)


def _within(child: str, parent: str) -> bool:
    nc = os.path.normcase(os.path.normpath(child))
    np_ = os.path.normcase(os.path.normpath(parent)).rstrip(os.sep)
    return nc == np_ or nc.startswith(np_ + os.sep)


def _error_dict(err: errors.SonarMcpError, *, partial: bool = False) -> dict[str, Any]:
    return {
        "success": False,
        "partialSuccess": partial,
        "errorCode": err.code,
        "errorMessage": err.user_message,
    }


def _make_skipped_summary(path: str, code: str, msg: str) -> Any:
    from .models import FileSummary

    return FileSummary(filePath=path, status="skipped", findingCount=0, detail=f"{code}: {msg}")


# ---------------------------------------------------------------------------
# cli.py 调用的入口
# ---------------------------------------------------------------------------


def run_stdio() -> None:
    """以 stdio 方式运行 MCP 服务,阻塞直至 stdin 关闭"""
    app = build_app()
    # FastMCP.run 是同步的,内部自行管理 asyncio 事件循环。
    app.run(transport="stdio")


async def run_stdio_async() -> None:
    """stdio 服务的异步入口(测试用)"""
    app = build_app()
    await app.run_stdio_async()
