"""后端抽象基类

定义分析后端的统一接口，JetBrains 和 Sonar 后端都实现此接口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AnalysisBackend(ABC):
    """分析后端抽象基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """后端标识名"""

    @abstractmethod
    async def is_available(self, **kwargs: Any) -> bool:
        """后端是否可用"""

    @abstractmethod
    async def analyze_files(
        self, file_paths: list[str], errors_only: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        """分析指定文件列表，返回结构化结果

        Args:
            file_paths: 绝对路径列表
            errors_only: 只返回 ERROR 级别问题

        Returns:
            包含 findings/problems/success/duration_ms 的 dict
        """

    @abstractmethod
    async def get_status(self, **kwargs: Any) -> dict[str, Any]:
        """返回后端状态"""
