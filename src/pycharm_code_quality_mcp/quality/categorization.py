"""问题类别归类

把不同后端、不同规则 id 的问题统一映射到 18 个稳定 category 之一。
category 用于:
  * 去重时的强禁止合并约束(不同 category 之间通常不能合并)。
  * 给模型/用户的稳定分组视图。

确定性:仅基于规则 id 与消息文本,不联网、不依赖外部 JSON 文件。
"""

from __future__ import annotations

import re
from typing import Final

# ---------------------------------------------------------------------------
# 18 个稳定 category 常量
# ---------------------------------------------------------------------------

UNUSED_PARAMETER: Final[str] = "unused_parameter"
UNUSED_VARIABLE: Final[str] = "unused_variable"
UNUSED_IMPORT: Final[str] = "unused_import"
UNRESOLVED_REFERENCE: Final[str] = "unresolved_reference"
TYPE_MISMATCH: Final[str] = "type_mismatch"
SYNTAX_ERROR: Final[str] = "syntax_error"
COMPLEXITY: Final[str] = "complexity"
DUPLICATED_CODE: Final[str] = "duplicated_code"
DEAD_CODE: Final[str] = "dead_code"
CONSTANT_CONDITION: Final[str] = "constant_condition"
NAMING: Final[str] = "naming"
SECURITY: Final[str] = "security"
RESOURCE_LEAK: Final[str] = "resource_leak"
EXCEPTION_HANDLING: Final[str] = "exception_handling"
STYLE: Final[str] = "style"
SPELLING: Final[str] = "spelling"
DOCUMENTATION: Final[str] = "documentation"
UNKNOWN: Final[str] = "unknown"

ALL_CATEGORIES: Final[tuple[str, ...]] = (
    UNUSED_PARAMETER,
    UNUSED_VARIABLE,
    UNUSED_IMPORT,
    UNRESOLVED_REFERENCE,
    TYPE_MISMATCH,
    SYNTAX_ERROR,
    COMPLEXITY,
    DUPLICATED_CODE,
    DEAD_CODE,
    CONSTANT_CONDITION,
    NAMING,
    SECURITY,
    RESOURCE_LEAK,
    EXCEPTION_HANDLING,
    STYLE,
    SPELLING,
    DOCUMENTATION,
    UNKNOWN,
)

# ---------------------------------------------------------------------------
# 规则 id -> category 映射
# 规则 id 一律按小写 + 去空白形式存储,匹配时同样规范化。
# 包含 Sonar 常见 ruleKey 与 JetBrains inspectionId。
# ---------------------------------------------------------------------------


def _normalize_rule_key(rule_id: str | None) -> str:
    """规则 id 归一化:lower、去空白、去分隔符,以便字典查找"""
    if not rule_id:
        return ""
    s = rule_id.strip().lower()
    out = []
    for ch in s:
        # 保留字母数字与冒号(Sonar ruleKey 形如 python:S1172),
        # 其他(空格、下划线、连字符)一律去掉。
        if ch.isalnum() or ch == ":":
            out.append(ch)
    return "".join(out)


