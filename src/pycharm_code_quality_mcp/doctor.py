"""doctor:环境诊断,不启动 MCP stdio 服务

输出分为三段:
  General   —— OS / 架构 / 版本 / 路径 / Git / Codex / Claude / workspace roots。
  JetBrains —— 配置 / loopback / initialize / tools/list / 项目 indexing / 文件探针。
  Sonar     —— 插件探测 / 端口扫描 / authority / 文件探针。

所有检查都不依赖外部 shell 工具(不使用 lsof/grep/sed/awk/netstat/PowerShell/Bash)。
端口扫描与 HTTP 探针复用与正式工具相同的 client 与发现逻辑,使 doctor 覆盖真实代码路径。

Sonar 未安装时 doctor 不应整体失败;JetBrains 未配置但 Sonar 可用时输出 degraded 警告。
"""

from __future__ import annotations

import asyncio
import io
import os
import pathlib
import platform
import re
import shutil
import socket
import sys
from collections.abc import Iterator, Sequence
from typing import Any

from . import __version__
from .backends.sonar.client import SonarClient
from .backends.sonar.discovery import PORT_MAX, PORT_MIN, IdeDiscovery, get_global_cache
from .logging_config import get_logger

_log = get_logger("doctor")


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
    # 保留旧标题(测试与现有用户脚本可能依赖该字符串)。
    report.line("PyCharm Sonar MCP Doctor")
    report.line("")

    # ---------------- General ----------------
    report.line("== General ==")
    _report_environment(report, env)
    _report_tools(report, env)
    _report_noqa_style(report, env, file_path)
    report.line("")

    # ---------------- JetBrains ----------------
    report.line("== JetBrains ==")
    jb_ok = _report_jetbrains(report, env, file_path)
    report.line("")

    # ---------------- Sonar ----------------
    report.line("== Sonar ==")
    sonar_instances = _report_sonar_instances(report, env)
    if sonar_instances:
        auth_ok, auth_detail = _check_authority(sonar_instances[0].port)
        (report.ok if auth_ok else report.fail)(f"HTTP authority (localhost): {auth_detail}")
    _report_file_probe(report, file_path, sonar_instances)
    report.line("")

    # ---------------- 总评 ----------------
    report.line("== Summary ==")
    if jb_ok and not report.failures:
        report.ok("Code quality analysis available through JetBrains inspections")
    elif jb_ok:
        report.ok("JetBrains inspections available (see warnings above)")
    elif sonar_instances:
        report.warn(
            "Degraded mode",
            "JetBrains not available; falling back to SonarQube for IDE as the only backend.",
        )
    else:
        report.fail(
            "No analysis backend available",
            "Configure JetBrains MCP or open PyCharm with the SonarQube for IDE plugin.",
        )
    report.line("")
    report.line(f"Result: {report.failures} failure(s), {report.warns} warning(s)")
    return 1 if report.failures else 0


# ---------------------------------------------------------------------------
# General
# ---------------------------------------------------------------------------


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


def _report_tools(report: _Report, env: dict[str, str]) -> None:
    """报告 git/codex/claude 是否存在以及工作区配置"""
    for name, label, missing in (
        ("git", "Git", "not on PATH (analyze_git_changes will be unavailable)"),
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


# ruff 风格的 noqa 后跟 Sonar 规则号(S 开头 + 数字),例如 "noqa: S3776"。
# SonarQube for IDE 不识别这种格式(它只认整行全大写的 nosonar 抑制符),所以这类
# ruff 风格的注释对 Sonar 静默失效。
# 注意:模式字符串拆开拼接,避免 ruff 把它当成真正的 noqa 指令解析。
# 该正则基于文本扫描,不解析 Python 语法,因此测试夹具里写成字符串字面量的
# ruff 风格 noqa 也会被匹配 —— 这是预期行为(用户可忽略此类已知夹具)。
_RUFF_NOQA_SONAR_RE = re.compile("#" + r"\s*" + "noqa" + r"\b[^\n]*\bS\d+", re.IGNORECASE)
# 扫描时要跳过的目录(噪声:虚拟环境、构建产物、缓存等)。
_NOQA_SCAN_SKIP_DIRS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "venv",
    }
)
_NOQA_PROJECT_MARKERS = frozenset(
    {
        ".git",
        ".hg",
        "noxfile.py",
        "pyproject.toml",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
        "tox.ini",
    }
)
_NOQA_SCAN_MAX_FILES = 5000


