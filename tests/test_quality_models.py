"""Tests for severity / categorization / normalization / fingerprints

覆盖 spec 第 8、9 节归一化要求:严重级别映射、category 稳定性、
消息规范化(中文/引号/数字/字符串字面量)、稳定 SHA-256 指纹。
"""

from __future__ import annotations

import pytest

from pycharm_code_quality_mcp.quality import categorization, normalization, severity
from pycharm_code_quality_mcp.quality.fingerprints import compute_finding_id

# ---------------------------------------------------------------------------
# severity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,source,expected_name,expected_rank",
    [
        ("BLOCKER", "sonar", "BLOCKER", 5),
        ("CRITICAL", "sonar", "CRITICAL", 4),
        ("MAJOR", "sonar", "MAJOR", 3),
        ("MINOR", "sonar", "MINOR", 2),
        ("INFO", "sonar", "INFO", 1),
        ("ERROR", "jetbrains", "CRITICAL", 4),
        ("WARNING", "jetbrains", "MAJOR", 3),
        ("WEAK WARNING", "jetbrains", "MINOR", 2),
        ("WEAK_WARNING", "jetbrains", "MINOR", 2),
        ("TYPO", "jetbrains", "MINOR", 2),
        ("INFORMATION", "jetbrains", "INFO", 1),
        ("INFO", "jetbrains", "INFO", 1),
        ("SERVER PROBLEM", "jetbrains", "MAJOR", 3),
        ("SERVER_PROBLEM", "jetbrains", "MAJOR", 3),
        ("weird-unknown-sev", "jetbrains", "UNKNOWN", 0),
        ("", "sonar", "UNKNOWN", 0),
    ],
)
def test_normalize_severity(raw: str, source: str, expected_name: str, expected_rank: int) -> None:
    name, rank = severity.normalize_severity(raw, source)
    assert name == expected_name
    assert rank == expected_rank


def test_normalize_severity_case_insensitive() -> None:
    assert severity.normalize_severity("error", "jetbrains") == ("CRITICAL", 4)
    assert severity.normalize_severity("Major", "sonar") == ("MAJOR", 3)


def test_highest_severity_picks_max_rank() -> None:
    sev = [("MINOR", 2), ("CRITICAL", 4), ("MAJOR", 3)]
    assert severity.highest_severity(sev) == ("CRITICAL", 4)


def test_highest_severity_empty_returns_unknown() -> None:
    assert severity.highest_severity([]) == ("UNKNOWN", 0)


def test_highest_severity_tie_keeps_first() -> None:
    """rank 相同时保留首次出现,保证确定性"""
    sev = [("MAJOR", 3), ("MAJOR", 3)]
    name, _rank = severity.highest_severity(sev)
    assert name == "MAJOR"


# ---------------------------------------------------------------------------
# categorization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule_id,expected",
    [
        ("python:S1172", categorization.UNUSED_PARAMETER),
        ("python:S2208", categorization.UNUSED_IMPORT),
        ("python:S1481", categorization.UNUSED_VARIABLE),
        ("python:S3776", categorization.COMPLEXITY),
        ("python:S1192", categorization.DUPLICATED_CODE),
        ("UnusedParameter", categorization.UNUSED_PARAMETER),
        ("UnusedImport", categorization.UNUSED_IMPORT),
        ("PyUnresolvedReferences", categorization.UNRESOLVED_REFERENCE),
        ("PyTypeChecker", categorization.TYPE_MISMATCH),
        ("SyntaxError", categorization.SYNTAX_ERROR),
        ("CyclomaticComplexMethod", categorization.COMPLEXITY),
        ("SpellCheckingInspection", categorization.SPELLING),
        ("PyDocStyle", categorization.DOCUMENTATION),
        ("PyNamingConvention", categorization.NAMING),
        ("DuplicatedCode", categorization.DUPLICATED_CODE),
        ("PointlessBooleanExpression", categorization.CONSTANT_CONDITION),
    ],
)
def test_categorize_by_rule(rule_id: str, expected: str) -> None:
    assert categorization.categorize(rule_id, "any", "") == expected


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("Unused parameter 'x'", categorization.UNUSED_PARAMETER),
        ("Unused import 'os'", categorization.UNUSED_IMPORT),
        ("Unused variable foo", categorization.UNUSED_VARIABLE),
        ("Cannot resolve reference 'bar'", categorization.UNRESOLVED_REFERENCE),
        ("Syntax error on line 5", categorization.SYNTAX_ERROR),
        ("Method is too complex", categorization.COMPLEXITY),
        ("Duplicated code detected", categorization.DUPLICATED_CODE),
        ("Dead code", categorization.DEAD_CODE),
        ("This condition is always true", categorization.CONSTANT_CONDITION),
        ("Security vulnerability: injection", categorization.SECURITY),
        ("Resource leak: file not closed", categorization.RESOURCE_LEAK),
        ("Bare except is too broad", categorization.EXCEPTION_HANDLING),
        ("Spelling mistake", categorization.SPELLING),
        ("Missing docstring", categorization.DOCUMENTATION),
    ],
)
def test_categorize_by_message(msg: str, expected: str) -> None:
    assert categorization.categorize(None, "any", msg) == expected


