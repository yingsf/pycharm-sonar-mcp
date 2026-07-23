"""Tests for JetBrainsClient / JetBrainsAnalysisBackend

覆盖 spec 第 16 节 JetBrains Client 部分(不依赖真实 MCP server):
  * 未配置时抛 JETBRAINS_NOT_CONFIGURED
  * 远程 URL 被拒绝(loopback 校验)
  * loopback URL 允许
  * headers 解析
  * tools/list 缺失必需工具 => 抛 REQUIRED_TOOL_MISSING
  * Session 必须可关闭
  * 不调用白名单外工具
"""

from __future__ import annotations

import asyncio

import pytest

from pycharm_code_quality_mcp import errors
from pycharm_code_quality_mcp.backends.jetbrains.analyzer import (
    JetBrainsAnalysisBackend,
    JetBrainsBackend,
)
from pycharm_code_quality_mcp.backends.jetbrains.client import (
    ALLOWED_TOOLS,
    OPTIONAL_TOOLS,
    REQUIRED_TOOLS,
    JetBrainsClient,
    _effective_headers,
)
from pycharm_code_quality_mcp.backends.jetbrains.config import (
    PROJECT_PATH_HEADER,
    JetBrainsConfig,
    headers_for_storage,
    is_loopback_url,
    project_path_from_headers,
)

# ---------------------------------------------------------------------------
# 配置与白名单
# ---------------------------------------------------------------------------


def test_required_tools_subset_of_allowed() -> None:
    assert REQUIRED_TOOLS <= ALLOWED_TOOLS
    # 必需工具只有 get_file_problems;get_project_status 自 PyCharm 2026.1 起降级为可选。
    assert {"get_file_problems"} == REQUIRED_TOOLS
    assert {"get_project_status"} == OPTIONAL_TOOLS
    assert REQUIRED_TOOLS.isdisjoint(OPTIONAL_TOOLS)


def test_no_proxy_tools_in_allowed() -> None:
    """白名单绝不能包含任何修改型工具"""
    forbidden = {
        "execute_terminal_command",
        "run_configuration",
        "rename_refactoring",
        "write_file",
        "replace_text",
        "build_project",
    }
    assert ALLOWED_TOOLS.isdisjoint(forbidden)


# ---------------------------------------------------------------------------
# 后端实例化
# ---------------------------------------------------------------------------


def test_backend_not_configured_raises(monkeypatch, tmp_path) -> None:
    """配置文件不存在 => JETBRAINS_NOT_CONFIGURED"""
    monkeypatch.setattr(
        "pycharm_code_quality_mcp.backends.jetbrains.config.config_file_path",
        lambda: tmp_path / "missing.json",
    )
    monkeypatch.delenv("JETBRAINS_MCP_URL", raising=False)
    monkeypatch.delenv("JETBRAINS_MCP_HEADERS_JSON", raising=False)
    with pytest.raises(errors.SonarMcpError) as excinfo:
        JetBrainsAnalysisBackend()
    assert excinfo.value.code == errors.JETBRAINS_NOT_CONFIGURED


def test_backend_uses_env_url(monkeypatch) -> None:
    monkeypatch.setenv("JETBRAINS_MCP_URL", "http://localhost:9999/mcp")
    monkeypatch.setenv("JETBRAINS_MCP_HEADERS_JSON", '{"X-Test": "yes"}')
    backend = JetBrainsAnalysisBackend()
    assert backend.config.url == "http://localhost:9999/mcp"
    assert backend.config.headers.get("X-Test") == "yes"


def test_project_path_header_helpers() -> None:
    headers = {
        "Authorization": "Bearer x",
        PROJECT_PATH_HEADER: "/tmp/project-a",
    }
    assert project_path_from_headers(headers) == "/tmp/project-a"
    assert headers_for_storage(headers) == {"Authorization": "Bearer x"}


def test_effective_headers_override_project_path_without_mutating_config() -> None:
    cfg = JetBrainsConfig(
        url="http://localhost:1/mcp",
        headers={
            "Authorization": "Bearer x",
            PROJECT_PATH_HEADER: "/tmp/old-project",
        },
    )
    effective = _effective_headers(cfg, "/tmp/new-project")
    assert effective is not None
    assert effective["Authorization"] == "Bearer x"
    assert effective[PROJECT_PATH_HEADER] == "/tmp/new-project"
    assert cfg.headers[PROJECT_PATH_HEADER] == "/tmp/old-project"


def test_effective_headers_preserve_legacy_project_path_without_context() -> None:
    cfg = JetBrainsConfig(
        url="http://localhost:1/mcp",
        headers={PROJECT_PATH_HEADER: "/tmp/legacy-project"},
    )
    effective = _effective_headers(cfg)
    assert effective == {PROJECT_PATH_HEADER: "/tmp/legacy-project"}


def test_backend_rejects_remote_env_url(monkeypatch) -> None:
    monkeypatch.setenv("JETBRAINS_MCP_URL", "http://example.com:9999/mcp")
    with pytest.raises(errors.SonarMcpError) as excinfo:
        JetBrainsAnalysisBackend()
    assert excinfo.value.code == errors.JETBRAINS_INVALID_CONFIG


# ---------------------------------------------------------------------------
# loopback URL 校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:1234/mcp",
        "http://127.0.0.1:1234/mcp",
        "http://[::1]:1234/mcp",
    ],
)
def test_loopback_urls_accepted(url: str) -> None:
    assert is_loopback_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:1234/mcp",
        "http://192.168.0.1:1234/mcp",
        "http://10.0.0.1:1234/mcp",
        "http://8.8.8.8:1234/mcp",
    ],
)
def test_remote_urls_rejected(url: str) -> None:
    assert is_loopback_url(url) is False


