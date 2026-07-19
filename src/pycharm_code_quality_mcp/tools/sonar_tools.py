"""Sonar 后端的单例管理(供 code_quality_* 工具复用)

本模块只维护 SonarClient / IdeDiscovery 的进程级单例,供
``tools._sonar_instances.get_sonar_backend()`` 包装成 SonarBackend
后注入 QualityOrchestrator。这样所有 code_quality_* 调用都复用同一份
端口缓存和 HTTP transport,不会出现两份并行的 Sonar 状态。

测试可通过 ``reset_singletons(client=..., discovery=...)`` 注入替身。
"""

from __future__ import annotations

from ..backends.sonar.client import SonarClient
from ..backends.sonar.discovery import IdeDiscovery

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


__all__ = [
    "get_discovery",
    "get_sonar_client",
    "reset_singletons",
]
