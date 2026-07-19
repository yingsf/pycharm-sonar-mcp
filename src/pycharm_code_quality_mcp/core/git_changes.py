"""Git 变更收集:以 NUL 分隔、不使用 shell=True、UTF-8 解码并容错替换

Spec 第 9.3 节(Git 调用安全):
  * 一律使用参数数组,绝不使用 shell=True。
  * 使用 `git ... -z` 并以 NUL(`\\0`)分隔 —— 绝不 splitlines()。
  * 正确处理路径中的空格、CJK、制表符与特殊字符。
  * 排除已删除文件、目录、不存在的路径、工作区外路径以及重复项。
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence

from .. import errors
from ..logging_config import get_logger
from .path_utils import is_within_workspace, normalize_path

_log = get_logger("git_changes")

_GIT_ENCODING = "utf-8"
_NUL = "\x00"


def _git(
    args: Sequence[str],
    *,
    cwd: str,
    timeout: float = 30.0,
) -> tuple[int, str, str]:
    """以参数数组形式运行 git,返回 (returncode, stdout, stderr),绝不使用 shell

    解码一律采用 UTF-8 + errors='replace',避免罕见的路径编码导致崩溃。
    """
    cmd = ["git", *list(args)]
    _log.debug("Running git in %s: %s", cwd, " ".join(_quote(a) for a in cmd))
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding=_GIT_ENCODING,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        raise errors.git_command_failed("git executable not found on PATH.") from e
    except subprocess.TimeoutExpired as e:
        raise errors.git_command_failed(f"git command timed out: {' '.join(args)}") from e
    except NotADirectoryError as e:
        # Windows raises WinError 267 when cwd does not exist; treat as a failed git call
        # so callers (is_git_repo / resolve_repo_root) report "not a repository" cleanly.
        raise errors.git_command_failed(f"working directory does not exist: {cwd}") from e
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _quote(arg: str) -> str:
    if not arg:
        return "''"
    if any(c in arg for c in " \t\"'\\"):
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return arg


def _split_nul(output: str) -> list[str]:
    """按 NUL 切分输出,丢弃末尾的空项"""
    if output == "":
        return []
    parts = output.split(_NUL)
    # git 总是在末尾输出一个 NUL,因此最后一个元素为空字符串。
    if parts and parts[-1] == "":
        parts.pop()
    return [p for p in parts if p]


def is_git_repo(path: str) -> bool:
    """判断 `path` 是否位于某个 git 工作区内"""
    try:
        rc, _, _ = _git(["rev-parse", "--is-inside-work-tree"], cwd=path)
    except errors.SonarMcpError:
        return False
    return rc == 0


def resolve_repo_root(path: str) -> str:
    """返回包含 `path` 的工作区绝对根路径"""
    norm = normalize_path(path)
    rc, out, err = _git(["rev-parse", "--show-toplevel"], cwd=norm)
    if rc != 0:
        if "not a git repository" in err.lower() or "not a git" in err.lower():
            raise errors.git_invalid_repository(f"Not a git repository: {norm}")
        raise errors.git_command_failed(f"git rev-parse failed: {err.strip()}")
    root = out.strip()
    if not root:
        raise errors.git_invalid_repository(f"git root empty for: {norm}")
    return normalize_path(root)


def validate_base_ref(base_ref: str, *, cwd: str) -> None:
    """确保 `base_ref` 可解析,无法解析时抛出 GIT_INVALID_BASE_REF"""
    rc, _, err = _git(["rev-parse", "--verify", base_ref], cwd=cwd)
    if rc != 0:
        raise errors.git_invalid_base_ref(
            f"base_ref {base_ref!r} does not resolve in this repository: {err.strip()}"
        )


def collect_changed_files(
    project_root: str,
    *,
    base_ref: str = "HEAD",
    include_staged: bool = True,
    include_unstaged: bool = True,
    include_untracked: bool = True,
    workspace_roots: list[str] | None = None,
) -> list[str]:
    """收集相对于 `base_ref` 发生变更的源文件绝对路径

    合并 staged + unstaged + untracked 变更,去重,排除已删除文件/目录/不存在路径,
    并在传入 workspace_roots 时丢弃工作区外的路径。

    Returns:
        归一化后的绝对路径,顺序稳定。
    """
    root = resolve_repo_root(project_root)
    validate_base_ref(base_ref, cwd=root)

    paths: list[str] = []

    # 相对 base_ref 的已跟踪变更(对已跟踪文件而言,包含 staged 与 unstaged)。
    # 三点 diff 会额外包含 upstream 的变更,此处只需本地变更。
    # 我们收集:base_ref 与工作树之间的变更、staged 相对 HEAD 的变更,以及 untracked。
    if include_staged or include_unstaged:
        # `git diff --name-only -z base_ref` → base_ref 与工作树之间的已跟踪变更。
        # 一次覆盖 staged 与 unstaged 的已跟踪变更。
        rc, out, err = _git(
            ["diff", "--name-only", "-z", base_ref],
            cwd=root,
        )
        if rc != 0:
            raise errors.git_command_failed(f"git diff failed: {err.strip()}")
        paths.extend(_join_root(root, p) for p in _split_nul(out))

    if include_untracked:
        # untracked 文件(git 尚未感知)。--others --exclude-standard 会尊重 .gitignore。
        rc, out, err = _git(
            ["ls-files", "--others", "--exclude-standard", "-z"],
            cwd=root,
        )
        if rc != 0:
            raise errors.git_command_failed(f"git ls-files failed: {err.strip()}")
        paths.extend(_join_root(root, p) for p in _split_nul(out))

    # 保序去重,最后再统一排序以保证稳定性。
    deduped = _dedupe_preserve_order(paths)

    # 过滤:必须存在、是普通文件、未被删除,且位于工作区内。
    accepted: list[str] = []
    for p in deduped:
        if not os.path.exists(p):
            _log.debug("Skipping non-existent changed path: %s", p)
            continue
        if not os.path.isfile(p):
            _log.debug("Skipping non-file changed path: %s", p)
            continue
        if workspace_roots and not is_within_workspace(p, workspace_roots):
            _log.debug("Skipping changed path outside workspace: %s", p)
            continue
        accepted.append(p)

    # 稳定排序。
    accepted.sort(key=lambda p: os.path.normcase(p))
    return accepted


def collect_project_files(
    project_root: str,
    *,
    extensions: Sequence[str] = (".py",),
    include_untracked: bool = True,
    workspace_roots: list[str] | None = None,
) -> list[str]:
    """收集整个仓库中匹配指定扩展名的源文件绝对路径

    用 `git ls-files -z` 列出 tracked 文件,可选追加 untracked(尊重 .gitignore)。
    复用与 :func:`collect_changed_files` 相同的安全与过滤规范:
      * 参数数组,绝不 shell=True;
      * NUL 分隔,UTF-8 容错解码;
      * 排除不存在/非普通文件/workspace 外路径;
      * 稳定排序。

    Args:
        project_root: 仓库内任意路径,会通过 ``resolve_repo_root`` 解析到仓库根。
        extensions: 允许的文件扩展名(大小写不敏感),默认只扫 Python。
        include_untracked: 是否包含未跟踪文件(尊重 .gitignore)。
        workspace_roots: 若提供,丢弃这些根之外的文件。

    Returns:
        归一化后的绝对路径,顺序稳定。
    """
    root = resolve_repo_root(project_root)
    norm_exts = tuple(e.lower() for e in extensions) if extensions else ()

    paths = _ls_tracked_files(root)
    if include_untracked:
        paths.extend(_ls_untracked_files(root))

    deduped = _dedupe_preserve_order(paths)

    accepted = _filter_project_files(deduped, norm_exts, workspace_roots)
    accepted.sort(key=lambda p: os.path.normcase(p))
    return accepted


def _ls_tracked_files(root: str) -> list[str]:
    """git ls-files 列出索引里的所有 tracked 文件,转绝对路径"""
    rc, out, err = _git(["ls-files", "-z"], cwd=root)
    if rc != 0:
        raise errors.git_command_failed(f"git ls-files failed: {err.strip()}")
    return [_join_root(root, p) for p in _split_nul(out)]


def _ls_untracked_files(root: str) -> list[str]:
    """git ls-files --others 列出未跟踪文件(尊重 .gitignore),转绝对路径"""
    rc, out, err = _git(
        ["ls-files", "--others", "--exclude-standard", "-z"],
        cwd=root,
    )
    if rc != 0:
        raise errors.git_command_failed(f"git ls-files (untracked) failed: {err.strip()}")
    return [_join_root(root, p) for p in _split_nul(out)]


def _filter_project_files(
    paths: list[str],
    norm_exts: tuple[str, ...],
    workspace_roots: list[str] | None,
) -> list[str]:
    """按扩展名 / 存在性 / 普通文件 / workspace 归属过滤路径"""
    accepted: list[str] = []
    for p in paths:
        if norm_exts and not p.lower().endswith(norm_exts):
            continue
        if not os.path.exists(p):
            continue
        if not os.path.isfile(p):
            continue
        if workspace_roots and not is_within_workspace(p, workspace_roots):
            _log.debug("Skipping project path outside workspace: %s", p)
            continue
        accepted.append(p)
    return accepted


def _join_root(root: str, rel: str) -> str:
    """把 git 上报的相对路径拼接到仓库根上,生成绝对路径"""
    if not rel:
        return root
    # 即便在 Windows 上,git 也以正斜杠上报路径。归一化后 os.path.join 能处理混合分隔符;
    # 最终在调用处统一归一化。
    joined = os.path.join(root, rel.replace("/", os.sep) if sys.platform.startswith("win") else rel)
    return normalize_path(joined)


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key = os.path.normcase(it)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out
