"""命令行接口:serve / doctor / setup / jetbrains / sonar / --version

`serve` 模式下 stdout 专用于 MCP JSON-RPC;其余子命令不是 MCP 服务,
其人类可读输出去往 stdout,诊断与错误输出去往 stderr。

子命令总览:
  (默认)             等价于 serve
  serve              运行 stdio MCP 服务
  doctor             运行环境诊断(不启动 MCP)
  setup              引导式配置向导(JetBrains HTTP Stream)
  jetbrains          JetBrains MCP 子命令组(configure / status / clear)
  sonar              SonarQube for IDE 子命令组(status)
  --version          打印版本并退出
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import sys
from collections.abc import Callable, Sequence
from typing import Any

from . import __version__
from .doctor import run_doctor
from .logging_config import configure_logging
from .server import run_stdio

_log = configure_logging()


def _build_parser() -> argparse.ArgumentParser:
    """构建 argparse 解析器"""
    parser = argparse.ArgumentParser(
        prog="pycharm-code-quality-mcp",
        description=(
            "Local MCP bridge to PyCharm code quality. Default backend is JetBrains "
            "inspections; SonarQube for IDE is an auto-detected optional enhancement. "
            "With no subcommand, runs the stdio MCP server."
        ),
        add_help=True,
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print version and exit.",
    )
    sub = parser.add_subparsers(dest="command")

    serve_p = sub.add_parser("serve", help="Run the stdio MCP server (default).")
    serve_p.set_defaults(func=_cmd_serve)

    doctor_p = sub.add_parser("doctor", help="Run environment diagnostics. Does NOT start MCP.")
    doctor_p.add_argument(
        "--file",
        dest="file",
        default=None,
        help="Optional absolute file path to probe indexing for.",
    )
    doctor_p.set_defaults(func=_cmd_doctor)

    # setup:非交互向导,引导用户配置 JetBrains MCP HTTP Stream。
    setup_p = sub.add_parser("setup", help="Guided first-time setup (non-interactive).")
    setup_p.add_argument(
        "--json",
        dest="json_input",
        default=None,
        help="Paste the full JetBrains 'Copy HTTP Stream Config' JSON to save and verify.",
    )
    setup_p.set_defaults(func=_cmd_setup)

    # jetbrains 子命令组。
    jb = sub.add_parser("jetbrains", help="Manage the JetBrains MCP backend.")
    jb_sub = jb.add_subparsers(dest="jetbrains_command")
    jb_cfg = jb_sub.add_parser("configure", help="Configure JetBrains MCP (interactive).")
    jb_cfg.add_argument(
        "--json",
        dest="json_input",
        default=None,
        help="JetBrains 'Copy HTTP Stream Config' JSON. If omitted, reads from stdin.",
    )
    jb_cfg.set_defaults(func=_cmd_jetbrains_configure)
    jb_status = jb_sub.add_parser("status", help="Show JetBrains MCP status.")
    jb_status.add_argument(
        "--project-root",
        dest="project_root",
        default=None,
        help="Optional project root used to route newer PyCharm MCP sessions.",
    )
    jb_status.set_defaults(func=_cmd_jetbrains_status)
    jb_clear = jb_sub.add_parser("clear", help="Remove stored JetBrains MCP config.")
    jb_clear.set_defaults(func=_cmd_jetbrains_clear)

    # sonar 子命令组。
    sn = sub.add_parser("sonar", help="Inspect the SonarQube for IDE backend.")
    sn_sub = sn.add_subparsers(dest="sonar_command")
    sn_status = sn_sub.add_parser("status", help="Show SonarQube for IDE status (port scan).")
    sn_status.set_defaults(func=_cmd_sonar_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    """[project.scripts] 注册的入口,返回进程退出码"""
    # Windows PyInstaller 冻结环境下,必须在任何子进程派生前调用 freeze_support。
    multiprocessing.freeze_support()

    parser = _build_parser()
    args, extra = parser.parse_known_args(argv)

    if extra:
        # 未知参数:argparse 已处理子命令不匹配,这里对剩余位置参数明确报错到 stderr。
        print(f"error: unrecognized arguments: {' '.join(extra)}", file=sys.stderr)
        return 2

    if getattr(args, "version", False):
        # 版本输出到 stdout —— 仅因 --version 不是 MCP 服务,不会污染 JSON-RPC。
        sys.stdout.write(f"pycharm-code-quality-mcp {__version__}\n")
        sys.stdout.flush()
        return 0

    func: Callable[..., int] | None = getattr(args, "func", None)
    if func is None:
        # 无子命令时默认等价于 serve。
        return _cmd_serve(args)
    return func(args)


# ---------------------------------------------------------------------------
# serve / doctor
# ---------------------------------------------------------------------------


def _cmd_serve(_args: argparse.Namespace) -> int:
    try:
        run_stdio()
    except KeyboardInterrupt:
        # Ctrl+C 干净退出,绝不向 stdout 输出内容。130 是 SIGINT 的约定退出码。
        return 130
    except Exception:
        _log.exception("MCP server crashed")
        return 1
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """运行 doctor 诊断,输出到 stdout"""
    file_arg: str | None = getattr(args, "file", None)
    return run_doctor(file_path=file_arg, stream=sys.stdout)


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def _cmd_setup(args: argparse.Namespace) -> int:
    """非交互式首次配置向导

    若通过 --json 提供 JetBrains 配置,则保存并校验;否则输出后续步骤说明
    (绝不卡住等待 stdin,以满足非交互安装场景)。
    """
    json_input: str | None = getattr(args, "json_input", None)
    print("PyCharm Code Quality MCP — setup")
    print("")
    print("This tool defaults to JetBrains inspections. To enable the JetBrains MCP backend:")
    print("  1. Open PyCharm → Settings → Tools → MCP Server.")
    print("  2. Enable MCP Server.")
    print(
        "  3. In 'Exposed Tools', enable: get_file_problems (required), get_project_status (optional)."
    )
    print("  4. Click 'Copy HTTP Stream Config'.")
    print("  5. Run: pycharm-code-quality-mcp jetbrains configure --json '<paste config>'")
    print("")
    print("SonarQube for IDE is auto-detected; no configuration is needed for it.")
    print("If you do not install Sonar, the JetBrains backend still works on its own.")
    print("")

    if json_input:
        return _save_jetbrains_config(json_input)

    print("No --json provided. Run 'pycharm-code-quality-mcp jetbrains configure' next.")
    print("Run 'pycharm-code-quality-mcp doctor' to verify everything.")
    return 0


# ---------------------------------------------------------------------------
# jetbrains 子命令
# ---------------------------------------------------------------------------


def _cmd_jetbrains_configure(args: argparse.Namespace) -> int:
    """交互/非交互配置 JetBrains MCP,接收完整 JSON 并校验必需工具"""
    json_input: str | None = getattr(args, "json_input", None)
    if not json_input:
        # 内外两层 if 语义不同,不合并:外层判 json_input 是否提供,内层判 stdin 是否可用。
        if not sys.stdin.isatty():
            json_input = sys.stdin.read().strip()
    if not json_input:
        print("error: no JSON provided. Use --json or pipe the config via stdin.", file=sys.stderr)
        print(
            "In PyCharm: Settings → Tools → MCP Server → Copy HTTP Stream Config, then:",
            file=sys.stderr,
        )
        print(
            "  pycharm-code-quality-mcp jetbrains configure --json '<paste>'",
            file=sys.stderr,
        )
        return 2
    return _save_jetbrains_config(json_input)


def _save_jetbrains_config(json_input: str) -> int:
    """解析并保存 JetBrains 配置;通过真实 MCP 连接校验必需工具"""
    from .backends.jetbrains import config as jb_config

    parsed = _parse_jetbrains_stream_json(json_input)
    if parsed is None:
        print("error: could not extract url/headers from the provided JSON.", file=sys.stderr)
        return 2

    url, headers = parsed
    try:
        jb_config.save_config(url, headers)
    except Exception as e:
        print(f"error: failed to save config: {e}", file=sys.stderr)
        return 2

    print(f"Saved JetBrains MCP config to {jb_config.config_file_path()}")
    print("Verifying connection (initialize + tools/list)…")

    # 真实连接校验:initialize + tools/list + 必需工具存在。
    import asyncio

    from .backends.jetbrains.analyzer import JetBrainsAnalysisBackend
    from .backends.jetbrains.client import REQUIRED_TOOLS

    stored_headers = jb_config.headers_for_storage(headers)
    verify_project_root = jb_config.project_path_from_headers(headers)

    async def _verify() -> tuple[bool, str]:
        try:
            cfg = jb_config.JetBrainsConfig(url=url, headers=stored_headers)
            backend = JetBrainsAnalysisBackend(cfg)
            status = await backend.get_status(project_root=verify_project_root)
            if not status.get("available"):
                return False, str(status.get("error") or "connection failed")
            tools = status.get("tools") or []
            missing = sorted(REQUIRED_TOOLS - set(tools))
            if missing:
                return False, f"missing required tools: {missing}. Enable them in Exposed Tools."
            return True, "ok"
        except Exception as e:  # pragma: no cover - defensive
            return False, str(e)

    ok, detail = asyncio.run(_verify())
    if ok:
        print("[OK] JetBrains MCP configured and reachable.")
        print("Run 'pycharm-code-quality-mcp doctor' to confirm.")
        return 0
    print(f"[FAIL] Configuration saved, but verification failed: {detail}", file=sys.stderr)
    print(
        "The config file is saved; re-open PyCharm's MCP Server settings and retry.",
        file=sys.stderr,
    )
    return 1


def _parse_jetbrains_stream_json(raw: str) -> tuple[str, dict[str, str]] | None:
    """从 JetBrains 'Copy HTTP Stream Config' 的 JSON 中抽取 url 与 headers

    接受多种形态:
      * {"url": "...", "headers": {...}}
      * {"transport": {"type": "streamable-http", "url": "...", "headers": {...}}}
      * {"mcpServers": {"<name>": {"url": "...", "headers": {...}}}}
      * {"type": "streamable-http", "url": "...", "headers": {...}}
    """
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    for cand in _jetbrains_stream_candidates(obj):
        url = cand.get("url") or cand.get("endpoint")
        if isinstance(url, str) and url.strip():
            headers = cand.get("headers")
            hdr_dict: dict[str, str] = {}
            if isinstance(headers, dict):
                hdr_dict = {str(k): str(v) for k, v in headers.items()}
            return url.strip(), hdr_dict
    return None


def _jetbrains_stream_candidates(obj: dict[str, object]) -> list[dict[str, object]]:
    """从原始 JSON 对象中收集所有可能携带 url/headers 的子对象"""
    candidates: list[dict[str, object]] = []
    if "url" in obj:
        candidates.append(obj)
    transport = obj.get("transport")
    if isinstance(transport, dict):
        candidates.append(transport)
    mcp_servers = obj.get("mcpServers")
    if isinstance(mcp_servers, dict):
        for v in mcp_servers.values():
            if isinstance(v, dict):
                candidates.append(v)
    return candidates


def _cmd_jetbrains_status(args: argparse.Namespace) -> int:
    """打印 JetBrains MCP 当前配置与连接状态"""
    import asyncio

    from .backends.jetbrains import config as jb_config

    cfg = jb_config.load_config()
    if cfg is None:
        print("JetBrains MCP: not configured.")
        print(f"Config file (would be): {jb_config.config_file_path()}")
        print("Run: pycharm-code-quality-mcp jetbrains configure")
        return 1
    print(f"Config file: {jb_config.config_file_path()}")
    print(f"URL: {cfg.url}")
    print(f"Transport: {cfg.transport}")
    print(f"Headers: {_redact_headers(cfg.headers)}")

    from .backends.jetbrains.analyzer import JetBrainsAnalysisBackend

    project_root = _resolve_status_project_root(getattr(args, "project_root", None))

    async def _probe() -> dict[str, Any]:
        try:
            backend = JetBrainsAnalysisBackend()
            return await backend.get_status(project_root=project_root)
        except Exception as e:  # pragma: no cover - defensive
            return {"available": False, "error": str(e)}

    status = asyncio.run(_probe())
    print("")
    print(f"Available:    {status.get('available')}")
    print(f"Project ready: {status.get('projectReady')}")
    print(f"Indexing:      {status.get('indexing')}")
    tools = status.get("tools") or []
    if tools:
        print(f"Tools:         {', '.join(tools)}")
    err = status.get("error")
    if err:
        print(f"Error:         {err}", file=sys.stderr)
        return 1
    return 0 if status.get("available") else 1


def _resolve_status_project_root(project_root: str | None) -> str | None:
    from pathlib import Path

    from .core.path_utils import normalize_path

    if project_root:
        return normalize_path(project_root)
    cwd = Path.cwd()
    home = Path.home()
    if _same_cli_dir(cwd, home):
        return None
    markers = (".git", ".hg", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")
    if any((cwd / marker).exists() for marker in markers):
        return str(cwd.resolve())
    return None


def _same_cli_dir(left: Any, right: Any) -> bool:
    import os

    try:
        left_s = str(left.resolve())
    except OSError:
        left_s = str(left.absolute())
    try:
        right_s = str(right.resolve())
    except OSError:
        right_s = str(right.absolute())
    return os.path.normcase(os.path.normpath(left_s)) == os.path.normcase(os.path.normpath(right_s))


def _cmd_jetbrains_clear(_args: argparse.Namespace) -> int:
    """删除已保存的 JetBrains 配置(幂等:无配置时也视为成功)"""
    from .backends.jetbrains import config as jb_config

    removed = jb_config.clear_config()
    path = jb_config.config_file_path()
    message = (
        f"Removed JetBrains MCP config at {path}"
        if removed
        else f"No config file at {path} (nothing to clear)."
    )
    print(message)
    # 幂等语义:无论是否实际删除,命令本身执行成功就返回 0。
    return 0


# ---------------------------------------------------------------------------
# sonar 子命令
# ---------------------------------------------------------------------------


def _cmd_sonar_status(_args: argparse.Namespace) -> int:
    """扫描 SonarQube for IDE 实例并打印状态"""
    import asyncio

    from .backends.sonar.client import SonarClient
    from .backends.sonar.discovery import PORT_MAX, PORT_MIN, IdeDiscovery

    sonar = SonarClient()
    try:
        discovery = IdeDiscovery(sonar)
        instances = asyncio.run(asyncio.to_thread(discovery.discover_all_instances))
    finally:
        sonar.close()

    if not instances:
        print(f"SonarQube for IDE: no instance found on ports {PORT_MIN}..{PORT_MAX}.")
        print("Open PyCharm with the plugin installed.")
        return 1
    print(f"SonarQube for IDE: {len(instances)} instance(s) found")
    for inst in instances:
        ide = inst.status.get("ideName") or inst.status.get("ide") or "<unknown>"
        print(f"  port {inst.port}: {ide}")
    if len(instances) > 1:
        print("")
        print("Multiple instances detected; consider setting SONAR_IDE_PORT to pick one.")
    return 0


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _redact_headers(headers: dict[str, str]) -> str:
    """打印 headers 时只显示 key,不输出 value(可能含鉴权)"""
    if not headers:
        return "{}"
    keys = ", ".join(sorted(headers.keys()))
    return f"<{len(headers)} key(s): {keys}>"


if __name__ == "__main__":
    raise SystemExit(main())


_ = Sequence  # 保留 Sequence 在模块命名空间(部分旧导入可能用到)
