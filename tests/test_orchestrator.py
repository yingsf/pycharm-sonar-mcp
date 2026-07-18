"""Tests for QualityOrchestrator:backend selection, merging, partial success, dedup.

Uses FakeBackend(直接实现 AnalysisBackend)避免依赖真实 MCP / Sonar HTTP。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pycharm_code_quality_mcp import errors
from pycharm_code_quality_mcp.quality.deduplication import DeduplicationMode
from pycharm_code_quality_mcp.quality.models import SourceFinding, UnifiedRange
from pycharm_code_quality_mcp.quality.orchestrator import (
    MODE_ALL,
    MODE_AUTO,
    MODE_JETBRAINS,
    MODE_SONAR,
    QualityOrchestrator,
)

# ---------------------------------------------------------------------------
# Fake backend
# ---------------------------------------------------------------------------


class FakeBackend:
    """可控的 AnalysisBackend 替身"""

    def __init__(
        self,
        name: str,
        *,
        available: bool = True,
        findings: list[SourceFinding] | None = None,
        raise_exc: Exception | None = None,
        success: bool = True,
        failed_files: list[dict[str, Any]] | None = None,
    ) -> None:
        self._name = name
        self._available = available
        self._findings = findings or []
        self._raise = raise_exc
        self._success = success
        self._failed = failed_files or []
        self.analyze_calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def is_available(self) -> bool:
        return self._available

    async def get_status(self) -> dict[str, Any]:
        return {"available": self._available, "name": self._name}

    async def analyze_files(
        self, file_paths: list[str], errors_only: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        self.analyze_calls += 1
        if self._raise is not None:
            raise self._raise
        return {
            "success": self._success,
            "available": True,
            "findings": list(self._findings),
            "raw_findings": [],
            "failed_files": list(self._failed),
            "duration_ms": 10,
            "error": None,
        }


def _sf(
    source: str,
    file_path: str,
    msg: str,
    *,
    line: int = 1,
    rule_id: str | None = None,
    severity: str = "MAJOR",
) -> SourceFinding:
    return SourceFinding(
        source=source,
        ruleId=rule_id,
        severity=severity,
        message=msg,
        filePath=file_path,
        range=UnifiedRange(startLine=line, startColumn=1, endLine=line, endColumn=10),
        raw={},
    )


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


def test_status_no_backends() -> None:
    orch = QualityOrchestrator(jetbrains=None, sonar=None)
    status = asyncio.run(orch.get_status())
    assert status["available"] is False
    assert status["defaultBackend"] == "none"


def test_status_both_available() -> None:
    jb = FakeBackend("jetbrains")
    sn = FakeBackend("sonar")
    orch = QualityOrchestrator(jetbrains=jb, sonar=sn)  # type: ignore[arg-type]
    status = asyncio.run(orch.get_status())
    assert status["available"] is True
    assert status["defaultBackend"] == "jetbrains"
    assert status["backends"]["jetbrains"]["available"] is True
    assert status["backends"]["sonar"]["available"] is True


# ---------------------------------------------------------------------------
# analyze_files:backend selection
# ---------------------------------------------------------------------------


def test_analyze_no_backend_available_returns_failure_result() -> None:
    """auto 模式下两个后端都没注入 => 产出 success=False 的结构化结果(不抛错)"""
    orch = QualityOrchestrator(jetbrains=None, sonar=None)
    result = asyncio.run(orch.analyze_files([], backend_mode=MODE_AUTO))
    assert result.success is False
    assert result.partial_success is False
    assert any("No analysis backend available" in n for n in result.notices)


def test_analyze_no_backend_available_explicit_mode_also_returns_failure() -> None:
    """显式 jetbrains 模式但后端未注入 => 仍产出失败结果"""
    orch = QualityOrchestrator(jetbrains=None, sonar=None)
    result = asyncio.run(orch.analyze_files([], backend_mode=MODE_JETBRAINS))
    assert result.success is False


def test_analyze_jetbrains_only_mode() -> None:
    jb = FakeBackend("jetbrains", findings=[_sf("jetbrains", "/p/a.py", "unused", line=5)])
    sn = FakeBackend("sonar", findings=[_sf("sonar", "/p/a.py", "other")])
    orch = QualityOrchestrator(jetbrains=jb, sonar=sn)  # type: ignore[arg-type]
    result = asyncio.run(
        orch.analyze_files(["/p/a.py"], backend_mode=MODE_JETBRAINS, project_root="/p")
    )
    assert result.success is True
    assert result.backends["jetbrains"].success is True
    assert "sonar" not in result.backends
    assert result.raw_finding_count == 1
    assert result.findings[0].sources == ["jetbrains"]


def test_analyze_sonar_only_mode() -> None:
    jb = FakeBackend("jetbrains", findings=[_sf("jetbrains", "/p/a.py", "unused")])
    sn = FakeBackend("sonar", findings=[_sf("sonar", "/p/a.py", "other")])
    orch = QualityOrchestrator(jetbrains=jb, sonar=sn)  # type: ignore[arg-type]
    result = asyncio.run(
        orch.analyze_files(["/p/a.py"], backend_mode=MODE_SONAR, project_root="/p")
    )
    assert "jetbrains" not in result.backends
    assert result.backends["sonar"].success is True
    assert result.findings[0].sources == ["sonar"]


def test_analyze_auto_merges_both_backends() -> None:
    """auto 模式:两个后端都可用,合并;同一文件的相同问题被去重"""
    jb = FakeBackend(
        "jetbrains",
        findings=[
            _sf("jetbrains", "/p/a.py", "Unused parameter 'x'", line=5, rule_id="UnusedParameter"),
        ],
    )
    sn = FakeBackend(
        "sonar",
        findings=[
            _sf("sonar", "/p/a.py", "Unused parameter 'x'", line=5, rule_id="python:S1172"),
        ],
    )
    orch = QualityOrchestrator(jetbrains=jb, sonar=sn)  # type: ignore[arg-type]
    result = asyncio.run(orch.analyze_files(["/p/a.py"], backend_mode=MODE_AUTO, project_root="/p"))
    assert result.success is True
    assert result.raw_finding_count == 2
    # 同位置同消息 => 合并为 1 条。
    assert result.unique_finding_count == 1
    assert result.duplicates_merged == 1
    finding = result.findings[0]
    assert set(finding.sources) == {"jetbrains", "sonar"}


def test_analyze_auto_sonar_not_installed_not_partial() -> None:
    """auto 模式:Sonar 未注入(未安装),JetBrains 成功 => success=True,非 partial"""
    jb = FakeBackend("jetbrains", findings=[_sf("jetbrains", "/p/a.py", "err")])
    orch = QualityOrchestrator(jetbrains=jb, sonar=None)  # type: ignore[arg-type]
    result = asyncio.run(orch.analyze_files(["/p/a.py"], backend_mode=MODE_AUTO, project_root="/p"))
    assert result.success is True
    assert result.partial_success is False
    assert "jetbrains" in result.backends


def test_analyze_auto_jetbrains_down_sonar_succeeds_degraded() -> None:
    """auto 模式:JetBrains 探测失败,Sonar 兜底 => degradedMode=True"""
    jb = FakeBackend("jetbrains", available=False)
    sn = FakeBackend("sonar", findings=[_sf("sonar", "/p/a.py", "err")])
    orch = QualityOrchestrator(jetbrains=jb, sonar=sn)  # type: ignore[arg-type]
    result = asyncio.run(orch.analyze_files(["/p/a.py"], backend_mode=MODE_AUTO, project_root="/p"))
    assert result.success is True
    # JetBrains 在 _resolve_runnable 阶段被剔除,只有 sonar 跑。
    assert "jetbrains" not in result.backends
    assert result.backends["sonar"].success is True
    assert result.degraded_mode is True


def test_analyze_all_mode_partial_when_one_fails() -> None:
    """all 模式:一个后端失败 => partialSuccess=True"""
    jb = FakeBackend("jetbrains", raise_exc=errors.jetbrains_tool_failed("boom"))
    sn = FakeBackend("sonar", findings=[_sf("sonar", "/p/a.py", "err")])
    orch = QualityOrchestrator(jetbrains=jb, sonar=sn)  # type: ignore[arg-type]
    result = asyncio.run(orch.analyze_files(["/p/a.py"], backend_mode=MODE_ALL, project_root="/p"))
    assert result.partial_success is True
    assert result.backends["jetbrains"].success is False
    assert result.backends["sonar"].success is True
    # Sonar 的那条 finding 仍在。
    assert result.unique_finding_count == 1


def test_analyze_invalid_backend_mode_rejected() -> None:
    orch = QualityOrchestrator(jetbrains=None, sonar=None)
    with pytest.raises(errors.SonarMcpError):
        asyncio.run(orch.analyze_files([], backend_mode="invalid"))


def test_analyze_severity_counts() -> None:
    jb = FakeBackend(
        "jetbrains",
        findings=[
            _sf("jetbrains", "/p/a.py", "e1", severity="ERROR"),
            _sf("jetbrains", "/p/a.py", "e2", line=2, severity="WARNING"),
        ],
    )
    orch = QualityOrchestrator(jetbrains=jb, sonar=None)  # type: ignore[arg-type]
    result = asyncio.run(
        orch.analyze_files(["/p/a.py"], backend_mode=MODE_JETBRAINS, project_root="/p")
    )
    # ERROR -> CRITICAL, WARNING -> MAJOR
    assert result.severity_counts.get("CRITICAL") == 1
    assert result.severity_counts.get("MAJOR") == 1


def test_analyze_dedup_off_keeps_all() -> None:
    jb = FakeBackend(
        "jetbrains",
        findings=[
            _sf("jetbrains", "/p/a.py", "dup", line=5),
            _sf("jetbrains", "/p/a.py", "dup", line=5),
        ],
    )
    orch = QualityOrchestrator(jetbrains=jb, sonar=None)  # type: ignore[arg-type]
    result = asyncio.run(
        orch.analyze_files(
            ["/p/a.py"],
            backend_mode=MODE_JETBRAINS,
            deduplication_mode=DeduplicationMode.OFF,
            project_root="/p",
        )
    )
    assert result.raw_finding_count == 2
    assert result.unique_finding_count == 2
    assert result.duplicates_merged == 0


def test_analyze_requested_file_count_preserved() -> None:
    """调用方过滤后 file_paths 变少时,requestedFileCount 仍记原始数"""
    jb = FakeBackend("jetbrains", findings=[])
    orch = QualityOrchestrator(jetbrains=jb, sonar=None)  # type: ignore[arg-type]
    result = asyncio.run(
        orch.analyze_files(
            ["/p/a.py"],
            backend_mode=MODE_JETBRAINS,
            project_root="/p",
            requested_file_count=7,
        )
    )
    assert result.requested_file_count == 7
