"""Tests for the MCP server: tool registration and schema sanity."""

from __future__ import annotations

import asyncio

from pycharm_code_quality_mcp.server import build_app

# ---------------------------------------------------------------------------
# Tool wiring / schema
# ---------------------------------------------------------------------------


def test_app_registers_eight_tools() -> None:
    app = build_app()
    tools = asyncio.run(app.list_tools())
    names = {t.name for t in tools}
    assert names == {
        # 统一默认(5)
        "code_quality_status",
        "code_quality_analyze_files",
        "code_quality_analyze_git_changes",
        "code_quality_analyze_project",
        "code_quality_clear_cache",
        # JetBrains 专用(3)
        "jetbrains_ide_status",
        "jetbrains_inspect_files",
        "jetbrains_inspect_git_changes",
    }


def test_tool_descriptions_nonempty() -> None:
    app = build_app()
    tools = asyncio.run(app.list_tools())
    for t in tools:
        assert t.description and len(t.description) > 10


def test_analyze_files_schema_has_required_inputs() -> None:
    app = build_app()
    tools = asyncio.run(app.list_tools())
    analyze = next(t for t in tools if t.name == "code_quality_analyze_files")
    schema = analyze.inputSchema or {}
    props = schema.get("properties", {})
    assert "file_absolute_paths" in props
    assert schema.get("required") == ["file_absolute_paths"]
