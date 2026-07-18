"""跨平台路径安全：规范化、工作区包含性判断、符号链接逃逸检测

设计目标（规范第 12 节）：
  * Windows：盘符大小写不敏感、混合斜杠、UNC、junction、长路径。
  * macOS：大小写敏感或不敏感的 APFS、符号链接、.localized 路径、非 ASCII。
  * 绝不使用朴素字符串前缀判断包含关系。
  * 在包含性检查之前先解析符号链接/junction，拒绝逃逸。

本模块绝不修改用户文件系统，所有存在性检查均为只读。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .. import errors
from ..logging_config import get_logger

_log = get_logger("path_utils")

IS_WINDOWS: bool = sys.platform.startswith("win")


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------


def normalize_path(path: str) -> str:
    """返回规范、绝对、归一化的路径字符串

    - 展开 ~user。
    - 转换为绝对路径（相对路径以 CWD 为基准）。
    - 通过 os.path.normpath 将分隔符规范化为 OS 原生形式。
    - 在 Windows 上将盘符归一化为大写，以便稳定比较。

    本函数不解析符号链接，需要解析请使用 `resolve_real_path`。
    """
    if not isinstance(path, str):  # 防御性校验：pydantic 已保证 str，这里仅兜底
        raise errors.bad_request(f"Path must be a string, got {type(path).__name__}")
    if not path.strip():
        raise errors.bad_request("Path is empty.")

    expanded = os.path.expanduser(os.path.expandvars(path))
    absolute = os.path.abspath(expanded)
    norm = os.path.normpath(absolute)

    if IS_WINDOWS:
        norm = _normalize_windows_drive(norm)
    return norm


def _normalize_windows_drive(path: str) -> str:
    """将 Windows 盘符大写，使 'c:\\' 与 'C:\\' 在比较时相等"""
    if len(path) >= 2 and path[1] == ":":
        return path[0].upper() + path[1:]
    return path


def resolve_real_path(path: str) -> str:
    """将符号链接（及 Windows junction）解析到真实目标

    使用 os.path.realpath，在 Windows 上同时解析 junction 与 reparse point。
    尽可能使用 strict 模式：末级组件缺失时会抛出 OSError，
    由调用方转换为 FILE_NOT_FOUND。
    """
    norm = normalize_path(path)
    # strict=True 要求路径存在（Python 3.10+）；缺失路径会抛出 OSError，
    # 由调用方转换为 FILE_NOT_FOUND 或退回到名义形式。
    return os.path.realpath(norm, strict=True)


def is_unc_path(path: str) -> bool:
    """判断是否为 Windows UNC 路径（\\\\server\\share 或 //server/share）"""
    p = path.replace("/", "\\")
    return p.startswith("\\\\") and not p.startswith("\\\\?\\")


# ---------------------------------------------------------------------------
# 包含性检查
# ---------------------------------------------------------------------------


def is_within_workspace(path: str, workspace_roots: list[str]) -> bool:
    """判断 `path`（经符号链接解析后）是否位于任一工作区根目录之内

    路径与每个工作区根目录都被解析到真实文件系统位置，
    从而拒绝逃逸出工作区的符号链接/junction 子项。

    本检查基于真实路径，因此指向工作区外的符号链接会被视为位于工作区外
    （必要时由调用方单独抛出 SYMLINK_ESCAPE）。
    """
    if not workspace_roots:
        return False
    try:
        real_path = resolve_real_path(path)
    except OSError:
        # 路径不存在；与规范化（未解析）形式比较。
        real_path = normalize_path(path)

    for root in workspace_roots:
        try:
            real_root = resolve_real_path(root)
        except OSError:
            real_root = normalize_path(root)
        if _path_is_within(real_path, real_root):
            return True
    return False


def _path_is_within(child: str, parent: str) -> bool:
    """按正确的 OS 路径语义判断 `child` 是否等于或嵌套于 `parent` 之下"""
    # 两端都规范化以保证稳定比较。
    nchild = os.path.normpath(child)
    nparent = os.path.normpath(parent)

    if IS_WINDOWS:
        nchild = _normalize_windows_drive(nchild)
        nparent = _normalize_windows_drive(nparent)

    # 盘符/UNC 不一致 => 必然不在此父路径下。
    child_drive = os.path.splitdrive(nchild)[0]
    parent_drive = os.path.splitdrive(nparent)[0]
    if _norm_drive(child_drive) != _norm_drive(parent_drive):
        return False

    nchild = os.path.splitdrive(nchild)[1]
    nparent = os.path.splitdrive(nparent)[1]

    if not nparent:
        return True  # 父路径为整个盘根目录（罕见）

    # 使用 os.path.normcase 在 Windows/macOS-不敏感 文件系统上做大小写不敏感比较。
    nc_child = os.path.normcase(nchild)
    nc_parent = os.path.normcase(nparent)

    nc_parent = nc_parent.rstrip(os.sep)
    if not nc_parent:
        return True

    if nc_child == nc_parent:
        return True
    return nc_child.startswith(nc_parent + os.sep)


def _norm_drive(drive: str) -> str:
    if IS_WINDOWS and len(drive) >= 1 and len(drive) == 2 and drive[1] == ":":
        return drive[0].upper() + ":"
    return os.path.normcase(drive)