def _report_noqa_style(report: _Report, env: dict[str, str], file_path: str | None) -> None:
    """扫描项目 Python 文件,检测 ruff 风格 `# noqa: Sxxx` 与 Sonar 不兼容的情况"""
    try:
        roots, skip_reason = _noqa_scan_roots(env, file_path)
        if not roots:
            report.info("Noqa style", f"skipped ({skip_reason})")
            return

        report.info("Python scan root", ", ".join(str(root) for root in roots))
        hits: list[tuple[str, int]] = []  # (相对路径, 行号)
        scanned = 0
        truncated = False
        for root in roots:
            for py in _iter_noqa_python_files(root):
                if scanned >= _NOQA_SCAN_MAX_FILES:
                    truncated = True
                    break
                rel = _relative_display_path(py, root)
                # 跳过噪声目录下的文件。
                if any(part in _NOQA_SCAN_SKIP_DIRS for part in pathlib.Path(rel).parts[:-1]):
                    continue
                scanned += 1
                try:
                    text = py.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for i, line in enumerate(text.splitlines(), start=1):
                    if _RUFF_NOQA_SONAR_RE.search(line):
                        hits.append((rel, i))
            if truncated:
                break
        if truncated and not hits:
            report.warn(
                "Noqa style",
                f"scan stopped after {_NOQA_SCAN_MAX_FILES} Python file(s); no conflict found before limit",
            )
            return
        if not hits:
            report.ok("Noqa style", f"no ruff-style '# noqa: S...' in {scanned} Python file(s)")
            return
        files = {h[0] for h in hits}
        first = hits[0]
        suffix = f"; scan stopped after {_NOQA_SCAN_MAX_FILES} Python file(s)" if truncated else ""
        report.warn(
            "Noqa style conflict",
            f"found {len(hits)} '# noqa: S...' comment(s) in {len(files)} file(s){suffix}; "
            f"SonarQube for IDE ignores these — use '# NOSONAR' instead. "
            f"First: {first[0]}:{first[1]}",
        )
    except Exception as e:  # pragma: no cover - 防御性
        report.warn("Noqa style", f"scan failed: {e}")


def _noqa_scan_roots(env: dict[str, str], file_path: str | None) -> tuple[list[pathlib.Path], str]:
    if file_path:
        target = pathlib.Path(file_path).expanduser()
        if target.is_file():
            return _existing_unique_dirs(
                [target.parent]
            ), "--file parent is not an existing directory"

    env_roots = _existing_unique_dirs(_raw_workspace_roots(env))
    if env_roots:
        return env_roots, f"no existing {env.get('SONAR_WORKSPACE_ROOTS', 'workspace root')}"

    cwd = pathlib.Path(os.getcwd())
    if _looks_like_project_dir(cwd):
        return _existing_unique_dirs([cwd]), "cwd is not an existing directory"
    return (
        [],
        "cwd is not a project directory; set SONAR_WORKSPACE_ROOTS or run doctor from a project root",
    )


def _raw_workspace_roots(env: dict[str, str]) -> list[pathlib.Path]:
    raw = env.get("SONAR_WORKSPACE_ROOTS", "").strip()
    if not raw:
        return []
    return [
        pathlib.Path(part.strip()).expanduser() for part in raw.split(os.pathsep) if part.strip()
    ]


