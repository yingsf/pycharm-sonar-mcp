"""统一问题模型

把来自不同后端(SonarQube for IDE / JetBrains inspections)的问题
统一表达为一组 Pydantic v2 模型,作为 quality/ 模块的对外数据契约。

风格与 ``backends/sonar/models.py`` 一致:
  * ``model_config = ConfigDict(extra="allow", populate_by_name=True)``
  * 字段用 alias 驼峰命名,Python 侧用 snake_case。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _cfg() -> ConfigDict:
    """统一 model_config:容忍额外字段,并允许按字段名或 alias 构造"""
    return ConfigDict(extra="allow", populate_by_name=True)


# ---------------------------------------------------------------------------
# 范围与单源问题
# ---------------------------------------------------------------------------


class UnifiedRange(BaseModel):
    """统一的文本范围,所有坐标均为 1-based"""

    model_config = _cfg()

    start_line: int = Field(alias="startLine")
    start_column: int = Field(alias="startColumn")
    end_line: int = Field(alias="endLine")
    end_column: int = Field(alias="endColumn")


class SourceFinding(BaseModel):
    """来自单个后端的一条原始问题"""

    model_config = _cfg()

    source: str
    rule_id: str | None = Field(default=None, alias="ruleId")
    severity: str
    message: str
    file_path: str = Field(alias="filePath")
    range: UnifiedRange | None = Field(default=None)
    # 原始后端响应(只读留存,便于排查),容忍任意结构。
    raw: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 去重元信息
# ---------------------------------------------------------------------------


class DeduplicationInfo(BaseModel):
    """单条 UnifiedFinding 的去重结果元信息"""

    model_config = _cfg()

    merged: bool = False
    confidence: float = 0.0
    reason: list[str] = Field(default_factory=list)


class UnifiedFinding(BaseModel):
    """跨后端去重后的统一问题"""

    model_config = _cfg()

    id: str
    sources: list[str] = Field(default_factory=list)
    # key: "jetbrains" / "sonar";value: 该来源下的规则 id 列表。
    rule_ids: dict[str, list[str]] = Field(default_factory=dict, alias="ruleIds")
    severity: str
    severity_rank: int
    message: str
    category: str
    file_path: str = Field(alias="filePath")
    range: UnifiedRange | None = Field(default=None)
    duplicate_count: int = Field(default=1, alias="duplicateCount")
    deduplication: DeduplicationInfo = Field(default_factory=DeduplicationInfo)
    source_findings: list[SourceFinding] = Field(default_factory=list, alias="sourceFindings")


# ---------------------------------------------------------------------------
# 后端状态
# ---------------------------------------------------------------------------


class BackendStatus(BaseModel):
    """单个后端在一次分析中的执行状态"""

    model_config = _cfg()

    attempted: bool = False
    available: bool = False
    success: bool = False
    finding_count: int = Field(default=0, alias="findingCount")
    duration_ms: int = Field(default=0, alias="durationMs")
    error: str | None = None


# ---------------------------------------------------------------------------
# 顶层分析结果
# ---------------------------------------------------------------------------


class QualityAnalysisResult(BaseModel):
    """一次代码质量分析的完整结果"""

    model_config = _cfg()

    success: bool
    partial_success: bool = Field(default=False, alias="partialSuccess")
    degraded_mode: bool = Field(default=False, alias="degradedMode")

    requested_file_count: int = Field(default=0, alias="requestedFileCount")
    analyzed_file_count: int = Field(default=0, alias="analyzedFileCount")

    raw_finding_count: int = Field(default=0, alias="rawFindingCount")
    unique_finding_count: int = Field(default=0, alias="uniqueFindingCount")
    duplicates_merged: int = Field(default=0, alias="duplicatesMerged")
    possible_duplicate_count: int = Field(default=0, alias="possibleDuplicateCount")
    deduplication_mode: str = Field(default="balanced", alias="deduplicationMode")

    severity_counts: dict[str, int] = Field(default_factory=dict, alias="severityCounts")
    backends: dict[str, BackendStatus] = Field(default_factory=dict)
    file_summaries: list[dict[str, Any]] = Field(default_factory=list, alias="fileSummaries")

    findings: list[UnifiedFinding] = Field(default_factory=list)
    # 去重组(已合并)与疑似重复组(未合并,需人工确认)。
    deduplication_groups: list[dict[str, Any]] = Field(
        default_factory=list, alias="deduplicationGroups"
    )
    possible_duplicate_groups: list[dict[str, Any]] = Field(
        default_factory=list, alias="possibleDuplicateGroups"
    )

    notices: list[str] = Field(default_factory=list)
    duration_ms: int = Field(default=0, alias="durationMs")


__all__ = [
    "BackendStatus",
    "DeduplicationInfo",
    "QualityAnalysisResult",
    "SourceFinding",
    "UnifiedFinding",
    "UnifiedRange",
]
