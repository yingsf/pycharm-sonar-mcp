"""结果汇总构建器:把批次结果与逐文件状态合并为 AnalysisResult

对应 spec 第 9.2 节("文件状态"、"部分失败"、"返回格式")。
核心规则:空 findings 不得等同于"分析成功" —— IDE 会显式上报"已索引但干净"的文件,
必须与"未索引"/"不支持"区分开。
"""

from __future__ import annotations

import os
import time
from typing import Any

from . import errors
from .logging_config import get_logger
from .models import (
    STATUS_ANALYZED,
    STATUS_FAILED,
    STATUS_NOT_INDEXED,
    STATUS_UNSUPPORTED_TYPE,
    AnalysisResult,
    BatchError,
    FailedFile,
    FileSummary,
)
from .sonar_client import _BatchOutcome

_log = get_logger("result_summary")


def _record_batch_error(
    outcome: _BatchOutcome,
    file_status: dict[str, tuple[str, str | None]],
    batch_errors: list[BatchError],
) -> None:
    """记录失败的批次:追加到 batch_errors,并把该批文件标记为失败"""
    batch_errors.append(
        BatchError(
            batchIndex=outcome.batch_index,
            fileCount=len(outcome.files),
            errorCode=outcome.error.code if outcome.error else "UNKNOWN",
            errorMessage=outcome.error.user_message if outcome.error else "",
        )
    )
    # 文件在进入批次前已去重,因此每个文件只出现在一个批次中;被标记为失败的文件
    # 不会同时存在成功的 findings 去反驳该状态。
    for f in outcome.files:
        if f in file_status and file_status[f][0] == STATUS_ANALYZED:
            file_status[f] = (STATUS_FAILED, outcome.error.user_message if outcome.error else None)


def _record_batch_success(
    outcome: _BatchOutcome,
    file_to_findings: dict[str, list[dict[str, Any]]],
    all_findings: list[dict[str, Any]],
    file_status: dict[str, tuple[str, str | None]],
) -> None:
    """记录成功的批次:汇总 findings 并应用逐文件标记"""
    for fnd in outcome.findings:
        fp = fnd.get("filePath")
        if not isinstance(fp, str):
            continue
        file_to_findings.setdefault(os.path.normpath(fp), []).append(fnd)
        all_findings.append(fnd)

    _apply_per_file_markers(outcome.raw, file_status)

    for f in outcome.files:
        if f not in file_status:
            file_status[f] = (STATUS_ANALYZED, None)


def _count_severity(findings: list[dict[str, Any]]) -> dict[str, int]:
    """按严重级别字符串对 findings 计数"""
    counts: dict[str, int] = {}
    for fnd in findings:
        sev = str(fnd.get("severity") or "UNKNOWN")
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def build_result(
    *,
    requested_files: list[str],
    batch_outcomes: list[_BatchOutcome],
    ide_port: int | None,
    start_time: float,
) -> AnalysisResult:
    """把各批次结果合并为单个 AnalysisResult

    逐文件状态按以下规则推断:
      * 文件出现在某成功批次的 findings 中,或被发送且未返回逐文件
        "not indexed"/"unsupported" 标记 → "analyzed"。
      * 文件所在的批次失败 → "failed"(附带该批次错误码)。
      * 文件被 IDE 上报为"未索引" → "not_indexed"。
    """
    duration_ms = int((time.monotonic() - start_time) * 1000)

    file_to_findings: dict[str, list[dict[str, Any]]] = {}
    file_status: dict[str, tuple[str, str | None]] = {}
    all_findings: list[dict[str, Any]] = []
    batch_errors: list[BatchError] = []

    for f in requested_files:
        file_status[f] = (STATUS_ANALYZED, None)

    for outcome in batch_outcomes:
        if outcome.error is not None:
            _record_batch_error(outcome, file_status, batch_errors)
        else:
            _record_batch_success(outcome, file_to_findings, all_findings, file_status)

    file_summaries, failed_files, skipped_files, analyzed_count, skipped_count, failed_count = (
        _build_file_summaries(requested_files, file_status, file_to_findings)
    )
    severity_counts = _count_severity(all_findings)

    partial_success = bool(batch_errors) or skipped_count > 0 or failed_count > 0
    # 跳过(not-indexed/unsupported)属于非致命情况;只有失败文件与批次错误才判定整体失败。
    success = failed_count <= 0 and not batch_errors

    notices: list[str] = []
    if batch_errors:
        notices.append(
            f"{len(batch_errors)} batch(es) failed; "
            f"{sum(b.file_count for b in batch_errors)} file(s) affected."
        )

    return AnalysisResult(
        success=success,
        partialSuccess=partial_success,
        idePort=ide_port,
        requestedFileCount=len(requested_files),
        analyzedFileCount=analyzed_count,
        skippedFileCount=skipped_count,
        failedFileCount=failed_count,
        findingCount=len(all_findings),
        severityCounts=severity_counts,
        fileSummaries=file_summaries,
        skippedFiles=skipped_files,
        failedFiles=failed_files,
        batchErrors=batch_errors,
        findings=all_findings,
        durationMs=duration_ms,
        notices=notices,
    )


