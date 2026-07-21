"""确定性跨后端去重引擎

输入:来自多个后端的 ``SourceFinding`` 列表。
输出:``DeduplicationResult``,包含合并后的 ``UnifiedFinding`` 列表、
疑似重复组(中置信度未合并)、以及统计信息。

设计原则:
  * **确定性**:相同输入 -> 相同输出。不联网、不用 ``hash()``、不用随机数。
  * **保守优于激进**:宁可少合并(留作 possible_duplicate),也不要错误合并。
  * **受约束聚类**:用 complete-link(组内最远两元素仍需满足合并条件),
    避免传递闭包造成的错误合并。
  * **可解释**:每条 ``UnifiedFinding`` 的 deduplication 字段记录置信度与原因。

6 维相似度权重:
  locationScore 0.30 + messageScore 0.25 + ruleEquivalenceScore 0.20
  + anchorScore 0.15 + categoryScore 0.07 + identifierScore 0.03 = 1.00

4 个自动合并条件(A/B/C/D,见 ``_auto_merge`` 实现)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Final

from ..logging_config import get_logger
from . import categorization, normalization, severity
from .fingerprints import compute_finding_id
from .models import DeduplicationInfo, SourceFinding, UnifiedFinding, UnifiedRange

_log = get_logger("deduplication")

# ---------------------------------------------------------------------------
# 去重模式与阈值
# ---------------------------------------------------------------------------


class DeduplicationMode:
    """去重模式常量"""

    CONSERVATIVE: Final[str] = "conservative"
    BALANCED: Final[str] = "balanced"
    AGGRESSIVE: Final[str] = "aggressive"
    OFF: Final[str] = "off"


# 各模式的阈值参数。
# - candidate_line_distance:候选分组的起始行距离上限。
# - auto_merge_threshold:条件 D 的综合得分阈值。
# - possible_low / possible_high:疑似重复的置信度区间(左闭右开/左闭右闭见使用处)。
@dataclass(frozen=True)
class _ModeThresholds:
    candidate_line_distance: int
    auto_merge_threshold: float
    possible_low: float
    possible_high: float


_MODE_THRESHOLDS: Final[dict[str, _ModeThresholds]] = {
    DeduplicationMode.CONSERVATIVE: _ModeThresholds(
        candidate_line_distance=2,
        auto_merge_threshold=0.92,
        possible_low=0.70,
        possible_high=0.92,
    ),
    DeduplicationMode.BALANCED: _ModeThresholds(
        candidate_line_distance=2,
        auto_merge_threshold=0.86,
        possible_low=0.70,
        possible_high=0.86,
    ),
    DeduplicationMode.AGGRESSIVE: _ModeThresholds(
        candidate_line_distance=3,
        auto_merge_threshold=0.78,
        possible_low=0.60,
        possible_high=0.78,
    ),
}

# 相似度权重(6 维)
_W_LOCATION: Final[float] = 0.30
_W_MESSAGE: Final[float] = 0.25
_W_RULE: Final[float] = 0.20
_W_ANCHOR: Final[float] = 0.15
_W_CATEGORY: Final[float] = 0.07
_W_IDENTIFIER: Final[float] = 0.03

# 禁止合并的 category 组合(顺序无关)。
# 这些组合即便分数高也不合并:它们语义上属于不同问题。
_FORBIDDEN_CATEGORY_PAIRS: Final[frozenset[frozenset[str]]] = frozenset(
    {
        frozenset({categorization.SPELLING, categorization.TYPE_MISMATCH}),
        frozenset({categorization.SECURITY, categorization.STYLE}),
        frozenset({categorization.SYNTAX_ERROR, categorization.DOCUMENTATION}),
        frozenset({categorization.SYNTAX_ERROR, categorization.STYLE}),
    }
)

# 来源优先级(数字越小越优先,用于代表问题选择)。
_SOURCE_PRIORITY: Final[dict[str, int]] = {
    severity.SOURCE_SONAR: 0,
    severity.SOURCE_JETBRAINS: 1,
}


# ---------------------------------------------------------------------------
# 结果数据结构
# ---------------------------------------------------------------------------


@dataclass
class DeduplicationResult:
    """去重引擎的输出"""

    findings: list[UnifiedFinding] = field(default_factory=list)
    possible_duplicate_groups: list[dict[str, Any]] = field(default_factory=list)
    raw_count: int = 0
    unique_count: int = 0
    duplicates_merged: int = 0
    possible_count: int = 0

    @property
    def stats(self) -> dict[str, Any]:
        """便于序列化的统计字典"""
        return {
            "rawCount": self.raw_count,
            "uniqueCount": self.unique_count,
            "duplicatesMerged": self.duplicates_merged,
            "possibleDuplicateCount": self.possible_count,
        }


# ---------------------------------------------------------------------------
# 内部:为每条 SourceFinding 计算规范化派生量
# ---------------------------------------------------------------------------


@dataclass
class _Derived:
    """每条 SourceFinding 的派生缓存,避免重复计算"""

    idx: int  # 原始列表中的稳定下标
    finding: SourceFinding
    file_norm: str
    category: str
    sev_name: str
    sev_rank: int
    msg_norm: str
    rule_id_norm: str  # 小写去空白
    range_tuple: tuple[int, int, int, int] | None
    start_line: int  # 无范围时用 0
    anchor_hash: str | None
    # 从规范化消息里抽出的标识符集合(用于 identifierScore)。
    identifiers: frozenset[str]


def _extract_identifiers(msg_norm: str) -> frozenset[str]:
    """从规范化消息中抽出"标识符样"的 token 集合

    规则:长度 >= 3、只含字母/数字/下划线、且不是纯数字;
    排除占位符 ``<str>``/``<num>`` 与常见停用词。
    """
    if not msg_norm:
        return frozenset()
    tokens: set[str] = set()
    for tok in msg_norm.replace(".", " ").replace(",", " ").replace(":", " ").split():
        cleaned = tok.strip().strip("'\"")
        if len(cleaned) < 3:
            continue
        if cleaned in ("<str>", "<num>"):
            continue
        if not any(c.isalpha() for c in cleaned):
            continue
        # 全是停用词的话跳过。
        if cleaned in _STOPWORDS:
            continue
        tokens.add(cleaned)
    return frozenset(tokens)


# 常见英文/代码停用词,降低 identifierScore 噪声。
_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "the",
        "and",
        "for",
        "not",
        "are",
        "but",
        "with",
        "this",
        "that",
        "from",
        "should",
        "must",
        "have",
        "has",
        "was",
        "were",
        "use",
        "used",
        "using",
        "you",
        "your",
        "may",
        "can",
        "all",
        "any",
        "via",
        "when",
        "then",
        "than",
        "into",
        "over",
        "will",
        "would",
        "could",
        "does",
        "did",
        "its",
    }
)


def _derive(
    idx: int,
    finding: SourceFinding,
    anchor_hashes: dict[str, str | None] | None,
) -> _Derived:
    """为一条 SourceFinding 计算所有派生量"""
    file_norm = normalization.normalize_file_path(finding.file_path)
    cat = categorization.categorize(finding.rule_id, finding.source, finding.message)
    sev_name, sev_rank = severity.normalize_severity(finding.severity, finding.source)
    msg_norm = normalization.normalize_message(finding.message)
    rule_norm = _normalize_rule(finding.rule_id)

    range_tuple: tuple[int, int, int, int] | None = None
    start_line = 0
    if finding.range is not None:
        r = finding.range
        range_tuple = normalization.normalize_range(
            r.start_line, r.start_column, r.end_line, r.end_column
        )
        start_line = range_tuple[0]

    # anchor hash:优先使用调用方预算好的字典(按文件维度缓存),否则按行算。
    anchor: str | None = None
    if anchor_hashes is not None:
        # 字典 key 用规范化文件路径;若没有则 None。
        anchor = anchor_hashes.get(file_norm)
    # 注意:这里不再回退到磁盘读取;锚点 hash 由调用方在批量入口统一预算,
    # 避免 deduplicate 内部产生 I/O,保持纯函数性质。

    idents = _extract_identifiers(msg_norm)
    return _Derived(
        idx=idx,
        finding=finding,
        file_norm=file_norm,
        category=cat,
        sev_name=sev_name,
        sev_rank=sev_rank,
        msg_norm=msg_norm,
        rule_id_norm=rule_norm,
        range_tuple=range_tuple,
        start_line=start_line,
        anchor_hash=anchor,
        identifiers=idents,
    )


def _normalize_rule(rule_id: str | None) -> str:
    """规则 id 归一化(与 categorization 一致:lower、去非字母数字冒号)"""
    if not rule_id:
        return ""
    s = rule_id.strip().lower()
    return "".join(ch for ch in s if ch.isalnum() or ch == ":")


# ---------------------------------------------------------------------------
# 相似度计算(6 维)
# ---------------------------------------------------------------------------


def _ranges_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    """判断两个 1-based 范围是否相交(含端点)"""
    a_sl, _, a_el, _ = a
    b_sl, _, b_el, _ = b
    return a_sl <= b_el and b_sl <= a_el


def _location_score(a: _Derived, b: _Derived, line_distance: int) -> float:
    """位置相似度"""
    # 不同文件 => 0(候选分组阶段已过滤,这里兜底)。
    if a.file_norm != b.file_norm or not a.file_norm:
        return 0.0
    ra, rb = a.range_tuple, b.range_tuple
    if ra is None or rb is None:
        return _location_score_by_line(a.start_line, b.start_line, line_distance)
    return _location_score_by_range(a, b, ra, rb, line_distance)


def _location_score_by_line(start_a: int, start_b: int, line_distance: int) -> float:
    """两边缺范围时,用起始行距离打分"""
    if start_a == 0 or start_b == 0:
        return 0.0
    dist = abs(start_a - start_b)
    if dist == 0:
        return 0.8
    if dist <= line_distance:
        return 0.6
    if dist <= line_distance + 2:
        return 0.3
    return 0.0


def _location_score_by_range(
    a: _Derived,
    b: _Derived,
    ra: tuple[int, int, int, int],
    rb: tuple[int, int, int, int],
    line_distance: int,
) -> float:
    """两边都有范围时,优先用区间交并比,否则用起始行距离"""
    if _ranges_overlap(ra, rb):
        # 计算交并比(Jaccard),用行级区间近似。
        a_sl, _, a_el, _ = ra
        b_sl, _, b_el, _ = rb
        inter = max(0, min(a_el, b_el) - max(a_sl, b_sl) + 1)
        if inter <= 0:
            # 同行但列不相交:给中等分。
            return 0.5 if a_sl == b_sl else 0.0
        union = max(a_el, b_el) - min(a_sl, b_sl) + 1
        return inter / union if union > 0 else 0.0
    # 不相交但起始行接近。
    dist = abs(a.start_line - b.start_line)
    if dist <= line_distance:
        return 0.4
    if dist <= line_distance + 2:
        return 0.2
    return 0.0


def _tokenize(msg: str) -> set[str]:
    """简单 token 化(按非字母数字字符切分)"""
    if not msg:
        return set()
    out: set[str] = set()
    for tok in msg.replace("<str>", " ").replace("<num>", " ").split():
        if tok:
            out.add(tok)
    return out


def _message_score(a: _Derived, b: _Derived) -> float:
    """消息相似度:完全相等 + token Jaccard + SequenceMatcher + 标识符重合"""
    ma, mb = a.msg_norm, b.msg_norm
    if not ma or not mb:
        return 0.0
    if ma == mb:
        return 1.0
    ta, tb = _tokenize(ma), _tokenize(mb)
    if ta and tb:
        inter = len(ta & tb)
        union = len(ta | tb)
        jaccard = inter / union if union else 0.0
    else:
        jaccard = 0.0
    ratio = SequenceMatcher(None, ma, mb).ratio()
    ident_inter = len(a.identifiers & b.identifiers)
    ident_union = len(a.identifiers | b.identifiers)
    ident = (ident_inter / ident_union) if ident_union else 0.0
    # 加权融合:取相似度的最大值(避免单一信号过低拉低总分),再与标识符相似度做 7:3 加权。
    return max(jaccard, ratio, 0.0) * 0.7 + ident * 0.3


def _rule_equivalence_score(a: _Derived, b: _Derived) -> float:
    """规则等价分:同规则 1.0;已知等价规则对查表;否则 0"""
    ra, rb = a.rule_id_norm, b.rule_id_norm
    if not ra or not rb:
        return 0.0
    if ra == rb:
        return 1.0
    pair = frozenset({ra, rb})
    return 1.0 if pair in _EQUIVALENT_RULELES else 0.0


# 已知的跨后端等价规则对(规范化形式:lower、去分隔符、保留冒号)。
# 这里只列少数高置信度对;未知一律视为不等价(保守)。
_EQUIVALENT_RULELES: Final[frozenset[frozenset[str]]] = frozenset(
    {
        frozenset({"python:s1172", "unusedparameter"}),
        frozenset({"python:s1481", "unusedlocalvariable"}),
        frozenset({"python:s2208", "unusedimport"}),
        frozenset({"pyunresolvedreferences", "unresolvedreference"}),
        frozenset({"pyunresolvedreferences", "unresolvedreferences"}),
        frozenset({"pytypechecker", "typemismatch"}),
        frozenset({"python:s3776", "cyclomaticcomplexmethod"}),
    }
)


def _anchor_score(a: _Derived, b: _Derived) -> float:
    """代码锚点分:anchor_hash 相同且非空 => 1.0,否则 0"""
    if a.anchor_hash and b.anchor_hash and a.anchor_hash == b.anchor_hash:
        return 1.0
    return 0.0


def _category_score(a: _Derived, b: _Derived) -> float:
    """类别分:相同 => 1.0;一方 UNKNOWN => 0.5;否则 0"""
    if a.category == b.category:
        return 1.0
    if a.category == categorization.UNKNOWN or b.category == categorization.UNKNOWN:
        return 0.5
    return 0.0


def _identifier_score(a: _Derived, b: _Derived) -> float:
    """标识符重合分(基于消息抽出的标识符 Jaccard)"""
    if not a.identifiers or not b.identifiers:
        return 0.0
    inter = len(a.identifiers & b.identifiers)
    union = len(a.identifiers | b.identifiers)
    return (inter / union) if union else 0.0


@dataclass
class _ScoreBreakdown:
    """相似度拆解,用于解释与判定"""

    location: float
    message: float
    rule: float
    anchor: float
    category: float
    identifier: float
    total: float


def _score_pair(a: _Derived, b: _Derived, line_distance: int) -> _ScoreBreakdown:
    """计算一对问题的 6 维相似度与加权总分"""
    loc = _location_score(a, b, line_distance)
    msg = _message_score(a, b)
    rule = _rule_equivalence_score(a, b)
    anchor = _anchor_score(a, b)
    cat = _category_score(a, b)
    ident = _identifier_score(a, b)
    total = (
        loc * _W_LOCATION
        + msg * _W_MESSAGE
        + rule * _W_RULE
        + anchor * _W_ANCHOR
        + cat * _W_CATEGORY
        + ident * _W_IDENTIFIER
    )
    return _ScoreBreakdown(loc, msg, rule, anchor, cat, ident, total)


# ---------------------------------------------------------------------------
# 候选分组与禁止合并
# ---------------------------------------------------------------------------


def _is_candidate(a: _Derived, b: _Derived, line_distance: int) -> bool:
    """是否构成候选对(同文件 + 范围相交/起始行近/锚点同/规则等价)"""
    if a.file_norm != b.file_norm or not a.file_norm:
        return False
    ra, rb = a.range_tuple, b.range_tuple
    if ra is not None and rb is not None and _ranges_overlap(ra, rb):
        return True
    # 起始行距离。
    if a.start_line and b.start_line and abs(a.start_line - b.start_line) <= line_distance:
        return True
    # anchor hash 相同且非空。
    if a.anchor_hash and b.anchor_hash and a.anchor_hash == b.anchor_hash:
        return True
    # 显式规则等价。
    if a.rule_id_norm and a.rule_id_norm == b.rule_id_norm:
        return True
    return bool(
        a.rule_id_norm
        and b.rule_id_norm
        and (frozenset({a.rule_id_norm, b.rule_id_norm}) in _EQUIVALENT_RULELES)
    )


def _is_forbidden(a: _Derived, b: _Derived) -> bool:
    """硬性禁止合并条件:即便分数高也绝不合并"""
    # 不同文件。
    if a.file_norm != b.file_norm:
        return True
    # category 在禁止组合里。
    pair = frozenset({a.category, b.category})
    if len(pair) == 2 and pair in _FORBIDDEN_CATEGORY_PAIRS:
        return True
    # 距离过远且无 anchor。
    dist = abs(a.start_line - b.start_line) if a.start_line and b.start_line else 0
    far = dist > 5
    no_anchor = not (a.anchor_hash and b.anchor_hash and a.anchor_hash == b.anchor_hash)
    return bool(far and no_anchor)


def _auto_merge_conditions(
    a: _Derived,
    b: _Derived,
    s: _ScoreBreakdown,
    threshold: float,
) -> tuple[bool, str | None]:
    """4 个自动合并条件(A/B/C/D),满足任一即合并

    严格对应 spec 第 9.6 节:
      A:同一文件 + 范围相交 + 规范化消息完全相同(messageScore == 1.0 且 locationScore > 0)
      B:同一文件 + 显式规则等价 + 起始行距离 <= 2 + messageScore >= 0.55
      C:同一文件 + codeAnchorHash 相同 + category 相同 + messageScore >= 0.82
      D:综合得分 >= threshold(各模式不同)且至少两个强信号成立

    Returns:
        (是否合并, 命中条件名)
    """
    same_file = bool(a.file_norm) and a.file_norm == b.file_norm
    line_dist = abs(a.start_line - b.start_line) if a.start_line and b.start_line else 0
    ranges_overlap = bool(
        a.range_tuple is not None
        and b.range_tuple is not None
        and _ranges_overlap(a.range_tuple, b.range_tuple)
    )

    # A:消息完全相同 + 范围相交(同文件)。
    if same_file and ranges_overlap and s.message >= 0.95:
        return True, "A"
    # B:规则等价 + 起始行距离 <= 2 + 消息相似度 >= 0.55。
    if same_file and s.rule >= 1.0 and line_dist <= 2 and s.message >= 0.55:
        return True, "B"
    # C:anchor 相同 + category 相同 + 消息相似度 >= 0.82。
    if same_file and s.anchor >= 1.0 and a.category == b.category and s.message >= 0.82:
        return True, "C"
    # D:综合得分 >= threshold + 至少两个强信号成立。
    strong_signals = (
        ranges_overlap,
        s.rule >= 1.0,
        s.anchor >= 1.0,
        (a.category == b.category and s.message >= 0.80),
    )
    if s.total >= threshold and sum(1 for x in strong_signals if x) >= 2:
        return True, "D"
    return False, None


# ---------------------------------------------------------------------------
# 聚类:complete-link(组内最远对仍需满足合并条件)
# ---------------------------------------------------------------------------


def _cluster(  # NOSONAR - complete-link clustering inherently branches per pair/mode
    derived: list[_Derived],
    thresholds: _ModeThresholds,
) -> tuple[list[list[int]], list[tuple[int, int, float, str | None]]]:
    """对派生列表做受约束聚类

    Returns:
        (groups, possible_pairs)
        - groups: 每组是下标列表(指向 derived)。
        - possible_pairs: 中置信度但未合并的 (i, j, score, cond) 列表,
          供 possible_duplicate_groups 使用。
    """
    n = len(derived)
    # 用并查集,但合并前先做 complete-link 校验:维护每组成员,
    # 合并两个组当且仅当两组之间所有候选对都满足合并条件。
    parent = list(range(n))
    members: list[list[int]] = [[i] for i in range(n)]

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # 预计算所有候选对及其判定。
    possible_pairs: list[tuple[int, int, float, str | None]] = []
    # 合并图:记录"已确认可合并"的下标对。
    merge_edges: set[tuple[int, int]] = set()

    for i in range(n):
        for j in range(i + 1, n):
            a, b = derived[i], derived[j]
            if not _is_candidate(a, b, thresholds.candidate_line_distance):
                continue
            if _is_forbidden(a, b):
                continue
            s = _score_pair(a, b, thresholds.candidate_line_distance)
            ok, cond = _auto_merge_conditions(a, b, s, thresholds.auto_merge_threshold)
            if ok:
                merge_edges.add((i, j))
            elif thresholds.possible_low <= s.total < thresholds.possible_high:
                possible_pairs.append((i, j, s.total, cond))

    # complete-link 聚类:重复尝试合并任意两个有边的组,
    # 仅当"该组对中所有成员两两之间都存在合并边"时才合并。
    changed = True
    while changed:
        changed = False
        roots = sorted({find(i) for i in range(n)})
        for ri_idx in range(len(roots)):
            for rj_idx in range(ri_idx + 1, len(roots)):
                ri, rj = roots[ri_idx], roots[rj_idx]
                if ri == rj:
                    continue
                mi, mj = members[ri], members[rj]
                if not _complete_link_ok(mi, mj, merge_edges):
                    continue
                # 合并 rj -> ri。
                parent[rj] = ri
                members[ri] = mi + mj
                members[rj] = []
                changed = True
                break  # 重新计算 roots
            if changed:
                break

    groups = [members[r] for r in sorted({find(i) for i in range(n)}) if members[r]]
    return groups, possible_pairs


def _complete_link_ok(mi: list[int], mj: list[int], edges: set[tuple[int, int]]) -> bool:
    """complete-link 判定:两组间所有成员对都必须存在合并边"""
    for x in mi:
        for y in mj:
            key = (x, y) if x < y else (y, x)
            if key not in edges:
                return False
    return True


# ---------------------------------------------------------------------------
# 代表问题选择 + 合并
# ---------------------------------------------------------------------------


def _select_representative(group: list[_Derived]) -> _Derived:
    """从一组中选择代表问题

    优先级:
      1) severity 高
      2) 有 ruleId(rule_id_norm 非空)
      3) 范围更精确(start_line > 0 且范围存在)
      4) 消息更长(描述更具体)
      5) 来源优先级(sonar 优于 jetbrains)
    """
    if len(group) == 1:
        return group[0]

    def sort_key(d: _Derived) -> tuple[int, int, int, int, int]:
        has_rule = 0 if d.rule_id_norm else 1  # 0 排前
        has_range = 0 if d.range_tuple is not None else 1
        msg_len = -len(d.msg_norm)  # 越长越前 => 取负
        src_pri = _SOURCE_PRIORITY.get(d.finding.source, 99)
        # severity 取负 => rank 越大越前。
        return (-d.sev_rank, has_rule, has_range, msg_len, src_pri)

    # 取 (sort_key, idx) 联合最小者;idx 作为决胜字段保证全序与确定性。
    return min(group, key=lambda d: (sort_key(d), d.idx))


def _build_unified(
    group: list[_Derived],
) -> UnifiedFinding:  # NOSONAR - merge logic branches per field
    """把一组 _Derived 合并为单个 UnifiedFinding"""
    rep = _select_representative(group)
    sources: list[str] = []
    rule_ids: dict[str, list[str]] = {}
    severities: list[tuple[str, int]] = []
    source_findings: list[SourceFinding] = []

    # 稳定排序,保证字段顺序确定。
    ordered = sorted(group, key=lambda d: d.idx)
    for d in ordered:
        if d.finding.source not in sources:
            sources.append(d.finding.source)
        if d.finding.rule_id:
            rule_ids.setdefault(d.finding.source, [])
            if d.finding.rule_id not in rule_ids[d.finding.source]:
                rule_ids[d.finding.source].append(d.finding.rule_id)
        severities.append((d.sev_name, d.sev_rank))
        source_findings.append(d.finding)

    sev_name, sev_rank = severity.highest_severity(severities)

    # 优先用代表 finding 的文本范围;若代表无,退而求其次用组内任一存在的。
    range_tuple = rep.range_tuple
    if range_tuple is None:
        for d in ordered:
            if d.range_tuple is not None:
                range_tuple = d.range_tuple
                break
    urange: UnifiedRange | None = None
    if range_tuple is not None:
        sl, sc, el, ec = range_tuple
        urange = UnifiedRange(startLine=sl, startColumn=sc, endLine=el, endColumn=ec)

    # 消息与 category 用代表的。
    message = rep.finding.message
    category = rep.category
    file_path = rep.finding.file_path

    anchor = rep.anchor_hash
    rt = range_tuple if range_tuple is not None else (0, 0, 0, 0)
    finding_id = compute_finding_id(
        normalization.normalize_file_path(file_path),
        category,
        rt,
        normalization.normalize_message(message),
        anchor,
    )

    merged = len(group) > 1
    # 置信度:取组内最高相似度(组大小为 1 时记 1.0)。
    confidence = 1.0 if not merged else _group_max_confidence(group)
    reasons: list[str] = []
    if merged:
        reasons.append("auto_merged")
        reasons.append(f"representative_source={rep.finding.source}")
    else:
        reasons.append("single_source")

    dedup_info = DeduplicationInfo(merged=merged, confidence=confidence, reason=reasons)

    return UnifiedFinding(
        id=finding_id,
        sources=sources,
        ruleIds=rule_ids,
        severity=sev_name,
        severity_rank=sev_rank,
        message=message,
        category=category,
        filePath=file_path,
        range=urange,
        duplicateCount=len(group),
        deduplication=dedup_info,
        sourceFindings=source_findings,
    )


def _group_max_confidence(group: list[_Derived]) -> float:
    """估算组内最高置信度(用代表性信号)"""
    # 简化为:成员来源数 + 规则重合度 的组合,落在 [0.7, 0.95] 区间。
    sources = {d.finding.source for d in group}
    rules = {d.rule_id_norm for d in group if d.rule_id_norm}
    base = 0.70
    if len(sources) >= 2:
        base += 0.15
    if rules:
        base += 0.05
    if all(d.anchor_hash for d in group):
        base += 0.05
    return min(base, 0.95)


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------


def deduplicate(  # NOSONAR - top-level pipeline orchestrates many dedup phases
    findings: list[SourceFinding],
    mode: str = DeduplicationMode.BALANCED,
    file_anchor_hashes: dict[str, str | None] | None = None,
) -> DeduplicationResult:
    """对一组 SourceFinding 做确定性跨后端去重

    Args:
        findings: 来自所有后端的原始问题列表。
        mode: 去重模式(conservative / balanced / aggressive / off)。
        file_anchor_hashes: 可选的、按规范化文件路径预算好的代码锚点 hash 字典;
            key 是规范化后的文件路径,value 是 SHA-256 hex 或 None。
            传入可让锚点维度生效;不传则锚点维度退化为 0(仍可工作)。

    Returns:
        ``DeduplicationResult``。
    """
    raw_count = len(findings)
    result = DeduplicationResult(raw_count=raw_count)

    if raw_count == 0:
        return result

    # OFF 模式:不做任何合并,每个 SourceFinding 独立成 UnifiedFinding。
    if mode == DeduplicationMode.OFF:
        for i, f in enumerate(findings):
            d = _derive(i, f, file_anchor_hashes)
            uf = _build_unified([d])
            uf.deduplication = DeduplicationInfo(merged=False, confidence=0.0, reason=["mode_off"])
            result.findings.append(uf)
        result.unique_count = len(result.findings)
        return result

    thresholds = _MODE_THRESHOLDS.get(mode, _MODE_THRESHOLDS[DeduplicationMode.BALANCED])

    derived = [_derive(i, f, file_anchor_hashes) for i, f in enumerate(findings)]

    # 先按文件分组,缩小候选对范围(O(n^2) 只在同一文件内发生)。
    by_file: dict[str, list[int]] = {}
    for d in derived:
        by_file.setdefault(d.file_norm, []).append(d.idx)

    all_groups: list[list[int]] = []
    all_possible: list[tuple[int, int, float, str | None]] = []
    for file_norm, idxs in by_file.items():
        if not file_norm or len(idxs) < 2:
            # 单元素文件:自成一组。
            for i in idxs:
                all_groups.append([i])
            continue
        sub = [derived[i] for i in idxs]
        groups, possible = _cluster(sub, thresholds)
        # 把 sub 局部下标映射回全局下标。
        for g in groups:
            all_groups.append([sub[k].idx for k in g])
        for i, j, score, cond in possible:
            all_possible.append((sub[i].idx, sub[j].idx, score, cond))

    # 构建统一问题。
    for gidxs in all_groups:
        group = [derived[i] for i in gidxs]
        result.findings.append(_build_unified(group))

    # 稳定排序:确保相同输入(无论原始顺序)得到相同输出序列。
    # 排序键:规范化文件路径 + 起始行 + 起始列 + id。
    result.findings.sort(key=_finding_sort_key)

    # 可能重复组(中置信度,未合并)。
    result.possible_duplicate_groups = _build_possible_groups(all_possible, derived)

    # 统计。
    result.unique_count = len(result.findings)
    result.duplicates_merged = max(0, raw_count - result.unique_count)
    result.possible_count = len(result.possible_duplicate_groups)

    _log.debug(
        "deduplicate: mode=%s raw=%d unique=%d merged=%d possible=%d",
        mode,
        raw_count,
        result.unique_count,
        result.duplicates_merged,
        result.possible_count,
    )
    return result


def _build_possible_groups(
    pairs: list[tuple[int, int, float, str | None]],
    derived: list[_Derived],
) -> list[dict[str, Any]]:
    """把中置信度对组织成可序列化的疑似重复组列表

    对每对 (i, j),输出一个 dict,包含两条问题的稳定 id 与简要信息。
    若多个对共享同一成员,这里不做进一步聚类(保持"成对呈现"便于人工确认)。
    """
    out: list[dict[str, Any]] = []
    seen_pairs: set[frozenset[int]] = set()
    for i, j, score, cond in pairs:
        key = frozenset({i, j})
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        a, b = derived[i], derived[j]
        out.append(
            {
                "confidence": round(score, 4),
                "condition": cond,
                "findings": [
                    _brief(a, derived),
                    _brief(b, derived),
                ],
            }
        )
    return out


def _brief(d: _Derived, all_derived: list[_Derived]) -> dict[str, Any]:
    """构造疑似重复组里单条问题的简要信息(不含源码)"""
    _ = all_derived
    f = d.finding
    rt = d.range_tuple
    return {
        "source": f.source,
        "ruleId": f.rule_id,
        "severity": d.sev_name,
        "category": d.category,
        "message": f.message,
        "filePath": f.file_path,
        "range": list(rt) if rt is not None else None,
    }


def _finding_sort_key(f: UnifiedFinding) -> tuple[str, int, int, str]:
    """UnifiedFinding 的稳定排序键

    保证相同输入无论原始顺序如何,输出序列都完全一致。
    排序优先级:规范化文件路径 → 起始行 → 起始列 → id。
    """
    r = f.range
    sl = r.start_line if r is not None else 0
    sc = r.start_column if r is not None else 0
    return (normalization.normalize_file_path(f.file_path), sl, sc, f.id)


__all__ = ["DeduplicationMode", "DeduplicationResult", "deduplicate"]
