"""MCP 服务:注册全部 11 个工具 + stdio 启动

工具总览(共 11 个):

  统一默认(code_quality_*,推荐):
    * code_quality_status
    * code_quality_analyze_files
    * code_quality_analyze_git_changes
    * code_quality_clear_cache

  JetBrains 专用(jetbrains_*):
    * jetbrains_ide_status
    * jetbrains_inspect_files
    * jetbrains_inspect_git_changes

  旧 Sonar(sonar_*,契约保持不变):
    * sonar_ide_status
    * sonar_analyze_files
    * sonar_analyze_git_changes
    * sonar_clear_cache

实现细节位于 ``tools/`` 子包,本模块只负责:
  * 创建 FastMCP 应用;
  * 注册工具(把参数声明映射到 tools 层的 impl 函数);
  * 提供 stdio 启动入口。

仅使用 stdio 传输;stdout 专用于 JSON-RPC,所有日志写入 stderr。
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .logging_config import get_logger
from .tools import jetbrains_tools, quality_tools, sonar_tools
from .tools._shared import AnyContext

_log = get_logger("server")


# ---------------------------------------------------------------------------
# FastMCP 应用构建
# ---------------------------------------------------------------------------


def build_app() -> FastMCP:
    """构建 FastMCP 应用并注册全部 11 个工具"""
    app = FastMCP(
        name="pycharm-code-quality",
        instructions=(
            "Local code-quality MCP bridging Codex/Claude Code to PyCharm. "
            "Default backend is JetBrains inspections (PyCharm's built-in MCP Server); "
            "SonarQube for IDE is an auto-detected optional enhancement. "
            "Prefer the code_quality_* tools: they run both backends in 'auto' mode and "
            "merge duplicates deterministically. The sonar_* tools are kept for backward "
            "compatibility and return their original contract."
        ),
    )

    # ------------------------------------------------------------------
    # 统一默认工具(4 个)
    # ------------------------------------------------------------------
    app.add_tool(
        _wrap_status(),
        name="code_quality_status",
        description=quality_tools.STATUS_DESCRIPTION,
    )
    app.add_tool(
        _wrap_quality_analyze_files(),
        name="code_quality_analyze_files",
        description=quality_tools.ANALYZE_FILES_DESCRIPTION,
    )
    app.add_tool(
        _wrap_quality_analyze_git_changes(),
        name="code_quality_analyze_git_changes",
        description=quality_tools.ANALYZE_GIT_DESCRIPTION,
    )
    app.add_tool(
        _wrap_quality_clear_cache(),
        name="code_quality_clear_cache",
        description=quality_tools.CLEAR_CACHE_DESCRIPTION,
    )

    # ------------------------------------------------------------------
    # JetBrains 专用工具(3 个)
    # ------------------------------------------------------------------
    app.add_tool(
        _wrap_jb_status(),
        name="jetbrains_ide_status",
        description=jetbrains_tools.STATUS_DESCRIPTION,
    )
    app.add_tool(
        _wrap_jb_inspect_files(),
        name="jetbrains_inspect_files",
        description=jetbrains_tools.INSPECT_FILES_DESCRIPTION,
    )
    app.add_tool(
        _wrap_jb_inspect_git_changes(),
        name="jetbrains_inspect_git_changes",
        description=jetbrains_tools.INSPECT_GIT_DESCRIPTION,
    )

    # ------------------------------------------------------------------
    # 旧 Sonar 工具(4 个,契约保持不变)
    # ------------------------------------------------------------------
    app.add_tool(
        _wrap_sonar_status(),
        name="sonar_ide_status",
        description=sonar_tools.STATUS_DESCRIPTION,
    )
    app.add_tool(
        _wrap_sonar_analyze_files(),
        name="sonar_analyze_files",
        description=sonar_tools.ANALYZE_DESCRIPTION,
    )
    app.add_tool(
        _wrap_sonar_analyze_git_changes(),
        name="sonar_analyze_git_changes",
        description=sonar_tools.GIT_DESCRIPTION,
    )
    app.add_tool(
        _wrap_sonar_clear_cache(),
        name="sonar_clear_cache",
        description=sonar_tools.CLEAR_DESCRIPTION,
    )

    return app


# ---------------------------------------------------------------------------
# 工具函数包装(把异步 impl 函数包装成 FastMCP 接受的 callable)
# ---------------------------------------------------------------------------


def _wrap_status() -> Any:
    async def code_quality_status(ctx: AnyContext | None = None) -> dict[str, Any]:
        """Report status of both JetBrains and Sonar backends"""
        return await quality_tools.impl_status(ctx)

    return code_quality_status


def _wrap_quality_analyze_files() -> Any:
    async def code_quality_analyze_files(
        file_absolute_paths: list[str],
        project_root: str | None = None,
        backend_mode: str = "auto",
        errors_only: bool = False,
        deduplication_mode: str = "balanced",
        ctx: AnyContext | None = None,
    ) -> dict[str, Any]:
        """Analyze files with the unified backend strategy and deterministic dedup"""
        return await quality_tools.impl_analyze_files(
            file_absolute_paths,
            project_root=project_root,
            backend_mode=backend_mode,
            errors_only=errors_only,
            deduplication_mode=deduplication_mode,
            ctx=ctx,
        )

    return code_quality_analyze_files


def _wrap_quality_analyze_git_changes() -> Any:
    async def code_quality_analyze_git_changes(
        project_root: str,
        base_ref: str = "HEAD",
        include_untracked: bool = True,
        include_staged: bool = True,
        include_unstaged: bool = True,
        backend_mode: str = "auto",
        errors_only: bool = False,
        deduplication_mode: str = "balanced",
        ctx: AnyContext | None = None,
    ) -> dict[str, Any]:
        """Collect git changes and analyze with the unified backend strategy"""
        return await quality_tools.impl_analyze_git_changes(
            project_root,
            base_ref=base_ref,
            include_untracked=include_untracked,
            include_staged=include_staged,
            include_unstaged=include_unstaged,
            backend_mode=backend_mode,
            errors_only=errors_only,
            deduplication_mode=deduplication_mode,
            ctx=ctx,
        )

    return code_quality_analyze_git_changes


def _wrap_quality_clear_cache() -> Any:
    async def code_quality_clear_cache(
        project_root: str | None = None,
    ) -> dict[str, Any]:
        """Clear in-memory caches for all backends"""
        return quality_tools.impl_clear_cache(project_root)

    return code_quality_clear_cache


# -- JetBrains --


def _wrap_jb_status() -> Any:
    async def jetbrains_ide_status() -> dict[str, Any]:
        """Probe JetBrains MCP Server configuration and availability"""
        return await jetbrains_tools.impl_ide_status()

    return jetbrains_ide_status


def _wrap_jb_inspect_files() -> Any:
    async def jetbrains_inspect_files(
        file_absolute_paths: list[str],
        project_root: str | None = None,
        errors_only: bool = False,
        timeout_ms: int | None = None,
        ctx: AnyContext | None = None,
    ) -> dict[str, Any]:
        """Inspect files with PyCharm's built-in JetBrains inspections"""
        return await jetbrains_tools.impl_inspect_files(
            file_absolute_paths,
            project_root=project_root,
            errors_only=errors_only,
            timeout_ms=timeout_ms,
            ctx=ctx,
        )

    return jetbrains_inspect_files


