"""JetBrains MCP 后端的连接配置管理

负责定位、读写、清除用户级 config.json,以及对外部可达性做最小约束。
配置优先级:环境变量 > 配置文件。出于安全考虑,只允许 loopback 地址。
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import platformdirs
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ... import errors
from ...logging_config import get_logger

_log = get_logger("jetbrains.config")

_APP_NAME = "pycharm-code-quality-mcp"
_CONFIG_FILENAME = "config.json"

# 环境变量(优先级最高,覆盖配置文件)
_ENV_URL = "JETBRAINS_MCP_URL"
_ENV_HEADERS_JSON = "JETBRAINS_MCP_HEADERS_JSON"

# 默认传输方式;目前仅实现 streamable-http
_DEFAULT_TRANSPORT = "streamable-http"


class JetBrainsConfig(BaseModel):
    """JetBrains MCP 连接配置

    Attributes:
        url: JetBrains MCP Server 的 streamable-http 端点(必须为 loopback)。
        headers: 附加到 HTTP 请求的请求头;不会出现在日志中。
        transport: 传输方式,固定为 streamable-http。
    """

    model_config = ConfigDict(populate_by_name=True)

    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    transport: str = _DEFAULT_TRANSPORT


def config_dir() -> Path:
    """返回配置目录的绝对路径,目录可能尚不存在"""
    return Path(platformdirs.user_config_dir(_APP_NAME))


def config_file_path() -> Path:
    """返回 config.json 的绝对路径,文件可能尚不存在"""
    return config_dir() / _CONFIG_FILENAME


def is_loopback_url(url: str) -> bool:
    """校验 URL 的 host 是否仅为本机回环地址

    允许:localhost、127.0.0.1、::1,以及带端口的形式。
    拒绝任何远程、局域网或公网地址,降低 SSRF 与误连风险。
    """
    if not url:
        return False
    # 只接受 http(s) 形式,避免 file:// 等奇怪 scheme。
    lowered = url.strip().lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        return False
    # 提取 netloc:scheme://[user@]host[:port]/...
    rest = url.split("://", 1)[1]
    # 去掉 path / query / fragment
    netloc = rest.split("/", 1)[0]
    # 去掉可能的用户信息
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    # 处理 IPv6 的方括号包裹:[::1]:port 或 [::1]
    if netloc.startswith("["):
        end = netloc.find("]")
        if end == -1:
            return False
        host = netloc[1:end]
        # 仅允许 ::1
        return host.strip().lower() in {"::1"}
    # 普通 host[:port],取最后一段以避免 IPv4 中误判
    host = netloc.rsplit(":", 1)[0] if ":" in netloc else netloc
    host = host.strip().lower()
    if host == "localhost" or host == "127.0.0.1":
        return True
    return _is_127_loopback(host)


def _is_127_loopback(host: str) -> bool:
    """判断 host 是否为 127.0.0.0/8 段内的 IPv4 回环地址"""
    parts = host.split(".")
    if len(parts) != 4:
        return False
    if parts[0] != "127":
        return False
    return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def load_config() -> JetBrainsConfig | None:
    """加载配置,优先级为环境变量 > 配置文件

    无任何配置时返回 None,不抛异常。
    配置存在但格式非法或 URL 非 loopback 时,抛出 SonarMcpError。
    """
    # 1. 环境变量(优先级最高)
    env_url = os.environ.get(_ENV_URL, "").strip()
    if env_url:
        headers: dict[str, str] = {}
        env_headers_raw = os.environ.get(_ENV_HEADERS_JSON)
        if env_headers_raw:
            try:
                parsed_headers = json.loads(env_headers_raw)
            except json.JSONDecodeError as e:
                raise errors.jetbrains_invalid_config(
                    f"Environment variable {_ENV_HEADERS_JSON} is not valid JSON: {e}"
                ) from e
            if not isinstance(parsed_headers, dict):
                raise errors.jetbrains_invalid_config(
                    f"Environment variable {_ENV_HEADERS_JSON} must be a JSON object."
                )
            headers = {str(k): str(v) for k, v in parsed_headers.items()}
        return _build_config(url=env_url, headers=headers)

    # 2. 配置文件
    path = config_file_path()
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise errors.jetbrains_invalid_config(f"Config file {path} is not valid JSON: {e}") from e
    except OSError as e:
        raise errors.jetbrains_invalid_config(f"Cannot read config file {path}: {e}") from e

    if not isinstance(data, dict):
        raise errors.jetbrains_invalid_config(
            f"Config file {path} must contain a JSON object at the top level."
        )

    jetbrains_section = data.get("jetbrains")
    if not isinstance(jetbrains_section, dict):
        raise errors.jetbrains_invalid_config(
            f"Config file {path} is missing the 'jetbrains' object."
        )

    url = str(jetbrains_section.get("url", "")).strip()
    headers_field = jetbrains_section.get("headers", {})
    if headers_field is None:
        headers_field = {}
    if not isinstance(headers_field, dict):
        raise errors.jetbrains_invalid_config(
            f"Config file {path}: 'jetbrains.headers' must be a JSON object."
        )
    file_headers = {str(k): str(v) for k, v in headers_field.items()}

    if not url:
        return None
    return _build_config(url=url, headers=file_headers)


def _build_config(*, url: str, headers: dict[str, str]) -> JetBrainsConfig:
    """构造并校验配置:URL 必须 loopback,失败时抛 SonarMcpError"""
    if not is_loopback_url(url):
        raise errors.jetbrains_invalid_config(
            "JetBrains MCP URL must target a loopback address (localhost / 127.0.0.1 / ::1)."
        )
    try:
        return JetBrainsConfig(url=url, headers=headers)
    except ValidationError as e:
        raise errors.jetbrains_invalid_config(f"Invalid JetBrains MCP config: {e}") from e


def save_config(url: str, headers: dict[str, str]) -> None:
    """保存配置到 config.json

    POSIX 文件系统上以 0600 权限保存,避免其他用户读取(可能包含鉴权头)。
    """
    if not is_loopback_url(url):
        raise errors.jetbrains_invalid_config("Refusing to save non-loopback JetBrains MCP URL.")
    # 先验证一遍模型,避免写出非法 JSON。
    cfg = _build_config(url=url, headers=headers)

    path = config_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # 如果旧文件存在且非 0600,先收紧权限避免在 open 阶段泄露。
    if path.exists():
        # 非致命:继续写入,但保留原权限以便用户排查。
        with contextlib.suppress(OSError):
            _chmod_0600(path)

    payload: dict[str, Any] = {
        "jetbrains": {
            "transport": cfg.transport,
            "url": cfg.url,
            "headers": cfg.headers,
        }
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, path)
    _log.info("Saved JetBrains MCP config to %s", path)
    # 最终再次收紧权限,覆盖 os.replace 可能复制的旧权限。
    try:
        _chmod_0600(path)
    except OSError as e:
        _log.warning("Could not tighten permissions on %s: %s", path, e)


def clear_config() -> bool:
    """删除配置文件,文件不存在时返回 False,删除成功返回 True"""
    path = config_file_path()
    if not path.is_file():
        return False
    try:
        path.unlink()
    except OSError as e:
        _log.warning("Failed to remove config file %s: %s", path, e)
        return False
    _log.info("Cleared JetBrains MCP config at %s", path)
    return True


def _chmod_0600(path: Path) -> None:
    """POSIX 系统上把文件权限收紧为 0600,Windows 上为无操作"""
    if os.name == "nt":
        return
    mode = stat.S_IRUSR | stat.S_IWUSR
    path.chmod(mode)
