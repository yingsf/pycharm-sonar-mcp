"""Tests for IDE discovery: scanning, status validation, cache, multi-instance matching."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from pycharm_sonar_mcp import errors
from pycharm_sonar_mcp.ide_discovery import (
    PORT_MAX,
    PORT_MIN,
    IdeDiscovery,
    PortCache,
    looks_like_sonar_status,
    parse_explicit_port,
)
from pycharm_sonar_mcp.sonar_client import SonarClient


def _build_discovery(client: SonarClient, **kwargs: Any) -> IdeDiscovery:
    kwargs.setdefault("cache", PortCache())
    return IdeDiscovery(client, **kwargs)


# ---------------------------------------------------------------------------
# looks_like_sonar_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "obj,expected",
    [
        ({"ideName": "PyCharm"}, True),
        ({"version": "1.0"}, True),
        ({"connectedMode": False}, True),
        ({}, False),
        ([1, 2, 3], False),
        ("not a dict", False),
        ({"foo": "bar"}, False),
        ({"sonarlint": {}}, True),
        ({"mySonarX": 1}, True),  # contains "sonar"
    ],
)
def test_looks_like_sonar_status(obj: Any, expected: bool) -> None:
    assert looks_like_sonar_status(obj) is expected


# ---------------------------------------------------------------------------
# Explicit port
# ---------------------------------------------------------------------------


def test_explicit_port_valid() -> None:
    assert parse_explicit_port({"SONAR_IDE_PORT": "64124"}) == 64124


def test_explicit_port_unset() -> None:
    assert parse_explicit_port({}) is None


def test_explicit_port_not_integer() -> None:
    with pytest.raises(errors.SonarMcpError) as ei:
        parse_explicit_port({"SONAR_IDE_PORT": "abc"})
    assert ei.value.code == errors.IDE_PORT_INVALID


@pytest.mark.parametrize("port", ["64119", "64131", "0", "99999"])
def test_explicit_port_out_of_range(port: str) -> None:
    with pytest.raises(errors.SonarMcpError) as ei:
        parse_explicit_port({"SONAR_IDE_PORT": port})
    assert ei.value.code == errors.IDE_PORT_OUT_OF_RANGE


# ---------------------------------------------------------------------------
# Scan / discovery
# ---------------------------------------------------------------------------


def test_scan_finds_single_instance(mock_sonar_factory, make_response) -> None:
    client, _routes = mock_sonar_factory(ports=[64120])
    discovery = _build_discovery(client)
    instances = discovery.discover_all_instances()
    assert len(instances) == 1
    assert instances[0].port == 64120
    assert instances[0].status["ideName"] == "PyCharm"


def test_scan_finds_multiple_instances(mock_sonar_factory) -> None:
    client, _routes = mock_sonar_factory(ports=[64120, 64122])
    discovery = _build_discovery(client)
    instances = discovery.discover_all_instances()
    ports = [i.port for i in instances]
    assert ports == [64120, 64122]


def test_scan_no_instances(mock_sonar_factory) -> None:
    # Default routes exist for 64120; configure discovery to scan an empty port list.
    client, _routes = mock_sonar_factory(ports=[64120])
    discovery = _build_discovery(client, ports=[])
    assert discovery.discover_all_instances() == []


def test_discover_for_project_single_instance_cached(mock_sonar_factory, fresh_cache) -> None:
    client, _routes = mock_sonar_factory(ports=[64120])
    discovery = _build_discovery(client, cache=fresh_cache)
    port = discovery.discover_for_project("/tmp/proj")
    assert port == 64120
    # Now cached.
    assert fresh_cache.get("/tmp/proj") == 64120


def test_discover_uses_explicit_port(mock_sonar_factory, fresh_cache) -> None:
    client, _routes = mock_sonar_factory(ports=[64120, 64125])
    discovery = _build_discovery(client, cache=fresh_cache, env={"SONAR_IDE_PORT": "64125"})
    port = discovery.discover_for_project("/tmp/proj")
    assert port == 64125


def test_explicit_port_invalid_service_raises(
    mock_sonar_factory, fresh_cache, make_response
) -> None:
    """Explicit port that isn't a Sonar service must NOT silently fall back."""
    client, routes = mock_sonar_factory(ports=[])

    # Make the explicit port return 404.
    def _404(req: httpx.Request) -> httpx.Response:
        return make_response(404, {"error": "no"})

    routes[(64125, "/sonarlint/api/status")] = _404
    discovery = _build_discovery(client, cache=fresh_cache, env={"SONAR_IDE_PORT": "64125"})
    with pytest.raises(errors.SonarMcpError) as ei:
        discovery.discover_for_project("/tmp/proj")
    assert ei.value.code in {errors.IDE_NOT_FOUND, errors.SONAR_BAD_RESPONSE}


