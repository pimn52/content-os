"""公开素材 HTTPS 读取：校验 DNS 后直连固定公网地址，TLS 仍核验原主机。"""
import http.client
import ipaddress
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as DNSDeadline
from urllib.error import HTTPError
from urllib.parse import urlsplit


class UnsafePublicURL(ValueError):
    pass


_DNS_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="public-media-dns")


def validate_public_https_url(url):
    try:
        parsed = urlsplit(url)
        if (not isinstance(url, str) or len(url) > 4096 or parsed.scheme != "https"
                or not parsed.hostname or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or "\\" in url or any(ord(c) < 33 or ord(c) == 127 for c in url)):
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii")
        if len(host) > 253 or "%" in host or host.endswith("."):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or any(not label or len(label) > 63
                    or not all(c.isalnum() or c == "-" for c in label) for label in host.split(".")):
                raise ValueError
        else:
            _require_public(address)
        return parsed, host
    except (TypeError, ValueError, AttributeError, UnicodeError):
        raise UnsafePublicURL("仅允许不含凭据的公网 HTTPS 地址") from None


def _require_public(address):
    if (not address.is_global or address.is_multicast or address.is_reserved
            or address.is_unspecified or address.is_loopback or address.is_link_local):
        raise UnsafePublicURL("不允许访问本机、私网或保留网络地址")


def _resolve_public(host, timeout):
    future = _DNS_POOL.submit(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
    try:
        addresses = future.result(timeout=timeout)
    except DNSDeadline:
        future.cancel()
        raise TimeoutError("公开来源 DNS 解析超时") from None
    if not addresses:
        raise OSError("公开来源 DNS 没有返回地址")
    # 混有私网地址也拒绝，不能挑一个公网结果掩盖危险 DNS。
    for _, _, _, _, sockaddr in addresses:
        _require_public(ipaddress.ip_address(sockaddr[0]))
    return addresses[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, resolved, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self._resolved = resolved
        self._transport, self._expired = None, False

    def connect(self):
        family, socktype, protocol, _, sockaddr = self._resolved
        raw = socket.socket(family, socktype, protocol)
        self._transport = raw
        try:
            if self._expired:
                raise TimeoutError("公开来源请求超时")
            raw.settimeout(self.timeout)
            raw.connect(sockaddr)  # 不再次解析域名，防 DNS 重绑定。
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
            self._transport = self.sock
            self.sock.do_handshake()
            if self._expired:
                raise TimeoutError("公开来源请求超时")
        except BaseException:
            self.close()
            raise

    def expire(self):
        self._expired = True
        transport = self._transport
        if transport is not None:
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.close()

    def close(self):
        super().close()
        # HTTPConnection 在 Connection: close 响应头后就会调用 close，正文
        # 仍由 makefile 持有 socket；保留引用供总时限定时器 shutdown。
        if self._transport is not None:
            self._transport.close()


class _Response:
    def __init__(self, response, connection, deadline, timer):
        self._response, self._connection, self._deadline = response, connection, deadline
        self._timer = timer
        self.status, self.headers = response.status, response.headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def read(self, size):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("公开来源读取超时")
        # read1 每次只读一批网络数据，避免慢速持续传输绕过总时间上限。
        sock = self._connection._transport
        if sock is not None:
            sock.settimeout(remaining)
        return self._response.read1(size)

    def close(self):
        self._timer.cancel()
        self._response.close()
        self._connection.close()


def open_public_https(request, *, timeout):
    parsed, host = validate_public_https_url(request.full_url)
    deadline = time.monotonic() + timeout
    resolved = _resolve_public(host, timeout)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("公开来源解析超时")
    connection = _PinnedHTTPSConnection(host, resolved, remaining)
    timer = threading.Timer(remaining, connection.expire)
    timer.daemon = True
    timer.start()
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    try:
        connection.request("GET", path, headers=dict(request.header_items()))
        response = _Response(connection.getresponse(), connection, deadline, timer)
    except BaseException:
        timer.cancel()
        connection.close()
        raise
    if response.status >= 300:
        # 跳转由 provider 逐跳重新校验；不使用代理、Cookie 或登录态。
        raise HTTPError(request.full_url, response.status, "公开来源返回非成功响应", response.headers, response)
    return response