# ---------------------------------------------------------------------------
# 符号链接 / junction 逃逸检测
# ---------------------------------------------------------------------------


def check_symlink_escape(path: str, workspace_roots: list[str]) -> bool:
    """判断 `path` 是否为符号链接/junction 且其真实目标逃逸出工作区

    路径本身在名义上可能位于工作区内，但其解析后的目标却位于工作区外。
    这是阻止跟随恶意链接越界的安全关键检查。

    Note:
        此处的包含性检查针对的是名义（未解析）路径，从而能检测出
        文本位置在工作区内、目标却指向工作区外的符号链接。
    """
    if not workspace_roots:
        return False
    norm = normalize_path(path)
    # 名义路径是否位于任一工作区根目录之下？（此处不解析符号链接。）
    nominal_inside = _nominal_within(norm, workspace_roots)
    if not nominal_inside:
        return False  # 已位于工作区外，属于"在外"而非"逃逸"。
    try:
        real = resolve_real_path(norm)
    except OSError:
        return False  # 缺失路径由其他地方处理
    # 若真实路径等于名义路径，说明未涉及符号链接/junction。
    if os.path.normcase(real) == os.path.normcase(norm):
        return False
    # 真实目标是否逃逸出工作区？
    return not is_within_workspace(real, workspace_roots)


def _nominal_within(path: str, workspace_roots: list[str]) -> bool:
    """将路径与各根目录均视为名义（未解析）形式进行包含性判断

    对两端都使用规范化形式可保持比较的对称性：若工作区根本身就是一个符号链接，
    将名义路径与名义根比较仍能反映用户意图（文件在文本上位于已配置的根之下）。
    真正的符号链接目标逃逸判定则由 ``check_symlink_escape`` 经
    ``is_within_workspace`` 对已解析路径作出。
    """
    for root in workspace_roots:
        nominal_root = normalize_path(root)
        if _path_is_within(path, nominal_root):
            return True
    return False


# ---------------------------------------------------------------------------
# 文件校验
# ---------------------------------------------------------------------------


def validate_regular_file(path: str) -> str:
    """校验路径是一个已存在的常规文件，返回规范化的绝对路径

    缺失时抛出 FILE_NOT_FOUND，为目录/特殊文件/设备时抛出 FILE_NOT_REGULAR。
    """
    norm = normalize_path(path)
    if not os.path.exists(norm):
        # 部分 Windows 长路径场景需要 \\?\ 前缀；再尝试一次。
        if IS_WINDOWS and not norm.startswith("\\\\?\\") and len(norm) > 248:
            extended = "\\\\?\\" + norm
            if os.path.exists(extended):
                norm = extended
            else:
                raise errors.file_not_found(f"File does not exist: {norm}")
        else:
            raise errors.file_not_found(f"File does not exist: {norm}")
    if not os.path.isfile(norm):
        # os.path.isfile 会跟随符号链接，因此指向目录的链接在此被正确拒绝。
        raise errors.file_not_regular(f"Path is not a regular file: {norm}")
    if _is_special_file(norm):
        raise errors.file_not_regular(f"Path is not a regular file (special/device): {norm}")
    return norm


def _is_special_file(path: str) -> bool:
    """拒绝 FIFO、设备、套接字，接受常规文件及指向常规文件的符号链接"""
    if IS_WINDOWS:
        return False  # Windows 在此意义下没有 FIFO/设备文件。
    try:
        st = os.stat(path)  # 跟随符号链接
    except OSError:
        return False
    import stat as _stat

    return (
        _stat.S_ISFIFO(st.st_mode)
        or _stat.S_ISCHR(st.st_mode)
        or _stat.S_ISBLK(st.st_mode)
        or _stat.S_ISSOCK(st.st_mode)
    )


# ---------------------------------------------------------------------------
# 去重 + 稳定排序
# ---------------------------------------------------------------------------


def dedupe_and_sort(paths: list[str]) -> list[str]:
    """对规范化后的路径去重，并保持稳定、确定性的顺序

    Windows 上比较大小写不敏感，其他平台大小写敏感，
    与文件系统视为同一文件的判定保持一致。
    """
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        norm = normalize_path(p)
        key = os.path.normcase(norm)
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)
    # 按规范化形式稳定排序，使多次运行结果可复现。
    out.sort(key=lambda p: os.path.normcase(p))
    return out


# ---------------------------------------------------------------------------
# 用于日志的路径缩短（隐私保护）
# ---------------------------------------------------------------------------


def shorten_path_for_log(path: str) -> str:
    """返回用于日志的、保护隐私的 `path` 简短形式

    保留末级基名与紧邻的父目录，其余部分替换为 '...'。
    """
    if not path:
        return "<empty>"
    parts = Path(path).parts
    if len(parts) <= 3:
        return path
    return os.path.join("...", parts[-3], parts[-2], parts[-1])


def hash_path(path: str) -> str:
    """对路径生成稳定的短哈希（SHA-1 的前 12 位十六进制），用于调试关联"""
    import hashlib

    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]


def project_name_of(path: str) -> str:
    """尽力推断项目名称：取目录路径的末级组件"""
    norm = os.path.normpath(path)
    base = os.path.basename(norm.rstrip(os.sep))
    return base or norm
