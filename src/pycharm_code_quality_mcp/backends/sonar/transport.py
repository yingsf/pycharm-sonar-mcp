"""自定义 httpx transport：TCP 连到 127.0.0.1，HTTP 权威保持 localhost

背景（规范第 4 节）：
  * SonarQube for IDE 会校验 HTTP Host/authority。`127.0.0.1:<port>` 会返回
    HTTP 421 Misdirected Request，仅 `localhost:<port>` 被接受。
  * 在某些系统上 `localhost` 会优先解析为 `::1`（IPv6），但 IDE 仅监听
    127.0.0.1（IPv4）。普通 httpx.Client 会先尝试 IPv6 从而失败。
  * 绝不能永久将 URL 改写为 `http://127.0.0.1`，Host 头与逻辑权威必须保持 `localhost`。

解决方案：自定义 httpx transport，先向 127.0.0.1:<port> 打开原始 TCP socket，
再以 `Host: localhost:<port>` 头进行 HTTP/1.1 通信。这样将网络层地址
（IPv4 loopback）与应用层权威（localhost）解耦。

实现上组合 httpx 的 `HTTPTransport`，但强制网络地址为 127.0.0.1，
同时保留出站请求上原始的 Host 头。
"""

from __future__ import annotations

import contextlib
import socket

import httpx

from ...logging_config import get_logger

_log = get_logger("local_transport")

LOOPBACK_IPV4 = "127.0.0.1"
ORIGIN_HEADER = "http://localhost"


class Ipv4LoopbackTransport(httpx.BaseTransport):
    """始终连接到 127.0.0.1，同时保持 Host/Origin 为 localhost 的 httpx transport

    核心手法：将请求交给绑定到已解析 127.0.0.1 地址的底层 httpx.HTTPTransport，
    但把请求 URL 的 host 改写为 `localhost`，从而发出的 `Host` 头与绝对 URI
    仍保留正确的权威。

    Implementation note:
        httpx 的 HTTPTransport 依据 URL host 建立连接。为了在不改动 URL host 的
        前提下强制 IPv4，通过 `httpx.HTTPTransport` 的 `uds` 式间接方式并不足够；
        因此直接基于普通 socket 实现线协议以获得完全控制。

    这是最简洁、最易测试的方式，也避免了随 httpx 版本变动的内部耦合。
    """

    def __init__(
        self,
        port: int,
        *,
        connect_timeout: float = 1.0,
        read_timeout: float = 60.0,
        origin: str = ORIGIN_HEADER,
    ) -> None:
        self.port = port
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.origin = origin

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # URL host 保持 localhost 以保证 Host 头正确，实际连接到 127.0.0.1。
        url = request.url
        host_header = f"localhost:{self.port}"
        target = _request_target(url)

        # 构造干净、去重的头部集合。httpx 的默认头部（Host、Origin、Connection、
        # Accept-Encoding）可能已存在且大小写任意，因此做大小写不敏感的归一化，
        # 避免发出重复/冲突的头部。
        headers = _HeaderSet(dict(request.headers))
        # 始终使用 identity 编码：我们读取原始字节且从不解压，
        # 请求 gzip 会让 gzip 压缩的 JSON 响应看起来像畸形载荷。
        headers.set("accept-encoding", "identity")
        headers.set("host", host_header)
        headers.set("origin", self.origin)
        # 每个 socket 只发一个请求：强制关闭，让服务端拆除连接。
        headers.set("connection", "close")

        body = request.read()

        sock = _connect_ipv4(self.port, self.connect_timeout)
        try:
            _send_request(
                sock,
                method=str(request.method),
                target=target,
                headers=headers.items(),
                body=body,
            )
            status_code, resp_headers, resp_body = _read_response(sock, self.read_timeout)
        finally:
            with contextlib.suppress(OSError):
                sock.close()

        response = httpx.Response(
            status_code=status_code,
            headers=resp_headers,
            content=resp_body,
            request=request,
        )
        return response

    def close(self) -> None:
        # 没有连接池；每个请求各自打开 socket。
        pass


# ---------------------------------------------------------------------------
# 底层 socket 辅助函数（保持模块私有且可测试）
# ---------------------------------------------------------------------------


