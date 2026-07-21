"""PyCharm Code Quality MCP —— 连接 Codex/Claude Code 与 PyCharm 代码质量检查的本地桥接层

默认使用 PyCharm 内置 JetBrains MCP Server 提供的 inspections。
SonarQube for IDE 是可选增强后端;没有安装 Sonar 插件时,工具仍然正常运行。

本包不打包、不分发、不修改任何 SonarSource 分析器或 JetBrains 二进制组件。
它只通过标准 MCP 协议调用用户本机已开放的本地接口。
"""

from __future__ import annotations

__version__ = "1.0.1"
__all__ = ["__version__"]