def test_categorize_unknown_when_no_signal() -> None:
    assert categorization.categorize(None, "any", "???") == categorization.UNKNOWN


def test_categorize_rule_overrides_message() -> None:
    """有规则 id 时优先按规则(更可靠)"""
    # 消息看起来像 unused_parameter,但规则是 complexity。
    assert (
        categorization.categorize("python:S3776", "any", "unused parameter")
        == categorization.COMPLEXITY
    )


def test_all_categories_have_18_entries() -> None:
    assert len(categorization.ALL_CATEGORIES) == 18
    assert categorization.UNKNOWN in categorization.ALL_CATEGORIES


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------


def test_normalize_file_path() -> None:
    assert normalization.normalize_file_path("/a/b/c.py")
    assert normalization.normalize_file_path("") == ""
    assert normalization.normalize_file_path(None) == ""  # type: ignore[arg-type]


def test_normalize_range_swaps_if_end_before_start() -> None:
    r = normalization.normalize_range(5, 10, 1, 2)
    assert r == (1, 2, 5, 10)


def test_normalize_range_clamps_non_positive_to_1() -> None:
    r = normalization.normalize_range(0, -1, 0, 0)
    assert r == (1, 1, 1, 1)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Hello World", "hello world"),
        ("Multiple    spaces", "multiple spaces"),
        ("Trailing punctuation.", "trailing punctuation"),
        ("Trailing comma,", "trailing comma"),
        ("String 'literal' here", "string <str> here"),
        ('String "literal" here', "string <str> here"),
        ("Number 12345 here", "number <num> here"),
        ("Float 3.14 here", "float <num> here"),
        ("中文测试", "中文测试"),
        ("Mixed Case TEXT", "mixed case text"),
    ],
)
def test_normalize_message(raw: str, expected: str) -> None:
    assert normalization.normalize_message(raw) == expected


def test_normalize_message_chinese_quotes() -> None:
    """中文引号应替换为英文引号后再做字面量替换"""
    result = normalization.normalize_message("变量 “foo” 未使用")
    # "foo" -> <str>
    assert "<str>" in result
    assert "变量" in result


def test_normalize_message_triple_quoted_string() -> None:
    result = normalization.normalize_message('docstring """hello""" end')
    assert "<str>" in result
    assert "docstring" in result
    assert "end" in result


def test_normalize_message_empty() -> None:
    assert normalization.normalize_message("") == ""
    assert normalization.normalize_message(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# fingerprints
# ---------------------------------------------------------------------------


def test_finding_id_starts_with_prefix() -> None:
    fid = compute_finding_id("/p/a.py", "unused_parameter", (1, 1, 1, 10), "msg", None)
    assert fid.startswith("qf_")
    assert len(fid) == len("qf_") + 12  # 12 hex chars


def test_finding_id_deterministic_same_input() -> None:
    args = ("/p/a.py", "unused_parameter", (1, 1, 1, 10), "msg", "abc123")
    id1 = compute_finding_id(*args)
    id2 = compute_finding_id(*args)
    assert id1 == id2


def test_finding_id_changes_with_different_input() -> None:
    base = compute_finding_id("/p/a.py", "cat", (1, 1, 1, 10), "msg", None)
    different_file = compute_finding_id("/p/b.py", "cat", (1, 1, 1, 10), "msg", None)
    different_cat = compute_finding_id("/p/a.py", "other", (1, 1, 1, 10), "msg", None)
    different_line = compute_finding_id("/p/a.py", "cat", (2, 1, 2, 10), "msg", None)
    different_msg = compute_finding_id("/p/a.py", "cat", (1, 1, 1, 10), "other", None)
    assert len({base, different_file, different_cat, different_line, different_msg}) == 5


def test_finding_id_anchor_none_vs_dash_differ() -> None:
    """anchor=None 与某字符串应产生不同 id"""
    with_anchor = compute_finding_id("/p/a.py", "cat", (1, 1, 1, 10), "m", "deadbeef")
    without = compute_finding_id("/p/a.py", "cat", (1, 1, 1, 10), "m", None)
    assert with_anchor != without
