"""JetBrainsBackend:基于 JetBrainsClient 的批量文件分析封装

建立一次 ClientSession,检查项目 indexing 状态,再顺序对每个文件调用
get_file_problems。单文件失败不会影响其他文件;连接级失败直接短路返回。

``JetBrainsAnalysisBackend`` 是同一能力的 ``AnalysisBackend`` 适配层,
把 ``JetBrainsAnalysisResult`` 统一为 orchestrator 期望的 dict +
``SourceFinding`` 列表。旧 ``jetbrains_inspect_*`` 工具直接调用
``JetBrainsBackend.analyze_files`` 拿原始 ``JetBrainsAnalysisResult``。
"""

from __future__ import annotations

import os
import time
from typing import Any

from ... import errors
from ...logging_config import get_logger
from ..base import AnalysisBackend
from ..sonar.analyzer import to_source_finding
from .client import ALLOWED_TOOLS, JetBrainsClient
from .config import JetBrainsConfig
from .models import FailedFile, JetBrainsAnalysisResult, JetBrainsProblem

_log = get_logger("jetbrains.analyzer")

# 单文件 inspection 默认超时(毫秒);可由环境变量覆盖。
_ENV_TIMEOUT_MS = "JETBRAINS_INSPECTION_TIMEOUT_MS"
_DEFAULT_TIMEOUT_MS = 30000