def test_multi_instance_match_by_file(mock_sonar_factory, fresh_cache, make_response) -> None:
    """Two instances, only one indexes the target file → match it."""
    client, routes = mock_sonar_factory(ports=[64120, 64121])
    # Port 64120 indexes; port 64121 reports not-indexed.
    # analyze_files on 64121 raises a FILE_NOT_INDEXED-coded error.

    def not_indexed(req: httpx.Request) -> httpx.Response:
        # Return a 200 with a notIndexedFiles marker (forward-compat shape).
        return make_response(200, {"findings": [], "notIndexedFiles": ["/proj/a.py"]})

    routes[(64121, "/sonarlint/api/analysis/files")] = not_indexed

    discovery = _build_discovery(client, cache=fresh_cache)
    port = discovery.match_instance_for_file("/proj/a.py")
    assert port == 64120


def test_multi_instance_no_match_raises(mock_sonar_factory, fresh_cache, make_response) -> None:
    client, routes = mock_sonar_factory(ports=[64120, 64121])

    def not_indexed(req: httpx.Request) -> httpx.Response:
        return make_response(200, {"findings": [], "notIndexedFiles": ["/proj/a.py"]})

    routes[(64120, "/sonarlint/api/analysis/files")] = not_indexed
    routes[(64121, "/sonarlint/api/analysis/files")] = not_indexed

    discovery = _build_discovery(client, cache=fresh_cache)
    with pytest.raises(errors.SonarMcpError) as ei:
        discovery.match_instance_for_file("/proj/a.py")
    assert ei.value.code == errors.IDE_NO_INSTANCE_INDEXES_FILE


def test_multi_instance_multiple_match_raises(mock_sonar_factory, fresh_cache) -> None:
    client, _routes = mock_sonar_factory(ports=[64120, 64121])
    discovery = _build_discovery(client, cache=fresh_cache)
    with pytest.raises(errors.SonarMcpError) as ei:
        discovery.match_instance_for_file("/proj/a.py")
    assert ei.value.code == errors.IDE_MULTIPLE_MATCHES


def test_no_instances_raises_not_found(mock_sonar_factory, fresh_cache) -> None:
    client, _routes = mock_sonar_factory(ports=[])
    discovery = _build_discovery(client, ports=[])
    with pytest.raises(errors.SonarMcpError) as ei:
        discovery.discover_for_project("/proj")
    assert ei.value.code == errors.IDE_NOT_FOUND


# ---------------------------------------------------------------------------
# Cache invalidation + rediscovery
# ---------------------------------------------------------------------------


def test_cache_invalidation_on_connection_failure(
    mock_sonar_factory, fresh_cache, make_response
) -> None:
    """Cached port becomes unreachable → clear and rediscover once."""
    client, routes = mock_sonar_factory(ports=[64120])
    discovery = _build_discovery(client, cache=fresh_cache)
    # First call populates cache.
    assert discovery.discover_for_project("/proj") == 64120

    # Break the cached port.
    def _404(req: httpx.Request) -> httpx.Response:
        return make_response(404, {})

    routes[(64120, "/sonarlint/api/status")] = _404
    # No alternative port → rediscovery should raise IDE_NOT_FOUND (not infinite retry).
    with pytest.raises(errors.SonarMcpError):
        discovery.discover_for_project("/proj")
    # Cache must have been cleared.
    assert fresh_cache.get("/proj") is None


def test_cache_hit_skips_scan(mock_sonar_factory, fresh_cache) -> None:
    client, _routes = mock_sonar_factory(ports=[64120])
    discovery = _build_discovery(client, cache=fresh_cache)
    discovery.discover_for_project("/proj")
    # Remove the only port; cached call should still return 64120 without scanning.
    discovery2 = _build_discovery(client, cache=fresh_cache, ports=[])
    assert discovery2.discover_for_project("/proj") == 64120


def test_clear_cache(fresh_cache: PortCache) -> None:
    fresh_cache.set("/a", 64120)
    fresh_cache.set("/b", 64121)
    ports = fresh_cache.clear()
    assert sorted(ports) == [64120, 64121]
    assert len(fresh_cache) == 0


def test_clear_cache_single(fresh_cache: PortCache) -> None:
    fresh_cache.set("/a", 64120)
    fresh_cache.set("/b", 64121)
    assert fresh_cache.invalidate("/a") is True
    assert fresh_cache.get("/a") is None
    assert fresh_cache.get("/b") == 64121
    assert fresh_cache.invalidate("/nope") is False


def test_port_range_constants() -> None:
    assert PORT_MIN == 64120
    assert PORT_MAX == 64130
