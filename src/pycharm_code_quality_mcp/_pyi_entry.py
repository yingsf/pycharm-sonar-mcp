"""PyInstaller 入口脚本

`__main__.py` 使用相对导入(``from .cli import main``),PyInstaller 把文件作为顶层脚本
运行时相对导人会失败。本文件使用绝对导入启动 CLI,仅供打包时使用;
正常的 `python -m pycharm_code_quality_mcp` 仍走 ``__main__.py``。
"""

from __future__ import annotations

import multiprocessing
import sys

from pycharm_code_quality_mcp.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
