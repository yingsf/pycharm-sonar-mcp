"""兼容包装层:pycharm_sonar_mcp 已重命名为 pycharm_code_quality_mcp

本包仅作为向后兼容入口,所有实现已迁移到 pycharm_code_quality_mcp。
新代码应直接导入 pycharm_code_quality_mcp。

迁移提示在 cli.main() 入口输出,避免每次 import 都触发 warning。
"""

from __future__ import annotations

from pycharm_code_quality_mcp import __version__

__all__ = ["__version__"]