def _existing_unique_dirs(paths: list[pathlib.Path]) -> list[pathlib.Path]:
    roots: list[pathlib.Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            root = path.resolve()
        except OSError:
            root = path.absolute()
        if not root.is_dir():
            continue
        key = os.path.normcase(os.path.normpath(str(root)))
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return roots


def _looks_like_project_dir(path: pathlib.Path) -> bool:
    if _same_dir(path, pathlib.Path.home()):
        return False
    return any((path / marker).exists() for marker in _NOQA_PROJECT_MARKERS)


def _same_dir(left: pathlib.Path, right: pathlib.Path) -> bool:
    try:
        left_s = str(left.resolve())
    except OSError:
        left_s = str(left.absolute())
    try:
        right_s = str(right.resolve())
    except OSError:
        right_s = str(right.absolute())
    return os.path.normcase(os.path.normpath(left_s)) == os.path.normcase(os.path.normpath(right_s))


def _iter_noqa_python_files(root: pathlib.Path) -> Iterator[pathlib.Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _skip_noqa_dir(d)]
        for filename in filenames:
            if filename.endswith(".py"):
                yield pathlib.Path(dirpath) / filename


def _skip_noqa_dir(dirname: str) -> bool:
    if dirname in _NOQA_SCAN_SKIP_DIRS:
        return True
    return dirname.startswith(".")


def _relative_display_path(path: pathlib.Path, root: pathlib.Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# JetBrains
# ---------------------------------------------------------------------------


def _report_jetbrains(report: _Report, env: dict[str, str], file_path: str | None) -> bool:
    """报告 JetBrains MCP 配置/连接/项目状态;返回 True 表示可用

    未配置时输出明确警告但不抛异常(仍允许 Sonar 单独工作)。
    """
    _ = env
    from .backends.jetbrains import config as jb_config

    cfg = jb_config.load_config()
    if cfg is None:
        report.info(
            "JetBrains MCP",
            "not configured. Run: pycharm-code-quality-mcp jetbrains configure",
        )
        return False

    report.ok(f"JetBrains MCP configured: {cfg.url}")
    # loopback 校验。
    if jb_config.is_loopback_url(cfg.url):
        report.ok("URL is loopback")
    else:
        report.fail("URL is NOT loopback", f"refusing to connect to {cfg.url}")
        return False

    # 真实连接 + initialize + tools/list + get_project_status。
    status = _probe_jetbrains_status(cfg)
    if status is None:
        report.fail("JetBrains MCP connect", "failed (see stderr for details)")
        return False
    if not status.get("available"):
        err = status.get("error") or "unreachable"
        report.fail("JetBrains MCP connect", err)
        return False

    tools = status.get("tools") or []
    if tools:
        report.ok(f"Tools exposed: {', '.join(tools)}")
    indexing = bool(status.get("indexing"))
    if indexing:
        report.warn("Project indexing", "PyCharm is still indexing; results may be incomplete")
    else:
        report.ok("Project ready")

    # 可选文件探针:用 jetbrains_inspect_files 走一次。
    if file_path:
        ok_file, detail = _probe_jetbrains_file(cfg, file_path)
        if ok_file:
            report.ok(f"Target file inspected by JetBrains: {detail}")
        else:
            report.warn(f"JetBrains target file: {detail}")
    return True


def _probe_jetbrains_status(cfg: Any) -> dict[str, Any] | None:
    """真实探测 JetBrains MCP 状态;异常时返回 None(避免污染 doctor 主流程)"""
    from .backends.jetbrains.analyzer import JetBrainsAnalysisBackend

    async def _go() -> dict[str, Any]:
        backend = JetBrainsAnalysisBackend(cfg)
        return await backend.get_status()

    try:
        return asyncio.run(_go())
    except Exception as e:  # pragma: no cover - defensive
        _log.debug("JetBrains status probe raised: %s", e)
        return None


def _probe_jetbrains_file(cfg: Any, file_path: str) -> tuple[bool, str]:
    """用 jetbrains_inspect_files 探测单个文件能否被分析"""
    if not os.path.isfile(file_path):
        return False, f"not a regular file: {file_path}"
    from .backends.jetbrains.analyzer import JetBrainsAnalysisBackend
    from .backends.jetbrains.models import JetBrainsAnalysisResult

    async def _go() -> JetBrainsAnalysisResult:
        backend = JetBrainsAnalysisBackend(cfg)
        return await backend.backend.analyze_files([file_path])

    try:
        result = asyncio.run(_go())
    except Exception as e:
        return False, f"inspection raised: {type(e).__name__}: {e}"
    problems = getattr(result, "problems", None) or []
    return True, f"inspected ({len(problems)} problem(s))"


# ---------------------------------------------------------------------------
# Sonar
# ---------------------------------------------------------------------------


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
        report.info(
            "SonarQube for IDE",
            f"no instance on ports {PORT_MIN}..{PORT_MAX} (optional backend)",
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


def _report_file_probe(
    report: _Report,
    file_path: str | None,
    instances: list[Any],
) -> None:
    """报告可选的目标文件索引探针(通过 Sonar IDE 实例查询)"""
    if not file_path:
        report.info("Target file", "not provided (use --file to test indexing)")
        return
    if not instances:
        report.warn("Target file check skipped", "no Sonar IDE instance available")
        return
    ok_file, detail = _check_file_indexed(file_path, instances)
    (report.ok if ok_file else report.warn)(f"Target file indexed: {detail}")


# ---------------------------------------------------------------------------
# 单项检查(只读)
# ---------------------------------------------------------------------------


def _os_info() -> tuple[str, str]:
    if sys.platform.startswith("win"):
        return "Windows", platform.version() or platform.release()
    if sys.platform == "darwin":
        return "macOS", platform.mac_ver()[0] or platform.release()
    return platform.system() or sys.platform, platform.release()


def _arch() -> str:
    machine = platform.machine() or os.uname().machine if hasattr(os, "uname") else ""
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
    """探查是否有任何 Sonar 实例索引了该文件"""
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


def _ide_name_of(status: dict[str, Any]) -> str:
    """从 Sonar status 对象中尽力推断 IDE 显示名"""
    return status.get("ideName") or status.get("ide") or status.get("productName") or "<unknown>"


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
