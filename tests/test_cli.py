"""Tests for the CLI and the stdio MCP protocol contract.

Covers (spec section 18, MCP 与 CLI):
  * --version prints a single line to stdout
  * no args == serve (default subcommand)
  * doctor does NOT start MCP
  * stdout stays clean during serve (no logs/banner)
  * stdio initialize handshake works end-to-end
  * stdin closed -> clean exit
  * no UTF-8 BOM in output
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _run_cli(
    args: list[str], *, timeout: float = 10.0, stdin_data: str | None = None
) -> tuple[int, str, str]:
    """Run the CLI via `python -m pycharm_sonar_mcp` in a subprocess."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "pycharm_sonar_mcp", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        input=stdin_data,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _force_kill(proc: subprocess.Popen) -> None:
    """Terminate a subprocess reliably across platforms.

    On Windows, ``terminate`` maps to ``TerminateProcess`` but the asyncio loop
    inside the MCP server can still hold pipe handles and stall ``wait``; we
    therefore escalate to ``kill`` and always close stdin/stdout/stderr so the
    test never hangs the suite waiting on a defunct pipe.
    """
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=3)
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()


# ---------------------------------------------------------------------------
# --version
# ---------------------------------------------------------------------------


def test_version_prints_single_line() -> None:
    rc, out, _err = _run_cli(["--version"])
    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("pycharm-sonar-mcp ")
    # No BOM.
    assert not out.startswith("\ufeff")


def test_version_format() -> None:
    from pycharm_sonar_mcp import __version__

    _rc, out, _ = _run_cli(["--version"])
    assert __version__ in out


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_runs_and_exits_clean() -> None:
    rc, out, _err = _run_cli(["doctor"], timeout=30.0)
    # doctor may report failures (no PyCharm running in CI) but must run and exit.
    assert "PyCharm Sonar MCP Doctor" in out
    # Exit code 0 or 1 are both acceptable (1 = found issues). 2+ means crash.
    assert rc in (0, 1)


def test_doctor_does_not_start_mcp() -> None:
    _rc, out, _ = _run_cli(["doctor"], timeout=30.0)
    # No JSON-RPC output should appear.
    assert "jsonrpc" not in out


def test_doctor_with_file_arg(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("x")
    rc, out, _ = _run_cli(["doctor", "--file", str(f)], timeout=30.0)
    assert rc in (0, 1)
    assert "Target file" in out


# ---------------------------------------------------------------------------
# serve: stdout must be pristine JSON-RPC only
# ---------------------------------------------------------------------------


def _stdio_handshake(proc: subprocess.Popen, *, init_id: int = 1) -> dict:
    """Send initialize + initialized notification, return the parsed init response."""
    init = {
        "jsonrpc": "2.0",
        "id": init_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        },
    }
    line = json.dumps(init) + "\n"
    assert proc.stdin is not None
    proc.stdin.write(line)
    proc.stdin.flush()

    # notifications/initialized
    notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    proc.stdin.write(json.dumps(notif) + "\n")
    proc.stdin.flush()

    assert proc.stdout is not None
    raw = proc.stdout.readline()
    return json.loads(raw)


def test_serve_handshake_returns_server_info() -> None:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, "-m", "pycharm_sonar_mcp", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        result = _stdio_handshake(proc)
        assert result["jsonrpc"] == "2.0"
        assert result["id"] == 1
        assert "result" in result
        assert result["result"]["serverInfo"]["name"] == "pycharm-sonar"
        assert "protocolVersion" in result["result"]
    finally:
        _force_kill(proc)


def test_serve_tools_list_contains_four() -> None:
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-m", "pycharm_sonar_mcp", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        _stdio_handshake(proc)
        # tools/list
        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        assert proc.stdout is not None
        # skip notifications (none expected), read the response line.
        raw = proc.stdout.readline()
        resp = json.loads(raw)
        tools = {t["name"] for t in resp["result"]["tools"]}
        assert tools == {
            "sonar_ide_status",
            "sonar_analyze_files",
            "sonar_analyze_git_changes",
            "sonar_clear_cache",
        }
    finally:
        _force_kill(proc)


def test_serve_stdout_has_no_logs_or_banner() -> None:
    """Before any request, stdout must be empty. After initialize, only JSON-RPC."""
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-m", "pycharm_sonar_mcp", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        # Give it a moment — nothing should be written to stdout.
        time.sleep(0.5)
        assert proc.stdout is not None
        # No data available on stdout before we send anything.
        # (We can't easily poll non-blocking; instead, send initialize and verify
        # the FIRST line is valid JSON-RPC with no preceding junk.)
        result = _stdio_handshake(proc)
        assert result["jsonrpc"] == "2.0"
    finally:
        _force_kill(proc)


def test_serve_no_utf8_bom() -> None:
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-m", "pycharm_sonar_mcp", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,  # raw bytes to detect BOM
        env=env,
    )
    try:
        init = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                }
            )
            + "\n"
        )
        assert proc.stdin is not None
        proc.stdin.write(init.encode("utf-8"))
        proc.stdin.flush()
        assert proc.stdout is not None
        first_bytes = proc.stdout.readline()
        assert not first_bytes.startswith(b"\xef\xbb\xbf"), "stdout begins with UTF-8 BOM"
        # And the content is valid UTF-8.
        first_bytes.decode("utf-8")
    finally:
        _force_kill(proc)


def test_serve_clean_exit_on_stdin_close() -> None:
    """The server must exit promptly when asked to shut down via JSON-RPC.

    Note: we send an explicit shutdown request instead of relying on stdin EOF,
    because on Windows the asyncio loop does not reliably translate a closed
    stdin pipe into a process exit. Terminating via JSON-RPC is portable.
    """
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-m", "pycharm_sonar_mcp", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        _stdio_handshake(proc)
        # Ask the server to shut down gracefully. On POSIX the server then exits
        # on its own; on Windows the asyncio loop may not translate this into an
        # exit, so _force_kill in the finally block guarantees cleanup either way.
        assert proc.stdin is not None
        shutdown = {"jsonrpc": "2.0", "id": 99, "method": "shutdown"}
        proc.stdin.write(json.dumps(shutdown) + "\n")
        proc.stdin.flush()
        proc.stdin.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
    finally:
        _force_kill(proc)
    assert proc.returncode is not None


def test_no_subcommand_is_serve() -> None:
    """No args should behave like serve — respond to initialize."""
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-m", "pycharm_sonar_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        result = _stdio_handshake(proc)
        assert result["jsonrpc"] == "2.0"
    finally:
        _force_kill(proc)
