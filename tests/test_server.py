"""Tests for the MCP server: tool schemas, analyze flow, partial failure, multi-project."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from pycharm_sonar_mcp import errors
from pycharm_sonar_mcp.ide_discovery import IdeDiscovery
from pycharm_sonar_mcp.server import (
    _impl_analyze_files,
    _impl_analyze_git_changes,
    _impl_clear_cache,
    _impl_ide_status,
    reset_singletons,
)
from pycharm_sonar_mcp.sonar_client import SonarClient

# ---------------------------------------------------------------------------
# Tool wiring / schema
# ---------------------------------------------------------------------------


def test_app_registers_four_tools() -> None:
    from pycharm_sonar_mcp.server import build_app

    app = build_app()

    tools = asyncio.run(app.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "sonar_ide_status",
        "sonar_analyze_files",
        "sonar_analyze_git_changes",
        "sonar_clear_cache",
    }


def test_tool_descriptions_nonempty() -> None:
    from pycharm_sonar_mcp.server import build_app

    app = build_app()
    tools = asyncio.run(app.list_tools())
    for t in tools:
        assert t.description and len(t.description) > 10


def test_analyze_files_schema_has_required_inputs() -> None:
    from pycharm_sonar_mcp.server import build_app

    app = build_app()
    tools = asyncio.run(app.list_tools())
    analyze = next(t for t in tools if t.name == "sonar_analyze_files")
    schema = analyze.inputSchema or {}
    props = schema.get("properties", {})
    assert "file_absolute_paths" in props
    assert schema.get("required") == ["file_absolute_paths"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wire_server(client: SonarClient, *, roots: list[str]) -> None:
    """Install mocked singletons into the server module + set workspace env."""
    os.environ["SONAR_WORKSPACE_ROOTS"] = os.pathsep.join(roots)
    discovery = IdeDiscovery(client)
    reset_singletons(client=client, discovery=discovery)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    return proj


# ---------------------------------------------------------------------------
# sonar_ide_status
# ---------------------------------------------------------------------------


def test_ide_status_reports_instances(mock_sonar_factory) -> None:
    client, _r = mock_sonar_factory(ports=[64120])
    _wire_server(client, roots=[])
    result = asyncio.run(_impl_ide_status())
    assert result["available"] is True
    assert result["instanceCount"] == 1
    assert result["instances"][0]["port"] == 64120


def test_ide_status_no_instances(mock_sonar_factory) -> None:
    client, _r = mock_sonar_factory(ports=[])
    _wire_server(client, roots=[])
    # Override discovery to scan empty ports.

    discovery = IdeDiscovery(client, ports=[])
    reset_singletons(client=client, discovery=discovery)
    result = asyncio.run(_impl_ide_status())
    assert result["available"] is False
    assert result["instanceCount"] == 0


# ---------------------------------------------------------------------------
# sonar_analyze_files
# ---------------------------------------------------------------------------


def _seed_files(proj: Path, names: list[str]) -> list[str]:
    out: list[str] = []
    for n in names:
        f = proj / "src" / n
        f.write_text("x = 1\n")
        out.append(str(f))
    return out


def test_analyze_files_single_success(mock_sonar_factory, workspace: Path) -> None:
    client, routes = mock_sonar_factory(
        ports=[64120],
        analysis_body={
            "findings": [
                {
                    "ruleKey": "python:S3776",
                    "message": "complex",
                    "severity": "CRITICAL",
                    "filePath": None,  # filled below
                    "textRange": {
                        "startLine": 1,
                        "startLineOffset": 0,
                        "endLine": 1,
                        "endLineOffset": 5,
                    },
                }
            ]
        },
    )
    files = _seed_files(workspace, ["a.py"])
    # Inject the right filePath into the canned response.
    payload = {
        "findings": [
            {
                "ruleKey": "python:S3776",
                "message": "complex",
                "severity": "CRITICAL",
                "filePath": files[0],
                "textRange": {
                    "startLine": 1,
                    "startLineOffset": 0,
                    "endLine": 1,
                    "endLineOffset": 5,
                },
            }
        ]
    }

    def handler(req):
        from tests.conftest import _make_status_response

        return _make_status_response(200, payload)

    routes[(64120, "/sonarlint/api/analysis/files")] = handler

    _wire_server(client, roots=[str(workspace)])
    result = asyncio.run(_impl_analyze_files(files, None))
    assert result["success"] is True
    assert result["findingCount"] == 1
    assert result["severityCounts"]["CRITICAL"] == 1
    assert result["fileSummaries"][0]["status"] == "analyzed"
    assert result["idePort"] == 64120


def test_analyze_files_empty_rejected(mock_sonar_factory, workspace: Path) -> None:
    client, _r = mock_sonar_factory(ports=[64120])
    _wire_server(client, roots=[str(workspace)])
    result = asyncio.run(_impl_analyze_files([], None))
    assert result["success"] is False
    assert result["errorCode"] == errors.BAD_REQUEST


def test_analyze_files_too_many(mock_sonar_factory, workspace: Path) -> None:
    client, _r = mock_sonar_factory(ports=[64120])
    _wire_server(client, roots=[str(workspace)])
    files = _seed_files(workspace, [f"f{i}.py" for i in range(201)])
    result = asyncio.run(_impl_analyze_files(files, None))
    assert result["success"] is False
    assert result["errorCode"] == errors.TOO_MANY_FILES


def test_analyze_files_dedupes(mock_sonar_factory, workspace: Path) -> None:
    client, _r = mock_sonar_factory(ports=[64120])
    files = _seed_files(workspace, ["a.py", "a.py"])
    _wire_server(client, roots=[str(workspace)])
    result = asyncio.run(_impl_analyze_files(files, None))
    assert result["requestedFileCount"] == 1


def test_analyze_files_relative_rejected(mock_sonar_factory, workspace: Path) -> None:
    client, _r = mock_sonar_factory(ports=[64120])
    _wire_server(client, roots=[str(workspace)])
    result = asyncio.run(_impl_analyze_files(["relative/a.py"], None))
    assert result["success"] is False


def test_analyze_files_outside_workspace(
    mock_sonar_factory, workspace: Path, tmp_path: Path
) -> None:
    client, _r = mock_sonar_factory(ports=[64120])
    outside = tmp_path / "outside.py"
    outside.write_text("x")
    _wire_server(client, roots=[str(workspace)])
    result = asyncio.run(_impl_analyze_files([str(outside)], None))
    # Either rejected as bad request or surfaced as skipped.
    assert result["success"] is False or any(
        s["status"] == "skipped" for s in result.get("fileSummaries", [])
    )


def test_analyze_files_no_workspace_rejected(mock_sonar_factory, workspace: Path) -> None:
    client, _r = mock_sonar_factory(ports=[64120])
    files = _seed_files(workspace, ["a.py"])
    os.environ.pop("SONAR_WORKSPACE_ROOTS", None)
    reset_singletons(client=client, discovery=IdeDiscovery(client))
    result = asyncio.run(_impl_analyze_files(files, None))
    assert result["success"] is False
    assert result["errorCode"] == errors.WORKSPACE_NOT_CONFIGURED


def test_analyze_files_partial_failure(mock_sonar_factory, workspace: Path, make_response) -> None:
    """Middle batch fails; result must keep partialSuccess + earlier findings."""
    client, routes = mock_sonar_factory(ports=[64120])
    files = _seed_files(workspace, [f"f{i}.py" for i in range(120)])

    call_count = {"n": 0}

    def handler(req):
        call_count["n"] += 1
        if call_count["n"] == 2:
            return make_response(503, {"indexing": True})
        return make_response(200, {"findings": []})

    routes[(64120, "/sonarlint/api/analysis/files")] = handler
    _wire_server(client, roots=[str(workspace)])
    result = asyncio.run(_impl_analyze_files(files, None))
    assert result["partialSuccess"] is True
    assert len(result["batchErrors"]) == 1
    assert result["batchErrors"][0]["batchIndex"] == 1


def test_analyze_files_137_files_three_batches(
    mock_sonar_factory, workspace: Path, make_response
) -> None:
    client, _r = mock_sonar_factory(ports=[64120])
    files = _seed_files(workspace, [f"f{i}.py" for i in range(137)])
    _wire_server(client, roots=[str(workspace)])
    result = asyncio.run(_impl_analyze_files(files, None))
    assert result["requestedFileCount"] == 137
    assert result["analyzedFileCount"] == 137


def test_analyze_files_multi_project_rejected(mock_sonar_factory, tmp_path: Path) -> None:
    client, _r = mock_sonar_factory(ports=[64120])
    p1 = tmp_path / "p1"
    p2 = tmp_path / "p2"
    (p1).mkdir()
    (p2).mkdir()
    f1 = p1 / "a.py"
    f2 = p2 / "b.py"
    f1.write_text("x")
    f2.write_text("x")
    _wire_server(client, roots=[str(p1), str(p2)])
    result = asyncio.run(_impl_analyze_files([str(f1), str(f2)], None))
    assert result["success"] is False
    assert result["errorCode"] == errors.MULTIPLE_PROJECT_ROOTS


def test_analyze_files_200_limit_ok(mock_sonar_factory, workspace: Path) -> None:
    client, _r = mock_sonar_factory(ports=[64120])
    files = _seed_files(workspace, [f"f{i}.py" for i in range(200)])
    _wire_server(client, roots=[str(workspace)])
    result = asyncio.run(_impl_analyze_files(files, None))
    assert result["requestedFileCount"] == 200


def test_analyze_files_empty_findings_marked_analyzed(mock_sonar_factory, workspace: Path) -> None:
    """No findings must NOT be conflated with failure — status must be 'analyzed'."""
    client, _r = mock_sonar_factory(ports=[64120], analysis_body={"findings": []})
    files = _seed_files(workspace, ["clean.py"])
    _wire_server(client, roots=[str(workspace)])
    result = asyncio.run(_impl_analyze_files(files, None))
    assert result["success"] is True
    assert result["fileSummaries"][0]["status"] == "analyzed"
    assert result["fileSummaries"][0]["findingCount"] == 0


# ---------------------------------------------------------------------------
# sonar_clear_cache
# ---------------------------------------------------------------------------


def test_clear_cache_all(mock_sonar_factory, workspace: Path) -> None:
    from pycharm_sonar_mcp.ide_discovery import get_global_cache

    client, _r = mock_sonar_factory(ports=[64120])
    _wire_server(client, roots=[str(workspace)])
    cache = get_global_cache()
    cache.set("/x", 64120)
    result = _impl_clear_cache(None)
    assert result["cleared"] is True
    assert len(cache) == 0


def test_clear_cache_single(mock_sonar_factory, workspace: Path) -> None:
    from pycharm_sonar_mcp.ide_discovery import get_global_cache

    client, _r = mock_sonar_factory(ports=[64120])
    _wire_server(client, roots=[str(workspace)])
    cache = get_global_cache()
    cache.set("/proj", 64120)
    result = _impl_clear_cache("/proj")
    assert result["cleared"] is True
    assert cache.get("/proj") is None


# ---------------------------------------------------------------------------
# sonar_analyze_git_changes
# ---------------------------------------------------------------------------


def test_analyze_git_changes_no_changes(mock_sonar_factory, tmp_path: Path) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(repo), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=str(repo), check=True, capture_output=True
    )
    (repo / "base.py").write_text("x=1\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )
    client, _r = mock_sonar_factory(ports=[64120])
    _wire_server(client, roots=[str(repo)])
    result = asyncio.run(_impl_analyze_git_changes(str(repo), "HEAD", True, True, True))
    assert result["changedFileCount"] == 0
    assert "No changed files" in result["notices"][0]
