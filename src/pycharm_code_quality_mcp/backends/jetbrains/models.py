"""JetBrains MCP 后端的 Pydantic 模型

用于描述 JetBrains MCP Server 的 inspection 结果与批量分析结果。
保留原始字段,容忍未知的额外字段,使得未来 IDE 版本升级不破坏解析。
行列号遵循 JetBrains 返回的 1-based 语义,本模块不做转换。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# 单条问题
# ---------------------------------------------------------------------------


class JetBrainsProblem(BaseModel):
    """JetBrains MCP 返回的单条问题(inspection finding)

    Attributes:
        source: 固定为 "jetbrains",用于区分 Sonar 后端。
        inspection_id: 检查的标识符(对应 IDE inspection id),可能为空。
        severity: 严重级别(JetBrains 的 InspectionSeverity 字符串)。
        description: 面向人类的问题描述。
        file_path: 绝对文件路径。
        start_line: 起始行(1-based)。
        start_column: 起始列(1-based)。
        end_line: 结束行(1-based)。
        end_column: 结束列(1-based)。
        raw: 保留原始 dict,用于前向兼容字段访问。
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    source: str = "jetbrains"
    inspection_id: str | None = Field(default=None, alias="inspectionId")
    severity: str
    description: str
    file_path: str = Field(alias="filePath")
    start_line: int = Field(alias="startLine")
    start_column: int = Field(alias="startColumn")
    end_line: int = Field(alias="endLine")
    end_column: int = Field(alias="endColumn")
    raw: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 批量分析结果
# ---------------------------------------------------------------------------


class FailedFile(BaseModel):
    """批量分析中单个文件失败的信息"""

    model_config = ConfigDict(populate_by_name=True)

    file_path: str = Field(alias="filePath")
    error_code: str = Field(alias="errorCode")
    error_message: str = Field(alias="errorMessage")


class JetBrainsAnalysisResult(BaseModel):
    """JetBrainsBackend.analyze_files 的返回结果

    Attributes:
        success: 是否完全成功(无文件失败、项目不在 indexing、无致命错误)。
        problems: 所有文件合并后的 JetBrainsProblem 列表。
        failed_files: 单文件失败的列表(不影响其他文件)。
        project_indexing: PyCharm 是否仍在 indexing(若为 True 建议稍后重试)。
        duration_ms: 本次批量分析的耗时(毫秒)。
        error: 致命错误的可读消息;非致命时为 None。
    """

    model_config = ConfigDict(populate_by_name=True)

    success: bool
    problems: list[JetBrainsProblem] = Field(default_factory=list)
    failed_files: list[FailedFile] = Field(default_factory=list, alias="failedFiles")
    project_indexing: bool = Field(default=False, alias="projectIndexing")
    duration_ms: int = Field(default=0, alias="durationMs")
    error: str | None = None
