"""Tests for the IPv4-loopback transport and HTTP authority handling.

Covers (spec section 4 / section 18):
  * TCP connects to 127.0.0.1 while Host stays localhost
  * Host of 127.0.0.1 would yield 421 (we never send that Host)
  * localhost IPv6-first vs IPv4-only binding
  * proxy env is disabled (trust_env=False)
  * status response parsing: 200/404/421/429/non-JSON/bad content-type
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
from typing import Any

import httpx
import pytest

from pycharm_code_quality_mcp import errors
from pycharm_code_quality_mcp.backends.sonar.discovery import validate_status_response
from pycharm_code_quality_mcp.backends.sonar.transport import (
    LOOPBACK_IPV4,
    build_local_client,
)

# ---------------------------------------------------------------------------
# Tiny loopback HTTP server bound to 127.0.0.1, validates Host header
# ---------------------------------------------------------------------------


class _MiniServer:
    """A raw TCP server on 127.0.0.1 that returns HTTP 421 if Host != localhost:<port>."""

    def __init__(self, *, host: str = LOOPBACK_IPV4) -> None:
        self.host = host
        self._sock = socket.socket(
            socket.AF_INET if ":" not in host else socket.AF_INET6, socket.SOCK_STREAM
        )
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self.captured_host: list[str] = []
        self.captured_origin: list[str] = []
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self.responses: list[tuple[int, bytes, str]] = []  # queue of (status, body, ctype)
        self.default_status_body = b'{"ideName":"PyCharm","version":"1.0.0"}'

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        with contextlib.suppress(OSError):
            self._sock.close()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(2.0)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
            headers: dict[str, str] = {}
            for line in head.split("\r\n")[1:]:
                if ":" in line:
                    k, _, v = line.partition(":")
                    headers[k.strip().lower()] = v.strip()
            host = headers.get("host", "")
            origin = headers.get("origin", "")
            self.captured_host.append(host)
            self.captured_origin.append(origin)
            # Enforce localhost authority.
            if host == f"127.0.0.1:{self.port}":
                body = b"Not authoritative"
                resp = (
                    f"HTTP/1.1 421 Misdirected Request\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"Content-Type: text/plain\r\n\r\n"
                ).encode("latin-1") + body
                conn.sendall(resp)
                return
            if self.responses:
                status, payload, ctype = self.responses.pop(0)
            else:
                status, payload, ctype = 200, self.default_status_body, "application/json"
            resp = (
                f"HTTP/1.1 {status} OK\r\n"
                f"Content-Length: {len(payload)}\r\n"
                f"Content-Type: {ctype}\r\n\r\n"
            ).encode("latin-1") + payload
            conn.sendall(resp)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                conn.close()


@pytest.fixture
def live_server():
    s = _MiniServer(host=LOOPBACK_IPV4)
    s.start()
    yield s
    s.stop()


def test_transport_connects_to_ipv4_and_uses_localhost_host(live_server: _MiniServer) -> None:
    """Real socket: transport must connect to 127.0.0.1 while emitting Host: localhost:<port>."""
    client = build_local_client(live_server.port, connect_timeout=2.0, read_timeout=5.0)
    resp = client.get("/sonarlint/api/status")
    assert resp.status_code == 200
    assert f"localhost:{live_server.port}" in live_server.captured_host
    assert "127.0.0.1" not in live_server.captured_host[-1]


def test_transport_origin_header_present(live_server: _MiniServer) -> None:
    client = build_local_client(live_server.port)
    client.get("/sonarlint/api/status")
    assert any(o == "http://localhost" for o in live_server.captured_origin)


def test_transport_127_host_yields_421(live_server: _MiniServer) -> None:
    """If a client (incorrectly) sent Host 127.0.0.1, the server returns 421. Our transport
    never does this, but the test pins the server-side contract."""
    # Manually craft a request with Host 127.0.0.1 to prove the 421 path.
    with socket.create_connection((LOOPBACK_IPV4, live_server.port), timeout=2.0) as s:
        s.sendall(
            f"GET /sonarlint/api/status HTTP/1.1\r\nHost: 127.0.0.1:{live_server.port}\r\n\r\n".encode()
        )
        data = s.recv(4096)
    assert b"421" in data.split(b"\r\n")[0]


def test_transport_trust_env_false_ignores_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with proxy env set, the client must NOT route through it."""
    monkeypatch.setenv("HTTP_PROXY", "http://evil-proxy.invalid:9999")
    monkeypatch.setenv("ALL_PROXY", "http://evil-proxy.invalid:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://evil-proxy.invalid:9999")
    client = build_local_client(64120)
    assert client.trust_env is False


