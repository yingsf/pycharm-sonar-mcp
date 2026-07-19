"""统一编排器:并行/隔离地调度 JetBrains + Sonar,合并并自动去重

职责:
  * 按 backend_mode(auto / jetbrains / sonar / all)选择参与的后端。
  * 后端之间隔离运行,单后端异常不取消另一个。
  * 把所有后端的原始问题合并为 SourceFinding 列表,送入 ``deduplicate``。
  * 产出 ``QualityAnalysisResult``:success / partialSuccess / degradedMode /
    severityCounts / deduplication 统计 / notices。

并发模型:两个后端可以用 asyncio.gather 并行;单个后端内部仍然顺序处理
(JetBrains 默认 JETBRAINS_MAX_CONCURRENCY=1)。任何后端抛异常都被捕获并
转成 BackendStatus.error,绝不向上传播到调用方。
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import Any

from .. import errors
from ..backends.base import AnalysisBackend
from ..backends.jetbrains.analyzer import JetBrainsAnalysisBackend
from ..backends.sonar.analyzer import SonarBackend
from ..logging_config import get_logger
from . import severity
from .deduplication import DeduplicationMode, deduplicate
from .models import BackendStatus, QualityAnalysisResult, SourceFinding, UnifiedFinding

_log = get_logger("orchestrator")


# backend_mode 取值常量
MODE_AUTO = "auto"
MODE_JETBRAINS = "jetbrains"
MODE_SONAR = "sonar"
MODE_ALL = "all"
_VALID_MODES: frozenset[str] = frozenset({MODE_AUTO, MODE_JETBRAINS, MODE_SONAR, MODE_ALL})


class QualityOrchestrator:
    """统一编排两个后端并产出 QualityAnalysisResult"""

    def __init__(
        self,
        *,
        jetbrains: JetBrainsAnalysisBackend | None = None,
        sonar: SonarBackend | None = None,
    ) -> None:
        """注入后端实例;None 表示"按需懒加载"或"不可用"

        调用方可以传入 None 表示 JetBrains 未配置;此时 auto 模式会自动降级。
        """
        self._jetbrains = jetbrains
        self._sonar = sonar

    @property
    def jetbrains(self) -> JetBrainsAnalysisBackend | None:
        return self._jetbrains

    @property
    def sonar(self) -> SonarBackend | None:
        return self._sonar

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    async def get_status(self) -> dict[str, Any]:
        """返回两个后端的完整状态(对应 code_quality_status)"""
        jb = await self._safe_status(self._jetbrains)
        sn = await self._safe_status(self._sonar)
        jb_available = bool(jb.get("available")) if jb else False
        sn_available = bool(sn.get("available")) if sn else False
        if jb_available:
            default_backend = "jetbrains"
        elif sn_available:
            default_backend = "sonar"
        else:
            default_backend = "none"
        return {
            "available": jb_available or sn_available,
            "defaultBackend": default_backend,
            "backends": {
                "jetbrains": jb or {"configured": False, "available": False},
                "sonar": sn or {"installed": False, "available": False, "instances": []},
            },
        }

    async def _safe_status(self, backend: AnalysisBackend | None) -> dict[str, Any] | None:
        if backend is None:
            return None
        try:
            return await backend.get_status()
        except Exception as e:  # pragma: no cover - 防御性
            _log.debug("get_status for %s failed: %s", getattr(backend, "name", "?"), e)
            return {"available": False, "error": str(e)}

    # ------------------------------------------------------------------
    # 文件分析
    # ------------------------------------------------------------------

    async def analyze_files(  # NOSONAR - orchestrates backend selection, partial-success, degraded mode
        self,
        file_paths: list[str],
        *,
        backend_mode: str = MODE_AUTO,
        errors_only: bool = False,
        deduplication_mode: str = DeduplicationMode.BALANCED,
        project_root: str | None = None,
        requested_file_count: int | None = None,
    ) -> QualityAnalysisResult:
        """编排分析,产出 QualityAnalysisResult

        Args:
            file_paths: 已校验、去重、排序后的绝对路径列表。
            backend_mode: auto / jetbrains / sonar / all。
            errors_only: 只返回错误级问题(各后端尽力过滤)。
            deduplication_mode: conservative / balanced / aggressive / off。
            project_root: 用于端口发现/锚点归一化的项目根。
            requested_file_count: 原始请求数(若调用方做了过滤,file_paths 可能更少)。
        """
        started = time.monotonic()
        if backend_mode not in _VALID_MODES:
            raise errors.bad_request(
                f"Invalid backend_mode {backend_mode!r}; expected one of {sorted(_VALID_MODES)}."
            )

        try:
            backends_to_run = self._select_backends(backend_mode)
        except errors.SonarMcpError as e:
            # 无任何后端可用:产出失败结果而非抛错,便于工具层统一返回结构化错误。
            return QualityAnalysisResult(
                success=False,
                partialSuccess=False,
                degradedMode=False,
                requestedFileCount=(
                    requested_file_count if requested_file_count is not None else len(file_paths)
                ),
                analyzedFileCount=len(file_paths),
                deduplicationMode=deduplication_mode,
                notices=[
                    f"No analysis backend available: [{e.code}] {e.user_message}",
                ],
                durationMs=int((time.monotonic() - started) * 1000),
            )
        # 探测各后端可用性(auto 模式下不可用则跳过)。
        runnable = await self._resolve_runnable(backends_to_run, backend_mode)

        if not runnable:
            # 无任何可用后端。
            return QualityAnalysisResult(
                success=False,
                partialSuccess=False,
                degradedMode=False,
                requestedFileCount=requested_file_count
                if requested_file_count is not None
                else len(file_paths),
                analyzedFileCount=len(file_paths),
                deduplicationMode=deduplication_mode,
                notices=[
                    "No analysis backend available. Configure JetBrains MCP or run SonarQube for IDE.",
                    f"errorCode={errors.NO_ANALYSIS_BACKEND_AVAILABLE}",
                ],
                durationMs=int((time.monotonic() - started) * 1000),
            )

        # 并行调度(单后端内部仍顺序执行)。
        tasks = [
            asyncio.create_task(self._run_backend(b, file_paths, errors_only, project_root))
            for b in runnable
        ]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        # 合并所有 SourceFinding。
        merged: list[SourceFinding] = []
        backend_statuses: dict[str, BackendStatus] = {}
        notices: list[str] = []
        degraded_mode = False
        partial_success = False

        for backend, outcome in zip(runnable, outcomes, strict=True):
            status, source_findings, note = self._consume_outcome(backend, outcome)
            backend_statuses[backend.name] = status
            merged.extend(source_findings)
            if note:
                notices.append(note)
            # degradedMode:主后端(jetbrains)失败/不可用,但 sonar 兜底成功。
            if (
                backend_mode == MODE_AUTO
                and backend.name == "sonar"
                and status.success
                and (
                    self._jetbrains is None
                    or not backend_statuses.get("jetbrains", BackendStatus()).success
                )
            ):
                degraded_mode = True
            # partialSuccess:任一后端失败,或 all 模式下有后端没成功。
            if not status.success:
                partial_success = True

        # all 模式下,若 sonar 未安装但显式要求 all,视为 partial。
        if backend_mode == MODE_ALL and self._sonar is None:
            notices.append("Sonar backend is not installed; 'all' mode could only run JetBrains.")
            partial_success = True

        # 去重。
        dedup_result = deduplicate(merged, mode=deduplication_mode)
        findings = dedup_result.findings

        severity_counts = _count_severities(findings)
        analyzed = len(file_paths)
        success = any(s.success for s in backend_statuses.values())

        # 判定 success:至少一个后端成功;若所有 runnable 都失败则 success=False。
        if not success:
            partial_success = False  # 全失败就不叫 partial 了

        file_summaries = _build_file_summaries(file_paths, findings, backend_statuses)

        result = QualityAnalysisResult(
            success=success,
            partialSuccess=partial_success,
            degradedMode=degraded_mode,
            requestedFileCount=requested_file_count
            if requested_file_count is not None
            else analyzed,
            analyzedFileCount=analyzed,
            rawFindingCount=dedup_result.raw_count,
            uniqueFindingCount=dedup_result.unique_count,
            duplicatesMerged=dedup_result.duplicates_merged,
            possibleDuplicateCount=dedup_result.possible_count,
            deduplicationMode=deduplication_mode,
            severityCounts=severity_counts,
            backends=backend_statuses,
            fileSummaries=file_summaries,
            findings=findings,
            deduplicationGroups=_build_dedup_groups(findings),
            possibleDuplicateGroups=dedup_result.possible_duplicate_groups,
            notices=notices,
            durationMs=int((time.monotonic() - started) * 1000),
        )
        return result

    # ------------------------------------------------------------------
    # 内部:后端选择与可用性探测
    # ------------------------------------------------------------------

    def _select_backends(self, mode: str) -> list[AnalysisBackend]:
        """按 mode 选出"想要运行"的后端列表;尚未做可用性过滤"""
        if mode == MODE_JETBRAINS:
            return [self._require_backend("jetbrains")]
        if mode == MODE_SONAR:
            return [self._require_backend("sonar")]
        if mode == MODE_ALL:
            out: list[AnalysisBackend] = []
            if self._jetbrains is not None:
                out.append(self._jetbrains)
            if self._sonar is not None:
                out.append(self._sonar)
            if not out:
                raise errors.no_analysis_backend_available("No backends configured for 'all' mode.")
            return out
        # MODE_AUTO:JetBrains 优先,Sonar 可选。
        out2: list[AnalysisBackend] = []
        if self._jetbrains is not None:
            out2.append(self._jetbrains)
        if self._sonar is not None:
            out2.append(self._sonar)
        if not out2:
            raise errors.no_analysis_backend_available(
                "No backends available. Configure JetBrains MCP or install SonarQube for IDE."
            )
        return out2

    def _require_backend(self, name: str) -> AnalysisBackend:
        b = self._jetbrains if name == "jetbrains" else self._sonar
        if b is None:
            raise errors.no_analysis_backend_available(
                f"Backend {name!r} is not available. "
                + (
                    "Run: pycharm-code-quality-mcp jetbrains configure"
                    if name == "jetbrains"
                    else "Open PyCharm with the SonarQube for IDE plugin."
                )
            )
        return b

    async def _resolve_runnable(
        self, backends: list[AnalysisBackend], mode: str
    ) -> list[AnalysisBackend]:
        """探测可用性;auto 模式下不可用的后端被剔除"""
        if mode == MODE_AUTO:
            runnable: list[AnalysisBackend] = []
            for b in backends:
                try:
                    ok = await b.is_available()
                except Exception:  # pragma: no cover - 防御性
                    ok = False
                if ok:
                    runnable.append(b)
            return runnable
        # 显式模式不做可用性剔除;让 analyze 自己报错并标记 partial。
        return list(backends)

    # ------------------------------------------------------------------
    # 内部:运行单个后端
    # ------------------------------------------------------------------

    async def _run_backend(
        self,
        backend: AnalysisBackend,
        file_paths: list[str],
        errors_only: bool,
        project_root: str | None,
    ) -> dict[str, Any]:
        """运行单个后端并返回它的原始结果 dict"""
        kwargs: dict[str, Any] = {}
        if project_root is not None:
            kwargs["project_root"] = project_root
        return await backend.analyze_files(file_paths, errors_only=errors_only, **kwargs)

    def _consume_outcome(
        self, backend: AnalysisBackend, outcome: Any
    ) -> tuple[BackendStatus, list[SourceFinding], str | None]:
        """把单后端的 asyncio 结果转成 (BackendStatus, findings, notice)"""
        name = backend.name
        if isinstance(outcome, errors.SonarMcpError):
            return (
                BackendStatus(
                    attempted=True,
                    available=False,
                    success=False,
                    findingCount=0,
                    durationMs=0,
                    error=f"[{outcome.code}] {outcome.user_message}",
                ),
                [],
                f"{name} backend failed: [{outcome.code}] {outcome.user_message}",
            )
        if isinstance(outcome, Exception):
            return (
                BackendStatus(
                    attempted=True,
                    available=False,
                    success=False,
                    findingCount=0,
                    durationMs=0,
                    error=str(outcome),
                ),
                [],
                f"{name} backend raised: {type(outcome).__name__}: {outcome}",
            )
        if not isinstance(outcome, dict):
            return (
                BackendStatus(
                    attempted=True,
                    available=False,
                    success=False,
                    findingCount=0,
                    durationMs=0,
                    error=f"unexpected result type: {type(outcome).__name__}",
                ),
                [],
                None,
            )

        success = bool(outcome.get("success", False))
        available = bool(outcome.get("available", success))
        findings = outcome.get("findings") or []
        # 只接受真正的 SourceFinding,过滤掉 dict 之类(防御性)。
        source_findings: list[SourceFinding] = [f for f in findings if isinstance(f, SourceFinding)]
        finding_count = len(source_findings)
        duration = int(outcome.get("duration_ms") or 0)
        err = outcome.get("error")
        # 子级别错误(部分文件失败)也算 partial,但仍 success=True。
        failed_files = outcome.get("failed_files") or []
        if failed_files and success:
            # 部分文件失败:backend 整体仍 success,但发 notice。
            pass
        status = BackendStatus(
            attempted=True,
            available=available,
            success=success,
            findingCount=finding_count,
            durationMs=duration,
            error=err if not success else None,
        )
        notice: str | None = None
        if not success and err:
            notice = f"{name} backend failed: {err}"
        elif failed_files and success:
            notice = f"{name}: {len(failed_files)} file(s) failed within backend."
        return status, source_findings, notice


# ---------------------------------------------------------------------------
# 模块级 helper
# ---------------------------------------------------------------------------


def _count_severities(findings: list[UnifiedFinding]) -> dict[str, int]:
    """统计统一问题的归一化严重级别分布"""
    counter: Counter[str] = Counter()
    for f in findings:
        counter[f.severity] += 1
    return dict(counter)


def _build_file_summaries(
    file_paths: list[str], findings: list[UnifiedFinding], backends: dict[str, BackendStatus]
) -> list[dict[str, Any]]:
    """按文件聚合 finding 数,生成 fileSummaries"""
    _ = backends
    per_file: dict[str, list[UnifiedFinding]] = {}
    for f in findings:
        # UnifiedFinding.filePath 可能是 SourceFinding 的路径;按归一化 key 分组。
        key = f.file_path
        per_file.setdefault(key, []).append(f)

    summaries: list[dict[str, Any]] = []
    requested_set = set(file_paths)
    # 先输出有 finding 的文件。
    for fp in file_paths:
        bucket = per_file.get(fp, [])
        summaries.append(
            {
                "filePath": fp,
                "findingCount": len(bucket),
                "severityBreakdown": dict(Counter(f.severity for f in bucket)),
                "sources": sorted({src for f in bucket for src in f.sources}),
            }
        )
    # 极少数情况下 finding.filePath 与请求路径大小写不同,补一条。
    for fp, bucket in per_file.items():
        if fp not in requested_set:
            summaries.append(
                {
                    "filePath": fp,
                    "findingCount": len(bucket),
                    "severityBreakdown": dict(Counter(f.severity for f in bucket)),
                    "sources": sorted({src for f in bucket for src in f.sources}),
                }
            )
    return summaries


def _build_dedup_groups(findings: list[UnifiedFinding]) -> list[dict[str, Any]]:
    """从已合并的 UnifiedFinding 中重建可读的去重组摘要"""
    groups: list[dict[str, Any]] = []
    for f in findings:
        if f.duplicate_count <= 1:
            continue
        groups.append(
            {
                "id": f.id,
                "filePath": f.file_path,
                "category": f.category,
                "severity": f.severity,
                "duplicateCount": f.duplicate_count,
                "sources": f.sources,
                "confidence": f.deduplication.confidence,
                "reasons": f.deduplication.reason,
            }
        )
    return groups


_ = severity  # 保留对 severity 模块的引用(避免被 lint 当作未使用)

__all__ = [
    "MODE_ALL",
    "MODE_AUTO",
    "MODE_JETBRAINS",
    "MODE_SONAR",
    "QualityOrchestrator",
]
