"""工具层共享 helper

把原先散落在 ``server.py`` 里的工作区解析、路径校验、错误格式化等纯 helper
集中到此处,供 ``sonar_tools`` / ``jetbrains_tools`` / ``quality_tools`` 复用,
避免在三套工具里维护三份相同逻辑。

设计约束:
  * 本模块不持有任何业务状态;单例(Sonar client / discovery / JetBrains config)
    仍在各自的工具模块里管理,便于按需替换与测试注入。
  * 所有日志写入 stderr,绝不污染 stdout(JSON-RPC 专线)。
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import Context

from .. import errors
from ..core.path_utils import (
    check_symlink_escape,
    is_within_workspace,
    normalize_path,
    validate_regular_file,
)
from ..core.workspace import resolve_workspace_roots
from ..logging_config import get_logger

_log = get_logger("tools.shared")

# FastMCP 的 Context 是泛型,参数化为 session/lifespan/request;这里不依赖具体形态,统一 Any。
AnyContext = Context[Any, Any, Any]

# 单次工具调用允许的最大文件数(spec 第 3.2 节)。
MAX_FILES = 200
# Sonar 批量分析的单批大小(一次 HTTP 请求的文件数上限)。
SONAR_BATCH_SIZE = 50


# ---------------------------------------------------------------------------
# Workspace roots
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
    import asyncio

    list_roots = getattr(session, "list_roots", None)
    if not callable(list_roots):
        return []
    try:
        roots_resp = await asyncio.wait_for(list_roots(), timeout=5.0)
    except TimeoutError:
        _log.warning(
            "Client declared Roots capability but did not respond to roots/list within 5s; "
            "falling back to SONAR_WORKSPACE_ROOTS"
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


async def gather_workspace_roots(ctx: AnyContext | None = None) -> list[str]:
    """解析工作区根目录,优先取 MCP Roots,回退到环境变量

    若提供了 FastMCP Context 且客户端声明了 Roots 能力,则拉取 roots 列表并把
    file:// URI 转换为本地路径。
    """
    import asyncio

    mcp_roots: list[str] = []
    if ctx is not None:
        try:
            request_ctx = getattr(ctx, "request_context", None)
            session = getattr(request_ctx, "session", None) if request_ctx is not None else None
            if session is not None and _client_supports_roots(session):
                mcp_roots = await _collect_roots_from_session(session)
        except Exception as e:  # pragma: no cover - defensive
            _log.debug("Could not read MCP roots: %s", e)
        # 防御性:某些 Context 实现里 list_roots 可能不是 coroutine,await 链路若抛
        # asyncio.CancelledError 也应原样上抛,不能吞掉(否则会让 stdio 卡住)。
        _ = asyncio

    return resolve_workspace_roots(mcp_roots or None)


# ---------------------------------------------------------------------------
# 路径校验
# ---------------------------------------------------------------------------


def filter_valid_files(
    raw_paths: list[Any],
    roots: list[str],
) -> tuple[list[str], list[tuple[str, str, str]]]:
    """把输入路径划分为已接受的绝对路径与 (path, code, msg) 跳过项

    每个被接受的路径都经过校验:存在、是普通文件、位于工作区内,且无 symlink/junction 逃逸。
    """
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


def within(child: str, parent: str) -> bool:
    """判断 ``child`` 路径是否位于 ``parent`` 之下(含相等)"""
    nc = os.path.normcase(os.path.normpath(child))
    np_ = os.path.normcase(os.path.normpath(parent)).rstrip(os.sep)
    return nc == np_ or nc.startswith(np_ + os.sep)


def ensure_workspace_roots(roots: list[str], ctx_project_root: str | None = None) -> list[str]:
    """确保工作区根目录非空;若提供了 project_root 且不在 roots 内则补入

    用于 git_changes 流程:即便客户端没声明 Roots,也要把显式传入的
    project_root 视作允许的工作区。
    """
    if not roots:
        if ctx_project_root:
            return [normalize_path(ctx_project_root)]
        return []
    if ctx_project_root and not any(within(normalize_path(ctx_project_root), r) for r in roots):
        return [*roots, normalize_path(ctx_project_root)]
    return roots


# ---------------------------------------------------------------------------
# 错误与摘要
# ---------------------------------------------------------------------------


def error_dict(err: errors.SonarMcpError, *, partial: bool = False) -> dict[str, Any]:
    """把 SonarMcpError 序列化为返回给 MCP 客户端的 dict"""
    return {
        "success": False,
        "partialSuccess": partial,
        "errorCode": err.code,
        "errorMessage": err.user_message,
    }


def make_skipped_summary(path: str, code: str, msg: str) -> Any:
    """构造一个 status=skipped 的 FileSummary(Sonar 工具结果中使用)"""
    from ..backends.sonar.models import FileSummary

    return FileSummary(filePath=path, status="skipped", findingCount=0, detail=f"{code}: {msg}")


__all__ = [
    "MAX_FILES",
    "SONAR_BATCH_SIZE",
    "AnyContext",
    "ensure_workspace_roots",
    "error_dict",
    "filter_valid_files",
    "gather_workspace_roots",
    "make_skipped_summary",
    "within",
]
