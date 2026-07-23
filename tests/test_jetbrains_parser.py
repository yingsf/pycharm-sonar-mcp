"""Tests for the JetBrains MCP result parser

覆盖 spec 第 6 节:structuredContent / JSON TextContent / 纯文本兼容解析 /
bad response 不崩溃 / 行列号 1-based / 字段缺失兜底。
"""

from __future__ import annotations

from pycharm_code_quality_mcp.backends.jetbrains.models import JetBrainsProblem
from pycharm_code_quality_mcp.backends.jetbrains.parser import (
    parse_get_file_problems_result,
    parse_project_status,
)


class _TextContent:
    """模拟 mcp.types.TextContent(避免依赖 SDK 实例化路径)"""

    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


_FILE = "/proj/src/a.py"


# ---------------------------------------------------------------------------
# parse_get_file_problems_result
# ---------------------------------------------------------------------------


def test_structured_content_extracted() -> None:
    structured = {
        "problems": [
            {
                "inspectionId": "UnusedParameter",
                "severity": "WARNING",
                "description": "Parameter 'x' is unused",
                "filePath": _FILE,
                "startLine": 5,
                "startColumn": 1,
                "endLine": 5,
                "endColumn": 10,
            }
        ]
    }
    problems = parse_get_file_problems_result([], structured, _FILE)
    assert len(problems) == 1
    p = problems[0]
    assert p.source == "jetbrains"
    assert p.inspection_id == "UnusedParameter"
    assert p.severity == "WARNING"
    assert p.start_line == 5
    assert p.start_column == 1
    assert p.end_column == 10
    assert p.file_path == _FILE


def test_json_text_content_extracted() -> None:
    text = (
        '{"findings": ['
        '{"inspectionId": "Typo", "severity": "TYPO", '
        '"description": "spelling", "startLine": 2}'
        "]}"
    )
    problems = parse_get_file_problems_result([_TextContent(text)], None, _FILE)
    assert len(problems) == 1
    p = problems[0]
    assert p.inspection_id == "Typo"
    assert p.file_path == _FILE  # 回填
    assert p.start_line == 2
    # 兜底列号应为 1。
    assert p.start_column == 1
    assert p.end_line == 2


def test_plain_text_falls_back_to_empty() -> None:
    """纯文本(非 JSON)=> 不崩溃,返回空列表"""
    problems = parse_get_file_problems_result([_TextContent("no problems found here")], None, _FILE)
    assert problems == []


def test_empty_structured_returns_empty() -> None:
    problems = parse_get_file_problems_result([], {"problems": []}, _FILE)
    assert problems == []


def test_unknown_fields_kept_in_raw() -> None:
    """未知字段(extra='allow')应保留在 raw 中,前向兼容"""
    structured = {
        "problems": [
            {
                "inspectionId": "X",
                "severity": "ERROR",
                "description": "boom",
                "startLine": 1,
                "startColumn": 1,
                "endLine": 1,
                "endColumn": 5,
                "futureField": "ignored-but-kept",
            }
        ]
    }
    problems = parse_get_file_problems_result([], structured, _FILE)
    assert len(problems) == 1
    assert problems[0].raw.get("futureField") == "ignored-but-kept"


def test_malformed_problem_skipped_not_raised() -> None:
    """单条畸形 problem 不应让整个解析崩溃"""
    structured = {
        "problems": [
            {"inspectionId": "Ok", "severity": "ERROR", "description": "ok", "startLine": 1},
            "not-a-dict",  # 畸形项
            {"severity": "ERROR", "description": "missing line"},  # 缺 startLine
        ]
    }
    problems = parse_get_file_problems_result([], structured, _FILE)
    # 第一条 OK,第三条 startLine 兜底为 1 也应解析成功;第二条跳过。
    assert len(problems) == 2


def test_fields_aliases_supported() -> None:
    """同时支持 errors/problems/findings/diagnostics/issues 多种字段名"""
    for key in ("errors", "problems", "findings", "diagnostics", "issues"):
        structured = {
            key: [
                {
                    "inspectionId": "X",
                    "severity": "ERROR",
                    "description": "d",
                    "startLine": 1,
                    "startColumn": 1,
                    "endLine": 1,
                    "endColumn": 1,
                }
            ]
        }
        problems = parse_get_file_problems_result([], structured, _FILE)
        assert len(problems) == 1, f"failed for key={key}"


def test_pycharm_errors_warning_shape_supported() -> None:
    structured = {
        "filePath": "src/a.py",
        "errors": [
            {
                "severity": "WARNING",
                "description": "No overloads for join match arguments",
                "lineContent": "','.join(statuses)",
                "line": 162,
                "column": 35,
            }
        ],
    }
    problems = parse_get_file_problems_result([], structured, _FILE)

    assert len(problems) == 1
    assert problems[0].severity == "WARNING"
    assert problems[0].start_line == 162
    assert problems[0].start_column == 35
    assert problems[0].file_path == _FILE


# ---------------------------------------------------------------------------
# parse_project_status
# ---------------------------------------------------------------------------


def test_project_status_isIndexing_from_structured() -> None:
    status = parse_project_status({"isIndexing": True}, [])
    assert status["isIndexing"] is True


def test_project_status_isIndexing_defaults_false() -> None:
    status = parse_project_status(None, [])
    assert status["isIndexing"] is False


def test_project_status_isIndexing_alias_keys() -> None:
    for alias in ("is_indexing", "indexing", "indexInProgress", "isSmartMode"):
        status = parse_project_status({alias: True}, [])
        assert status["isIndexing"] is True, f"alias {alias} not recognized"


def test_project_status_json_text_merged() -> None:
    text = '{"isIndexing": false, "projectName": "demo"}'
    status = parse_project_status(None, [_TextContent(text)])
    assert status["isIndexing"] is False
    assert status["projectName"] == "demo"


# ---------------------------------------------------------------------------
# 模型契约
# ---------------------------------------------------------------------------


def test_model_serializes_with_camel_aliases() -> None:
    p = JetBrainsProblem(
        source="jetbrains",
        inspectionId="X",
        severity="ERROR",
        description="d",
        filePath="/p/a.py",
        startLine=1,
        startColumn=2,
        endLine=3,
        endColumn=4,
    )
    d = p.model_dump(by_alias=True)
    assert d["inspectionId"] == "X"
    assert d["filePath"] == "/p/a.py"
    assert d["startLine"] == 1
    assert d["endColumn"] == 4
