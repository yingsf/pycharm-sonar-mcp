"""JetBrainsBackend:基于 JetBrainsClient 的批量文件分析封装

建立一次 ClientSession,检查项目 indexing 状态,再顺序对每个文件调用
get_file_problems。单文件失败不会影响其他文件;连接级失败直接短路返回。
"""

from __future__ import annotations

import time

from ... import errors
from ...logging_config import get_logger
from .client import JetBrainsClient
from .config import JetBrainsConfig
from .models import FailedFile, JetBrainsAnalysisResult, JetBrainsProblem

_log = get_logger("jetbrains.analyzer")

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

    def __init__(self, config: JetBrainsConfig) -> None:
        self._config = config

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

        client = JetBrainsClient(self._config)
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


__all__ = ["JETBRAINS_MAX_CONCURRENCY", "JetBrainsBackend"]
