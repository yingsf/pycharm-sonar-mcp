"""严重程度归一化

不同后端用各自的严重等级词汇表:
  * SonarQube for IDE: BLOCKER / CRITICAL / MAJOR / MINOR / INFO
  * JetBrains inspections: ERROR / WARNING / WEAK WARNING / TYPO / INFORMATION / SERVER PROBLEM ...

本模块把它们统一映射到 6 个稳定等级(BLOCKER/CRITICAL/MAJOR/MINOR/INFO/UNKNOWN),
并为每个等级赋予单调的整数 rank,方便比较"谁更严重"。

确定性:相同输入始终得到相同输出;不联网、不依赖外部数据。
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# 稳定等级常量与 rank
# ---------------------------------------------------------------------------

BLOCKER: Final[str] = "BLOCKER"
CRITICAL: Final[str] = "CRITICAL"
MAJOR: Final[str] = "MAJOR"
MINOR: Final[str] = "MINOR"
INFO: Final[str] = "INFO"
UNKNOWN: Final[str] = "UNKNOWN"

# rank 越大越严重;UNKNOWN 最低,代表"我们识别不出来"。
RANK_BLOCKER: Final[int] = 5
RANK_CRITICAL: Final[int] = 4
RANK_MAJOR: Final[int] = 3
RANK_MINOR: Final[int] = 2
RANK_INFO: Final[int] = 1
RANK_UNKNOWN: Final[int] = 0

# 等级名 -> rank,用于 rank 反查与稳定排序。
SEVERITY_RANK: Final[dict[str, int]] = {
    BLOCKER: RANK_BLOCKER,
    CRITICAL: RANK_CRITICAL,
    MAJOR: RANK_MAJOR,
    MINOR: RANK_MINOR,
    INFO: RANK_INFO,
    UNKNOWN: RANK_UNKNOWN,
}

# ---------------------------------------------------------------------------
# 后端词汇表 -> 统一等级
# ---------------------------------------------------------------------------

# SonarQube for IDE 的等级已经是规范名,直接 1:1 映射。
_SONAR_MAP: Final[dict[str, str]] = {
    "BLOCKER": BLOCKER,
    "CRITICAL": CRITICAL,
    "MAJOR": MAJOR,
    "MINOR": MINOR,
    "INFO": INFO,
}

# JetBrains inspection severity 名存在多种拼写(含空格、大小写、下划线),
# 统一规范化到稳定等级。注意 SERVER PROBLEM 视为 MAJOR(通常是需要人工介入的真实问题)。
_JETBRAINS_MAP: Final[dict[str, str]] = {
    "ERROR": CRITICAL,
    "WARNING": MAJOR,
    "WEAK WARNING": MINOR,
    "WEAK_WARNING": MINOR,
    "TYPO": MINOR,
    "INFORMATION": INFO,
    "INFO": INFO,
    "SERVER PROBLEM": MAJOR,
    "SERVER_PROBLEM": MAJOR,
}

# 后端来源名(与 source 字段一致)。
SOURCE_SONAR: Final[str] = "sonar"
SOURCE_JETBRAINS: Final[str] = "jetbrains"


def _clean(raw: str) -> str:
    """清理原始等级字符串:去空白、统一空格、uppercase"""
    if not raw:
        return ""
    s = raw.strip().upper()
    # 把下划线/连字符等替换为空格,再做空白归并,以便匹配 "WEAK WARNING" 这类带空格的键。
    s = s.replace("_", " ").replace("-", " ")
    parts = [p for p in s.split() if p]
    return " ".join(parts)


def normalize_severity(raw: str, source: str) -> tuple[str, int]:
    """把后端原始 severity 字符串归一化为 (统一等级名, rank)

    Args:
        raw: 后端返回的 severity 原文(可能是 "ERROR"、"WEAK WARNING"、"Major" 等)。
        source: 后端来源,"sonar" 或 "jetbrains";其他/未知按通用规则尽力识别。

    Returns:
        (normalized_name, rank) 二元组。无法识别时返回 (UNKNOWN, 0)。
    """
    cleaned = _clean(raw)
    if not cleaned:
        return UNKNOWN, RANK_UNKNOWN

    src = (source or "").strip().lower()

    # 优先按来源的专属映射查找。
    table: dict[str, str] | None = None
    if src == SOURCE_SONAR:
        table = _SONAR_MAP
    elif src == SOURCE_JETBRAINS:
        table = _JETBRAINS_MAP

    if table is not None:
        mapped = table.get(cleaned)
        if mapped is not None:
            return mapped, SEVERITY_RANK[mapped]

    # 通用兜底:尝试两个表都查一遍,再尝试直接当成规范等级名。
    for tbl in (_SONAR_MAP, _JETBRAINS_MAP):
        mapped = tbl.get(cleaned)
        if mapped is not None:
            return mapped, SEVERITY_RANK[mapped]

    if cleaned in SEVERITY_RANK:
        return cleaned, SEVERITY_RANK[cleaned]

    return UNKNOWN, RANK_UNKNOWN


def highest_severity(severities: list[tuple[str, int]]) -> tuple[str, int]:
    """从多个 (等级名, rank) 中选出最严重的一个

    Args:
        severities: 候选 (name, rank) 列表;空列表返回 (UNKNOWN, 0)。

    Returns:
        rank 最大的那个 (name, rank);rank 相同时保留列表中首次出现的那个,
        以保证确定性。
    """
    if not severities:
        return UNKNOWN, RANK_UNKNOWN
    best_name, best_rank = severities[0]
    for name, rank in severities[1:]:
        if rank > best_rank:
            best_name, best_rank = name, rank
    return best_name, best_rank


__all__ = [
    "BLOCKER",
    "CRITICAL",
    "INFO",
    "MAJOR",
    "MINOR",
    "RANK_BLOCKER",
    "RANK_CRITICAL",
    "RANK_INFO",
    "RANK_MAJOR",
    "RANK_MINOR",
    "RANK_UNKNOWN",
    "SEVERITY_RANK",
    "SOURCE_JETBRAINS",
    "SOURCE_SONAR",
    "UNKNOWN",
    "highest_severity",
    "normalize_severity",
]