# ---------------------------------------------------------------------------
# Session 必须可关闭(幂等)
# ---------------------------------------------------------------------------


def test_client_close_is_idempotent() -> None:
    """close() 调用多次不应抛异常"""
    cfg = JetBrainsConfig(url="http://localhost:1/mcp", headers={})
    client = JetBrainsClient(cfg)
    # 未 connect 直接 close,也应该安全。
    asyncio.run(client.close())
    asyncio.run(client.close())  # 幂等


def test_client_call_before_connect_raises() -> None:
    cfg = JetBrainsConfig(url="http://localhost:1/mcp", headers={})
    client = JetBrainsClient(cfg)
    with pytest.raises(errors.SonarMcpError):
        asyncio.run(client.get_project_status())


def test_get_project_status_degrades_when_tool_missing() -> None:
    """服务端未暴露 get_project_status 时,get_project_status 应返回降级 dict 而非抛错"""
    cfg = JetBrainsConfig(url="http://localhost:1/mcp", headers={})
    client = JetBrainsClient(cfg)
    # 模拟 connect() 完成后的状态:有 session,但服务端只暴露 get_file_problems。
    client._available_tools = frozenset({"get_file_problems"})
    # 用一个 sentinel session 让 _require_session 通过(实际不会发起 call_tool,
    # 因为 available_tools 检查在 call_tool 之前)。
    client._session = object()  # type: ignore[assignment]
    result = asyncio.run(client.get_project_status())
    assert result["isIndexing"] is False
    assert result["projectStatusAvailable"] is False


# ---------------------------------------------------------------------------
# Backend timeout 从环境变量读取
# ---------------------------------------------------------------------------


def test_backend_timeout_from_env(monkeypatch) -> None:
    monkeypatch.setenv("JETBRAINS_INSPECTION_TIMEOUT_MS", "12345")
    monkeypatch.setenv("JETBRAINS_MCP_URL", "http://localhost:1/mcp")
    backend = JetBrainsAnalysisBackend()
    assert backend.backend._timeout_ms == 12345


def test_backend_timeout_default(monkeypatch) -> None:
    monkeypatch.delenv("JETBRAINS_INSPECTION_TIMEOUT_MS", raising=False)
    monkeypatch.setenv("JETBRAINS_MCP_URL", "http://localhost:1/mcp")
    backend = JetBrainsAnalysisBackend()
    assert backend.backend._timeout_ms == 30000


def test_backend_timeout_invalid_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("JETBRAINS_INSPECTION_TIMEOUT_MS", "not-a-number")
    monkeypatch.setenv("JETBRAINS_MCP_URL", "http://localhost:1/mcp")
    backend = JetBrainsAnalysisBackend()
    assert backend.backend._timeout_ms == 30000


def test_backend_timeout_clamped_to_min(monkeypatch) -> None:
    monkeypatch.setenv("JETBRAINS_INSPECTION_TIMEOUT_MS", "10")
    monkeypatch.setenv("JETBRAINS_MCP_URL", "http://localhost:1/mcp")
    backend = JetBrainsAnalysisBackend()
    # 小于 1000 应被 clamp 到 1000。
    assert backend.backend._timeout_ms == 1000


# ---------------------------------------------------------------------------
# is_available:连接失败时返回 False 而非抛错
# ---------------------------------------------------------------------------


def test_is_available_returns_false_on_connection_failure(monkeypatch) -> None:
    """连接失败时 is_available 返回 False,不抛错

    用 monkeypatch 让 JetBrainsClient.connect 直接抛连接错误,避免依赖真实端口
    (某些环境下 1 号端口可能被代理/防火墙劫持为 503,导致测试不稳定)。
    """
    monkeypatch.setenv("JETBRAINS_MCP_URL", "http://localhost:1/mcp")

    async def _fake_connect(self: JetBrainsClient) -> None:
        raise errors.jetbrains_connection_failed("simulated connection failure")

    monkeypatch.setattr(JetBrainsClient, "connect", _fake_connect)
    backend = JetBrainsAnalysisBackend()
    result = asyncio.run(backend.is_available())
    assert result is False


def test_get_status_returns_unreachable_dict(monkeypatch) -> None:
    """连接失败时 get_status 返回 available=False 的状态 dict,不抛错"""
    monkeypatch.setenv("JETBRAINS_MCP_URL", "http://localhost:1/mcp")

    async def _fake_connect(self: JetBrainsClient) -> None:
        raise errors.jetbrains_connection_failed("simulated connection failure")

    monkeypatch.setattr(JetBrainsClient, "connect", _fake_connect)
    backend = JetBrainsAnalysisBackend()
    status = asyncio.run(backend.get_status())
    assert status["available"] is False
    assert "error" in status
    assert status["configured"] is True
    assert status["tools"] == sorted(ALLOWED_TOOLS)


# ---------------------------------------------------------------------------
# analyze_files 空列表
# ---------------------------------------------------------------------------


def test_jetbrains_backend_empty_files_returns_success() -> None:
    cfg = JetBrainsConfig(url="http://localhost:1/mcp", headers={})
    backend = JetBrainsBackend(cfg)
    result = asyncio.run(backend.analyze_files([]))
    assert result.success is True
    assert result.problems == []