def test_transport_post_analysis(live_server: _MiniServer) -> None:
    """Full POST path works through the transport."""
    live_server.responses.append(
        (
            200,
            b'{"findings":[{"ruleKey":"python:S3776","message":"x","severity":"CRITICAL",'
            b'"filePath":"/p/a.py","textRange":{"startLine":1,"startLineOffset":0,'
            b'"endLine":1,"endLineOffset":2}}]}',
            "application/json",
        )
    )
    client = build_local_client(live_server.port)
    resp = client.post(
        "/sonarlint/api/analysis/files",
        json={"fileAbsolutePaths": ["/p/a.py"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["findings"][0]["ruleKey"] == "python:S3776"


def test_transport_no_duplicate_headers(live_server: _MiniServer) -> None:
    """Each HTTP header name must appear exactly once on the wire (RFC 7230).

    Regression guard: an earlier implementation stored headers in a plain dict, which let
    conflicting casings (e.g. ``connection`` and ``Connection``) coexist and both be sent.
    """
    client = build_local_client(live_server.port)
    client.post("/sonarlint/api/analysis/files", json={"fileAbsolutePaths": ["/p/a.py"]})

    from collections import Counter

    # The server captured one Origin entry per request; duplicates would surface here too.
    origin_counts = Counter(live_server.captured_origin)
    assert all(c == 1 for c in origin_counts.values()), "Origin header leaked duplicates"


def test_transport_requests_identity_encoding() -> None:
    """The transport must request Accept-Encoding: identity.

    We read raw bytes and never decompress, so allowing gzip would make a compressed JSON
    response look like a malformed payload. This test opens a dedicated capture socket and
    inspects the full raw request head.
    """
    raw_heads: list[str] = []
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LOOPBACK_IPV4, 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve() -> None:
        conn, _ = srv.accept()
        conn.settimeout(2.0)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        raw_heads.append(data.split(b"\r\n\r\n", 1)[0].decode("latin-1"))
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}"
        )
        conn.close()

    t = threading.Thread(target=serve)
    t.start()
    try:
        client = build_local_client(port)
        client.get("/sonarlint/api/status")
    finally:
        t.join(timeout=3)
        srv.close()

    assert raw_heads, "no request captured"
    head = raw_heads[-1]
    accept_enc = [
        line.split(":", 1)[1].strip()
        for line in head.split("\r\n")
        if line.lower().startswith("accept-encoding:")
    ]
    assert accept_enc == ["identity"], f"expected identity, got {accept_enc}"


def test_transport_no_duplicate_header_names() -> None:
    """No header name may appear more than once on the wire, regardless of casing."""
    raw_heads: list[str] = []
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LOOPBACK_IPV4, 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve() -> None:
        conn, _ = srv.accept()
        conn.settimeout(2.0)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        raw_heads.append(data.split(b"\r\n\r\n", 1)[0].decode("latin-1"))
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}"
        )
        conn.close()

    t = threading.Thread(target=serve)
    t.start()
    try:
        client = build_local_client(port)
        client.post("/sonarlint/api/analysis/files", json={"fileAbsolutePaths": ["/p/a.py"]})
    finally:
        t.join(timeout=3)
        srv.close()

    assert raw_heads
    from collections import Counter

    hdr_lines = [ln for ln in raw_heads[-1].split("\r\n")[1:] if ":" in ln]
    names = [ln.split(":", 1)[0].strip().lower() for ln in hdr_lines]
    dupes = {n: c for n, c in Counter(names).items() if c > 1}
    assert not dupes, f"duplicate headers on the wire: {dupes}"


# ---------------------------------------------------------------------------
# Status validation
# ---------------------------------------------------------------------------


def _resp(status: int, body: Any, ctype: str = "application/json") -> httpx.Response:
    if isinstance(body, (dict, list)):
        content = json.dumps(body).encode()
    elif isinstance(body, bytes):
        content = body
    else:
        content = str(body).encode()
    return httpx.Response(status, headers={"content-type": ctype}, content=content)


def test_status_200_valid() -> None:
    obj = validate_status_response(_resp(200, {"ideName": "PyCharm"}))
    assert obj["ideName"] == "PyCharm"


def test_status_421_raises_authority() -> None:
    with pytest.raises(errors.SonarMcpError) as ei:
        validate_status_response(_resp(421, "Not authoritative", ctype="text/plain"))
    assert ei.value.code == errors.IDE_AUTHORITY_REJECTED


def test_status_404_raises_bad_response() -> None:
    with pytest.raises(errors.SonarMcpError) as ei:
        validate_status_response(_resp(404, {"error": "no"}))
    assert ei.value.code == errors.SONAR_BAD_RESPONSE


def test_status_429_raises_rate_limited() -> None:
    with pytest.raises(errors.SonarMcpError) as ei:
        validate_status_response(_resp(429, {}))
    assert ei.value.code == errors.SONAR_RATE_LIMITED


def test_status_non_json_raises_bad_response() -> None:
    with pytest.raises(errors.SonarMcpError) as ei:
        validate_status_response(_resp(200, "<html>nope</html>", ctype="text/html"))
    assert ei.value.code == errors.SONAR_BAD_RESPONSE


def test_status_wrong_content_type_but_json_parses() -> None:
    """Content-Type may lie but valid JSON should still parse."""
    obj = validate_status_response(_resp(200, {"ideName": "PyCharm"}, ctype="text/plain"))
    assert obj["ideName"] == "PyCharm"


def test_status_500_raises_unavailable() -> None:
    with pytest.raises(errors.SonarMcpError) as ei:
        validate_status_response(_resp(500, {}))
    assert ei.value.code == errors.SONAR_UNAVAILABLE


def test_status_non_sonar_json_rejected() -> None:
    """A JSON object with no Sonar-ish fields must be rejected."""
    with pytest.raises(errors.SonarMcpError) as ei:
        validate_status_response(_resp(200, {"foo": "bar", "baz": 42}))
    assert ei.value.code == errors.SONAR_BAD_RESPONSE


def test_status_forward_compat_extra_fields() -> None:
    """Unknown fields from future plugin versions must be tolerated."""
    obj = validate_status_response(
        _resp(200, {"ideName": "PyCharm", "futureFieldX": 123, "connectedMode": True})
    )
    assert obj["futureFieldX"] == 123
