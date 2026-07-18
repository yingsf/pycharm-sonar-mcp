"""SonarBackend:把现有 SonarQube for IDE 客户端包装成 AnalysisBackend

本类是 Sonar 侧的统一后端实现:
  * 复用 ``SonarClient`` + ``IdeDiscovery``(同一套端口发现与 HTTP 传输)。
  * 把 Sonar 的 ``Finding`` 转换成统一 ``SourceFinding``,供 orchestrator 去重。
  * ``is_available`` 通过端口扫描判断,不抛异常。
  * ``analyze_files`` 单次完成:端口发现 → 分批分析 → 转 SourceFinding。

不维护第二套 Sonar 业务逻辑;旧 ``sonar_*`` 工具仍直接调用底层 client/discovery,
契约保持不变。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from typing import Any

from ... import errors
from ...logging_config import get_logger
from ...quality.models import SourceFinding, UnifiedRange
from ..base import AnalysisBackend
from .client import SonarClient
from .discovery import IdeDiscovery
from .models import InstanceInfo

_log = get_logger("sonar.analyzer")

# Sonar 分批大小,与旧工具保持一致。
_DEFAULT_BATCH_SIZE = 50
_DEFAULT_PER_BATCH_TIMEOUT = 60.0


class SonarBackend(AnalysisBackend):
    """SonarQube for IDE 后端

    构造时注入共享的 SonarClient / IdeDiscovery,便于复用 MCP server 的单例。
    若不注入,则按默认配置新建(主要用于 doctor 等独立场景)。
    """

    def __init__(
        self,
        *,
        client: SonarClient | None = None,
        discovery: IdeDiscovery | None = None,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        per_batch_timeout: float = _DEFAULT_PER_BATCH_TIMEOUT,
    ) -> None:
        self._client = client if client is not None else SonarClient()
        self._discovery = discovery if discovery is not None else IdeDiscovery(self._client)
        self._owns_client = client is None
        self._batch_size = batch_size
        self._per_batch_timeout = per_batch_timeout

    @property
    def name(self) -> str:
        return "sonar"

    @property
    def client(self) -> SonarClient:
        return self._client

    @property
    def discovery(self) -> IdeDiscovery:
        return self._discovery

    # ------------------------------------------------------------------
    # AnalysisBackend 实现
    # ------------------------------------------------------------------

    async def is_available(self) -> bool:
        """扫描端口,只要发现一个可用实例就返回 True;扫描过程不抛异常"""
        try:
            instances = await asyncio.to_thread(self._discovery.discover_all_instances)
        except Exception as e:  # pragma: no cover - 防御性
            _log.debug("Sonar is_available scan failed: %s", e)
            return False
        return bool(instances)

    async def get_status(self) -> dict[str, Any]:
        """返回 Sonar 后端状态:installed / available / instances"""
        try:
            instances = await asyncio.to_thread(self._discovery.discover_all_instances)
        except Exception as e:  # pragma: no cover - 防御性
            _log.debug("Sonar get_status scan failed: %s", e)
            instances = []
        return {
            "installed": True,
            "available": bool(instances),
            "instances": [_instance_summary(i) for i in instances],
        }

    async def analyze_files(
        self,
        file_paths: list[str],
        errors_only: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """分析给定文件,返回统一结构

        kwargs 支持:
          * project_root(str):用于端口发现的 project root;若未提供则按文件推断。

        Returns:
            success/available/findings(SourceFinding)/raw_findings/ide_port/
            failed_files/duration_ms/error。
        """
        _ = errors_only
        started = time.monotonic()
        project_root = kwargs.get("project_root")

        if not file_paths:
            return _empty_result(started)

        # 端口发现:优先用传入的 project_root,否则用首个文件所在的工作区根。
        try:
            port = await self._discover_port(file_paths, project_root)
        except errors.SonarMcpError as e:
            _log.warning("SonarBackend port discovery failed: [%s] %s", e.code, e.user_message)
            return _failure_result(started, e, available=False)
        except Exception as e:
            _log.exception("SonarBackend port discovery raised")
            return _failure_result(
                started,
                errors.internal_error(f"Sonar port discovery failed: {e}"),
                available=False,
            )

        outcomes = await asyncio.to_thread(
            self._client.analyze_files_batched,
            port,
            list(file_paths),
            batch_size=self._batch_size,
            per_batch_timeout=self._per_batch_timeout,
        )

        raw_findings: list[dict[str, Any]] = []
        failed_files: list[dict[str, Any]] = []
        for outcome in outcomes:
            if outcome.error is not None:
                for fp in outcome.files:
                    failed_files.append(
                        {
                            "filePath": fp,
                            "errorCode": outcome.error.code,
                            "errorMessage": outcome.error.user_message,
                        }
                    )
                continue
            raw_findings.extend(outcome.findings)

        # 把 Sonar finding dict 转成统一 SourceFinding(纯转换,无 I/O;锚点 hash 由去重引擎统一预算)。
        source_findings = [sf for sf in (to_source_finding(f, "sonar") for f in raw_findings) if sf]

        duration_ms = int((time.monotonic() - started) * 1000)
        success = not failed_files
        return {
            "success": success,
            "available": True,
            "findings": source_findings,
            "raw_findings": raw_findings,
            "ide_port": port,
            "failed_files": failed_files,
            "duration_ms": duration_ms,
            "error": None,
        }

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    async def _discover_port(self, file_paths: list[str], project_root: str | None) -> int:
        """端口发现:优先 project_root,缺失时用首个文件所在目录"""
        if project_root:
            pr = os.path.normpath(os.path.abspath(project_root))
        else:
            # 用首个文件所在目录作为 project_root 兜底。
            pr = os.path.normpath(os.path.abspath(os.path.dirname(file_paths[0])))
        return await asyncio.to_thread(self._discovery.discover_for_project, pr)

    def close(self) -> None:
        """释放底层资源(仅在 backend 自己拥有 client 时关闭)"""
        if self._owns_client:
            with contextlib.suppress(Exception):
                self._client.close()


# ---------------------------------------------------------------------------
# 后端原始 finding dict -> SourceFinding 转换(模块级函数,orchestrator/jetbrains 复用)
# ---------------------------------------------------------------------------


def to_source_finding(raw: dict[str, Any], source: str) -> SourceFinding | None:
    """把单条后端原始 finding dict 转换为 SourceFinding

    支持 Sonar 与 JetBrains 两种字段形态:
      * Sonar:ruleKey / message / severity / filePath / textRange(startLine/...)
      * JetBrains:inspectionId / description / severity / filePath / startLine/...
    """
    if not isinstance(raw, dict):
        return None

    file_path = _first_str(raw, "filePath", "file_path", "path")
    if not file_path:
        return None
    message = _first_str(raw, "message", "description", "msg") or ""
    severity = _first_str(raw, "severity") or "UNKNOWN"
    rule_id = _first_str(raw, "ruleKey", "rule_key", "inspectionId", "inspection_id")

    urange = _build_range(raw)
    # 锚点 hash 由 deduplication 引擎按文件维度统一预算(传入 file_anchor_hashes),
    # 避免每条 finding 都读一次磁盘;to_source_finding 保持纯转换、无 I/O。

    return SourceFinding(
        source=source,
        ruleId=rule_id,
        severity=severity,
        message=message,
        filePath=file_path,
        range=urange,
        raw=raw,
    )


def _build_range(raw: dict[str, Any]) -> UnifiedRange | None:
    """从原始 finding 抽取统一范围(1-based);字段缺失返回 None"""
    text_range = raw.get("textRange")
    if isinstance(text_range, dict):
        sl = _as_int(text_range.get("startLine"))
        sc = _as_int(text_range.get("startColumn"), _as_int(text_range.get("startLineOffset")))
        el = _as_int(text_range.get("endLine"))
        ec = _as_int(text_range.get("endColumn"), _as_int(text_range.get("endLineOffset")))
    else:
        sl = _as_int(raw.get("startLine"))
        sc = _as_int(raw.get("startColumn"))
        el = _as_int(raw.get("endLine"))
        ec = _as_int(raw.get("endColumn"))
    if sl is None and sc is None and el is None and ec is None:
        return None
    return UnifiedRange(
        startLine=max(1, sl or 1),
        startColumn=max(1, sc or 1),
        endLine=max(1, el or sl or 1),
        endColumn=max(1, ec or sc or 1),
    )


def _as_int(value: Any, fallback: int | None = None) -> int | None:
    """把任意值转成 int;失败时返回 fallback"""
    if value is None:
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _first_str(raw: dict[str, Any], *keys: str) -> str | None:
    """按优先级返回首个非空字符串字段"""
    for k in keys:
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if v is not None and not isinstance(v, (dict, list)):
            s = str(v).strip()
            if s:
                return s
    return None


def _instance_summary(inst: InstanceInfo) -> dict[str, Any]:
    """构造 Sonar 实例的简要状态"""
    return {"port": inst.port, "ideName": _ide_name(inst.status)}


def _ide_name(status: dict[str, Any]) -> str:
    """从 Sonar status 中尽力推断 IDE 显示名"""
    return status.get("ideName") or status.get("ide") or status.get("productName") or "<unknown>"


def _empty_result(started: float) -> dict[str, Any]:
    return {
        "success": True,
        "available": True,
        "findings": [],
        "raw_findings": [],
        "ide_port": None,
        "failed_files": [],
        "duration_ms": int((time.monotonic() - started) * 1000),
        "error": None,
    }


def _failure_result(
    started: float, err: errors.SonarMcpError, *, available: bool
) -> dict[str, Any]:
    return {
        "success": False,
        "available": available,
        "findings": [],
        "raw_findings": [],
        "ide_port": None,
        "failed_files": [],
        "duration_ms": int((time.monotonic() - started) * 1000),
        "error": f"[{err.code}] {err.user_message}",
    }


__all__ = ["SonarBackend", "to_source_finding"]
