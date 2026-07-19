"""IDE 发现:端口扫描、状态校验、多实例匹配与缓存

发现策略(spec 第 10 节):

    显式端口 (SONAR_IDE_PORT)
      -> 项目端口缓存
      -> 扫描 64120..64130
      -> 校验 /sonarlint/api/status
      -> 通过目标文件探针匹配正确的 IDE 实例
      -> 缓存 project_root -> port 映射
      -> 失败时清空缓存并重试一次
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Mapping
from typing import Any, Protocol

import httpx

from ... import errors
from ...logging_config import get_logger
from .models import IdeStatus, InstanceInfo

_log = get_logger("ide_discovery")

# SonarQube for IDE 内嵌 HTTP server 使用的端口区间。
PORT_MIN = 64120
PORT_MAX = 64130
DEFAULT_PORTS = list(range(PORT_MIN, PORT_MAX + 1))

ENV_SONAR_IDE_PORT = "SONAR_IDE_PORT"
STATUS_PATH = "/sonarlint/api/status"
ANALYSIS_PATH = "/sonarlint/api/analysis/files"

# 对无监听的端口快速失败,单端口耗时应明显小于 1 秒。
DEFAULT_CONNECT_TIMEOUT = 0.6
DEFAULT_READ_TIMEOUT = 8.0
DEFAULT_STATUS_TIMEOUT = 2.5


class _SonarProbeLike(Protocol):
    """Sonar probe 客户端需满足的最小接口(便于单元测试替换)"""

    def get_status(self, port: int, *, timeout: float = ...) -> dict[str, Any]: ...

    def analyze_files(
        self, port: int, file_paths: list[str], *, timeout: float = ...
    ) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# 状态启发式校验
# ---------------------------------------------------------------------------


def looks_like_sonar_status(obj: Any) -> bool:
    """启发式校验解析后的 JSON 对象是否为 SonarQube for IDE 的 status

    不假定跨版本存在固定 schema,但要求满足:
      * 顶层是一个对象(dict)
      * 至少存在一个名称看起来与 IDE/Sonar 相关的字段
    """
    if not isinstance(obj, dict):
        return False
    if not obj:
        return False
    # SonarQube for IDE 各版本常见的 status 字段名。
    hints = (
        "ideName",
        "ide",
        "version",
        "sonarlint",
        "connectedMode",
        "serverVersion",
        "rules",
        "enabledLanguages",
        "languages",
        "productName",
        "standalone",
    )
    keys = {str(k) for k in obj}
    return any(h in keys for h in hints) or any(
        "sonar" in k.lower() or "lint" in k.lower() for k in keys
    )


def _extract_path(entry: Any) -> str | None:
    """从可能是 str 或 dict 的标记项中提取文件路径"""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        path = entry.get("filePath")
        return path if isinstance(path, str) else None
    return None


def _file_marked_not_indexed(raw: dict[str, Any], target_file: str) -> bool:
    """检查 Sonar 分析响应体是否将 ``target_file`` 标记为未索引

    SonarQube for IDE API 未正式文档化此行为,但若未来版本返回
    ``notIndexedFiles`` / ``not_indexed_files`` 列表,我们予以尊重,
    以免发现逻辑选中了一个无法分析该文件的实例。
    """
    target_norm = os.path.normcase(os.path.normpath(target_file))
    for key in ("notIndexedFiles", "not_indexed_files"):
        lst = raw.get(key)
        if not isinstance(lst, list):
            continue
        for entry in lst:
            path = _extract_path(entry)
            if path is not None and os.path.normcase(os.path.normpath(path)) == target_norm:
                return True
    return False


# ---------------------------------------------------------------------------
# 端口缓存
# ---------------------------------------------------------------------------


class PortCache:
    """内存态的 project_root -> port 缓存,不持久化,线程安全

    MCP server 共享一个全局实例;每次调用 `code_quality_clear_cache` 会清空它。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._map: dict[str, int] = {}
        self._ts: dict[str, float] = {}

    def get(self, project_root: str) -> int | None:
        key = os.path.normcase(os.path.normpath(project_root))
        with self._lock:
            return self._map.get(key)

    def set(self, project_root: str, port: int) -> None:
        key = os.path.normcase(os.path.normpath(project_root))
        with self._lock:
            self._map[key] = port
            self._ts[key] = time.monotonic()

    def invalidate(self, project_root: str) -> bool:
        key = os.path.normcase(os.path.normpath(project_root))
        with self._lock:
            existed = key in self._map
            self._map.pop(key, None)
            self._ts.pop(key, None)
            return existed

    def clear(self) -> list[int]:
        with self._lock:
            ports = list(self._map.values())
            self._map.clear()
            self._ts.clear()
            return ports

    def __len__(self) -> int:
        with self._lock:
            return len(self._map)


