"""代码上下文锚点读取(仅用于去重指纹)

本模块只读取问题行±少量上下文行(最多 3 行),用于计算稳定 hash,
**绝不**将源码文本输出到日志或返回给调用方,从而避免泄露敏感代码片段。

设计要点:
  * 只读取磁盘上已存在的常规文件;读取失败一律返回 None,不抛异常。
  * 规范化(去首尾空白、合并连续空白、lowercase)在算 hash 之前完成,
    以抵抗无关的空白差异。
  * 字符数上限保护日志体积与 hash 计算成本。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ..logging_config import get_logger

_log = get_logger("file_context")

# 单次读取的最大行数(目标行 + 上下各 context_lines 行),硬上限 3 行。
_MAX_CONTEXT_LINES: int = 1
# 单次读取的最大字符数,避免一行源码极长时拖慢 hash 计算。
_DEFAULT_MAX_CHARS: int = 500
# 硬性行数上限:即便调用方传入更大的 context_lines,也不会超过 3 行。
_HARD_MAX_TOTAL_LINES: int = 3

# 用于合并连续空白的正则;编译一次复用。
_WS_RE: re.Pattern[str] = re.compile(r"\s+")


def read_code_anchor(
    file_path: str, line: int, context_lines: int = 1, max_chars: int = _DEFAULT_MAX_CHARS
) -> str | None:
    """读取问题行±context_lines 行(最多 3 行,最多 max_chars 字符),返回规范化后的文本

    Args:
        file_path: 文件路径(任意形式,内部不做规范化,只尝试读取)。
        line: 1-based 的问题行号;小于 1 视为 1。
        context_lines: 上下文行数,实际行数受硬上限 3 行约束。
        max_chars: 返回文本的字符上限,超出则截断。

    Returns:
        规范化后的文本(去首尾空白、合并连续空白、lowercase);
        文件不存在或读取失败返回 None。**绝不**向日志输出源码内容。
    """
    if not file_path or not isinstance(file_path, str):
        return None
    # 约束上下文行数:每侧最多 1 行 => 总行数 <= 3。
    ctx = max(0, min(int(context_lines), _MAX_CONTEXT_LINES))
    if ctx == 0:
        # 仍允许只读问题行本身,但总行数仍受 _HARD_MAX_TOTAL_LINES 约束。
        total_lines = 1
    else:
        total_lines = 1 + 2 * ctx
        if total_lines > _HARD_MAX_TOTAL_LINES:
            total_lines = _HARD_MAX_TOTAL_LINES

    try:
        target = max(1, int(line))
    except (TypeError, ValueError):
        return None

    try:
        with open(file_path, encoding="utf-8", errors="replace") as fh:
            # 只读取需要的窗口,避免把整个大文件加载进内存。
            lines = _read_window(fh, target, ctx, total_lines)
    except OSError as exc:
        # 不输出文件内容,只记录失败原因与行号。
        _log.debug("Failed to read code anchor: %s (line=%d)", type(exc).__name__, target)
        return None

    if not lines:
        return None

    text = "\n".join(lines)
    if max_chars > 0:
        text = text[:max_chars]
    return _normalize_anchor(text)


def _read_window(fh: Any, target: int, ctx: int, total_lines: int) -> list[str]:
    """从已打开的文件对象中读取 [target-ctx, target+ctx] 窗口内的非空行文本

    返回原始(未规范化)行列表;文件过短或目标行超出范围时返回尽可能多的行。
    """
    start = max(1, target - ctx)
    # 需要读取到 end 行(含),因此最多读取 end 行。
    end = target + ctx
    wanted_count = end - start + 1
    if wanted_count > total_lines:
        wanted_count = total_lines

    collected: list[str] = []
    current = 0
    for raw in fh:
        current += 1
        if current < start:
            continue
        if current > end:
            break
        # 去掉行尾换行符,保留行内空白以备规范化阶段统一处理。
        collected.append(raw.rstrip("\r\n"))
        if len(collected) >= wanted_count:
            break
    return collected


def _normalize_anchor(text: str) -> str:
    """规范化锚点文本:去首尾空白、合并连续空白、lowercase

    规范化的目的是让 hash 只反映"语义相同的代码片段",
    而不受无关空白差异影响。
    """
    if not text:
        return ""
    stripped = text.strip()
    if not stripped:
        return ""
    compact = _WS_RE.sub(" ", stripped)
    return compact.lower()


def compute_anchor_hash(file_path: str, line: int, context_lines: int = 1) -> str | None:
    """读取代码上下文并计算 SHA-256 hash

    只返回 hash 十六进制字符串,**不**返回源码文本,也**不**记录源码到日志。
    文件不存在或读取失败时返回 None。

    Args:
        file_path: 文件路径。
        line: 1-based 的问题行号。
        context_lines: 上下文行数(每侧),受硬上限约束。

    Returns:
        SHA-256 十六进制 digest;失败返回 None。
    """
    anchor = read_code_anchor(file_path, line, context_lines=context_lines)
    if anchor is None or anchor == "":
        return None
    return hashlib.sha256(anchor.encode("utf-8")).hexdigest()


__all__ = ["compute_anchor_hash", "read_code_anchor"]
