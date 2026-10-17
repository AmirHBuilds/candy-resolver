"""Download one subtitle file safely, with the standard library only (runs in a worker thread):
 - http/https only; redirects are followed by hand, and EVERY hop is checked
 - the hostname is resolved first and the connection goes to that exact validated address, so a hostname that
   later resolves to something internal (DNS rebinding) cannot sneak in; internal/private addresses are refused
 - the size cap is enforced while streaming (not after), and a hard deadline covers slow-drip servers"""
import http.client
import ipaddress
import socket
import ssl
import time
import zlib
from typing import Callable
from urllib.parse import urljoin, urlsplit

MAX_REDIRECTS = 5
DEFAULT_UA = "candyresolver/0.1"
_DROP_HEADERS = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding", "upgrade"}
_CROSS_HOST_DROP = {"authorization", "cookie", "proxy-authorization"}


class FetchError(Exception):
    pass


def is_public_ip(ip: str) -> bool:
    """True only for normal public internet addresses (no loopback, private, link-local, multicast, reserved...)."""
    a = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


def _resolve(host: str, port: int, policy: Callable[[str], bool]) -> list[str]:
    try:
        ips = [str(ipaddress.ip_address(host.strip("[]")))]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror:
            raise FetchError("host not found")
        ips = list(dict.fromkeys(i[4][0] for i in infos))
    if not ips:
        raise FetchError("host not found")
    if not all(policy(ip) for ip in ips):                 # one internal answer is enough to refuse
        raise FetchError("blocked address")
    return ips


class _Conn(http.client.HTTPConnection):
    pass


class _SConn(http.client.HTTPSConnection):
    """TLS to an IP address, but with SNI + certificate check against the real hostname."""
    def __init__(self, ip, port, hostname, timeout, ctx):
        super().__init__(ip, port, timeout=timeout, context=ctx)
        self._hostname = hostname

    def connect(self):
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._hostname)


def fetch_blocking(url: str, headers: dict | None, *, max_bytes: int, timeout_s: float,
                   policy: Callable[[str], bool] = is_public_ip) -> bytes:
    deadline = time.monotonic() + timeout_s
    ctx = ssl.create_default_context()
    custom = {str(k): str(v) for k, v in (headers or {}).items() if str(k).lower() not in _DROP_HEADERS}
    first_host = None

    for _hop in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise FetchError("only http/https links are allowed")
        https = parts.scheme == "https"
        host = parts.hostname
        port = parts.port or (443 if https else 80)
        first_host = first_host or host
        if time.monotonic() >= deadline:
            raise FetchError("timed out")
        ips = _resolve(host, port, policy)

        hostport = (f"[{host}]" if ":" in host else host) + ("" if port == (443 if https else 80) else f":{port}")
        send = {"User-Agent": DEFAULT_UA, "Accept": "*/*", "Accept-Encoding": "gzip, identity", "Host": hostport}
        for k, v in custom.items():
            if host != first_host and k.lower() in _CROSS_HOST_DROP:
                continue                                    # never send credentials to another site
            send[k] = v
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")

        conn, last = None, None
        for ip in ips[:3]:
            c = _SConn(ip, port, host, 10, ctx) if https else _Conn(ip, port, timeout=10)
            try:
                c.connect()
                conn = c
                break
            except (OSError, ssl.SSLError) as e:
                last = e
                c.close()
        if conn is None:
            raise FetchError(f"could not connect ({last.__class__.__name__})")
        try:
            conn.request("GET", path, headers=send)
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308) and resp.getheader("Location"):
                url = urljoin(url, resp.getheader("Location"))
                continue
            if resp.status != 200:
                raise FetchError(f"HTTP {resp.status}")
            cl = resp.getheader("Content-Length", "")
            if cl.isdigit() and int(cl) > max_bytes:
                raise FetchError("file too large")
            enc = (resp.getheader("Content-Encoding") or "identity").lower()
            if enc not in ("identity", "gzip", "x-gzip"):
                raise FetchError(f"unsupported content encoding: {enc}")
            dec = zlib.decompressobj(16 + zlib.MAX_WBITS) if enc != "identity" else None
            out = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise FetchError("timed out")
                chunk = resp.read1(8192)                    # returns as soon as ANY data arrives (not 8192 bytes)
                if not chunk:
                    break
                if dec:
                    data = dec.decompress(chunk, max_bytes + 1 - len(out))
                    while True:
                        out += data
                        if len(out) > max_bytes:
                            raise FetchError("file too large")
                        if not dec.unconsumed_tail:
                            break
                        data = dec.decompress(dec.unconsumed_tail, max_bytes + 1 - len(out))
                else:
                    out += chunk
                    if len(out) > max_bytes:
                        raise FetchError("file too large")   # stop reading right here: the rest is never downloaded
            return bytes(out)
        except (OSError, http.client.HTTPException, zlib.error) as e:
            if isinstance(e, FetchError):
                raise
            raise FetchError(f"download failed ({e.__class__.__name__})")
        finally:
            conn.close()
    raise FetchError("too many redirects")
