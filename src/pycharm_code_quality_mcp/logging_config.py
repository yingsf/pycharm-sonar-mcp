"""日志配置

所有日志输出写入 stderr;stdout 专门保留给 MCP JSON-RPC 通信,任何日志都不得污染 stdout。
日志级别通过环境变量 PYCHARM_SONAR_MCP_LOG_LEVEL 配置,取值 DEBUG/INFO/WARNING/ERROR,默认 INFO。
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Final

_ENV_LOG_LEVEL: Final[str] = "PYCHARM_CODE_QUALITY_MCP_LOG_LEVEL"
_DEFAULT_LEVEL: Final[str] = "INFO"
_VALID_LEVELS: Final[frozenset[str]] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})
_CONFIGURED: bool = False

_LOGGER_NAME: Final[str] = "pycharm_code_quality_mcp"


_LEGACY_ENV_LOG_LEVEL: Final[str] = "PYCHARM_SONAR_MCP_LOG_LEVEL"


def _resolve_level() -> int:
    """根据环境变量解析日志级别整数,非法值回退到 INFO

    优先读新名 PYCHARM_CODE_QUALITY_MCP_LOG_LEVEL,
    回退到旧名 PYCHARM_SONAR_MCP_LOG_LEVEL 以兼容已部署用户。
    """
    raw = os.environ.get(_ENV_LOG_LEVEL) or os.environ.get(_LEGACY_ENV_LOG_LEVEL) or _DEFAULT_LEVEL
    raw = raw.strip().upper()
    if raw not in _VALID_LEVELS:
        raw = _DEFAULT_LEVEL
    return int(getattr(logging, raw))


def configure_logging() -> logging.Logger:
    """配置并返回包级 logger

    幂等:可多次调用,仅首次创建 handler,后续调用仅刷新级别。
    所有 handler 写入 stderr,且 `propagate=False` 防止日志冒泡到可能持有 stdout 的 root logger。
    """
    global _CONFIGURED
    logger = logging.getLogger(_LOGGER_NAME)

    if _CONFIGURED:
        logger.setLevel(_resolve_level())
        return logger

    logger.setLevel(_resolve_level())
    # 关闭向 root logger 冒泡,避免第三方库的 root handler 把日志写到 stdout。
    logger.propagate = False

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # 时间戳统一使用 UTC,保证不同时区机器的日志可比较。
    formatter.converter = _utc_time  # type: ignore[assignment]
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    _CONFIGURED = True
    return logger


def _utc_time(*args: object) -> float:
    """返回当前 UTC 时间的 struct_time,供 logging.Formatter.converter 使用"""
    return time.mktime(time.gmtime())


def get_logger(name: str | None = None) -> logging.Logger:
    """返回包级 logger 的子 logger,首次调用时完成配置

    Args:
        name: 子 logger 名称;None 或包名返回根包 logger,否则返回 `pycharm_sonar_mcp.<name>`。
    """
    configure_logging()
    if name is None or name == _LOGGER_NAME:
        return logging.getLogger(_LOGGER_NAME)
    if name.startswith(f"{_LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")