def _build_file_summaries(
    requested_files: list[str],
    file_status: dict[str, tuple[str, str | None]],
    file_to_findings: dict[str, list[dict[str, Any]]],
) -> tuple[list[FileSummary], list[FailedFile], list[FailedFile], int, int, int]:
    """构建逐文件摘要,并把文件划分为 analyzed/skipped/failed 三类

    Returns:
        (summaries, failed_files, skipped_files, analyzed_count, skipped_count, failed_count)
    """
    summaries: list[FileSummary] = []
    failed_files: list[FailedFile] = []
    skipped_files: list[FailedFile] = []
    analyzed_count = skipped_count = failed_count = 0

    for f in requested_files:
        status, detail = file_status.get(f, (STATUS_ANALYZED, None))
        finding_count = len(file_to_findings.get(os.path.normpath(f), []))
        if status == STATUS_ANALYZED:
            analyzed_count += 1
        elif status in (STATUS_NOT_INDEXED, STATUS_UNSUPPORTED_TYPE):
            skipped_count += 1
            skipped_files.append(
                FailedFile(filePath=f, errorCode=status, errorMessage=detail or status)
            )
        elif status == STATUS_FAILED:
            failed_count += 1
            failed_files.append(
                FailedFile(
                    filePath=f, errorCode="BATCH_ERROR", errorMessage=detail or "batch error"
                )
            )
        summaries.append(
            FileSummary(filePath=f, status=status, findingCount=finding_count, detail=detail)
        )
    return summaries, failed_files, skipped_files, analyzed_count, skipped_count, failed_count


def _extract_marker_path(entry: Any) -> str | None:
    """从逐文件标记项中提取文件路径,兼容 str 或 dict 两种形态"""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        path = entry.get("filePath")
        return path if isinstance(path, str) else None
    return None


def _apply_marker_list(
    entries: Any,
    new_status: str,
    detail: str,
    file_status: dict[str, tuple[str, str | None]],
) -> None:
    """将 ``entries`` 中每个与请求文件匹配的项标记为 ``new_status``"""
    if not isinstance(entries, list):
        return
    for entry in entries:
        path = _extract_marker_path(entry)
        if not isinstance(path, str):
            continue
        np = os.path.normcase(os.path.normpath(path))
        for key in file_status:
            if os.path.normcase(os.path.normpath(key)) == np:
                file_status[key] = (new_status, detail)


def _apply_per_file_markers(
    raw: dict[str, Any],
    file_status: dict[str, tuple[str, str | None]],
) -> None:
    """若 IDE 返回了逐文件标记(例如 'notIndexedFiles'),则应用这些标记

    SonarQube for IDE API 当前未正式文档化这些字段,因此我们容忍几种可能的形态,
    而不假定它们一定存在。
    """
    _apply_marker_list(
        raw.get("notIndexedFiles") or raw.get("not_indexed_files"),
        STATUS_NOT_INDEXED,
        "File not indexed by this IDE instance",
        file_status,
    )
    _apply_marker_list(
        raw.get("unsupportedFiles") or raw.get("unsupported_files"),
        STATUS_UNSUPPORTED_TYPE,
        "File type not supported",
        file_status,
    )


# ---------------------------------------------------------------------------
# 多项目根校验
# ---------------------------------------------------------------------------


def _resolve_existing(path: str) -> str:
    """若 ``path`` 存在则返回其真实路径,否则原样返回"""
    try:
        return os.path.realpath(path, strict=True)
    except OSError:
        return path


def _find_root_for_file(file_path: str, workspace_roots: list[str]) -> str | None:
    """返回包含 ``file_path`` 的 workspace root,未匹配则返回 None"""
    real_f = _resolve_existing(file_path)
    for root in workspace_roots:
        real_root = _resolve_existing(root)
        if _within(real_f, real_root):
            return real_root
    return None


def assert_single_project_root(
    files: list[str],
    workspace_roots: list[str],
) -> str:
    """确保所有文件共享同一个 project root,并返回该 root

    Spec 第 11 节:首版要求所有文件必须归属同一个项目根。
    """
    if not files:
        raise errors.bad_request("No files to analyze.")
    if not workspace_roots:
        raise errors.workspace_not_configured(
            "No workspace roots configured; cannot determine project root."
        )

    roots_found: list[str] = []
    seen: set[str] = set()
    for f in files:
        root = _find_root_for_file(f, workspace_roots)
        if root is None:
            continue
        key = os.path.normcase(root)
        if key not in seen:
            seen.add(key)
            roots_found.append(root)

    if not roots_found:
        raise errors.workspace_violation(
            "None of the provided files are inside the configured workspace roots."
        )
    if len(roots_found) > 1:
        raise errors.multiple_project_roots(
            "Files belong to multiple project roots. Analyze each project separately. "
            f"Roots detected: {sorted(roots_found)}"
        )
    return roots_found[0]


def _within(child: str, parent: str) -> bool:
    nc = os.path.normcase(os.path.normpath(child))
    np_ = os.path.normcase(os.path.normpath(parent)).rstrip(os.sep)
    return nc == np_ or nc.startswith(np_ + os.sep)
