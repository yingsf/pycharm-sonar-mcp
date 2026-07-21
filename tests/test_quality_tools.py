"""Tests for code_quality_* tools

通过 monkeypatch ``quality_tools._build_orchestrator`` 注入 FakeBackend,
避免依赖真实 MCP / Sonar HTTP。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from pycharm_code_quality_mcp import errors
from pycharm_code_quality_mcp.quality.models import SourceFinding, UnifiedRange
from pycharm_code_quality_mcp.tools import quality_tools

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeBackend:
    def __init__(
        self,
        name: str,
        *,
        available: bool = True,
        findings: list[SourceFinding] | None = None,
        success: bool = True,
    ) -> None:
        self._name = name
        self._available = available
        self._findings = findings or []
        self._success = success

    @property
    def name(self) -> str:
        return self._name

    async def is_available(self) -> bool:
        return self._available

    async def get_status(self) -> dict[str, Any]:
        return {"available": self._available, "name": self._name}

    async def analyze_files(
        self, file_paths: list[str], errors_only: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        _ = errors_only, kwargs
        return {
            "success": self._success,
            "available": True,
            "findings": list(self._findings),
            "raw_findings": [],
            "failed_files": [],
            "duration_ms": 5,
            "error": None,
        }


def _sf(
    source: str,
    file_path: str,
    msg: str,
    *,
    line: int = 1,
    severity: str = "MAJOR",
    rule_id: str | None = None,
) -> SourceFinding:
    return SourceFinding(
        source=source,
        ruleId=rule_id,
        severity=severity,
        message=msg,
        filePath=file_path,
        range=UnifiedRange(startLine=line, startColumn=1, endLine=line, endColumn=10),
        raw={},
    )


def _patch_orch(monkeypatch, jb: _FakeBackend | None, sn: _FakeBackend | None) -> None:
    """让 quality_tools._build_orchestrator 返回注入的后端"""
    from pycharm_code_quality_mcp.quality.orchestrator import QualityOrchestrator

    def _factory() -> QualityOrchestrator:
        return QualityOrchestrator(jetbrains=jb, sonar=sn)  # type: ignore[arg-type]

    monkeypatch.setattr(quality_tools, "_build_orchestrator", _factory)


# ---------------------------------------------------------------------------
# code_quality_status
# ---------------------------------------------------------------------------


def test_status_reports_both_backends(monkeypatch) -> None:
    _patch_orch(monkeypatch, _FakeBackend("jetbrains"), _FakeBackend("sonar"))
    result = asyncio.run(quality_tools.impl_status(None))
    assert result["available"] is True
    assert result["defaultBackend"] == "jetbrains"
    assert result["backends"]["jetbrains"]["available"] is True
    assert result["backends"]["sonar"]["available"] is True


def test_status_no_backends(monkeypatch) -> None:
    _patch_orch(monkeypatch, None, None)
    result = asyncio.run(quality_tools.impl_status(None))
    assert result["available"] is False
    assert result["defaultBackend"] == "none"


# ---------------------------------------------------------------------------
# code_quality_analyze_files
# ---------------------------------------------------------------------------


def _seed_file(workspace: Path, name: str = "a.py") -> str:
    f = workspace / "src" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x = 1\n")
    return str(f)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    return proj


def test_analyze_files_success(monkeypatch, workspace: Path) -> None:
    file_path = _seed_file(workspace)
    os.environ["SONAR_WORKSPACE_ROOTS"] = str(workspace)
    jb = _FakeBackend(
        "jetbrains", findings=[_sf("jetbrains", file_path, "Unused variable", line=1)]
    )
    _patch_orch(monkeypatch, jb, None)
    result = asyncio.run(quality_tools.impl_analyze_files([file_path]))
    assert result["success"] is True
    assert result["uniqueFindingCount"] == 1
    assert result["findings"][0]["sources"] == ["jetbrains"]


def test_analyze_files_empty_rejected(monkeypatch, workspace: Path) -> None:
    os.environ["SONAR_WORKSPACE_ROOTS"] = str(workspace)
    _patch_orch(monkeypatch, _FakeBackend("jetbrains"), None)
    result = asyncio.run(quality_tools.impl_analyze_files([]))
    assert result["success"] is False
    assert result["errorCode"] == errors.BAD_REQUEST


def test_analyze_files_no_workspace_rejected(monkeypatch, workspace: Path) -> None:
    os.environ.pop("SONAR_WORKSPACE_ROOTS", None)
    file_path = _seed_file(workspace)
    _patch_orch(monkeypatch, _FakeBackend("jetbrains"), None)
    result = asyncio.run(quality_tools.impl_analyze_files([file_path]))
    assert result["success"] is False
    assert result["errorCode"] == errors.WORKSPACE_NOT_CONFIGURED


def test_analyze_files_no_workspace_uses_project_root(monkeypatch, workspace: Path) -> None:
    """无 MCP Roots + 无 env 时,显式传 project_root 应作为兜底工作区"""
    os.environ.pop("SONAR_WORKSPACE_ROOTS", None)
    file_path = _seed_file(workspace)
    _patch_orch(monkeypatch, _FakeBackend("jetbrains"), None)
    result = asyncio.run(quality_tools.impl_analyze_files([file_path], project_root=str(workspace)))
    assert result["success"] is True


def test_analyze_files_too_many(monkeypatch, workspace: Path) -> None:
    os.environ["SONAR_WORKSPACE_ROOTS"] = str(workspace)
    files = [_seed_file(workspace, f"f{i}.py") for i in range(201)]
    _patch_orch(monkeypatch, _FakeBackend("jetbrains"), None)
    result = asyncio.run(quality_tools.impl_analyze_files(files))
    assert result["success"] is False
    assert result["errorCode"] == errors.TOO_MANY_FILES


def test_analyze_files_invalid_backend_mode(monkeypatch, workspace: Path) -> None:
    os.environ["SONAR_WORKSPACE_ROOTS"] = str(workspace)
    file_path = _seed_file(workspace)
    _patch_orch(monkeypatch, _FakeBackend("jetbrains"), None)
    result = asyncio.run(quality_tools.impl_analyze_files([file_path], backend_mode="invalid"))
    assert result["success"] is False
    assert result["errorCode"] == errors.BAD_REQUEST


def test_analyze_files_invalid_dedup_mode(monkeypatch, workspace: Path) -> None:
    os.environ["SONAR_WORKSPACE_ROOTS"] = str(workspace)
    file_path = _seed_file(workspace)
    _patch_orch(monkeypatch, _FakeBackend("jetbrains"), None)
    result = asyncio.run(quality_tools.impl_analyze_files([file_path], deduplication_mode="bogus"))
    assert result["success"] is False
    assert result["errorCode"] == errors.BAD_REQUEST


def test_analyze_files_no_backend_available_returns_failure(monkeypatch, workspace: Path) -> None:
    os.environ["SONAR_WORKSPACE_ROOTS"] = str(workspace)
    file_path = _seed_file(workspace)
    _patch_orch(monkeypatch, None, None)
    result = asyncio.run(quality_tools.impl_analyze_files([file_path]))
    assert result["success"] is False
    # 应该带 NO_ANALYSIS_BACKEND_AVAILABLE 的提示。
    assert any("No analysis backend" in n for n in result.get("notices", []))


def test_analyze_files_deduplicates_cross_backend(monkeypatch, workspace: Path) -> None:
    file_path = _seed_file(workspace)
    os.environ["SONAR_WORKSPACE_ROOTS"] = str(workspace)
    jb = _FakeBackend(
        "jetbrains",
        findings=[_sf("jetbrains", file_path, "Unused parameter 'x'", line=1)],
    )
    sn = _FakeBackend(
        "sonar",
        findings=[_sf("sonar", file_path, "Unused parameter 'x'", line=1)],
    )
    _patch_orch(monkeypatch, jb, sn)
    result = asyncio.run(quality_tools.impl_analyze_files([file_path], backend_mode="auto"))
    assert result["rawFindingCount"] == 2
    assert result["uniqueFindingCount"] == 1
    assert set(result["findings"][0]["sources"]) == {"jetbrains", "sonar"}


def test_analyze_files_skipped_outside_workspace(
    monkeypatch, workspace: Path, tmp_path: Path
) -> None:
    os.environ["SONAR_WORKSPACE_ROOTS"] = str(workspace)
    outside = tmp_path / "outside.py"
    outside.write_text("x")
    _patch_orch(monkeypatch, _FakeBackend("jetbrains"), None)
    # 全部 outside => bad request(no valid files)。
    result = asyncio.run(quality_tools.impl_analyze_files([str(outside)]))
    assert result["success"] is False


# ---------------------------------------------------------------------------
# code_quality_clear_cache
# ---------------------------------------------------------------------------


def test_clear_cache_all() -> None:
    from pycharm_code_quality_mcp.backends.sonar.discovery import get_global_cache

    cache = get_global_cache()
    cache.set("/tmp/proj", 64120)
    result = quality_tools.impl_clear_cache(None)
    assert result["cleared"] is True
    assert cache.get("/tmp/proj") is None
    assert result["backends"]["sonar"] == "cleared"


def test_clear_cache_single() -> None:
    from pycharm_code_quality_mcp.backends.sonar.discovery import get_global_cache

    cache = get_global_cache()
    cache.set("/tmp/proj2", 64121)
    result = quality_tools.impl_clear_cache("/tmp/proj2")
    assert result["cleared"] is True
    assert cache.get("/tmp/proj2") is None


# ---------------------------------------------------------------------------
# code_quality_analyze_git_changes:空变更场景
# ---------------------------------------------------------------------------


def test_analyze_git_changes_no_changes(monkeypatch, tmp_path: Path) -> None:
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
    _patch_orch(monkeypatch, _FakeBackend("jetbrains"), None)
    result = asyncio.run(quality_tools.impl_analyze_git_changes(str(repo)))
    assert result["success"] is True
    assert result["changedFileCount"] == 0
    assert "No changed files" in result["notices"][0]


# ---------------------------------------------------------------------------
# code_quality_analyze_project
# ---------------------------------------------------------------------------


def _init_repo(repo: Path) -> None:
    """在 tmp_path 下建一个带初始 commit 的 git repo(用于 analyze_project 测试)"""
    import subprocess

    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(repo), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=str(repo), check=True, capture_output=True
    )


def _git_commit(repo: Path) -> None:
    import subprocess

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


def test_analyze_project_success(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "a.py").write_text("x=1\n")
    (repo / "b.py").write_text("y=2\n")
    _git_commit(repo)

    _patch_orch(monkeypatch, _FakeBackend("jetbrains"), None)
    result = asyncio.run(quality_tools.impl_analyze_project(str(repo)))
    assert result["success"] is True
    assert result["scannedFileCount"] == 2
    assert result["extensions"] == [".py"]


def test_analyze_project_respects_extensions(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "a.py").write_text("x=1\n")
    (repo / "b.txt").write_text("hello\n")
    (repo / "c.md").write_text("# title\n")
    _git_commit(repo)

    _patch_orch(monkeypatch, _FakeBackend("jetbrains"), None)
    result = asyncio.run(quality_tools.impl_analyze_project(str(repo), extensions=[".py"]))
    assert result["success"] is True
    assert result["scannedFileCount"] == 1
    assert result["extensions"] == [".py"]


def test_analyze_project_empty_repo(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "README.md").write_text("# empty\n")
    _git_commit(repo)

    _patch_orch(monkeypatch, _FakeBackend("jetbrains"), None)
    result = asyncio.run(quality_tools.impl_analyze_project(str(repo)))
    assert result["success"] is True
    assert result["scannedFileCount"] == 0
    assert "No files matching extensions" in result["notices"][0]


def test_analyze_project_not_a_repo(tmp_path: Path) -> None:
    not_repo = tmp_path / "not_a_repo"
    not_repo.mkdir()
    (not_repo / "a.py").write_text("x=1\n")

    result = asyncio.run(quality_tools.impl_analyze_project(str(not_repo)))
    assert result["success"] is False
    assert result["errorCode"] == errors.GIT_INVALID_REPOSITORY


def test_analyze_project_no_workspace_uses_project_root(monkeypatch, tmp_path: Path) -> None:
    """无 MCP Roots + 无 SONAR_WORKSPACE_ROOTS 时,project_root 应作为兜底工作区"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "a.py").write_text("x=1\n")
    _git_commit(repo)

    # 清空环境变量,模拟无 Roots 场景。
    monkeypatch.delenv("SONAR_WORKSPACE_ROOTS", raising=False)
    _patch_orch(monkeypatch, _FakeBackend("jetbrains"), None)
    result = asyncio.run(quality_tools.impl_analyze_project(str(repo)))
    assert result["success"] is True
    assert result["scannedFileCount"] == 1