_RULE_CATEGORY_RAW: Final[dict[str, str]] = {
    # --- 未使用类 ---
    "python:S1172": UNUSED_PARAMETER,
    "python:S1186": DEAD_CODE,
    "python:S1258": DEAD_CODE,
    "python:S5713": UNUSED_VARIABLE,
    "python:S1488": UNUSED_VARIABLE,
    "python:S1854": DEAD_CODE,
    "python:S1065": DEAD_CODE,
    # Sonar 通用未使用
    "common-python:unusedparameter": UNUSED_PARAMETER,
    "pyunresolvedreferences": UNRESOLVED_REFERENCE,
    "python:S2208": UNUSED_IMPORT,
    "python:S3776": COMPLEXITY,
    "python:S1541": COMPLEXITY,
    "python:S134": COMPLEXITY,
    "python:S1192": DUPLICATED_CODE,
    "python:S4144": DUPLICATED_CODE,
    "python:S1067": NAMING,
    "python:S117": NAMING,
    "python:S114": NAMING,
    "python:S116": NAMING,
    "python:S115": NAMING,
    "python:S100": NAMING,
    "python:S101": NAMING,
    "python:S110": EXCEPTION_HANDLING,
    "python:S1481": UNUSED_VARIABLE,
    # --- JetBrains inspectionId ---
    "unusedparameter": UNUSED_PARAMETER,
    "unusedlocalvariable": UNUSED_VARIABLE,
    "unusedglobalvariable": UNUSED_VARIABLE,
    "unusedimport": UNUSED_IMPORT,
    "unusedsymbol": DEAD_CODE,
    "unuseddeclaration": DEAD_CODE,
    "unused": DEAD_CODE,
    "unresolvedreference": UNRESOLVED_REFERENCE,
    "unresolvedreferences": UNRESOLVED_REFERENCE,
    "typemismatch": TYPE_MISMATCH,
    "pytypechecker": TYPE_MISMATCH,
    "mismatchedcollectionstransform": TYPE_MISMATCH,
    "incompatibletypes": TYPE_MISMATCH,
    "pythoncomplianceregexp": SYNTAX_ERROR,
    "syntaxerror": SYNTAX_ERROR,
    "pyrecursive": SYNTAX_ERROR,
    "overlycomplex": COMPLEXITY,
    "cyclomaticcomplexmethod": COMPLEXITY,
    "complexcondition": COMPLEXITY,
    "excessivemethodlength": COMPLEXITY,
    "duplicatedcode": DUPLICATED_CODE,
    "duplicatedstring": DUPLICATED_CODE,
    "pointlessbooleanexpression": CONSTANT_CONDITION,
    "pointlessbitwiseexpression": CONSTANT_CONDITION,
    "constantcondition": CONSTANT_CONDITION,
    "conditionalwithidenticalbranches": CONSTANT_CONDITION,
    "namingconvention": NAMING,
    "pynamingconvention": NAMING,
    "pep8naming": NAMING,
    "spellcheckinginspection": SPELLING,
    "typo": SPELLING,
    "spellcheck": SPELLING,
    "missingdocstring": DOCUMENTATION,
    "pydocstyle": DOCUMENTATION,
    "htmldocumentation": DOCUMENTATION,
    "emptymethod": DEAD_CODE,
    "emptytryblock": DEAD_CODE,
    "emptycatchblock": DEAD_CODE,
}


def _build_normalized_rules(raw: dict[str, str]) -> dict[str, str]:
    """把原始规则映射(可能含大小写混合的 key)统一规范化为查找用小写 key"""
    out: dict[str, str] = {}
    for k, v in raw.items():
        nk = _normalize_rule_key(k)
        if nk:
            out[nk] = v
    return out


# 查找用字典:所有 key 已经过 _normalize_rule_key 处理(小写、去分隔符)。
_RULE_CATEGORY: Final[dict[str, str]] = _build_normalized_rules(_RULE_CATEGORY_RAW)

# ---------------------------------------------------------------------------
# 消息模式 -> category(在规则 id 没命中时使用)
# 关键词基于消息做"包含"判断;模式已预先 lower-case。
# 顺序很重要:更具体的类别排在前面。
# ---------------------------------------------------------------------------

