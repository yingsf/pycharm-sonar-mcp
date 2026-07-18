"""兼容入口:已迁移到 pycharm_code_quality_mcp.cli"""

from __future__ import annotations

import sys


def main() -> int:
    """旧命令 pycharm-sonar-mcp 的兼容入口

    输出迁移提示后转发到新实现。
    """
    sys.stderr.write(
        "注意:pycharm-sonar-mcp 已升级为 pycharm-code-quality-mcp。\n"
        "请更新命令为 pycharm-code-quality-mcp,并更新 MCP 配置名为 "
        "pycharm-code-quality。\n旧命令将继续工作,但建议尽快迁移。\n\n"
    )
    from pycharm_code_quality_mcp.cli import main as _main

    return _main()
