"""用于 SonarQube for IDE 响应与 MCP 工具结果的 Pydantic 模型

保留原始 Sonar 字段（ruleKey、message、severity、filePath、textRange），
并容忍未知的额外字段，使得未来插件版本不会破坏解析。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Sonar IDE API 响应模型
# ---------------------------------------------------------------------------


class TextRange(BaseModel):
    """表示 Sonar 文本范围，行内偏移均为从零起算的字节级偏移"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    start_line: int = Field(alias="startLine")
    start_line_offset: int = Field(alias="startLineOffset")
    end_line: int = Field(alias="endLine")
    end_line_offset: int = Field(alias="endLineOffset")


class Finding(BaseModel):
    """表示单条 Sonar 发现结果

    `extra` 通过 model_config 的 extra="allow" 收集，因此在模型重新序列化时
    未知字段会被保留。下方五个规范字段始终存在。
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    rule_key: str = Field(alias="ruleKey")
    message: str
    severity: str
    file_path: str = Field(alias="filePath")
    text_range: TextRange | None = Field(default=None, alias="textRange")


class AnalysisResponse(BaseModel):
    """POST /sonarlint/api/analysis/files 的响应体"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    findings: list[Finding] = Field(default_factory=list)


class IdeStatus(BaseModel):
    """GET /sonarlint/api/status 的响应体

    SonarQube for IDE 的状态对象在不同版本间存在差异，因此接受任意额外字段。
    启发式校验位于 `ide_discovery.looks_like_sonar_status`。
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ---------------------------------------------------------------------------
# 工具结果模型
# ---------------------------------------------------------------------------

# 单文件分析状态取值，属于公开契约的一部分。
STATUS_ANALYZED = "analyzed"
STATUS_NOT_FOUND = "not_found"
STATUS_NOT_REGULAR = "not_regular"
STATUS_OUTSIDE_WORKSPACE = "outside_workspace"
STATUS_SYMLINK_ESCAPE = "symlink_escape"
STATUS_NOT_INDEXED = "not_indexed"
STATUS_UNSUPPORTED_TYPE = "unsupported_type"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"


class InstanceInfo(BaseModel):
    """表示一个已发现的 SonarQube for IDE 实例"""

    model_config = ConfigDict(populate_by_name=True)

    port: int
    status: dict[str, Any] = Field(default_factory=dict)


class IdeStatusResult(BaseModel):
    """Sonar 后端 IDE 状态探测的结果(供 code_quality_status 内部使用)"""

    model_config = ConfigDict(populate_by_name=True)

    available: bool
    instance_count: int = Field(alias="instanceCount")
    instances: list[InstanceInfo] = Field(default_factory=list)


class FileSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    file_path: str = Field(alias="filePath")
    status: str
    finding_count: int = Field(default=0, alias="findingCount")
    detail: str | None = None


class BatchError(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    batch_index: int = Field(alias="batchIndex")
    file_count: int = Field(alias="fileCount")
    error_code: str = Field(alias="errorCode")
    error_message: str = Field(alias="errorMessage")


class FailedFile(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    file_path: str = Field(alias="filePath")
    error_code: str = Field(alias="errorCode")
    error_message: str = Field(alias="errorMessage")


class AnalysisResult(BaseModel):
    """Sonar 后端单次分析(文件或 git 变更)的统一结果"""

    model_config = ConfigDict(populate_by_name=True)

    success: bool
    partial_success: bool = Field(default=False, alias="partialSuccess")
    ide_port: int | None = Field(default=None, alias="idePort")
    requested_file_count: int = Field(alias="requestedFileCount")
    analyzed_file_count: int = Field(default=0, alias="analyzedFileCount")
    skipped_file_count: int = Field(default=0, alias="skippedFileCount")
    failed_file_count: int = Field(default=0, alias="failedFileCount")
    finding_count: int = Field(default=0, alias="findingCount")
    severity_counts: dict[str, int] = Field(default_factory=dict, alias="severityCounts")
    file_summaries: list[FileSummary] = Field(default_factory=list, alias="fileSummaries")
    skipped_files: list[FailedFile] = Field(default_factory=list, alias="skippedFiles")
    failed_files: list[FailedFile] = Field(default_factory=list, alias="failedFiles")
    batch_errors: list[BatchError] = Field(default_factory=list, alias="batchErrors")
    findings: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: int = Field(default=0, alias="durationMs")

    # git 专用附加字段（普通文件分析时为空）
    project_root: str | None = Field(default=None, alias="projectRoot")
    base_ref: str | None = Field(default=None, alias="baseRef")
    changed_file_count: int | None = Field(default=None, alias="changedFileCount")

    # 给模型的非致命诊断消息
    notices: list[str] = Field(default_factory=list)


class ClearCacheResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    cleared: bool
    cleared_ports: list[int] = Field(default_factory=list, alias="clearedPorts")
    message: str