_MSG_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # 未使用类(顺序在 dead_code 之前,避免 "never used" 被误判为 dead code)
    (
        re.compile(r"unused param|parameter .* (is )?(not used|unused)|param .* is not used"),
        UNUSED_PARAMETER,
    ),
    (
        re.compile(r"unused import|import .* (is )?(not used|unused)|remove unused import"),
        UNUSED_IMPORT,
    ),
    # 两种词序都覆盖:"unused ... variable" 或 "variable ... unused/never used"。
    (
        re.compile(
            r"unused .*(var|variable|local|assignment|value)|(var|variable|local|assignment|value) .*(unused|never used|not used)"
        ),
        UNUSED_VARIABLE,
    ),
    (
        re.compile(
            r"\bunresolved\b|\bcannot find\b|\bcannot resolve\b|undefined (name|reference|variable)"
        ),
        UNRESOLVED_REFERENCE,
    ),
    (
        re.compile(
            r"\btype\b.*\b(mismatch|expected|incompatible|annotation)|incompatible type|expected type"
        ),
        TYPE_MISMATCH,
    ),
    (re.compile(r"syntax error|unexpected token|invalid syntax|unexpected indent"), SYNTAX_ERROR),
    (
        re.compile(
            r"complex|too many (branches|nested|arguments|parameters|return statements)|cyclomatic"
        ),
        COMPLEXITY,
    ),
    (re.compile(r"duplicat|copy[- ]paste"), DUPLICATED_CODE),
    # dead_code 放在 unused_* 之后:"never used" 若有变量语境会被上面拦下。
    (re.compile(r"\bdead code\b|unreachable|remove .* (statement|expression|import)"), DEAD_CODE),
    (
        re.compile(r"always (true|false)|constant (condition|expression)|pointless"),
        CONSTANT_CONDITION,
    ),
    (re.compile(r"naming convention|should be named|invalid name|rename to|naming"), NAMING),
    (
        re.compile(
            r"security|injection|xss|csrf|hardcoded (password|secret|credential)|cve[- ]?\d"
        ),
        SECURITY,
    ),
    (
        re.compile(
            r"resource leak|file (handle|descriptor) (never )?closed|unclosed|connection (not )?closed"
        ),
        RESOURCE_LEAK,
    ),
    (
        re.compile(
            r"exception|try .*(block|catch)|bare except|swallow.* exception|catch .* too broad"
        ),
        EXCEPTION_HANDLING,
    ),
    (re.compile(r"spell|typo|misspell"), SPELLING),
    (re.compile(r"docstring|documentation|missing doc|comment"), DOCUMENTATION),
)


def _normalize_message_for_match(msg: str) -> str:
    """消息文本归一化用于关键词匹配:lower + 合并空白"""
    if not msg:
        return ""
    s = msg.strip().lower()
    return re.sub(r"\s+", " ", s)


def categorize(rule_id: str | None, source: str, message: str) -> str:
    """按规则 id 与消息文本确定性归类

    Args:
        rule_id: 规则标识(Sonar ruleKey 或 JetBrains inspectionId),可为 None。
        source: 后端来源(目前未参与判断,保留参数以备未来按来源细化)。
        message: 问题消息原文,可为空。

    Returns:
        18 个 category 常量之一;无法识别时返回 UNKNOWN。
    """
    _ = source  # 预留:当前不按来源区分,但保持签名稳定。

    # 1) 先查规则 id 表(最可靠)。
    key = _normalize_rule_key(rule_id)
    if key:
        cat = _RULE_CATEGORY.get(key)
        if cat is not None:
            return cat

    # 2) 再按消息关键词模式归类。
    msg = _normalize_message_for_match(message)
    if msg:
        for pattern, category in _MSG_PATTERNS:
            if pattern.search(msg):
                return category

    return UNKNOWN


__all__ = [
    "ALL_CATEGORIES",
    "COMPLEXITY",
    "CONSTANT_CONDITION",
    "DEAD_CODE",
    "DOCUMENTATION",
    "DUPLICATED_CODE",
    "EXCEPTION_HANDLING",
    "NAMING",
    "RESOURCE_LEAK",
    "SECURITY",
    "SPELLING",
    "STYLE",
    "SYNTAX_ERROR",
    "TYPE_MISMATCH",
    "UNKNOWN",
    "UNRESOLVED_REFERENCE",
    "UNUSED_IMPORT",
    "UNUSED_PARAMETER",
    "UNUSED_VARIABLE",
    "categorize",
]