# 模块级缓存单例,测试可替换它。
_GLOBAL_CACHE = PortCache()


def get_global_cache() -> PortCache:
    return _GLOBAL_CACHE


def set_global_cache(cache: PortCache) -> None:
    global _GLOBAL_CACHE
    _GLOBAL_CACHE = cache


# ---------------------------------------------------------------------------
# 显式端口校验
# ---------------------------------------------------------------------------


def parse_explicit_port(env: Mapping[str, str] | None = None) -> int | None:
    """返回显式配置的端口,未配置则返回 None

    Raises:
        SonarMcpError(IDE_PORT_INVALID):已设置但不是合法整数。
        SonarMcpError(IDE_PORT_OUT_OF_RANGE):已设置但超出 64120..64130 区间。
    """
    env_map: Mapping[str, str] = env if env is not None else os.environ
    raw = env_map.get(ENV_SONAR_IDE_PORT, "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError as e:
        raise errors.ide_port_invalid(
            f"{ENV_SONAR_IDE_PORT}={raw!r} is not a valid integer port."
        ) from e
    if not (PORT_MIN <= port <= PORT_MAX):
        raise errors.ide_port_out_of_range(
            f"{ENV_SONAR_IDE_PORT}={port} is outside the supported range {PORT_MIN}..{PORT_MAX}."
        )
    return port


# ---------------------------------------------------------------------------
# 状态响应校验
# ---------------------------------------------------------------------------


def validate_status_response(resp: httpx.Response) -> dict[str, Any]:
    """校验来自 /sonarlint/api/status 的 HTTP 响应

    成功时返回解析后的 JSON dict。任何校验失败都抛出带有具体错误码的
    SonarMcpError,以便调用方据此决定缓存失效的语义。
    """
    if resp.status_code == 421:
        raise errors.ide_authority_rejected(
            "HTTP 421 Misdirected Request on port (authority rejected). "
            "This indicates the Host/Origin header is not 'localhost'."
        )
    if resp.status_code == 404:
        raise errors.sonar_bad_response(
            "HTTP 404 on status endpoint — port is not a SonarQube for IDE service."
        )
    if resp.status_code == 429:
        raise errors.sonar_rate_limited("HTTP 429 from Sonar IDE (rate limited).")
    if resp.status_code >= 500:
        raise errors.sonar_unavailable(
            f"Sonar IDE returned HTTP {resp.status_code}; backend unavailable."
        )
    if resp.status_code != 200:
        raise errors.sonar_bad_response(
            f"Sonar IDE returned HTTP {resp.status_code} on status endpoint."
        )
    ctype = resp.headers.get("content-type", "")
    if "json" not in ctype.lower():
        # 某些响应即便未带正确 ctype 也可能仍是 JSON,因此仍尝试解析。
        _log.debug("Status content-type is %r (expected JSON); attempting parse anyway", ctype)
    try:
        obj = resp.json()
    except Exception as e:
        raise errors.sonar_bad_response("Status response is not valid JSON.") from e
    if not isinstance(obj, dict):
        raise errors.sonar_bad_response(
            f"Status response is JSON but not an object (got {type(obj).__name__})."
        )
    # 借助 Pydantic 允许额外字段(前向兼容)并校验基本结构。
    try:
        IdeStatus.model_validate(obj)
    except Exception as e:
        raise errors.sonar_bad_response(f"Status JSON failed schema validation: {e}") from e
    if not looks_like_sonar_status(obj):
        raise errors.sonar_bad_response(
            "Status JSON does not look like a SonarQube for IDE status response."
        )
    return obj


# ---------------------------------------------------------------------------
# 发现编排
# ---------------------------------------------------------------------------


class IdeDiscovery:
    """编排 SonarQube for IDE 的端口发现流程

    构造时注入一个 Sonar 客户端(Protocol),便于单元测试 mock HTTP。
    """

    def __init__(
        self,
        sonar_probe: _SonarProbeLike,
        *,
        cache: PortCache | None = None,
        ports: list[int] | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        status_timeout: float = DEFAULT_STATUS_TIMEOUT,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._probe = sonar_probe
        self._cache = cache if cache is not None else get_global_cache()
        self._ports = ports if ports is not None else DEFAULT_PORTS
        self._connect_timeout = connect_timeout
        self._status_timeout = status_timeout
        self._env: Mapping[str, str] = env if env is not None else os.environ

    # -- public API --

    def discover_all_instances(self) -> list[InstanceInfo]:
        """扫描所有端口并返回通过状态校验的实例"""
        found: list[InstanceInfo] = []
        for port in self._ports:
            try:
                status = self._probe.get_status(port, timeout=self._status_timeout)
            except Exception as e:
                _log.debug("Port %d: no valid Sonar status (%s: %s)", port, type(e).__name__, e)
                continue
            if looks_like_sonar_status(status):
                found.append(InstanceInfo(port=port, status=status))
        return found

    def discover_for_project(self, project_root: str) -> int:
        """为指定 project root 发现端口

        顺序:显式端口 -> 缓存 -> 扫描 + 文件探针匹配。

        Returns:
            端口号。失败时抛出 SonarMcpError(IDE_*)。
        """
        project_root_norm = os.path.normcase(os.path.normpath(project_root))

        # 1. 显式端口。
        explicit = parse_explicit_port(self._env)
        if explicit is not None:
            # 仍需校验它确实是 Sonar 服务;显式端口不会静默回退到其他来源。
            try:
                self._probe.get_status(explicit, timeout=self._status_timeout)
            except errors.SonarMcpError:
                raise
            except Exception as e:
                raise errors.ide_not_found(
                    f"{ENV_SONAR_IDE_PORT}={explicit} is set but is not a reachable "
                    f"SonarQube for IDE service: {e}"
                ) from e
            self._cache.set(project_root_norm, explicit)
            return explicit

        # 2. 缓存。
        cached = self._cache.get(project_root_norm)
        if cached is not None:
            try:
                self._probe.get_status(cached, timeout=self._status_timeout)
                return cached
            except errors.SonarMcpError as e:
                if e.code in _REDISCOVER_CODES:
                    _log.info(
                        "Cached port %d invalid (%s); clearing cache and rediscovering once.",
                        cached,
                        e.code,
                    )
                    self._cache.invalidate(project_root_norm)
                    return self._scan_and_match(project_root_norm)
                raise
            except Exception as e:
                _log.info(
                    "Cached port %d unreachable (%s); rediscovering once.",
                    cached,
                    type(e).__name__,
                )
                self._cache.invalidate(project_root_norm)
                return self._scan_and_match(project_root_norm)

        # 3. 扫描 + 匹配。
        return self._scan_and_match(project_root_norm)

    def match_instance_for_file(self, target_file: str) -> int:
        """查找其 IDE 索引了 ``target_file`` 的端口

        无实例时抛 IDE_NOT_FOUND,无一匹配时抛 IDE_NO_INSTANCE_INDEXES_FILE,
        多于一个匹配时抛 IDE_MULTIPLE_MATCHES。
        """
        instances = self.discover_all_instances()
        if not instances:
            raise errors.ide_not_found(
                "No SonarQube for IDE instance found on ports "
                f"{PORT_MIN}..{PORT_MAX}. Open PyCharm with the SonarQube for IDE plugin "
                "and ensure the project is loaded."
            )
        if len(instances) == 1:
            # 只有一个实例:直接采纳,避免额外的文件探针请求。
            return instances[0].port

        # 多个实例:分别用目标文件探针并收集匹配项。
        matching: list[int] = []
        last_error = ""
        for inst in instances:
            outcome = self._probe_instance_indexes_file(inst.port, target_file)
            if outcome is True:
                matching.append(inst.port)
            elif isinstance(outcome, str):
                last_error = outcome

        if len(matching) > 1:
            raise errors.ide_multiple_matches(
                "Multiple SonarQube for IDE instances index this file on ports "
                f"{sorted(matching)}. Close duplicate project windows, or set "
                f"{ENV_SONAR_IDE_PORT} to one of them."
            )
        if not matching:
            raise errors.ide_no_instance_indexes_file(
                "Found SonarQube for IDE instances, but none of them index the target file. "
                "Ensure Codex/Claude Code and PyCharm opened the same physical working directory."
                + (f" (last status: {last_error})" if last_error else "")
            )
        return matching[0]

    def _probe_instance_indexes_file(self, port: int, target_file: str) -> bool | str:
        """用目标文件探针单个实例

        Returns:
            实例可分析该文件时返回 True;文件未索引/不支持时返回对应错误码字符串;
            探针结果歧义(实例被跳过)时返回空字符串。
        """
        _NON_MATCH_CODES = {"FILE_NOT_INDEXED", "FILE_TYPE_UNSUPPORTED"}
        try:
            result = self._probe.analyze_files(
                port, [target_file], timeout=self._status_timeout * 4
            )
        except errors.SonarMcpError as e:
            if e.code in _NON_MATCH_CODES:
                return e.code
            _log.debug("Instance on port %d returned %s during file probe", port, e.code)
            return ""
        except Exception as e:
            _log.debug("Instance on port %d raised %s during file probe", port, type(e).__name__)
            return ""

        code = str(result.get("errorCode") or result.get("error_code") or "")
        if code in _NON_MATCH_CODES:
            return code
        raw = result.get("raw") if isinstance(result, dict) else None
        if isinstance(raw, dict) and _file_marked_not_indexed(raw, target_file):
            return "FILE_NOT_INDEXED"
        return True

    # -- internals --

    def _scan_and_match(self, project_root_norm: str) -> int:
        instances = self.discover_all_instances()
        if not instances:
            raise errors.ide_not_found(
                "No SonarQube for IDE instance found on ports "
                f"{PORT_MIN}..{PORT_MAX}. Confirm PyCharm is running with the "
                "SonarQube for IDE plugin installed and the project open."
            )
        if len(instances) == 1:
            port = instances[0].port
            self._cache.set(project_root_norm, port)
            return port

        # 多实例时缺少目标文件无法匹配,调用方应改用 match_instance_for_file。这里直接抛明确错误。
        ports = [i.port for i in instances]
        raise errors.ide_multiple_matches(
            "Multiple SonarQube for IDE instances found on ports "
            f"{ports}. Open exactly one project in PyCharm, or set {ENV_SONAR_IDE_PORT}."
        )


# 触发缓存失效并重新发现时合理的错误码集合(spec 第 10 节)。
_REDISCOVER_CODES = {
    errors.IDE_NOT_FOUND,
    errors.IDE_PORT_INVALID,
    errors.IDE_AUTHORITY_REJECTED,
    errors.IDE_IPV4_CONNECTION_FAILED,
    errors.SONAR_BAD_RESPONSE,
    errors.SONAR_UNAVAILABLE,
}
