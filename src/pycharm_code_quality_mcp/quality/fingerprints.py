"""稳定 SHA-256 指纹

为每一条问题生成确定性的短 id(前缀 ``qf_`` + 12 位 hex)。

关键约束:
  * **绝不**使用内置 ``hash()``,因为它是随机化的(进程间不稳定)。
  * 输入字段(文件路径、category、范围、消息、代码锚点 hash)在拼接前
    已经在各自的规范化模块里处理过,因此指纹只反映"语义等价类"。
  * 字段之间使用不会出现在字段内部的分隔符(记录分隔符 ``\\x1e``),
    避免"字段粘连"造成碰撞。
"""

from __future__ import annotations

import hashlib
from typing import Final

# 记录分隔符(ASCII 0x1e),不会出现在规范化后的路径/消息/规则 id 里。
_SEP: Final[str] = "\x1e"

# 统一问题 id 前缀。
_ID_PREFIX: Final[str] = "qf_"

# 截取的 hex 位数(48-bit,碰撞概率对本地代码质量场景足够低)。
_HEX_LEN: Final[int] = 12


def compute_finding_id(
    file_path: str,
    category: str,
    range_tuple: tuple[int, int, int, int],
    message: str,
    anchor_hash: str | None,
) -> str:
    """根据稳定字段计算问题 id

    Args:
        file_path: 已规范化的文件路径。
        category: 归一化后的问题类别常量。
        range_tuple: (start_line, start_col, end_line, end_col),1-based。
        message: 已规范化的消息文本。
        anchor_hash: 代码锚点 SHA-256 hex;无锚点时传 None。

    Returns:
        形如 ``qf_<12 hex>`` 的稳定 id。
    """
    sl, sc, el, ec = range_tuple
    anchor = anchor_hash if anchor_hash else "-"
    payload = _SEP.join(
        [
            file_path or "",
            category or "",
            f"{sl}:{sc}:{el}:{ec}",
            message or "",
            anchor,
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{_ID_PREFIX}{digest[:_HEX_LEN]}"


__all__ = ["compute_finding_id"]
