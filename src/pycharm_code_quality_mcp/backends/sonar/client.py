"""Sonar HTTP 客户端:封装 IPv4 loopback 传输层,对外暴露 status 与 analysis

本模块是唯一直接了解 SonarQube for IDE HTTP API 形状的地方。它实现了
`ide_discovery` 所依赖的 `_SonarProbeLike` Protocol,并提供批量分析能力。
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any

import httpx

from ... import errors
from ...logging_config import get_logger
from .discovery import (
    ANALYSIS_PATH,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    STATUS_PATH,
    validate_status_response,
)
from .models import AnalysisResponse
from .transport import build_local_client

_log = get_logger("sonar_client")


class SonarClient:
    """通过 IPv4 loopback 传输层调用 SonarQube for IDE 本地 HTTP API

    每个端口按需创建独立的 httpx client(请求量小且为 loopback,无需连接池)。
    测试通过 `transport_override` 注入来 mock HTTP,无需真实 socket。
    """

    def __init__(
        self,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        transport_factory: Any = None,
    ) -> None:
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._transport_factory = transport_factory
        # port -> httpx.Client 的缓存,避免每次调用都重建传输层。
        self._clients: dict[int, httpx.Client] = {}
        # 保护 _clients:MCP server 可能并发发起 to_thread 调用,首次命中同一端口时
        # 若无锁,两个线程会各自创建 client,其中一个会成为孤儿(永不关闭)。
        self._lock = threading.Lock()

    def _client(self, port: int) -> httpx.Client:
        # 快速路径:client 已存在时不持锁读取。
        cli = self._clients.get(port)
        if cli is not None:
            return cli
        with self._lock:
            cli = self._clients.get(port)
            if cli is not None:
                return cli
            transport_override = None
            if self._transport_factory is not None:
                transport_override = self._transport_factory(port)
            cli = build_local_client(
                port,
                connect_timeout=self._connect_timeout,
                read_timeout=self._read_timeout,
                transport_override=transport_override,
            )
            self._clients[port] = cli
            return cli

    def close(self) -> None:
        for cli in self._clients.values():
            with contextlib.suppress(Exception):
                cli.close()
        self._clients.clear()

    # -- _SonarProbeLike implementation --

    def get_status(self, port: int, *, timeout: float = 2.5) -> dict[str, Any]:
        """GET /sonarlint/api/status,返回解析后的 JSON dict 或抛出 SonarMcpError"""
        try:
            resp = self._client(port).get(STATUS_PATH, timeout=timeout)
        except httpx.ConnectError as e:
            raise errors.ide_ipv4_connection_failed(
                f"Cannot connect to 127.0.0.1:{port}: {e}"
            ) from e
        except httpx.ConnectTimeout as e:
            raise errors.ide_ipv4_connection_failed(
                f"Connection timeout to 127.0.0.1:{port}: {e}"
            ) from e
        except httpx.ReadTimeout as e:
            raise errors.sonar_unavailable(f"Read timeout from port {port}: {e}") from e
        except httpx.HTTPError as e:
            raise errors.sonar_unavailable(f"HTTP error contacting port {port}: {e}") from e
        return validate_status_response(resp)

    def analyze_files(
        self,
        port: int,
        file_paths: list[str],
        *,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """POST /sonarlint/api/analysis/files,返回解析后的 JSON dict

        返回的 dict 包含:
          * "findings": list[Finding](已解析)
          * "raw": Sonar 返回的原始 dict(用于前向兼容字段访问)

        传输/协议错误时抛出 SonarMcpError。
        """
        if not file_paths:
            return {"findings": [], "raw": {"findings": []}}

        payload = {"fileAbsolutePaths": list(file_paths)}
        try:
            resp = self._client(port).post(
                ANALYSIS_PATH,
                json=payload,
                timeout=timeout,
            )
        except httpx.ConnectError as e:
            raise errors.ide_ipv4_connection_failed(
                f"Cannot connect to 127.0.0.1:{port} for analysis: {e}"
            ) from e
        except httpx.ConnectTimeout as e:
            raise errors.ide_ipv4_connection_failed(
                f"Connection timeout to 127.0.0.1:{port} for analysis: {e}"
            ) from e
        except httpx.ReadTimeout as e:
            raise errors.batch_timeout(f"Analysis request timed out on port {port}: {e}") from e
        except httpx.HTTPError as e:
            raise errors.sonar_unavailable(f"HTTP error during analysis on port {port}: {e}") from e

        return _validate_analysis_response(resp)

    # -- batched analysis helper --

    def analyze_files_batched(
        self,
        port: int,
        file_paths: list[str],
        *,
        batch_size: int = 50,
        per_batch_timeout: float = 60.0,
    ) -> list[_BatchOutcome]:
        """按 `batch_size` 顺序分批分析文件

        每批返回一个 _BatchOutcome。单批失败绝不抛异常,而是把错误记录进 outcome,
        从而保留部分结果。
        """
        outcomes: list[_BatchOutcome] = []
        for idx in range(0, len(file_paths), batch_size):
            batch = file_paths[idx : idx + batch_size]
            batch_index = idx // batch_size
            try:
                result = self.analyze_files(port, batch, timeout=per_batch_timeout)
                outcomes.append(
                    _BatchOutcome(
                        batch_index=batch_index,
                        files=batch,
                        findings=result["findings"],
                        raw=result["raw"],
                        error=None,
                    )
                )
            except errors.SonarMcpError as e:
                outcomes.append(
                    _BatchOutcome(
                        batch_index=batch_index,
                        files=batch,
                        findings=[],
                        raw={},
                        error=e,
                    )
                )
            except Exception as e:  # pragma: no cover - defensive
                outcomes.append(
                    _BatchOutcome(
                        batch_index=batch_index,
                        files=batch,
                        findings=[],
                        raw={},
                        error=errors.internal_error(
                            f"Unexpected error in batch {batch_index}: {e}"
                        ),
                    )
                )
        return outcomes


class _BatchOutcome:
    """单个批次的内部结果,即便部分失败也保留已成功的 findings"""

    __slots__ = ("batch_index", "error", "files", "findings", "raw")

    def __init__(
        self,
        *,
        batch_index: int,
        files: list[str],
        findings: list[dict[str, Any]],
        raw: dict[str, Any],
        error: errors.SonarMcpError | None,
    ) -> None:
        self.batch_index = batch_index
        self.files = files
        self.findings = findings
        self.raw = raw
        self.error = error


# ---------------------------------------------------------------------------
# 响应校验
# ---------------------------------------------------------------------------


def _validate_analysis_response(resp: httpx.Response) -> dict[str, Any]:
    """校验分析 HTTP 响应,返回包含 'findings' 与 'raw' 的 dict"""
    if resp.status_code == 421:
        raise errors.ide_authority_rejected(
            "HTTP 421 Misdirected Request during analysis. The Host/Origin header is "
            "not 'localhost'."
        )
    if resp.status_code == 404:
        raise errors.sonar_bad_response(
            "HTTP 404 on analysis endpoint — port is not a SonarQube for IDE service."
        )
    if resp.status_code == 429:
        raise errors.sonar_rate_limited(
            "HTTP 429 from Sonar IDE during analysis (rate limited). Reduce batch size "
            "or retry later."
        )
    if resp.status_code == 503:
        raise errors.ide_indexing(
            "HTTP 503 — PyCharm is still indexing or the Sonar backend is starting."
        )
    if resp.status_code >= 500:
        raise errors.sonar_unavailable(
            f"Sonar IDE returned HTTP {resp.status_code} during analysis."
        )
    if resp.status_code >= 400:
        # 尝试从响应体透出错误信息,但不泄露源码内容。
        try:
            body_text = resp.text[:500]
        except Exception:
            body_text = "<unreadable>"
        raise errors.sonar_bad_response(
            f"Sonar IDE returned HTTP {resp.status_code} during analysis: {body_text}"
        )
    ctype = resp.headers.get("content-type", "")
    try:
        obj = resp.json()
    except Exception as e:
        raise errors.sonar_bad_response(
            f"Analysis response is not valid JSON (content-type={ctype!r})."
        ) from e
    if not isinstance(obj, dict):
        raise errors.sonar_bad_response(
            f"Analysis response is JSON but not an object (got {type(obj).__name__})."
        )
    # 使用 Pydantic 解析,同时保留原始对象以兼容前向字段。
    try:
        parsed = AnalysisResponse.model_validate(obj)
    except Exception as e:
        raise errors.sonar_bad_response(f"Analysis JSON failed schema validation: {e}") from e

    # 把 findings 序列化回 dict,以保留原始别名与额外字段。
    findings = [f.model_dump(by_alias=True, exclude_none=False) for f in parsed.findings]
    return {"findings": findings, "raw": obj}
