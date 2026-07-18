# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for pycharm-code-quality-mcp.
#
# Produces a single-file (onefile) executable. On Windows the build target is a
# console-less-appropriate console exe: we keep console=True so the stdio MCP server
# can read/write stdin/stdout (a windowed/gui exe has no stdio handles).
#
# Build:
#   pyinstaller --clean --noconfirm pyinstaller/pycharm-code-quality-mcp.spec
# Output appears under pyinstaller/dist/.

from __future__ import annotations

import sys
from pathlib import Path

block_cipher = None

# Project root (two levels up from this spec file).
ROOT = Path(SPECPATH).resolve().parent  # type: ignore[name-defined]
PKG_SRC = ROOT / "src" / "pycharm_code_quality_mcp"

a_datas: list[tuple[str, str]] = []  # no external data files required

a_binaries: list[tuple[str, str]] = []

a_hiddenimports = [
    "pycharm_code_quality_mcp",
    "pycharm_code_quality_mcp.__main__",
    "pycharm_code_quality_mcp.cli",
    "pycharm_code_quality_mcp.server",
    # Hidden imports that PyInstaller's static analysis sometimes misses.
    "mcp.server.fastmcp",
    "mcp.server.lowlevel.server",
    "pydantic",
    "httpx",
    "anyio",
]

a = Analysis(
    [str(ROOT / "src" / "pycharm_code_quality_mcp" / "_pyi_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=a_binaries,
    datas=a_datas,
    hiddenimports=a_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude test + dev tooling that should never ship in the binary.
        "tests",
        "pytest",
        "mypy",
        "ruff",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # type: ignore[name-defined]

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="pycharm-code-quality-mcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # Windows: keep console so stdio works. On macOS/Linux this has no effect.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon=None,  # add an .icns/.ico when available
)
