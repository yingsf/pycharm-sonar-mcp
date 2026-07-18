"""Tests for path_utils: normalization, containment, symlink escape, dedup.

Platform-conditional: Windows-only behaviors (drive letter, UNC, junction) are skipped
on non-Windows but the logic is exercised on all platforms where possible.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from pycharm_code_quality_mcp import errors
from pycharm_code_quality_mcp.core import path_utils as pu

IS_WINDOWS = sys.platform.startswith("win")


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_normalize_relative_becomes_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    n = pu.normalize_path("sub/file.py")
    assert os.path.isabs(n)
    assert n.endswith(os.path.join("sub", "file.py"))


def test_normalize_empty_raises() -> None:
    with pytest.raises(errors.SonarMcpError):
        pu.normalize_path("")


def test_normalize_user_expansion(tmp_path: Path) -> None:
    # ~ should expand; result must be absolute.
    n = pu.normalize_path("~")
    assert os.path.isabs(n)


@pytest.mark.skipif(not IS_WINDOWS, reason="drive letter casing is Windows-only")
def test_normalize_uppercases_drive_letter_windows() -> None:
    lower = pu.normalize_path(r"c:\Windows\System32")
    upper = pu.normalize_path(r"C:\Windows\System32")
    assert lower == upper
    assert lower.startswith("C:")


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------


def test_is_within_workspace_true(tmp_path: Path) -> None:
    root = str(tmp_path)
    child = tmp_path / "a" / "b.py"
    child.parent.mkdir(parents=True)
    child.write_text("x")
    assert pu.is_within_workspace(str(child), [root])


def test_is_within_workspace_false_outside(tmp_path: Path) -> None:
    root = str(tmp_path / "proj")
    os.makedirs(root)
    outside = tmp_path / "other" / "x.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("x")
    assert not pu.is_within_workspace(str(outside), [root])


def test_is_within_workspace_multiple_roots(tmp_path: Path) -> None:
    r1 = tmp_path / "p1"
    r2 = tmp_path / "p2"
    r1.mkdir()
    r2.mkdir()
    f1 = r1 / "a.py"
    f1.write_text("x")
    f2 = r2 / "b.py"
    f2.write_text("x")
    assert pu.is_within_workspace(str(f1), [str(r1), str(r2)])
    assert pu.is_within_workspace(str(f2), [str(r1), str(r2)])


@pytest.mark.skipif(not IS_WINDOWS, reason="drive letter cross-drive is Windows-only")
def test_cross_drive_not_within(tmp_path: Path) -> None:
    root = r"C:\proj"
    candidate = r"D:\other\file.py"
    assert not pu.is_within_workspace(candidate, [root])


# ---------------------------------------------------------------------------
# Symlink escape
# ---------------------------------------------------------------------------


@pytest.mark.skipif(IS_WINDOWS or (os.geteuid() == 0 and False), reason="POSIX symlinks")
def test_symlink_escape_detected(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    # Create a symlink inside root pointing outside.
    link = root / "escape.py"
    try:
        os.symlink(outside, link)
    except OSError as e:
        pytest.skip(f"cannot create symlink: {e}")
    assert pu.check_symlink_escape(str(link), [str(root)])


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX-only")
def test_symlink_inside_not_escape(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    target = root / "sub" / "real.py"
    target.write_text("x")
    link = root / "link.py"
    try:
        os.symlink(target, link)
    except OSError as e:
        pytest.skip(f"cannot create symlink: {e}")
    assert not pu.check_symlink_escape(str(link), [str(root)])


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX-only")
def test_symlink_escape_when_workspace_root_is_symlink(tmp_path: Path) -> None:
    """Escape must still be detected when the workspace root itself is a symlink.

    Regression guard: an asymmetric nominal-vs-resolved comparison previously made
    ``check_symlink_escape`` return False (downgrading the escape to a plain workspace
    violation) when the configured root was a symlink. The escape link must be addressed
    via the symlinked root path so its nominal location is inside the configured root.
    """
    real_root = tmp_path / "real_proj"
    real_root.mkdir()
    root_link = tmp_path / "proj_link"
    try:
        os.symlink(real_root, root_link)
    except OSError as e:
        pytest.skip(f"cannot create symlink: {e}")

    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    # Create the escape link inside real_root, then address it via root_link.
    escape_link_real = real_root / "escape.py"
    try:
        os.symlink(outside, escape_link_real)
    except OSError as e:
        pytest.skip(f"cannot create symlink: {e}")
    escape_link_via_root = root_link / "escape.py"

    # Addressed via the symlinked root: nominal location is inside, target is outside.
    assert pu.check_symlink_escape(str(escape_link_via_root), [str(root_link)])


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX-only")
def test_normal_file_under_symlinked_root_not_escape(tmp_path: Path) -> None:
    """A regular file under a symlinked root must not be flagged as escape."""
    real_root = tmp_path / "real_proj"
    (real_root / "sub").mkdir(parents=True)
    root_link = tmp_path / "proj_link"
    try:
        os.symlink(real_root, root_link)
    except OSError as e:
        pytest.skip(f"cannot create symlink: {e}")
    regular = real_root / "sub" / "a.py"
    regular.write_text("x")
    # Addressed via the symlinked root path.
    regular_via_root = root_link / "sub" / "a.py"
    assert not pu.check_symlink_escape(str(regular_via_root), [str(root_link)])


# ---------------------------------------------------------------------------
# File validation
# ---------------------------------------------------------------------------


def test_validate_regular_file_ok(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("x")
    n = pu.validate_regular_file(str(f))
    assert os.path.isfile(n)


def test_validate_regular_file_missing(tmp_path: Path) -> None:
    with pytest.raises(errors.SonarMcpError) as ei:
        pu.validate_regular_file(str(tmp_path / "nope.py"))
    assert ei.value.code == errors.FILE_NOT_FOUND


def test_validate_regular_file_is_directory(tmp_path: Path) -> None:
    with pytest.raises(errors.SonarMcpError) as ei:
        pu.validate_regular_file(str(tmp_path))
    assert ei.value.code == errors.FILE_NOT_REGULAR


# ---------------------------------------------------------------------------
# Dedupe + stable order
# ---------------------------------------------------------------------------


def test_dedupe_and_sort_removes_dupes(tmp_path: Path) -> None:
    files = [
        str(tmp_path / "b.py"),
        str(tmp_path / "a.py"),
        str(tmp_path / "b.py"),
    ]
    for f in files:
        Path(f).write_text("x")
    out = pu.dedupe_and_sort(files)
    assert len(out) == 2
    assert out[0].endswith("a.py")
    assert out[1].endswith("b.py")


def test_dedupe_case_insensitive_on_windows(tmp_path: Path) -> None:
    if not IS_WINDOWS:
        pytest.skip("Windows case-insensitive FS only")
    f = tmp_path / "A.py"
    f.write_text("x")
    upper = str(tmp_path / "A.py")
    lower = str(tmp_path / "a.py")
    out = pu.dedupe_and_sort([upper, lower])
    assert len(out) == 1


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------


def test_shorten_path_for_log_long() -> None:
    s = pu.shorten_path_for_log("/a/b/c/d/e/file.py")
    assert "..." in s
    assert s.endswith("file.py")


def test_shorten_path_for_log_short() -> None:
    assert pu.shorten_path_for_log("/a/b.py") == "/a/b.py"


def test_hash_path_stable() -> None:
    assert pu.hash_path("/x.py") == pu.hash_path("/x.py")
    assert pu.hash_path("/x.py") != pu.hash_path("/y.py")


def test_project_name_of() -> None:
    assert pu.project_name_of("/home/user/my-proj") == "my-proj"
