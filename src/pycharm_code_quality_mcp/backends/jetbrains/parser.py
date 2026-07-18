"""解析 JetBrains MCP CallToolResult,提取问题列表与项目状态

解析优先级:
1. structuredContent(MCP 规范化结构化输出,优先使用)
2. content 中的 TextContent(尝试 json.loads)
3. 纯文本兼容解析(最终兜底)

所有解析都容忍未知字段(extra="allow"),避免未来 IDE 版本字段变动导致崩溃。
行列号保持 JetBrains 返回的 1-based 语义,不做任何转换。
"""

from __future__ import annotations

import json
from typing import Any

from ...logging_config import get_logger
from .models import JetBrainsProblem

_log = get_logger("jetbrains.parser")


def parse_get_file_problems_result(
    content: list[Any],
    structured_content: dict[str, Any] | None,
    file_path: str,
) -> list[JetBrainsProblem]:
    """从 get_file_problems 的 CallToolResult 中提取问题列表

    Args:
        content: CallToolResult.content 原始列表(TextContent 等)。
        structured_content: CallToolResult.structuredContent,可能为 None。
        file_path: 本次请求的文件路径,用于在缺失 filePath 字段时回填。

    Returns:
        解析得到的问题列表;无问题时返回空列表。
    """
    # 1. 优先使用 structuredContent
    if structured_content is not None:
        problems = _extract_problems_from_object(structured_content, file_path)
        if problems is not None:
            return problems

    # 2. 尝试从 TextContent 解析 JSON
    text_payloads = _collect_text_payloads(content)
    for text in text_payloads:
        parsed_obj = _try_json_object(text)
        if parsed_obj is not None:
            problems = _extract_problems_from_object(parsed_obj, file_path)
            if problems is not None:
                return problems

    # 3. 纯文本兼容解析:把整段文本当作单条描述/无可解析结果
    # 若所有 content 都是纯文本且无法解析为 JSON,则视为无结构化问题。
    _log.debug(
        "get_file_problems: no parseable problems for %s (structured=%s, text_blocks=%d)",
        file_path,
        structured_content is not None,
        len(text_payloads),
    )
    return []


def parse_project_status(
    structured_content: dict[str, Any] | None,
    content: list[Any],
) -> dict[str, Any]:
    """解析 get_project_status 的结果

    Returns:
        一个 dict,至少包含 `isIndexing: bool` 字段;无法判断时默认 False。
        若 structuredContent/content 中包含其他字段,会一并无损透传。
    """
    merged: dict[str, Any] = {}

    # 1. 优先 structuredContent
    if isinstance(structured_content, dict):
        merged.update(structured_content)

    # 2. TextContent 中的 JSON 对象
    for text in _collect_text_payloads(content):
        parsed_obj = _try_json_object(text)
        if isinstance(parsed_obj, dict):
            merged.update(parsed_obj)

    # 兼容大小写与可选别名
    if "isIndexing" not in merged:
        for key in ("is_indexing", "indexing", "indexInProgress", "isSmartMode"):
            if key in merged:
                merged["isIndexing"] = bool(merged[key])
                break
    # 默认值:无法确定时不阻塞分析
    merged.setdefault("isIndexing", False)
    return merged


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _collect_text_payloads(content: list[Any]) -> list[str]:
    """从 content 列表中提取所有 TextContent 的纯文本字符串"""
    out: list[str] = []
    if not content:
        return out
    for block in content:
        text = _text_from_block(block)
        if text:
            out.append(text)
    return out


def _text_from_block(block: Any) -> str | None:
    """从单个 content block 提取纯文本

    支持 mcp.types.TextContent 实例,以及带 'type'/'text' 字段的 dict。
    """
    if block is None:
        return None
    # mcp.types.TextContent 实例
    text_attr = getattr(block, "text", None)
    block_type = getattr(block, "type", None)
    if isinstance(text_attr, str) and block_type in ("text", None):
        return text_attr
    # dict-like
    if isinstance(block, dict):
        if block.get("type", "text") == "text" and isinstance(block.get("text"), str):
            text_value: str = block["text"]
            return text_value
        # 兼容没有 type 字段的纯文本 dict
        fallback_text = block.get("text")
        if isinstance(fallback_text, str):
            return fallback_text
    # 字符串兜底
    if isinstance(block, str):
        return block
    return None


def _try_json_object(text: str) -> dict[str, Any] | None:
    """尝试把字符串解析为 JSON 对象,失败或非对象时返回 None"""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(obj, dict):
        return obj
    return None


def _extract_problems_from_object(
    obj: dict[str, Any],
    file_path: str,
) -> list[JetBrainsProblem] | None:
    """从单个 JSON 对象中提取 problems 列表

    支持多种常见字段名:problems / findings / diagnostics / issues。
    缺失时返回 None,表示此对象不含 problems(交由调用方继续尝试其他来源)。
    返回空列表表示确实无问题。
    """
    raw_list: Any = None
    for key in ("problems", "findings", "diagnostics", "issues"):
        if key in obj and isinstance(obj[key], list):
            raw_list = obj[key]
            break
    if raw_list is None:
        return None

    problems: list[JetBrainsProblem] = []
    for idx, item in enumerate(raw_list):
        if not isinstance(item, dict):
            _log.debug("Skipping non-object problem at index %d: %r", idx, item)
            continue
        enriched = dict(item)
        # 若问题本身没有 filePath,用本次请求的 file_path 回填。
        enriched.setdefault("filePath", file_path)
        # 行列号兜底:缺失时按 1-based 起点填充,避免下游崩溃。
        enriched.setdefault("startLine", 1)
        enriched.setdefault("startColumn", 1)
        enriched.setdefault("endLine", enriched.get("startLine", 1))
        enriched.setdefault("endColumn", enriched.get("startColumn", 1))
        try:
            problem = JetBrainsProblem.model_validate(enriched)
        except Exception as e:  # 解析必须不崩溃
            _log.warning(
                "Failed to parse problem #%d for %s: %s; raw=%r",
                idx,
                file_path,
                e,
                item,
            )
            continue
        # 把原始 dict 挂到 raw,便于前向兼容字段访问。
        if not problem.raw:
            problem.raw = item
        problems.append(problem)
    return problems


__all__ = [
    "parse_get_file_problems_result",
    "parse_project_status",
]
