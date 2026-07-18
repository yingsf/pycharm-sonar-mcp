"""模块入口:`python -m pycharm_sonar_mcp` 等价于运行 CLI"""

from __future__ import annotations

import multiprocessing
import sys

from .cli import main

if __name__ == "__main__":
    # Windows PyInstaller 冻结环境下,任何 multiprocessing 使用前必须调用 freeze_support。
    multiprocessing.freeze_support()
    sys.exit(main())