def _resolve_timeout_ms() -> int:
    """读取 JETBRAINS_INSPECTION_TIMEOUT_MS;非法/未设置回退到 30000"""
    raw = os.environ.get(_ENV_TIMEOUT_MS, "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_MS
    try:
        v = int(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_MS
    return max(1000, v)


# 默认顺序分析(避免给本地 IDE inspection 引入并发压力)
JETBRAINS_MAX_CONCURRENCY = 1

# 单文件分析失败时,记录到 failed_files 而不抛出的错误码白名单。
# 连接级 / 配置级错误直接短路抛出。
_NON_FATAL_ERROR_CODES: frozenset[str] = frozenset(
    {
        errors.JETBRAINS_TOOL_FAILED,
        errors.JETBRAINS_BAD_RESPONSE,
        errors.JETBRAINS_TIMEOUT,
    }
)


class JetBrainsBackend:
    """对外的 JetBrains MCP 批量分析入口

    每次 analyze_files 都会独立建立一次连接,分析完成后关闭,保证调用幂等。
    """

    def __init__(self, config: JetBrainsConfig, *, timeout_ms: int | None = None) -> None:
        self._config = config
        self._timeout_ms = timeout_ms if timeout_ms is not None else _resolve_timeout_ms()

    async def analyze_files(
        self,
        file_paths: list[str],
        errors_only: bool = False,
    ) -> JetBrainsAnalysisResult:
        """顺序分析给定文件,合并返回所有问题

        Args:
            file_paths: 绝对文件路径列表。
            errors_only: 是否只返回 error 级别问题。

        Returns:
            JetBrainsAnalysisResult:包含成功标志、合并问题、失败文件等。
        """
        started = time.monotonic()
        if not file_paths:
            return JetBrainsAnalysisResult(
                success=True,
                problems=[],
                failedFiles=[],
                projectIndexing=False,
                durationMs=0,
                error=None,
            )

        client = JetBrainsClient(self._config, timeout_ms=self._timeout_ms)
        all_problems: list[JetBrainsProblem] = []
        failed_files: list[FailedFile] = []
        project_indexing = False

        try:
            async with client:
                # 1. 项目状态检查(indexing 时不短路,只标记,继续尝试)
                project_indexing = await self._check_project_indexing(client)

                # 2. 顺序分析每个文件
                for fp in file_paths:
                    outcome = await self._analyze_one(client, fp, errors_only)
                    if outcome.failure is not None:
                        failed_files.append(outcome.failure)
                    all_problems.extend(outcome.problems)

        except errors.SonarMcpError as e:
            # 连接级失败:把已收集的部分结果连同错误一起返回
            duration_ms = int((time.monotonic() - started) * 1000)
            return JetBrainsAnalysisResult(
                success=False,
                problems=all_problems,
                failedFiles=failed_files,
                projectIndexing=project_indexing,
                durationMs=duration_ms,
                error=f"[{e.code}] {e.user_message}",
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        success = not failed_files
        return JetBrainsAnalysisResult(
            success=success,
            problems=all_problems,
            failedFiles=failed_files,
            projectIndexing=project_indexing,
            durationMs=duration_ms,
            error=None,
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    async def _check_project_indexing(self, client: JetBrainsClient) -> bool:
        """查询项目 indexing 状态;查询本身失败不致命,降级为 False"""
        try:
            status = await client.get_project_status()
        except errors.SonarMcpError as e:
            _log.warning(
                "get_project_status failed, assuming not indexing: [%s] %s",
                e.code,
                e.user_message,
            )
            return False
        is_indexing = bool(status.get("isIndexing", False))
        if is_indexing:
            _log.info("PyCharm is currently indexing; results may be incomplete.")
        return is_indexing

    async def _analyze_one(
        self,
        client: JetBrainsClient,
        file_path: str,
        errors_only: bool,
    ) -> _OneOutcome:
        """分析单个文件,返回 _OneOutcome(成功 problems 或 failure 二选一)"""
        try:
            problems = await client.get_file_problems(file_path, errors_only=errors_only)
        except errors.SonarMcpError as e:
            _log.warning(
                "get_file_problems failed for %s: [%s] %s",
                file_path,
                e.code,
                e.user_message,
            )
            # 连接级 / 配置级错误:上抛,让上层决定是否短路
            if e.code not in _NON_FATAL_ERROR_CODES:
                raise
            return _OneOutcome(
                problems=[],
                failure=FailedFile(
                    filePath=file_path,
                    errorCode=e.code,
                    errorMessage=e.user_message,
                ),
            )
        except Exception as e:  # 防御性兜底
            _log.warning("get_file_problems raised unexpected error for %s: %s", file_path, e)
            return _OneOutcome(
                problems=[],
                failure=FailedFile(
                    filePath=file_path,
                    errorCode=errors.JETBRAINS_TOOL_FAILED,
                    errorMessage=str(e),
                ),
            )
        _log.debug("get_file_problems ok for %s: %d problem(s)", file_path, len(problems))
        return _OneOutcome(problems=problems, failure=None)


class _OneOutcome:
    """单个文件分析结果:成功 problems 与失败 failure 二选一"""

    __slots__ = ("failure", "problems")

    def __init__(
        self,
        *,
        problems: list[JetBrainsProblem],
        failure: FailedFile | None,
    ) -> None:
        self.problems = problems
        self.failure = failure


class JetBrainsAnalysisBackend(AnalysisBackend):
    """``AnalysisBackend`` 适配层:把 JetBrainsBackend 包装成统一后端接口

    内部委托给 ``JetBrainsBackend``,并把返回值从 ``JetBrainsAnalysisResult``
    转换为 orchestrator 期望的 dict(含 ``SourceFinding`` 列表)。
    """

    def __init__(
        self,
        config: JetBrainsConfig | None = None,
        *,
        backend: JetBrainsBackend | None = None,
    ) -> None:
        if backend is not None:
            self._backend = backend
            self._config = backend._config
            self._timeout_ms = backend._timeout_ms
        else:
            if config is None:
                # 延迟加载:避免 import 时就触发配置文件 IO。
                from .config import load_config

                loaded = load_config()
                if loaded is None:
                    raise errors.jetbrains_not_configured(
                        "JetBrains MCP is not configured. "
                        "Run: pycharm-code-quality-mcp jetbrains configure"
                    )
                config = loaded
            self._config = config
            self._timeout_ms = _resolve_timeout_ms()
            self._backend = JetBrainsBackend(config, timeout_ms=self._timeout_ms)

    @property
    def name(self) -> str:
        return "jetbrains"

    @property
    def config(self) -> JetBrainsConfig:
        return self._config

    @property
    def backend(self) -> JetBrainsBackend:
        return self._backend

    async def is_available(self) -> bool:
        """配置存在 + 能成功 initialize + tools/list 即视为可用"""
        try:
            client = JetBrainsClient(self._config, timeout_ms=self._timeout_ms)
        except Exception:  # pragma: no cover - 防御性
            return False
        try:
            async with client:
                _status = await client.get_project_status()
            return True
        except Exception as e:
            _log.debug("JetBrains is_available probe failed: %s", e)
            return False

    async def get_status(self) -> dict[str, Any]:
        """返回 JetBrains 后端状态:configured/available/projectReady/tools"""
        result: dict[str, Any] = {
            "configured": True,
            "available": False,
            "projectReady": False,
            "indexing": False,
            "tools": sorted(ALLOWED_TOOLS),
            "url": _safe_url(self._config.url),
        }
        try:
            client = JetBrainsClient(self._config, timeout_ms=self._timeout_ms)
        except Exception as e:  # pragma: no cover - 防御性
            result["error"] = str(e)
            return result
        try:
            async with client:
                status = await client.get_project_status()
            result["available"] = True
            result["indexing"] = bool(status.get("isIndexing", False))
            result["projectReady"] = not result["indexing"]
        except errors.SonarMcpError as e:
            result["error"] = f"[{e.code}] {e.user_message}"
        except Exception as e:  # pragma: no cover - 防御性
            result["error"] = str(e)
        return result

    async def analyze_files(
        self,
        file_paths: list[str],
        errors_only: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """委托给 JetBrainsBackend,再把结果转换为统一 dict"""
        started = time.monotonic()
        res: JetBrainsAnalysisResult = await self._backend.analyze_files(
            file_paths, errors_only=errors_only
        )
        # 把每个 JetBrainsProblem 转成 SourceFinding(锚点 hash 内部预算)。
        source_findings = [_problem_to_source(p) for p in res.problems]
        source_findings = [sf for sf in source_findings if sf is not None]
        failed_files = [f.model_dump(by_alias=True) for f in res.failed_files]
        return {
            "success": res.success,
            "available": True,
            "findings": source_findings,
            "raw_findings": [p.model_dump(by_alias=True) for p in res.problems],
            "failed_files": failed_files,
            "project_indexing": res.project_indexing,
            "duration_ms": res.duration_ms or int((time.monotonic() - started) * 1000),
            "error": res.error,
        }


# ---------------------------------------------------------------------------
# 内部:JetBrainsProblem -> SourceFinding
# ---------------------------------------------------------------------------


def _problem_to_source(problem: JetBrainsProblem) -> Any:
    """把 JetBrainsProblem 转成 SourceFinding(锚点 hash 内部预算)"""
    from ...quality.models import UnifiedRange

    raw = problem.model_dump(by_alias=True)
    # raw 已含 startLine/startColumn/endLine/endColumn/filePath/inspectionId/description/severity。
    # to_source_finding 兼容这些字段名。
    sf = to_source_finding(raw, "jetbrains")
    if sf is None:
        # 极端情况:filePath 缺失,用 problem 内的字段直接构造。
        return None
    # description 在 raw 里,message 字段为空时回填。
    if not sf.message and problem.description:
        sf = sf.model_copy(update={"message": problem.description})
    # 确保 range 存在(JetBrainsProblem 总有行列号)。
    if sf.range is None:
        sf = sf.model_copy(
            update={
                "range": UnifiedRange(
                    startLine=problem.start_line,
                    startColumn=problem.start_column,
                    endLine=problem.end_line,
                    endColumn=problem.end_column,
                )
            }
        )
    return sf


def _safe_url(url: str) -> str:
    """日志安全地暴露 URL(不暴露 headers,URL 本身是 loopback)"""
    return url or ""


__all__ = [
    "ALLOWED_TOOLS",
    "JETBRAINS_MAX_CONCURRENCY",
    "JetBrainsAnalysisBackend",
    "JetBrainsBackend",
]
