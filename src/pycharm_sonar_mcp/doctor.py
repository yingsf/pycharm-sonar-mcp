"""doctor:环境诊断,不启动 MCP stdio 服务

所有检查都不依赖外部 shell 工具(不使用 lsof/grep/sed/awk/netstat/PowerShell/Bash)。
端口扫描与 HTTP 探针复用与正式工具相同的 SonarClient 与发现逻辑,使 doctor 覆盖真实代码路径。
"""

from __future__ import annotations

import io
import os
import platform
import shutil
import socket
import sys
from collections.abc import Sequence
from typing import Any

from . import __version__
from .ide_discovery import PORT_MAX, PORT_MIN, IdeDiscovery, get_global_cache
from .logging_config import get_logger
from .sonar_client import SonarClient

_log = get_logger("doctor")


def _ide_name_of(status: dict[str, Any]) -> str:
    """从 Sonar status 对象中尽力推断 IDE 显示名"""
    return status.get("ideName") or status.get("ide") or status.get("productName") or "<unknown>"


def _report_environment(report: _Report, env: dict[str, str]) -> None:
    """报告 OS/架构/版本/路径/代理等基础信息"""
    os_name, os_version = _os_info()
    report.ok(f"Operating system: {os_name} {os_version} {_arch()}")
    exe_path = _safe_exe_path()
    report.ok(f"Program: {exe_path}")
    report.ok(f"MCP version: {__version__}")
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    report.ok(f"Python: {platform.python_implementation()} {py_ver}")
    report.ok(f"Install dir: {os.path.dirname(exe_path)}")
    report.ok(f"Path separator: {os.pathsep!r}")

    ipv4_ok, ipv4_detail = _check_ipv4_loopback()
    (report.ok if ipv4_ok else report.fail)(f"localhost IPv4 loopback: {ipv4_detail}")

    proxy_vars = [k for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY") if env.get(k)]
    if proxy_vars:
        report.warn(
            "Proxy env detected",
            f"{','.join(proxy_vars)} set (will be ignored for loopback; trust_env=False)",
        )
    else:
        report.ok("Proxy env: none")


def _report_sonar_instances(report: _Report, env: dict[str, str]) -> list[Any]:
    """扫描 Sonar 实例并报告,返回发现的实例列表"""
    sonar = SonarClient()
    try:
        discovery = IdeDiscovery(sonar, env=env, cache=get_global_cache())
        instances = discovery.discover_all_instances()
    except Exception as e:
        report.fail("Sonar IDE discovery", f"unexpected error: {e}")
        return []
    finally:
        sonar.close()

    if not instances:
        report.fail(
            "SonarQube for IDE",
            f"no instance on ports {PORT_MIN}..{PORT_MAX}; open PyCharm with the plugin",
        )
        return instances

    ports = ", ".join(str(i.port) for i in instances)
    report.ok(f"SonarQube for IDE found: ports {ports}")
    if len(instances) > 1:
        report.warn(
            "Multiple IDE instances",
            f"{len(instances)} instances on {ports} — may need SONAR_IDE_PORT",
        )
    for inst in instances:
        report.ok(f"  port {inst.port}: {_ide_name_of(inst.status)}")
    return instances


def _report_tools(report: _Report, env: dict[str, str]) -> None:
    """报告 git/codex/claude 是否存在以及工作区配置"""
    for name, label, missing in (
        ("git", "Git", "not on PATH (sonar_analyze_git_changes will be unavailable)"),
        ("codex", "Codex CLI", "not found (optional)"),
        ("claude", "Claude Code", "not found (optional)"),
    ):
        if _has_executable(name):
            report.ok(f"{label}: found")
        else:
            report.warn(label, missing)

    roots_env = env.get("SONAR_WORKSPACE_ROOTS", "")
    if roots_env:
        report.ok(f"SONAR_WORKSPACE_ROOTS: {roots_env}")
    else:
        report.info("SONAR_WORKSPACE_ROOTS", "not set (will rely on MCP client Roots)")


def run_doctor(
    *,
    file_path: str | None = None,
    stream: Any = None,
    env: dict[str, str] | None = None,
) -> int:
    """运行全部检查,把结果写入 ``stream``,返回退出码(0=正常,1=有问题)"""
    if stream is None:
        stream = sys.stdout
    env = env if env is not None else dict(os.environ)

    report = _Report(_Writer(stream))
    report.line("PyCharm Sonar MCP Doctor")
    report.line("")

    _report_environment(report, env)
    instances = _report_sonar_instances(report, env)

    if instances:
        auth_ok, auth_detail = _check_authority(instances[0].port)
        (report.ok if auth_ok else report.fail)(f"HTTP authority (localhost): {auth_detail}")

    _report_file_probe(report, file_path, instances)
    _report_tools(report, env)

    report.line("")
    report.line(f"Result: {report.failures} failure(s), {report.warns} warning(s)")
    return 1 if report.failures else 0


def _report_file_probe(report: _Report, file_path: str | None, instances: list[Any]) -> None:
    """报告可选的目标文件索引探针"""
    if file_path and instances:
        ok_file, detail = _check_file_indexed(file_path, instances)
        (report.ok if ok_file else report.warn)(f"Target file indexed: {detail}")
    elif file_path and not instances:
        report.warn("Target file check skipped", "no Sonar IDE instance available")
    elif file_path is None:
        report.info("Target file", "not provided (use --file to test indexing)")


# ---------------------------------------------------------------------------
# 单项检查
# ---------------------------------------------------------------------------


def _os_info() -> tuple[str, str]:
    if sys.platform.startswith("win"):
        return "Windows", platform.version() or platform.release()
    if sys.platform == "darwin":
        return "macOS", platform.mac_ver()[0] or platform.release()
    return platform.system() or sys.platform, platform.release()


def _arch() -> str:
    machine = platform.machine() or os.uname().machine if hasattr(os, "uname") else ""
    # 归一化常见的架构拼写。
    m = (machine or "").lower()
    if m in {"arm64", "aarch64"}:
        return "arm64"
    if m in {"x86_64", "amd64"}:
        return "x64"
    return machine or "unknown"


def _safe_exe_path() -> str:
    try:
        return os.path.abspath(sys.argv[0] or sys.executable)
    except Exception:
        return sys.executable


def _check_ipv4_loopback() -> tuple[bool, str]:
    """在 127.0.0.1 上打开回环套接字以确认 IPv4 回环可用"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        addr = s.getsockname()
        s.close()
        return True, f"bound 127.0.0.1:{addr[1]} ok"
    except OSError as e:
        return False, f"cannot bind 127.0.0.1: {e}"


def _check_authority(port: int) -> tuple[bool, str]:
    """验证 status 接口接受 localhost authority(不返回 421)"""
    sonar = SonarClient(connect_timeout=0.8, read_timeout=4.0)
    try:
        status = sonar.get_status(port, timeout=4.0)
        ide = status.get("ideName") or status.get("ide") or "ok"
        return True, f"status OK ({ide})"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        sonar.close()


def _check_file_indexed(file_path: str, instances: Sequence[Any]) -> tuple[bool, str]:
    """探查是否有任何实例索引了该文件"""
    if not os.path.isfile(file_path):
        return False, f"not a regular file: {file_path}"
    sonar = SonarClient(connect_timeout=0.8, read_timeout=10.0)
    try:
        for inst in instances:
            try:
                result = sonar.analyze_files(inst.port, [file_path], timeout=15.0)
                findings = result.get("findings") or []
                return True, f"analyzed by port {inst.port} ({len(findings)} findings)"
            except Exception as e:
                _log.debug("probe on port %s failed: %s", inst.port, e)
                continue
        return False, "no instance analyzed this file"
    finally:
        sonar.close()


def _has_executable(name: str) -> bool:
    """判断 `name` 是否在 PATH 上,使用 shutil.which(不调用 shell)"""
    return shutil.which(name) is not None


# ---------------------------------------------------------------------------
# 输出辅助
# ---------------------------------------------------------------------------


class _Writer:
    def __init__(self, stream: Any) -> None:
        self._s = stream

    def line(self, text: str = "") -> None:
        try:
            self._s.write(text + "\n")
            self._s.flush()
        except Exception:
            # 即便控制台无法渲染某些 Unicode(少数 Windows GBK 环境),也绝不崩溃。
            try:
                safe = text.encode("ascii", "replace").decode("ascii")
                self._s.write(safe + "\n")
                self._s.flush()
            except Exception:
                pass

    def kv(self, level: str, label: str, detail: str = "") -> None:
        tag = f"[{level}]"
        msg = f"{tag} {label}"
        if detail:
            msg += f": {detail}"
        self.line(msg)


class _Report:
    """累积 doctor 检查结果并通过 `_Writer` 写出"""

    def __init__(self, writer: _Writer) -> None:
        self._writer = writer
        self.failures = 0
        self.warns = 0

    def line(self, text: str = "") -> None:
        self._writer.line(text)

    def info(self, label: str, detail: str = "") -> None:
        self._writer.kv("INFO", label, detail)

    def ok(self, label: str, detail: str = "") -> None:
        self._writer.kv("OK", label, detail)

    def warn(self, label: str, detail: str = "") -> None:
        self.warns += 1
        self._writer.kv("WARN", label, detail)

    def fail(self, label: str, detail: str = "") -> None:
        self.failures += 1
        self._writer.kv("FAIL", label, detail)


# 便于测试中用 StringIO 调用 run_doctor。
def run_doctor_to_string(*, file_path: str | None = None, env: dict[str, str] | None = None) -> str:
    buf = io.StringIO()
    run_doctor(file_path=file_path, stream=buf, env=env)
    return buf.getvalue()
