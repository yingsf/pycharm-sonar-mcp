"""规范化工具(路径 / 范围 / 消息)

把来自不同后端的"同一信息"的不同表达,统一到稳定可比较的形式,
这是确定性去重的前置步骤。

本模块不抛异常:任何异常输入都退化为安全默认值(空串 / 1-based 最小坐标),
保证去重引擎永远拿到可用数据。
"""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Final

# ---------------------------------------------------------------------------
# 正则:编译一次复用
# ---------------------------------------------------------------------------

# 连续空白 -> 单空格
_WS_RE: Final[re.Pattern[str]] = re.compile(r"\s+")

# 结尾标点(中英文)统一去除
_TRAILING_PUNCT_RE: Final[re.Pattern[str]] = re.compile(r"[。.,;；!！?？.]+$")

# 字符串字面量(单/双/三引号,含中文引号配对)-> <str>
# 顺序:三引号在前,避免被单引号规则吃掉。
_STR_LIT_RE: Final[re.Pattern[str]] = re.compile(
    r'"""[\s\S]*?"""'  # 三双引号
    r"|'''[\s\S]*?'''"  # 三单引号
    r'|"(?:\\.|[^"\\])*"'  # 双引号
    r"|'(?:\\.|[^'\\])*'"  # 单引号
)

# 中文引号配对的字符串字面量(“…”/‘…’)
_CN_STR_RE: Final[re.Pattern[str]] = re.compile(r"“[^”]*”|‘[^’]*’")

# 整数/浮点(含千分位、负号、科学计数法)-> <num>
_NUM_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z_])-?\d[\d,]*\.?\d+(?:[eE][+-]?\d+)?(?![A-Za-z_])"
)

# 中文标点引号 -> 英文引号(成对替换,先做)
_QUOTE_MAP: Final[dict[str, str]] = {
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "「": "'",
    "」": "'",
    "『": "'",
    "』": "'",
    "《": "<",
    "》": ">",
}


def normalize_file_path(path: str) -> str:
    """规范化文件路径,用于稳定比较

    实现:os.path.normpath + os.path.normcase。
    与 core.path_utils.normalize_path 不同,这里**不**解析为绝对路径,
    因为我们只在归一化后比较"是否同文件",而不是判断工作区包含关系;
    调用方在更早阶段已经传入规范化后的绝对路径。

    空串或非字符串返回空串。
    """
    if not path or not isinstance(path, str):
        return ""
    norm = os.path.normpath(path)
    return os.path.normcase(norm)


def normalize_range(
    start_line: int,
    start_col: int,
    end_line: int,
    end_col: int,
) -> tuple[int, int, int, int]:
    """规范化文本范围,确保 1-based 且 end 不在 start 之前

    Args:
        start_line: 起始行(1-based)。
        start_col: 起始列(1-based,字节偏移或字符偏移均可,只要前后端一致)。
        end_line: 结束行。
        end_col: 结束列。

    Returns:
        (start_line, start_col, end_line, end_col),所有值 >= 1,
        且 end_line/end_col 不会小于 start_line/start_col(否则交换)。
    """
    sl = _to_positive_int(start_line)
    sc = _to_positive_int(start_col)
    el = _to_positive_int(end_line)
    ec = _to_positive_int(end_col)

    # 保证 end 不在 start 之前(同列比较只对同行有意义)。
    if (el, ec) < (sl, sc):
        sl, sc, el, ec = el, ec, sl, sc
    return sl, sc, el, ec


def _to_positive_int(value: int) -> int:
    """把任意值转成 >= 1 的整数;非法值降级为 1"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 1
    return v if v >= 1 else 1


def normalize_message(msg: str) -> str:
    """规范化问题消息文本,用于相似度比较与指纹计算

    处理顺序很重要(每一步都依赖前一步的输出):
      1. Unicode NFKC(兼容性分解,半角化全角字符)。
      2. 中文标点/引号替换为英文等价物。
      3. lowercase。
      4. trim 首尾空白。
      5. 合并连续空白为单空格。
      6. 去除结尾标点。
      7. 字符串字面量(含中文引号串)替换为 ``<str>``。
      8. 数字字面量替换为 ``<num>``。

    本函数兼容中文消息,不依赖英文分词;空/非字符串返回空串。
    """
    if not msg or not isinstance(msg, str):
        return ""

    # 1) Unicode NFKC
    s = unicodedata.normalize("NFKC", msg)

    # 2) 中文标点引号替换
    for cn, en in _QUOTE_MAP.items():
        s = s.replace(cn, en)

    # 3) lowercase
    s = s.lower()

    # 4) trim
    s = s.strip()

    # 5) 合并连续空白
    s = _WS_RE.sub(" ", s)

    # 6) 去除结尾标点(可能多次,循环去除)
    # 用 while 而非正则全局替换,因为我们只关心结尾。
    prev: str | None = None
    while prev != s:
        prev = s
        s = _TRAILING_PUNCT_RE.sub("", s)
    s = s.strip()

    # 7) 字符串字面量 -> <str>(先中文配对引号,再标准引号)
    s = _CN_STR_RE.sub("<str>", s)
    s = _STR_LIT_RE.sub("<str>", s)

    # 8) 数字 -> <num>
    s = _NUM_RE.sub("<num>", s)

    # 替换后再次合并空白(替换不会引入空白,但稳妥起见)。
    s = _WS_RE.sub(" ", s).strip()
    return s


__all__ = ["normalize_file_path", "normalize_message", "normalize_range"]
