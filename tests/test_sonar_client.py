"""Tests for SonarClient: status + analyze + batched analysis + error mapping."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pycharm_sonar_mcp import errors
from pycharm_sonar_mcp.sonar_client import SonarClient


def _ok_status() -> dict[str, Any]:
    return {"ideName": "PyCharm", "version": "1.0.0"}


def test_get_status_success(mock_sonar_factory) -> None:
    client, _r = mock_sonar_factory()
    obj = client.get_status(64120)
    assert obj["ideName"] == "PyCharm"


def test_get_status_connection_refused(make_response) -> None:
    """A 404 from a non-Sonar port surfaces as a bad-response error."""

    def handler(req: httpx.Request) -> httpx.Response:
        return make_response(404, {"no": "service"})

    from tests.conftest import MockTransport

    client = SonarClient(transport_factory=lambda p: MockTransport(handler))
    with pytest.raises(errors.SonarMcpError):
        client.get_status(64199)


def test_get_status_421(mock_sonar_factory, make_response) -> None:
    client, routes = mock_sonar_factory(ports=[64120])

    def _421(req: httpx.Request) -> httpx.Response:
        return make_response(421, "Not authoritative", content_type="text/plain")

    routes[(64120, "/sonarlint/api/status")] = _421
    with pytest.raises(errors.SonarMcpError) as ei:
        client.get_status(64120)
    assert ei.value.code == errors.IDE_AUTHORITY_REJECTED


def test_get_status_429(mock_sonar_factory, make_response) -> None:
    client, routes = mock_sonar_factory(ports=[64120])

    def _429(req: httpx.Request) -> httpx.Response:
        return make_response(429, {})

    routes[(64120, "/sonarlint/api/status")] = _429
    with pytest.raises(errors.SonarMcpError) as ei:
        client.get_status(64120)
    assert ei.value.code == errors.SONAR_RATE_LIMITED


def test_get_status_503_indexing(mock_sonar_factory, make_response) -> None:
    client, routes = mock_sonar_factory(ports=[64120])

    def _503(req: httpx.Request) -> httpx.Response:
        return make_response(503, {"busy": True})

    routes[(64120, "/sonarlint/api/status")] = _503
    with pytest.raises(errors.SonarMcpError) as ei:
        client.get_status(64120)
    assert ei.value.code == errors.SONAR_UNAVAILABLE  # status path uses UNAVAILABLE for 5xx


def test_analyze_empty_returns_empty(mock_sonar_factory) -> None:
    client, _r = mock_sonar_factory()
    res = client.analyze_files(64120, [])
    assert res["findings"] == []


def test_analyze_success(mock_sonar_factory, make_response) -> None:
    findings_payload = {
        "findings": [
            {
                "ruleKey": "python:S3776",
                "message": "Reduce cognitive complexity",
                "severity": "CRITICAL",
                "filePath": "/proj/a.py",
                "textRange": {
                    "startLine": 10,
                    "startLineOffset": 0,
                    "endLine": 10,
                    "endLineOffset": 20,
                },
            }
        ]
    }
    client, _routes = mock_sonar_factory(ports=[64120], analysis_body=findings_payload)
    res = client.analyze_files(64120, ["/proj/a.py"])
    assert len(res["findings"]) == 1
    assert res["findings"][0]["ruleKey"] == "python:S3776"
    assert res["findings"][0]["filePath"] == "/proj/a.py"
    # Original raw dict preserved.
    assert res["raw"]["findings"][0]["severity"] == "CRITICAL"


def test_analyze_preserves_unknown_fields(mock_sonar_factory, make_response) -> None:
    findings_payload = {
        "findings": [
            {
                "ruleKey": "python:S1192",
                "message": "x",
                "severity": "MINOR",
                "filePath": "/proj/a.py",
                "textRange": {
                    "startLine": 1,
                    "startLineOffset": 0,
                    "endLine": 1,
                    "endLineOffset": 1,
                },
                "futureField": "value",
            }
        ]
    }
    client, _r = mock_sonar_factory(ports=[64120], analysis_body=findings_payload)
    res = client.analyze_files(64120, ["/proj/a.py"])
    # The serialized finding retains the unknown field (forward-compat).
    assert res["findings"][0].get("futureField") == "value"


def test_analyze_421_raises_authority(mock_sonar_factory, make_response) -> None:
    client, routes = mock_sonar_factory(ports=[64120])

    def _421(req: httpx.Request) -> httpx.Response:
        return make_response(421, "nope", content_type="text/plain")

    routes[(64120, "/sonarlint/api/analysis/files")] = _421
    with pytest.raises(errors.SonarMcpError) as ei:
        client.analyze_files(64120, ["/proj/a.py"])
    assert ei.value.code == errors.IDE_AUTHORITY_REJECTED


def test_analyze_429_raises_rate_limited(mock_sonar_factory, make_response) -> None:
    client, routes = mock_sonar_factory(ports=[64120])

    def _429(req: httpx.Request) -> httpx.Response:
        return make_response(429, {})

    routes[(64120, "/sonarlint/api/analysis/files")] = _429
    with pytest.raises(errors.SonarMcpError) as ei:
        client.analyze_files(64120, ["/proj/a.py"])
    assert ei.value.code == errors.SONAR_RATE_LIMITED


def test_analyze_503_raises_indexing(mock_sonar_factory, make_response) -> None:
    client, routes = mock_sonar_factory(ports=[64120])

    def _503(req: httpx.Request) -> httpx.Response:
        return make_response(503, {})

    routes[(64120, "/sonarlint/api/analysis/files")] = _503
    with pytest.raises(errors.SonarMcpError) as ei:
        client.analyze_files(64120, ["/proj/a.py"])
    assert ei.value.code == errors.IDE_INDEXING


def test_analyze_non_json_raises_bad_response(mock_sonar_factory, make_response) -> None:
    client, routes = mock_sonar_factory(ports=[64120])

    def _bad(req: httpx.Request) -> httpx.Response:
        return make_response(200, "<html/>", content_type="text/html")

    routes[(64120, "/sonarlint/api/analysis/files")] = _bad
    with pytest.raises(errors.SonarMcpError) as ei:
        client.analyze_files(64120, ["/proj/a.py"])
    assert ei.value.code == errors.SONAR_BAD_RESPONSE


def test_analyze_non_object_json_raises(mock_sonar_factory, make_response) -> None:
    client, routes = mock_sonar_factory(ports=[64120])

    def _arr(req: httpx.Request) -> httpx.Response:
        return make_response(200, [1, 2, 3])

    routes[(64120, "/sonarlint/api/analysis/files")] = _arr
    with pytest.raises(errors.SonarMcpError) as ei:
        client.analyze_files(64120, ["/proj/a.py"])
    assert ei.value.code == errors.SONAR_BAD_RESPONSE


def test_batched_analysis_splits_into_batches(mock_sonar_factory, make_response) -> None:
    """51 files must produce two batches (50 + 1)."""
    seen_counts: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content.decode())
        seen_counts.append(len(body["fileAbsolutePaths"]))
        return make_response(200, {"findings": []})

    client, routes = mock_sonar_factory(ports=[64120])
    routes[(64120, "/sonarlint/api/analysis/files")] = handler

    files = [f"/proj/f{i}.py" for i in range(51)]
    outcomes = client.analyze_files_batched(64120, files, batch_size=50)
    assert len(outcomes) == 2
    assert seen_counts == [50, 1]
    assert all(o.error is None for o in outcomes)


def test_batched_partial_failure_preserves_success(mock_sonar_factory, make_response) -> None:
    """Middle batch fails; earlier and later successes survive."""
    call_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 2:
            return make_response(503, {"indexing": True})
        return make_response(200, {"findings": []})

    client, routes = mock_sonar_factory(ports=[64120])
    routes[(64120, "/sonarlint/api/analysis/files")] = handler

    files = [f"/proj/f{i}.py" for i in range(120)]  # 3 batches of 50,50,20
    outcomes = client.analyze_files_batched(64120, files, batch_size=50)
    assert len(outcomes) == 3
    assert outcomes[0].error is None
    assert outcomes[1].error is not None
    assert outcomes[1].error.code == errors.IDE_INDEXING
    assert outcomes[2].error is None


def test_sonar_client_thread_safe_client_cache(mock_sonar_factory) -> None:
    """Concurrent first-access to the same port must not orphan httpx.Client instances.

    Regression guard: the client cache was unguarded; two threads hitting the same port
    for the first time would each build a client and one would be leaked (never closed).
    """
    import threading

    client, _routes = mock_sonar_factory(ports=[64120])
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        # Repeatedly fetch the cached client; should be stable across threads.
        for _ in range(20):
            c = client._client(64120)
            assert c is not None

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    # Exactly one client should have been built for this port.
    assert len(client._clients) == 1
    client.close()
