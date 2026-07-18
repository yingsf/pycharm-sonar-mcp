"""命令行接口:serve / doctor / --version

`serve` 模式下 stdout 专用于 MCP JSON-RPC;`doctor` 与 `--version` 不是 MCP 服务,
其人类可读输出去往 stdout,诊断与错误输出去往 stderr。
"""

from __future__ import annotations

import argparse
import multiprocessing
import sys
from collections.abc import Callable

from . import __version__
from .doctor import run_doctor
from .logging_config import configure_logging
from .server import run_stdio

_log = configure_logging()


def _build_parser() -> argparse.ArgumentParser:
    """构建 argparse 解析器"""
    parser = argparse.ArgumentParser(
        prog="pycharm-sonar-mcp",
        description=(
            "Local MCP bridge to PyCharm's SonarQube for IDE. "
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
        sys.stdout.write(f"pycharm-sonar-mcp {__version__}\n")
        sys.stdout.flush()
        return 0

    func: Callable[..., int] | None = getattr(args, "func", None)
    if func is None:
        # 无子命令时默认等价于 serve。
        return _cmd_serve(args)
    return func(args)


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


if __name__ == "__main__":
    raise SystemExit(main())
