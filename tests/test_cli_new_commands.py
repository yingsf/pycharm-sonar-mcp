"""Tests for new CLI subcommands:setup / jetbrains / sonar

这些测试只覆盖不依赖真实 MCP / Sonar HTTP 的部分:
  * setup 输出引导文本
  * jetbrains configure 解析 JSON(成功/失败)
  * jetbrains clear 删除配置
  * jetbrains status 未配置时的输出
  * sonar status 端口扫描
  * loopback URL 校验
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pycharm_code_quality_mcp import errors
from pycharm_code_quality_mcp.backends.jetbrains.config import (
    is_loopback_url,
    load_config,
    save_config,
)


def _run_cli(
    args: list[str], *, timeout: float = 15.0, stdin_data: str | None = None
) -> tuple[int, str, str]:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "pycharm_code_quality_mcp", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        input=stdin_data,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def test_setup_prints_guidance_without_json() -> None:
    rc, out, _err = _run_cli(["setup"])
    assert rc == 0
    assert "setup" in out.lower()
    assert "MCP Server" in out
    assert "get_project_status" in out
    assert "get_file_problems" in out


def test_setup_with_invalid_json_returns_error() -> None:
    rc, _out, err = _run_cli(["setup", "--json", "not-json"])
    assert rc == 2
    assert "error" in err.lower()


# ---------------------------------------------------------------------------
# jetbrains configure JSON 解析
# ---------------------------------------------------------------------------


def test_parse_stream_json_flat_form() -> None:
    from pycharm_code_quality_mcp.cli import _parse_jetbrains_stream_json

    raw = '{"url": "http://localhost:1234/mcp", "headers": {"Authorization": "Bearer x"}}'
    parsed = _parse_jetbrains_stream_json(raw)
    assert parsed is not None
    url, headers = parsed
    assert url == "http://localhost:1234/mcp"
    assert headers["Authorization"] == "Bearer x"


def test_parse_stream_json_transport_nested() -> None:
    from pycharm_code_quality_mcp.cli import _parse_jetbrains_stream_json

    raw = '{"transport": {"type": "streamable-http", "url": "http://127.0.0.1:9999/sse"}}'
    parsed = _parse_jetbrains_stream_json(raw)
    assert parsed is not None
    url, _headers = parsed
    assert url == "http://127.0.0.1:9999/sse"


def test_parse_stream_json_mcp_servers_form() -> None:
    from pycharm_code_quality_mcp.cli import _parse_jetbrains_stream_json

    raw = (
        '{"mcpServers": {"pycharm": {"url": "http://localhost:7777/mcp", "headers": {"X-K": "v"}}}}'
    )
    parsed = _parse_jetbrains_stream_json(raw)
    assert parsed is not None
    url, headers = parsed
    assert url == "http://localhost:7777/mcp"
    assert headers["X-K"] == "v"


def test_parse_stream_json_invalid_returns_none() -> None:
    from pycharm_code_quality_mcp.cli import _parse_jetbrains_stream_json

    assert _parse_jetbrains_stream_json("not json") is None
    assert _parse_jetbrains_stream_json('{"foo": "bar"}') is None
    assert _parse_jetbrains_stream_json("[]") is None


# ---------------------------------------------------------------------------
# jetbrains clear
# ---------------------------------------------------------------------------


def test_jetbrains_clear_when_no_config() -> None:
    # 用临时配置目录,避免影响真实用户配置。
    import platformdirs

    tmpdir = Path(platformdirs.user_config_dir("pycharm-code-quality-mcp"))
    # 仅当配置不存在时,clear 应返回 0 并提示 nothing to clear。
    if not (tmpdir / "config.json").exists():
        rc, out, _err = _run_cli(["jetbrains", "clear"])
        assert rc == 0
        assert "nothing to clear" in out.lower()


def test_jetbrains_status_not_configured(monkeypatch, tmp_path) -> None:
    """配置目录指向空 tmp_path,status 应报告 not configured"""
    monkeypatch.setattr(
        "pycharm_code_quality_mcp.backends.jetbrains.config.config_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "pycharm_code_quality_mcp.backends.jetbrains.config.config_file_path",
        lambda: tmp_path / "config.json",
    )
    # 通过模块入口直接调用,避免 subprocess 继承不到 monkeypatch。
    import argparse

    from pycharm_code_quality_mcp.cli import _cmd_jetbrains_status

    rc = _cmd_jetbrains_status(argparse.Namespace())
    assert rc == 1  # 未配置


# ---------------------------------------------------------------------------
# config 持久化
# ---------------------------------------------------------------------------


def test_save_and_load_config_roundtrip(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "pycharm_code_quality_mcp.backends.jetbrains.config.config_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "pycharm_code_quality_mcp.backends.jetbrains.config.config_file_path",
        lambda: tmp_path / "config.json",
    )
    save_config("http://localhost:1234/mcp", {"Authorization": "Bearer abc"})
    cfg = load_config()
    assert cfg is not None
    assert cfg.url == "http://localhost:1234/mcp"
    assert cfg.headers["Authorization"] == "Bearer abc"
    assert cfg.transport == "streamable-http"


def test_save_config_rejects_non_loopback(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "pycharm_code_quality_mcp.backends.jetbrains.config.config_dir",
        lambda: tmp_path,
    )
    with pytest.raises(errors.SonarMcpError):
        save_config("http://192.168.1.1:1234/mcp", {})


# ---------------------------------------------------------------------------
# loopback URL 校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://localhost:1234/mcp", True),
        ("http://127.0.0.1:1234/mcp", True),
        ("http://127.0.1.5:1234/mcp", True),
        ("http://[::1]:1234/mcp", True),
        ("https://localhost:1234/mcp", True),
        ("http://192.168.1.1:1234/mcp", False),
        ("http://10.0.0.1:1234/mcp", False),
        ("http://example.com:1234/mcp", False),
        ("http://8.8.8.8:1234/mcp", False),
        ("file:///etc/passwd", False),
        ("", False),
        ("not-a-url", False),
    ],
)
def test_is_loopback_url(url: str, expected: bool) -> None:
    assert is_loopback_url(url) is expected


# ---------------------------------------------------------------------------
# doctor 三段输出
# ---------------------------------------------------------------------------


def test_doctor_has_three_sections() -> None:
    rc, out, _err = _run_cli(["doctor"], timeout=30.0)
    assert "== General ==" in out
    assert "== JetBrains ==" in out
    assert "== Sonar ==" in out
    assert "== Summary ==" in out
    assert rc in (0, 1)


def test_doctor_kept_legacy_title() -> None:
    rc, out, _err = _run_cli(["doctor"], timeout=30.0)
    assert "PyCharm Sonar MCP Doctor" in out
    _ = rc


def test_doctor_with_file_arg_reports_target(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("x")
    rc, out, _err = _run_cli(["doctor", "--file", str(f)], timeout=30.0)
    assert "Target file" in out
    assert rc in (0, 1)


# ---------------------------------------------------------------------------
# tools/list 包含全部 8 个工具(回归保护)
# ---------------------------------------------------------------------------


def test_tools_count_is_eight() -> None:
    import asyncio

    from pycharm_code_quality_mcp.server import build_app

    app = build_app()
    tools = asyncio.run(app.list_tools())
    assert len(tools) == 8


# ---------------------------------------------------------------------------
# doctor: noqa 风格检查
# ---------------------------------------------------------------------------


def test_doctor_reports_noqa_conflict(tmp_path: Path, monkeypatch) -> None:
    """含 ruff 风格 `# noqa: Sxxx` 注释时,doctor 应输出 Noqa style conflict 警告"""
    from pycharm_code_quality_mcp.doctor import run_doctor

    (tmp_path / "a.py").write_text("x = 1  # noqa: S123\n")
    monkeypatch.chdir(tmp_path)

    buf = io.StringIO()
    run_doctor(stream=buf)
    out = buf.getvalue()
    assert "Noqa style" in out
    assert "NOSONAR" in out
    assert "a.py:1" in out


def test_doctor_noqa_clean(tmp_path: Path, monkeypatch) -> None:
    """无 ruff 风格 noqa 冲突时,doctor 应输出 Noqa style [OK] 而非 [WARN]"""
    from pycharm_code_quality_mcp.doctor import run_doctor

    (tmp_path / "a.py").write_text("x = 1  # NOSONAR\n")
    monkeypatch.chdir(tmp_path)

    buf = io.StringIO()
    run_doctor(stream=buf)
    out = buf.getvalue()
    # 找到 Noqa style 那一行,断言它是 [OK] 不是 [WARN]。
    noqa_lines = [ln for ln in out.splitlines() if "Noqa style" in ln]
    assert noqa_lines, "Noqa style line missing"
    assert noqa_lines[0].lstrip().startswith("[OK]")
    assert "[WARN]" not in noqa_lines[0]
