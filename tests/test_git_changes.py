"""Tests for git change collection: NUL-separated parsing, no shell=True, dedup, filters.

Uses a real temporary git repository so the actual `git` subprocess path is exercised.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pycharm_code_quality_mcp import errors
from pycharm_code_quality_mcp.core import git_changes


def _git(args: list[str], *, cwd: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", *args], cwd=cwd, check=True, env=env, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _git(["init"], cwd=str(repo_path))
    # Try to set default branch name explicitly (older git may reject; not fatal).
    with contextlib.suppress(subprocess.CalledProcessError):
        _git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=str(repo_path))
    _git(["config", "user.name", "T"], cwd=str(repo_path))
    _git(["config", "user.email", "t@t"], cwd=str(repo_path))
    # Initial commit with a base file.
    (repo_path / "base.py").write_text("x = 1\n")
    _git(["add", "."], cwd=str(repo_path))
    _git(["commit", "-m", "init"], cwd=str(repo_path))
    return repo_path


def _write(repo: Path, rel: str, content: str = "y = 2\n") -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def test_collect_modified_tracked(repo: Path) -> None:
    _write(repo, "base.py", "modified\n")
    files = git_changes.collect_changed_files(str(repo), workspace_roots=[str(repo)])
    assert any(os.path.normcase(f) == os.path.normcase(str(repo / "base.py")) for f in files)


def test_collect_untracked(repo: Path) -> None:
    _write(repo, "new.py")
    files = git_changes.collect_changed_files(str(repo), workspace_roots=[str(repo)])
    assert any(f.endswith("new.py") for f in files)


def test_collect_staged(repo: Path) -> None:
    _write(repo, "staged.py")
    _git(["add", "staged.py"], cwd=str(repo))
    files = git_changes.collect_changed_files(str(repo), workspace_roots=[str(repo)])
    assert any(f.endswith("staged.py") for f in files)


def test_exclude_deleted(repo: Path) -> None:
    _write(repo, "base.py", "modified\n")
    os.remove(repo / "base.py")
    files = git_changes.collect_changed_files(str(repo), workspace_roots=[str(repo)])
    # base.py is deleted → must not be present.
    assert not any(f.endswith("base.py") for f in files)


def test_exclude_directories(repo: Path) -> None:
    (repo / "newdir").mkdir()
    files = git_changes.collect_changed_files(str(repo), workspace_roots=[str(repo)])
    assert not any("newdir" in f for f in files)


def test_dedupe(repo: Path) -> None:
    # Stage + modify the same file → should appear once.
    _write(repo, "dup.py")
    _git(["add", "dup.py"], cwd=str(repo))
    _write(repo, "dup.py", "again\n")
    files = git_changes.collect_changed_files(str(repo), workspace_roots=[str(repo)])
    assert sum(1 for f in files if f.endswith("dup.py")) == 1


def test_filename_with_spaces(repo: Path) -> None:
    _write(repo, "my file.py")
    files = git_changes.collect_changed_files(str(repo), workspace_roots=[str(repo)])
    assert any("my file.py" in f for f in files)


def test_filename_with_chinese(repo: Path) -> None:
    _write(repo, "中文.py")
    files = git_changes.collect_changed_files(str(repo), workspace_roots=[str(repo)])
    assert any("中文.py" in f for f in files)


def test_filename_with_tab(repo: Path) -> None:
    # Tabs in filenames are legal on POSIX; ensure NUL parsing handles them.
    if sys.platform.startswith("win"):
        pytest.skip("tab in filename not allowed on Windows")
    _write(repo, "tab\tfile.py")
    files = git_changes.collect_changed_files(str(repo), workspace_roots=[str(repo)])
    assert any("tab\tfile.py" in f for f in files)


def test_not_a_repo(tmp_path: Path) -> None:
    with pytest.raises(errors.SonarMcpError) as ei:
        git_changes.resolve_repo_root(str(tmp_path / "nope"))
    assert ei.value.code in {errors.GIT_INVALID_REPOSITORY, errors.GIT_COMMAND_FAILED}


def test_invalid_base_ref(repo: Path) -> None:
    with pytest.raises(errors.SonarMcpError) as ei:
        git_changes.validate_base_ref("nonexistent-ref", cwd=str(repo))
    assert ei.value.code == errors.GIT_INVALID_BASE_REF


def test_resolve_repo_root(repo: Path) -> None:
    root = git_changes.resolve_repo_root(str(repo))
    assert os.path.normcase(root) == os.path.normcase(str(repo))


def test_is_git_repo(repo: Path, tmp_path: Path) -> None:
    assert git_changes.is_git_repo(str(repo)) is True
    assert git_changes.is_git_repo(str(tmp_path / "nope")) is False


def test_workspace_filter(repo: Path) -> None:
    _write(repo, "inside.py")
    # Pass a workspace root that does NOT contain the repo → file filtered out.
    other = repo.parent / "other_ws"
    other.mkdir()
    files = git_changes.collect_changed_files(str(repo), workspace_roots=[str(other)])
    assert files == []


def test_split_nul_handling() -> None:
    # Direct unit test of the NUL splitter.
    assert git_changes._split_nul("") == []
    assert git_changes._split_nul("a\x00b\x00") == ["a", "b"]
    assert git_changes._split_nul("a\x00") == ["a"]
    # Embedded newline must survive (proves we don't use splitlines).
    assert git_changes._split_nul("a\nb\x00c\x00") == ["a\nb", "c"]


def test_no_shell_true() -> None:
    """Defensive: the git runner must call subprocess.run without shell=True."""
    import inspect

    src = inspect.getsource(git_changes._git)
    # The actual subprocess.run call must not pass shell=True.
    assert "shell=True" not in src
    assert "shell = True" not in src