class _HeaderSet:
    """大小写不敏感、每个名字仅保留最后一个值的头部容器

    HTTP 头部名大小写不敏感，而 Python dict 的键则不是。若用普通 dict 存储头部，
    会让冲突的变体（``connection`` 与 ``Connection``）共存并都被写入线缆，
    这违反 RFC 7230。本包装器按小写名归一化查找，使最终线缆输出每个头部只有一个值。
    """

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._items: dict[str, tuple[str, str]] = {}
        if initial:
            for k, v in initial.items():
                self.set(k, v)

    def set(self, name: str, value: str) -> None:
        self._items[name.lower()] = (name, value)

    def items(self) -> list[tuple[str, str]]:
        return list(self._items.values())


def _connect_ipv4(port: int, timeout: float) -> socket.socket:
    """打开到 127.0.0.1:<port> 的 TCP socket，失败时抛出 httpx.ConnectError"""
    try:
        sock = socket.create_connection((LOOPBACK_IPV4, port), timeout=timeout)
    except ConnectionRefusedError as e:
        raise httpx.ConnectError(f"Connection refused to {LOOPBACK_IPV4}:{port}") from e
    except TimeoutError as e:
        raise httpx.ConnectTimeout(f"Timeout connecting to {LOOPBACK_IPV4}:{port}") from e
    except OSError as e:
        raise httpx.ConnectError(f"Failed to connect to {LOOPBACK_IPV4}:{port}: {e}") from e
    # 为低延迟的本地回环流量禁用 Nagle。
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


def _request_target(url: httpx.URL) -> str:
    """为 HTTP 请求行构造 request-target（path + query）"""
    path = url.path or "/"
    if url.query:
        return f"{path}?{url.query.decode('latin-1')}"
    return path


