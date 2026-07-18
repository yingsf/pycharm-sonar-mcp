"""把 sonar_tools 的 SonarClient/IdeDiscovery 单例包装成 SonarBackend

quality_tools 复用 sonar_tools 维护的 client/discovery 单例,避免出现两份
端口缓存与两套 HTTP transport。sonar_tools 负责生命周期(测试可注入),
本模块只做适配。
"""

from __future__ import annotations

from ..backends.sonar.analyzer import SonarBackend
from . import sonar_tools


def get_sonar_backend() -> SonarBackend:
    """返回复用 sonar_tools 单例的 SonarBackend"""
    return SonarBackend(
        client=sonar_tools.get_sonar_client(), discovery=sonar_tools.get_discovery()
    )


__all__ = ["get_sonar_backend"]
