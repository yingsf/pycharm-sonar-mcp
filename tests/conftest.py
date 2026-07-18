"""Shared pytest fixtures.

The key fixture is `mock_transport_factory`, which returns a callable mapping a port
to an httpx transport that serves canned responses. This lets every HTTP-touching test
run without a real PyCharm or socket.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from pycharm_sonar_mcp.ide_discovery import PortCache, set_global_cache
from pycharm_sonar_mcp.server import reset_singletons
from pycharm_sonar_mcp.sonar_client import SonarClient


class MockTransport(httpx.BaseTransport):
    """httpx transport that routes requests to a per-port handler callable."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._handler(request)

    def close(self) -> None:
        pass


def _make_status_response(
    status_code: int = 200,
    body: Any = None,
    content_type: str = "application/json",
) -> httpx.Response:
    if body is None:
        body = {"ideName": "PyCharm", "version": "1.0.0", "connectedMode": False}
    if isinstance(body, (dict, list)):
        content = json.dumps(body).encode("utf-8")
    elif isinstance(body, bytes):
        content = body
    else:
        content = str(body).encode("utf-8")
    headers = {"content-type": content_type}
    return httpx.Response(status_code, headers=headers, content=content)


@pytest.fixture
def fresh_cache() -> PortCache:
    """Replace the global port cache with a fresh one for each test."""
    cache = PortCache()
    set_global_cache(cache)
    return cache


@pytest.fixture
def make_response() -> Callable[..., httpx.Response]:
    return _make_status_response


@pytest.fixture
def mock_sonar_factory(
    make_response: Callable[..., httpx.Response],
) -> Callable[..., tuple[SonarClient, dict[str, Any]]]:
    """Factory: build a SonarClient whose HTTP is fully mocked.

    Returns (client, routes) where routes is the live dict the test can mutate to
    change behavior per-port per-path. Default route serves a valid PyCharm status
    on port 64120 and empty findings.
    """

    def _factory(
        status_body: Any = None,
        analysis_body: Any = None,
        status_code: int = 200,
        ports: list[int] | None = None,
    ) -> tuple[SonarClient, dict[str, Any]]:
        active_ports = ports or [64120]

        def default_status(port: int) -> httpx.Response:
            return make_response(
                200,
                status_body
                if status_body is not None
                else {
                    "ideName": "PyCharm",
                    "version": "1.0.0",
                    "connectedMode": False,
                },
            )

        def default_analysis(port: int) -> httpx.Response:
            return make_response(200, analysis_body or {"findings": []})

        # Routes: {(port, path_suffix): callable(request) -> Response}
        routes: dict[tuple[int, str], Callable[[httpx.Request], httpx.Response]] = {}
        for p in active_ports:
            routes[(p, "/sonarlint/api/status")] = lambda req, p=p: default_status(p)
            routes[(p, "/sonarlint/api/analysis/files")] = lambda req, p=p: default_analysis(p)

        def handler(request: httpx.Request) -> httpx.Response:
            # Determine port from base_url host (localhost:<port>).
            port = request.url.port or 64120
            path = request.url.path
            # Ports not in the active list behave like a closed/non-Sonar port.
            if port not in active_ports:
                return make_response(404, {"error": "no service"})
            key = (port, path)
            if key in routes:
                return routes[key](request)
            return make_response(404, {"error": "no route"})

        def transport_factory(port: int) -> httpx.BaseTransport:
            return MockTransport(handler)

        client = SonarClient(transport_factory=transport_factory)
        return client, routes

    return _factory


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Ensure each test starts with clean server singletons + cache."""
    reset_singletons()
    yield
    reset_singletons()
