"""JetBrains MCP 客户端:基于 streamable_http + ClientSession 调用 IDE inspections

本模块只读:仅调用 get_file_problems(必需)与 get_project_status(可选)两个白名单工具,
绝不实现任何代理 / 执行类工具。所有调用都带超时保护,headers 不进入日志。

为什么 get_project_status 是可选的:PyCharm MCP Server 在 2026.1 起不再暴露该工具,
但 get_file_problems 仍然是分析能力的最小必需。get_project_status 缺失时,
项目 indexing 状态会降级为"未知"(False),不影响 analyze 流程。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import httpx

from ... import errors
from ...logging_config import get_logger
from .config import JetBrainsConfig
from .models import JetBrainsProblem
from .parser import parse_get_file_problems_result, parse_project_status

if TYPE_CHECKING:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import GetSessionIdCallback

# mcp SDK 自带类型存根,strict mypy 可直接解析。
# MemoryObject* 类型在 mcp.shared.memory 中只是隐式重导出,mypy 报 attr-defined;
# 因此直接从其真实定义处 anyio.streams.memory 导入,语义完全等价。
from anyio.streams.memory import (
    MemoryObjectReceiveStream,
    MemoryObjectSendStream,
)
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

_log = get_logger("jetbrains.client")

# 只允许调用这两个工具,杜绝越权执行
ALLOWED_TOOLS: frozenset[str] = frozenset({"get_project_status", "get_file_problems"})
# 必需工具:连接时校验存在;get_project_status 自 PyCharm 2026.1 起不再暴露,降级为可选。
REQUIRED_TOOLS: frozenset[str] = frozenset({"get_file_problems"})
# 可选工具:缺失或调用失败时,相关字段降级(不影响 analyze 主流程)。
OPTIONAL_TOOLS: frozenset[str] = frozenset({"get_project_status"})

_CLIENT_NAME = "pycharm-code-quality-mcp-client"
_CLIENT_VERSION = "1.0.0"


class JetBrainsClient:
    """通过 MCP streamable-http 协议调用 PyCharm 的 JetBrains MCP Server

    生命周期:connect() -> get_project_status / get_file_problems ... -> close()。
    亦支持 async with 上下文管理。每次 connect 都会做 initialize + tools/list,
    并校验两个必需工具存在。
    """

    def __init__(self, config: JetBrainsConfig, timeout_ms: int = 30000) -> None:
        self._config = config
        self._timeout_s = max(1.0, timeout_ms / 1000.0)
        # 由 connect() 创建的资源,close() 时清理。
        self._exit_stack: contextlib.AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._session_id_getter: GetSessionIdCallback | None = None
        # connect() 时填充:服务端实际暴露的工具名集合。
        self._available_tools: frozenset[str] = frozenset()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """建立 streamable_http 连接 + ClientSession,初始化并校验工具

        失败时抛 SonarMcpError(JETBRAINS_CONNECTION_FAILED / TIMEOUT /
        REQUIRED_TOOL_MISSING 等),并自动释放已申请的资源。
        """
        if self._session is not None:
            return  # 幂等
        # 注意:日志里绝不输出 headers。
        _log.info("Connecting to JetBrains MCP at %s", self._config.url)
        exit_stack = contextlib.AsyncExitStack()
        try:
            # 1. httpx.AsyncClient(承载 headers,不走弃用的 headers 参数)
            http_client = await exit_stack.enter_async_context(
                _http_client(self._config, self._timeout_s)
            )
            # 2. streamable_http_client(显式注入 http_client)
            transport_ctx = streamable_http_client(self._config.url, http_client=http_client)
            streams = await exit_stack.enter_async_context(transport_ctx)
            read_stream, write_stream, session_id_cb = _unpack_streams(streams)
            self._session_id_getter = session_id_cb

            # 3. ClientSession + initialize
            session = await exit_stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    client_info=types.Implementation(
                        name=_CLIENT_NAME,
                        version=_CLIENT_VERSION,
                    ),
                )
            )
            try:
                await asyncio.wait_for(session.initialize(), timeout=self._timeout_s)
            except TimeoutError as e:
                raise errors.jetbrains_timeout(
                    f"JetBrains MCP initialize timed out after {self._timeout_s:.0f}s."
                ) from e
            except Exception as e:
                raise errors.jetbrains_connection_failed(
                    f"JetBrains MCP initialize failed: {e}"
                ) from e

            # 4. tools/list + 校验必需工具
            await self._verify_required_tools(session)

            self._exit_stack = exit_stack
            self._session = session
            session_id = self._safe_session_id()
            _log.info("JetBrains MCP connected (session=%s, tools verified)", session_id)
        except errors.SonarMcpError:
            await _aclose(exit_stack)
            self._session = None
            self._exit_stack = None
            self._session_id_getter = None
            raise
        except Exception as e:
            await _aclose(exit_stack)
            self._session = None
            self._exit_stack = None
            self._session_id_getter = None
            raise errors.jetbrains_connection_failed(
                f"Failed to connect to JetBrains MCP: {e}"
            ) from e

    async def _verify_required_tools(self, session: ClientSession) -> None:
        """list_tools 并校验必需工具存在;同时缓存可用工具供后续可选工具判断"""
        try:
            list_result = await asyncio.wait_for(session.list_tools(), timeout=self._timeout_s)
        except TimeoutError as e:
            raise errors.jetbrains_timeout(
                f"JetBrains MCP tools/list timed out after {self._timeout_s:.0f}s."
            ) from e
        except Exception as e:
            raise errors.jetbrains_connection_failed(f"JetBrains MCP tools/list failed: {e}") from e
        available = {tool.name for tool in list_result.tools}
        self._available_tools = frozenset(available)
        missing = REQUIRED_TOOLS - self._available_tools
        if missing:
            raise errors.jetbrains_required_tool_missing(
                "JetBrains MCP server is missing required tools: "
                f"{sorted(missing)}. Available: {sorted(available)}."
            )
        # 提示可选工具缺失(仅日志,不抛错)。
        missing_optional = OPTIONAL_TOOLS - self._available_tools
        if missing_optional:
            _log.info(
                "JetBrains MCP optional tools not exposed by server (will degrade): %s",
                sorted(missing_optional),
            )

    # ------------------------------------------------------------------
    # 工具调用
    # ------------------------------------------------------------------

    async def get_project_status(self, project_root: str | None = None) -> dict[str, Any]:
        """调用 get_project_status 工具,返回解析后的 dict(含 isIndexing)

        若服务端未暴露该工具(PyCharm 2026.1+),返回降级 dict 而非抛错:
        ``{"isIndexing": False, "projectStatusAvailable": False}``。

        Args:
            project_root: 项目根目录。多项目场景下建议传入以消歧。
        """
        session = self._require_session()
        # 可选工具:服务端未暴露时降级。
        if "get_project_status" not in self._available_tools:
            _log.debug("get_project_status not exposed by server; returning degraded status.")
            return {"isIndexing": False, "projectStatusAvailable": False}
        arguments: dict[str, Any] = {}
        if project_root:
            arguments["projectPath"] = project_root
        try:
            result = await asyncio.wait_for(
                session.call_tool("get_project_status", arguments=arguments or None),
                timeout=self._timeout_s,
            )
        except TimeoutError as e:
            raise errors.jetbrains_timeout(
                f"get_project_status timed out after {self._timeout_s:.0f}s."
            ) from e
        except errors.SonarMcpError:
            raise
        except Exception as e:
            raise errors.jetbrains_tool_failed(f"get_project_status call failed: {e}") from e
        if result.isError:
            raise errors.jetbrains_tool_failed(_format_tool_error("get_project_status", result))
        structured = result.structuredContent
        content_list = _coerce_content(result.content)
        return parse_project_status(structured, content_list)

    async def get_file_problems(
        self,
        file_path: str,
        project_root: str | None = None,
        errors_only: bool = False,
    ) -> list[JetBrainsProblem]:
        """调用 get_file_problems 工具,返回解析后的问题列表

        Args:
            file_path: 绝对文件路径。
            project_root: 项目根目录绝对路径。**强烈建议传入**:PyCharm MCP Server
                要求 ``projectPath`` 来消歧(尤其是用户开了多个项目时);不传会报
                "Unable to determine the target project"。
            errors_only: 若为 True,请求 IDE 仅返回 error 级别问题(参数尽力而为)。
        """
        session = self._require_session()
        # PyCharm MCP Server 的 get_file_problems 期望:
        #   - filePath:  相对 projectPath 的路径(schema required 字段)
        #   - projectPath: 项目根目录(强烈建议,多项目场景必需)
        #   - errorsOnly: 可选布尔
        # 我们对外 API 接收绝对路径(与 Sonar 后端一致),内部转换。
        arguments: dict[str, Any] = {}
        if project_root:
            arguments["projectPath"] = project_root
            arguments["filePath"] = _to_relative_path(file_path, project_root)
        else:
            # 没有 project_root 时退回绝对路径(可能被 PyCharm 拒绝,但保留兼容)。
            arguments["filePath"] = file_path
        if errors_only:
            arguments["errorsOnly"] = True
        try:
            result = await asyncio.wait_for(
                session.call_tool("get_file_problems", arguments=arguments),
                timeout=self._timeout_s,
            )
        except TimeoutError as e:
            raise errors.jetbrains_timeout(
                f"get_file_problems timed out after {self._timeout_s:.0f}s for {file_path}."
            ) from e
        except errors.SonarMcpError:
            raise
        except Exception as e:
            raise errors.jetbrains_tool_failed(
                f"get_file_problems call failed for {file_path}: {e}"
            ) from e
        if result.isError:
            raise errors.jetbrains_tool_failed(
                _format_tool_error("get_file_problems", result, file_path)
            )
        structured = result.structuredContent
        content_list = _coerce_content(result.content)
        return parse_get_file_problems_result(content_list, structured, file_path)

    # ------------------------------------------------------------------
    # 关闭 / 上下文管理
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """关闭 session 与底层传输,幂等"""
        stack = self._exit_stack
        self._session = None
        self._exit_stack = None
        self._session_id_getter = None
        if stack is not None:
            await _aclose(stack)

    async def __aenter__(self) -> JetBrainsClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise errors.jetbrains_connection_failed(
                "JetBrainsClient is not connected; call connect() first."
            )
        return self._session

    def _safe_session_id(self) -> str | None:
        getter = self._session_id_getter
        if getter is None:
            return None
        try:
            return getter()
        except Exception:
            return None


# ---------------------------------------------------------------------------
# 模块级辅助函数
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _http_client(
    config: JetBrainsConfig, timeout_s: float
) -> AsyncIterator[httpx.AsyncClient]:
    """构造带 headers 的 httpx.AsyncClient 作为 streamable_http 的底层 HTTP"""
    client = httpx.AsyncClient(
        headers=dict(config.headers) if config.headers else None,
        timeout=httpx.Timeout(timeout_s),
    )
    try:
        yield client
    finally:
        await client.aclose()


def _unpack_streams(
    streams: Any,
) -> tuple[
    MemoryObjectReceiveStream[Any],
    MemoryObjectSendStream[Any],
    GetSessionIdCallback,
]:
    """从 streamable_http_client 的 yield 值中解出 (read, write, session_id_cb)"""
    # 不同 mcp 版本可能返回 2 元组或 3 元组,统一兜底处理。
    if len(streams) >= 3:
        return streams[0], streams[1], streams[2]
    if len(streams) == 2:
        # 极少数情况下没有 session_id 回调,补一个空实现。
        return streams[0], streams[1], _noop_session_id
    raise errors.jetbrains_bad_response(
        f"Unexpected number of streams from streamable_http_client: {len(streams)}"
    )


def _noop_session_id() -> str | None:
    return None


def _to_relative_path(file_path: str, project_root: str) -> str:
    """把绝对文件路径转成相对 project_root 的路径(PyCharm MCP 的 filePath 参数要求)

    若 file_path 不在 project_root 下(无法相对化),原样返回绝对路径(让 PyCharm 报错)。
    使用 os.path.relpath 处理跨平台分隔符。
    """
    import os

    rel = os.path.relpath(file_path, project_root)
    # PyCharm 期望正斜杠(即便在 Windows 上)。
    return rel.replace(os.sep, "/")


def _coerce_content(content: Any) -> list[Any]:
    """把 CallToolResult.content 转成普通 list,容忍 None 或非列表类型"""
    if content is None:
        return []
    if isinstance(content, list):
        return content
    return [content]


def _format_tool_error(tool_name: str, result: Any, file_path: str | None = None) -> str:
    """把 CallToolResult 的 isError=True 转成可读消息,不泄露堆栈"""
    where = f" for {file_path}" if file_path else ""
    text_parts: list[str] = []
    content = _coerce_content(getattr(result, "content", None))
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            text_parts.append(text.strip())
            if sum(len(p) for p in text_parts) > 500:
                break
    detail = "; ".join(text_parts) if text_parts else "<no detail>"
    return f"JetBrains MCP tool {tool_name}{where} returned an error: {detail}"


async def _aclose(stack: contextlib.AsyncExitStack) -> None:
    """安全关闭 AsyncExitStack,吞掉关闭过程中的异常避免遮蔽原始错误"""
    with contextlib.suppress(Exception):
        await stack.aclose()


__all__ = ["ALLOWED_TOOLS", "OPTIONAL_TOOLS", "REQUIRED_TOOLS", "JetBrainsClient"]
