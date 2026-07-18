"""PyCharm Sonar MCP —— 连接 Codex/Claude Code 与 PyCharm 中 SonarQube for IDE 的本地桥接层

本包仅作为本地桥接层,不打包、不分发、不修改任何 SonarSource 分析器、插件、规则包或
二进制组件。它只调用用户本机 PyCharm 中 SonarQube for IDE 已在回环接口上开放的本地 HTTP 接口。
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