def _send_request(
    sock: socket.socket,
    *,
    method: str,
    target: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> None:
    """向 socket 写入一条完整的 HTTP/1.1 请求

    头部已由调用方去重（见 ``_HeaderSet``）。本函数在规范位置
    （紧随请求行之后）发出 Host 与 Content-Length，其余头部按原顺序发出。
    """
    # 先写请求行。
    lines = [f"{method} {target} HTTP/1.1"]
    # Host 始终紧跟请求行；调用方保证其存在。
    insert_at = 1
    for k, v in headers:
        if k.lower() == "host":
            lines.insert(insert_at, f"{k}: {v}")
            insert_at += 1
    # Content-Length 由请求体推导，保持其权威性。
    if body:
        lines.insert(insert_at, f"Content-Length: {len(body)}")
    # 其余头部（排除已处理的 host/content-length）按原顺序。
    for k, v in headers:
        kl = k.lower()
        if kl in ("host", "content-length"):
            continue
        lines.append(f"{k}: {v}")

    head = "\r\n".join(lines) + "\r\n\r\n"
    sock.sendall(head.encode("latin-1") + body)


def _read_response(
    sock: socket.socket,
    timeout: float,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """读取完整的 HTTP/1.1 响应，支持 Content-Length 与分块传输"""
    sock.settimeout(timeout)
    buf = _recv_until_headers(sock)
    head, rest = _split_head(buf)
    status_code, headers = _parse_status_and_headers(head)

    transfer_encoding = ""
    content_length: int | None = None
    for k, v in headers:
        if k.lower() == "transfer-encoding":
            transfer_encoding = v.lower()
        elif k.lower() == "content-length":
            try:
                content_length = int(v)
            except ValueError:
                content_length = None

    if "chunked" in transfer_encoding:
        body = _read_chunked(sock, rest)
    elif content_length is not None:
        body = _read_fixed(sock, rest, content_length)
    else:
        # 没有长度信息：读到 EOF（Connection: close 语义）。
        body = _read_until_eof(sock, rest)

    return status_code, headers, body


def _recv_until_headers(sock: socket.socket) -> bytearray:
    """从 socket 读取直到出现 \\r\\n\\r\\n（头部结束）"""
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > 1 * 1024 * 1024:  # 1 MiB 头部上限保护
            raise httpx.ReadError("Response headers exceeded 1 MiB")
    return buf


def _split_head(buf: bytearray) -> tuple[bytes, bytes]:
    idx = buf.find(b"\r\n\r\n")
    if idx == -1:
        raise httpx.ReadError("Incomplete response (no header terminator)")
    head = bytes(buf[:idx])
    rest = bytes(buf[idx + 4 :])
    return head, rest


def _parse_status_and_headers(head: bytes) -> tuple[int, list[tuple[str, str]]]:
    text = head.decode("latin-1", errors="replace")
    lines = text.split("\r\n")
    if not lines or not lines[0].startswith("HTTP/"):
        raise httpx.ReadError(f"Malformed status line: {lines[0]!r}")
    status_line = lines[0]
    parts = status_line.split(" ", 2)
    try:
        status_code = int(parts[1])
    except (IndexError, ValueError) as e:
        raise httpx.ReadError(f"Bad status line: {status_line!r}") from e
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        headers.append((k.strip(), v.strip()))
    return status_code, headers


def _read_fixed(sock: socket.socket, initial: bytes, length: int) -> bytes:
    out = bytearray(initial)
    while len(out) < length:
        chunk = sock.recv(min(65536, length - len(out)))
        if not chunk:
            break
        out.extend(chunk)
    return bytes(out)


def _recv_until(sock: socket.socket, pending: bytearray, delimiter: bytes) -> bytearray | None:
    """从 ``sock`` 读取并扩展 ``pending``，直到出现 ``delimiter``

    返回填充后的 pending 缓冲区；若 socket 在遇到分隔符前关闭则返回 None。
    """
    while delimiter not in pending:
        chunk = sock.recv(65536)
        if not chunk:
            return None
        pending.extend(chunk)
    return pending


def _fill_at_least(sock: socket.socket, pending: bytearray, length: int) -> bool:
    """扩展 ``pending`` 直到至少拥有 ``length`` 字节；遇 EOF 返回 False"""
    while len(pending) < length:
        chunk = sock.recv(65536)
        if not chunk:
            return False
        pending.extend(chunk)
    return True


def _read_chunked(sock: socket.socket, initial: bytes) -> bytes:
    out = bytearray()
    pending = bytearray(initial)
    while True:
        if _recv_until(sock, pending, b"\r\n") is None:
            return bytes(out)
        size_line, _, rest = bytes(pending).partition(b"\r\n")
        pending = bytearray(rest)
        try:
            size = int(size_line.split(b";")[0].strip(), 16)
        except ValueError as e:
            raise httpx.ReadError(f"Bad chunk size: {size_line!r}") from e
        if size == 0:
            # 消费完所有块 trailer 之后的尾随 CRLF，然后停止。
            _recv_until(sock, pending, b"\r\n")
            break
        # 确保至少有 `size` 字节加上尾随的 CRLF。
        _fill_at_least(sock, pending, size + 2)
        out.extend(pending[:size])
        del pending[: size + 2]  # 跳过尾随 CRLF
    return bytes(out)


def _read_until_eof(sock: socket.socket, initial: bytes) -> bytes:
    out = bytearray(initial)
    while True:
        try:
            chunk = sock.recv(65536)
        except TimeoutError as e:
            raise httpx.ReadError("Timeout reading response body") from e
        if not chunk:
            break
        out.extend(chunk)
    return bytes(out)


# ---------------------------------------------------------------------------
# 公开客户端工厂
# ---------------------------------------------------------------------------


def build_local_client(
    port: int,
    *,
    connect_timeout: float = 1.0,
    read_timeout: float = 60.0,
    transport_override: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """构建一个绑定到 127.0.0.1:<port> 且权威为 localhost 的 httpx.Client

    trust_env=False 会禁用 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 与 netrc，确保绝不把
    回环流量路由到系统代理。自定义 transport 保证无论 `localhost` 如何解析，
    TCP 端点都是 127.0.0.1（IPv4）。

    测试时可用 `transport_override` 注入 mock transport。
    """
    transport: httpx.BaseTransport = transport_override or Ipv4LoopbackTransport(
        port,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
    )
    return httpx.Client(
        transport=transport,
        trust_env=False,
        # Base URL 以 localhost 作为权威——这是应用层名称。
        base_url=f"http://localhost:{port}",
        headers={
            "Origin": ORIGIN_HEADER,
            "Accept": "application/json",
        },
        # 不希望支持代理。
    )
