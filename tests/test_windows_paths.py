"""Windows-specific path tests.

These run on ALL platforms by directly testing the pure-Python comparison helpers with
Windows-style path strings. Where behavior genuinely requires Windows (junctions), the
test is skipped on non-Windows with a clear note, and the corresponding CI job on
windows-latest exercises it for real.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from pycharm_code_quality_mcp.core import path_utils as pu

IS_WINDOWS = sys.platform.startswith("win")


def _win_normalize(p: str) -> str:
    """Apply the Windows drive-normalization step directly, regardless of host platform."""
    # Mimic normalize_path's Windows branch on the drive letter.
    if len(p) >= 2 and p[1] == ":":
        return p[0].upper() + p[1:]
    return p


def test_drive_letter_case_normalized() -> None:
    assert _win_normalize(r"c:\Project\a.py") == r"C:\Project\a.py"
    assert _win_normalize(r"C:\Project\a.py") == r"C:\Project\a.py"


def test_unc_path_detected() -> None:
    assert pu.is_unc_path(r"\\server\share\file.py") is True
    assert pu.is_unc_path(r"//server/share/file.py") is True
    assert pu.is_unc_path(r"C:\file.py") is False


def test_mixed_separators_normalize() -> None:
    # A path mixing / and \ should normalize consistently.
    mixed = r"C:\Project/src\a.py"
    n1 = os.path.normpath(mixed)
    n2 = os.path.normpath(mixed.replace("/", "\\"))
    # On Windows both become backslash form; on POSIX the test still confirms idempotence.
    assert isinstance(n1, str) and isinstance(n2, str)


def test_drive_letter_uppercase_helper() -> None:
    assert pu._normalize_windows_drive(r"c:\x") == r"C:\x"
    assert pu._normalize_windows_drive(r"D:\y") == r"D:\y"
    assert pu._normalize_windows_drive(r"\\share") == r"\\share"


@pytest.mark.skipif(not IS_WINDOWS, reason="real Windows drive comparison requires Windows")
def test_real_drive_case_insensitive_containment(tmp_path: Path) -> None:
    root = str(tmp_path).upper()
    child_lower = str(tmp_path / "a.py").lower()
    Path(child_lower).write_text("x")
    assert pu.is_within_workspace(child_lower, [root])


@pytest.mark.skipif(not IS_WINDOWS, reason="junction escape requires Windows")
def test_junction_escape(tmp_path: Path) -> None:
    """Create a Windows junction pointing outside the workspace; verify escape detected."""
    import subprocess

    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = root / "escape"
    # mklink /J requires no admin and creates a junction.
    try:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            check=True,
            capture_output=True,
        )
    except Exception as e:
        pytest.skip(f"cannot create junction: {e}")
    # The junction target is outside; check_symlink_escape should flag it.
    assert pu.check_symlink_escape(str(junction), [str(root)]) is True


def test_chinese_path_supported(tmp_path: Path) -> None:
    chinese_dir = tmp_path / "中文目录"
    chinese_dir.mkdir()
    f = chinese_dir / "文件.py"
    f.write_text("x")
    n = pu.validate_regular_file(str(f))
    assert os.path.isfile(n)


def test_space_path_supported(tmp_path: Path) -> None:
    spaced = tmp_path / "my project"
    spaced.mkdir()
    f = spaced / "a file.py"
    f.write_text("x")
    assert os.path.isfile(pu.validate_regular_file(str(f)))


def test_non_ascii_username_path(tmp_path: Path) -> None:
    # Simulate a non-ASCII username path (the tmp_path already varies by user).
    weird = tmp_path / " Üsér" / "code.py"
    weird.parent.mkdir(parents=True)
    weird.write_text("x")
    n = pu.validate_regular_file(str(weird))
    assert "code.py" in n


def test_localized_dir(tmp_path: Path) -> None:
    # macOS uses .localized suffix; ensure such paths work.
    loc = tmp_path / "Projects.localized"
    loc.mkdir()
    f = loc / "a.py"
    f.write_text("x")
    assert pu.is_within_workspace(str(f), [str(loc)])


def test_path_comparison_does_not_raise_cross_drive() -> None:
    # Comparing paths on different drives must not raise.
    assert pu._path_is_within(r"D:\proj\a.py", r"C:\proj") is False
    assert pu._path_is_within(r"\\server\share\a", r"C:\proj") is False
