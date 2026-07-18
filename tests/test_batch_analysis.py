"""Tests for workspace roots resolution and batch analysis result building."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pycharm_sonar_mcp import errors
from pycharm_sonar_mcp.result_summary import assert_single_project_root
from pycharm_sonar_mcp.workspace import require_workspace_roots, resolve_workspace_roots

# ---------------------------------------------------------------------------
# resolve_workspace_roots
# ---------------------------------------------------------------------------


def test_resolve_env_single(tmp_path: Path) -> None:
    root = str(tmp_path)
    res = resolve_workspace_roots(None, env={"SONAR_WORKSPACE_ROOTS": root})
    assert res == [os.path.normpath(root)]


def test_resolve_env_multiple(tmp_path: Path) -> None:
    r1 = tmp_path / "a"
    r2 = tmp_path / "b"
    r1.mkdir()
    r2.mkdir()
    sep = os.pathsep
    res = resolve_workspace_roots(None, env={"SONAR_WORKSPACE_ROOTS": f"{r1}{sep}{r2}"})
    assert len(res) == 2


def test_resolve_mcp_roots_preferred(tmp_path: Path) -> None:
    env_root = str(tmp_path / "env")
    os.makedirs(env_root)
    mcp_root = str(tmp_path / "mcp")
    os.makedirs(mcp_root)
    res = resolve_workspace_roots([mcp_root], env={"SONAR_WORKSPACE_ROOTS": env_root})
    assert res == [os.path.normpath(mcp_root)]


def test_resolve_empty_returns_empty() -> None:
    assert resolve_workspace_roots(None, env={}) == []


def test_require_raises_when_none() -> None:
    with pytest.raises(errors.SonarMcpError) as ei:
        require_workspace_roots(None, env={})
    assert ei.value.code == errors.WORKSPACE_NOT_CONFIGURED


def test_resolve_dedupes_case_variants(tmp_path: Path) -> None:
    root = str(tmp_path)
    # Same path twice should collapse to one.
    res = resolve_workspace_roots([root, root], env={})
    assert len(res) == 1


# ---------------------------------------------------------------------------
# assert_single_project_root
# ---------------------------------------------------------------------------


def test_single_root_ok(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    f1 = root / "src" / "a.py"
    f2 = root / "src" / "b.py"
    f1.write_text("x")
    f2.write_text("x")
    detected = assert_single_project_root([str(f1), str(f2)], [str(root)])
    assert os.path.normcase(detected) == os.path.normcase(str(root))


def test_multiple_roots_rejected(tmp_path: Path) -> None:
    r1 = tmp_path / "p1"
    r2 = tmp_path / "p2"
    (r1).mkdir()
    (r2).mkdir()
    f1 = r1 / "a.py"
    f2 = r2 / "b.py"
    f1.write_text("x")
    f2.write_text("x")
    with pytest.raises(errors.SonarMcpError) as ei:
        assert_single_project_root([str(f1), str(f2)], [str(r1), str(r2)])
    assert ei.value.code == errors.MULTIPLE_PROJECT_ROOTS


def test_no_files_raises() -> None:
    with pytest.raises(errors.SonarMcpError):
        assert_single_project_root([], ["/proj"])


def test_outside_workspace_rejected(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    f = outside / "x.py"
    f.write_text("x")
    with pytest.raises(errors.SonarMcpError) as ei:
        assert_single_project_root([str(f)], [str(root)])
    assert ei.value.code == errors.WORKSPACE_VIOLATION