def _wrap_jb_inspect_git_changes() -> Any:
    async def jetbrains_inspect_git_changes(
        project_root: str,
        base_ref: str = "HEAD",
        include_untracked: bool = True,
        include_staged: bool = True,
        include_unstaged: bool = True,
        errors_only: bool = False,
        ctx: AnyContext | None = None,
    ) -> dict[str, Any]:
        """Collect git changes and inspect them with JetBrains inspections"""
        return await jetbrains_tools.impl_inspect_git_changes(
            project_root,
            base_ref=base_ref,
            include_untracked=include_untracked,
            include_staged=include_staged,
            include_unstaged=include_unstaged,
            errors_only=errors_only,
            ctx=ctx,
        )

    return jetbrains_inspect_git_changes


# -- Sonar (legacy) --


def _wrap_sonar_status() -> Any:
    async def sonar_ide_status() -> dict[str, Any]:
        """Scan ports 64120..64130 and report SonarQube for IDE instances"""
        return await sonar_tools.impl_ide_status()

    return sonar_ide_status


def _wrap_sonar_analyze_files() -> Any:
    async def sonar_analyze_files(
        file_absolute_paths: list[str],
        project_root: str | None = None,
        ctx: AnyContext | None = None,
    ) -> dict[str, Any]:
        """Analyze files with the user's local SonarQube for IDE"""
        return await sonar_tools.impl_analyze_files(file_absolute_paths, project_root, ctx)

    return sonar_analyze_files


def _wrap_sonar_analyze_git_changes() -> Any:
    async def sonar_analyze_git_changes(
        project_root: str,
        base_ref: str = "HEAD",
        include_untracked: bool = True,
        include_staged: bool = True,
        include_unstaged: bool = True,
        ctx: AnyContext | None = None,
    ) -> dict[str, Any]:
        """Collect git changes and analyze them with Sonar"""
        return await sonar_tools.impl_analyze_git_changes(
            project_root, base_ref, include_untracked, include_staged, include_unstaged, ctx
        )

    return sonar_analyze_git_changes


def _wrap_sonar_clear_cache() -> Any:
    async def sonar_clear_cache(
        project_root: str | None = None,
    ) -> dict[str, Any]:
        """Clear the in-memory Sonar port discovery cache"""
        return sonar_tools.impl_clear_cache(project_root)

    return sonar_clear_cache


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


# ---------------------------------------------------------------------------
# 向后兼容:旧测试 / 兼容层可能从 server 模块导入这些符号
# ---------------------------------------------------------------------------

# FastMCP Context 的再导出(旧代码可能 `from .server import AnyContext`)。
_ = Context  # 保持 Context 在模块命名空间可见

# 旧 sonar_* 工具的 impl 与单例 —— 已迁移到 tools.sonar_tools。
# 保留 re-export 以避免破坏旧测试 / 兼容层(不在此处维护任何业务逻辑)。
# ruff 的 F401/I001 不适用于显式 re-export,整体加 noqa。
from .tools.sonar_tools import (  # noqa: E402,F401,I001
    get_discovery as get_discovery,
    get_sonar_client as get_sonar_client,
    impl_analyze_files as _impl_analyze_files,
    impl_analyze_git_changes as _impl_analyze_git_changes,
    impl_clear_cache as _impl_clear_cache,
    impl_ide_status as _impl_ide_status,
    reset_singletons as reset_singletons,
)

__all__ = [
    "AnyContext",
    "build_app",
    "get_discovery",
    "get_sonar_client",
    "reset_singletons",
    "run_stdio",
    "run_stdio_async",
]
