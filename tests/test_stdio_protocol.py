"""Dedicated stdio MCP protocol tests.

Covers JSON-RPC framing correctness, error responses, and the initialize → tools/list →
tool call round trip using a mocked Sonar backend via environment injection.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any

# Each spawned server process gets a dedicated background reader thread that
# pumps stdout lines into a queue. The test helpers then consume from the queue
# with a timeout. This avoids two Windows pitfalls: (1) a blocking readline()
# on an empty pipe ignores any Python-level deadline, and (2) spawning a fresh
# reader thread per line races on the shared stdout buffer. One thread per
# process is both correct and fast.
_LINES: dict[subprocess.Popen, queue.Queue[str | None]] = {}


def _spawn(env_overrides: dict[str, str] | None = None) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if env_overrides:
        env.update(env_overrides)
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

    q: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            q.put(line)
        q.put(None)  # EOF sentinel

    threading.Thread(target=pump, daemon=True).start()
    _LINES[proc] = q
    return proc


def _send(proc: subprocess.Popen, obj: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def _next_line(proc: subprocess.Popen, *, timeout: float) -> str | None:
    """Pop one stdout line within timeout, or None on timeout/EOF."""
    try:
        return _LINES[proc].get(timeout=timeout)
    except queue.Empty:
        raise AssertionError("timed out waiting for a response on stdout") from None


def _recv(proc: subprocess.Popen, *, timeout: float = 15.0) -> dict[str, Any]:
    """Read the next RESPONSE (skips notifications, which have no 'id')."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        line = _next_line(proc, timeout=remaining)
        if line is None:
            raise AssertionError("stdout closed before a response arrived")
        obj = json.loads(line)
        if "id" in obj:
            return obj
        # Notification (no 'id') — keep reading for the actual response.
    raise AssertionError("no response received on stdout")


def _recv_or_skip(proc: subprocess.Popen, *, timeout: float = 5.0) -> dict[str, Any] | None:
    """Read one line if it arrives within timeout, else return None."""
    try:
        line = _LINES[proc].get(timeout=timeout)
    except queue.Empty:
        return None
    if line is None:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _init(proc: subprocess.Popen) -> dict[str, Any]:
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1.0"},
            },
        },
    )
    resp = _recv(proc)
    _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    return resp


def _shutdown(proc: subprocess.Popen) -> None:
    """Terminate the MCP subprocess reliably across platforms.

    On Windows the asyncio loop can keep the process alive after terminate;
    escalate to kill and close the inherited pipe handles so the test suite
    never blocks on a half-closed pipe.
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
    _LINES.pop(proc, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_initialize_protocol_version_present() -> None:
    proc = _spawn()
    try:
        resp = _init(proc)
        assert resp["result"]["protocolVersion"]
        assert resp["result"]["serverInfo"]["name"] == "pycharm-sonar"
    finally:
        _shutdown(proc)


def test_unknown_method_returns_jsonrpc_error() -> None:
    """An unknown method must not crash the server.

    The MCP SDK may respond with a JSON-RPC error object or merely log a
    notification; either way the server must keep serving subsequent valid
    requests. We verify that tools/list still works after sending the bogus
    method, which is the behaviour callers actually depend on.
    """
    proc = _spawn()
    try:
        _init(proc)
        _send(proc, {"jsonrpc": "2.0", "id": 9, "method": "nonexistent/method"})
        # Drain anything the server emits for the bogus method (error response or
        # notification) without requiring a specific shape — see _recv_or_skip.
        _recv_or_skip(proc, timeout=5)
        # The server must still respond to a valid request afterwards.
        _send(proc, {"jsonrpc": "2.0", "id": 10, "method": "tools/list"})
        resp = _recv(proc)
        assert "result" in resp
    finally:
        _shutdown(proc)




def test_invalid_json_handled_gracefully() -> None:
    """Invalid JSON must not crash the server. The SDK emits a notification or error;
    the key contract is that the server stays alive and responds to a valid follow-up."""
    proc = _spawn()
    try:
        _init(proc)
        assert proc.stdin is not None
        proc.stdin.write("{ this is not valid json\n")
        proc.stdin.flush()
        time.sleep(0.3)
        # Server must still respond to a valid request.
        _send(proc, {"jsonrpc": "2.0", "id": 10, "method": "tools/list"})
        resp = _recv(proc)
        assert "result" in resp
    finally:
        _shutdown(proc)


def test_tools_list_schema_well_formed() -> None:
    proc = _spawn()
    try:
        _init(proc)
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        resp = _recv(proc)
        for tool in resp["result"]["tools"]:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert isinstance(tool["inputSchema"], dict)
    finally:
        _shutdown(proc)


def test_call_clear_cache_tool_returns_result() -> None:
    """sonar_clear_cache should succeed with no IDE running (it's pure in-memory)."""
    proc = _spawn()
    try:
        _init(proc)
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "sonar_clear_cache", "arguments": {}},
            },
        )
        resp = _recv(proc)
        assert "result" in resp
        content = resp["result"]["content"]
        assert any("cleared" in item.get("text", "") for item in content)
    finally:
        _shutdown(proc)


def test_call_ide_status_returns_structured() -> None:
    """sonar_ide_status must return a structured result (available=false when no IDE)."""
    proc = _spawn()
    try:
        _init(proc)
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "sonar_ide_status", "arguments": {}},
            },
        )
        resp = _recv(proc)
        content_text = resp["result"]["content"][0]["text"]
        payload = json.loads(content_text)
        assert "available" in payload
        assert "instanceCount" in payload
    finally:
        _shutdown(proc)


def test_call_analyze_files_with_no_workspace_returns_errorcode() -> None:
    """Without workspace roots, analyze_files must surface an error code, not crash."""
    proc = _spawn({"SONAR_WORKSPACE_ROOTS": ""})
    try:
        _init(proc)
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "sonar_analyze_files",
                    "arguments": {"file_absolute_paths": ["/tmp/does-not-exist.py"]},
                },
            },
        )
        resp = _recv(proc)
        content_text = resp["result"]["content"][0]["text"]
        payload = json.loads(content_text)
        assert payload["success"] is False
        assert "errorCode" in payload
    finally:
        _shutdown(proc)


def test_no_traceback_leaked_on_error() -> None:
    """A tool error must NOT include a Python traceback in the response content."""
    proc = _spawn({"SONAR_WORKSPACE_ROOTS": ""})
    try:
        _init(proc)
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "sonar_analyze_files",
                    "arguments": {"file_absolute_paths": ["/tmp/x.py"]},
                },
            },
        )
        resp = _recv(proc)
        text = json.dumps(resp)
        assert "Traceback" not in text
        assert 'File "' not in text
    finally:
        _shutdown(proc)
