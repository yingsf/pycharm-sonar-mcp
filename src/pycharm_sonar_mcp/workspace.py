"""Workspace roots 解析

来源优先级(spec 第 9 节 "允许工作区来源"):
  1. MCP client 的 Roots capability(按请求传入)。
  2. SONAR_WORKSPACE_ROOTS 环境变量(以 os.pathsep 分隔)。
  3. 否则:拒绝分析。

绝不放行对任意磁盘路径的分析。
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from . import errors
from .logging_config import get_logger
from .path_utils import normalize_path

_log = get_logger("workspace")

ENV_WORKSPACE_ROOTS = "SONAR_WORKSPACE_ROOTS"


def _collect_mcp_roots(mcp_roots: list[str] | None) -> list[str]:
    """从 MCP client 传入的 roots 中返回去空白后的非空项"""
    if not mcp_roots:
        return []
    roots = [r.strip() for r in mcp_roots if r and r.strip()]
    if roots:
        _log.debug("Using %d MCP client roots", len(roots))
    return roots


def _collect_env_roots(env_map: Mapping[str, str]) -> list[str]:
    """从 SONAR_WORKSPACE_ROOTS 环境变量解析 roots,未设置时返回空"""
    raw = env_map.get(ENV_WORKSPACE_ROOTS, "").strip()
    if not raw:
        return []
    parts = raw.split(os.pathsep)
    roots = [p.strip() for p in parts if p.strip()]
    if roots:
        _log.debug("Using %d SONAR_WORKSPACE_ROOTS roots", len(roots))
    return roots


def resolve_workspace_roots(
    mcp_roots: list[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """解析允许的 workspace roots 列表

    Args:
        mcp_roots: MCP client 针对本次请求上报的 Roots,可为空或 None。
        env: 可选的环境变量覆盖(供测试使用),默认为 os.environ。

    Returns:
        归一化后的绝对根路径列表;未配置时返回空列表。

    Raises:
        SonarMcpError(WORKSPACE_NOT_CONFIGURED):仅由 `require_workspace_roots` 抛出。
    """
    env_map: Mapping[str, str] = env if env is not None else os.environ

    roots = _collect_mcp_roots(mcp_roots)
    if not roots:
        roots = _collect_env_roots(env_map)

    # 归一化并去重。
    normed: list[str] = []
    seen: set[str] = set()
    for r in roots:
        try:
            n = normalize_path(r)
        except errors.SonarMcpError:
            _log.warning("Skipping un-normalizable workspace root: %s", r)
            continue
        key = os.path.normcase(n)
        if key in seen:
            continue
        seen.add(key)
        normed.append(n)
    return normed


def require_workspace_roots(
    mcp_roots: list[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """同 resolve_workspace_roots,但在未配置任何 root 时抛出异常"""
    roots = resolve_workspace_roots(mcp_roots, env)
    if not roots:
        raise errors.workspace_not_configured(
            "No workspace roots configured. The MCP client did not provide Roots, and "
            f"{ENV_WORKSPACE_ROOTS} is not set. Configure Roots in your MCP client, or set "
            f"{ENV_WORKSPACE_ROOTS} to one or more absolute project paths separated by "
            f"os.pathsep ({os.pathsep!r})."
        )
    return roots
