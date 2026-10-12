"""Plain HTTP(S) requests, bounded for a research tool.

- Public internet only. Every connection, including each redirect hop, resolves the host,
  refuses it if any address is loopback, private, link-local or otherwise not global, and
  then connects to an address it checked (so DNS can't swap in a local one in between).
- Bounded: total deadline, per-read socket timeout, redirect limit, and a cap on the
  (decompressed) body size; anything cut off is reported as truncated.
- Nothing ambient: no proxies, cookies, credentials or other state. Requests carry only
  the headers below. URLs with a user:password part are refused.

The address check lives in this plugin, not in whim's sandbox: the `net` permission gives
the sandbox the host's whole network, so it guards against following links into local
services by accident; it is not a boundary for code that chooses to ignore it.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time
import zlib
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
MAX_BYTES = 25 * 1024 * 1024
MAX_REDIRECTS = 8
READ_TIMEOUT = 20.0
_TLS = ssl.create_default_context()


class FetchError(Exception):
    pass


@dataclass
class Response:
    url: str              # the final URL, after redirects
    status: int
    reason: str
    headers: http.client.HTTPMessage
    body: bytes
    truncated: bool       # the body was longer than the size limit
    redirects: list[str]  # the URLs that redirected, in order

    @property
    def content_type(self) -> str:
        return self.headers.get_content_type() if self.headers.get("Content-Type") else ""

    @property
    def charset(self) -> str | None:
        return self.headers.get_content_charset()


def normalize(url: str) -> str:
    """An absolute http(s) URL with an ASCII host and a percent-encoded path and query.
    A bare 'example.com/x' becomes https://example.com/x. The fragment is dropped."""
    url = url.strip().removeprefix("<").removesuffix(">")  # as Markdown writes some links
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", url):
        url = "https://" + url
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise FetchError(f"only http and https URLs can be fetched, not {scheme}:")
    if parts.username is not None or parts.password is not None:
        raise FetchError("URLs with a user name or password are not supported")
    try:
        host, port = parts.hostname, parts.port
        host = host.encode("idna").decode("ascii") if host else ""
    except (ValueError, UnicodeError) as e:
        raise FetchError(f"invalid URL: {e}") from None
    if not host:
        raise FetchError("the URL has no host name")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=~")
    query = quote(parts.query, safe="/%:@!$&'()*+,;=~?")
    return urlunsplit((scheme, netloc, path, query, ""))


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _connect(address, timeout=None, source_address=None, *args, **kwargs) -> socket.socket:
    """Replaces http.client's socket.create_connection: resolve, check, connect."""
    host, port = address
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"could not resolve {host}: {e.strerror}") from None
    for info in infos:
        if not _is_public(info[4][0]):
            raise FetchError(f"{host} resolves to a non-public address ({info[4][0]}); "
                             "only the public internet can be fetched")
    error: OSError | None = None
    for family, kind, proto, _name, sockaddr in infos:
        sock = socket.socket(family, kind, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
            return sock
        except OSError as e:
            sock.close()
            error = e
    raise FetchError(f"could not connect to {host}: {error}")


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise FetchError("timed out")
    return min(left, READ_TIMEOUT)


def _read(resp: http.client.HTTPResponse, sock, deadline: float, max_bytes: int) -> tuple[bytes, bool]:
    encoding = (resp.getheader("Content-Encoding") or "").strip().lower()
    if encoding not in ("", "identity", "gzip", "x-gzip", "deflate"):
        raise FetchError(f"the server sent {encoding}-compressed data, which can't be decoded here")
    inflate = zlib.decompressobj(zlib.MAX_WBITS | 32) if encoding in ("gzip", "x-gzip", "deflate") else None
    body = bytearray()
    while True:
        sock.settimeout(_remaining(deadline))  # read1 is one socket read: never past the deadline
        chunk = resp.read1(65536)
        if not chunk:
            return bytes(body), False
        if inflate is not None:
            try:
                chunk = inflate.decompress(chunk, max_bytes + 1 - len(body))
            except zlib.error as e:
                raise FetchError(f"could not decompress the response ({encoding}): {e}") from None
        body += chunk
        if len(body) > max_bytes:
            return bytes(body[:max_bytes]), True


def get(url: str, *, accept: str, deadline: float, max_bytes: int = MAX_BYTES,
        headers: dict[str, str] | None = None, json_body: bytes | None = None) -> Response:
    """GET `url` (or POST `json_body`, which only the plugin's fixed search providers use),
    following up to MAX_REDIRECTS redirects. Raises FetchError for anything that prevents
    an HTTP response; HTTP error statuses are returned, not raised."""
    url = normalize(url)
    redirects: list[str] = []
    while True:
        parts = urlsplit(url)
        cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        kwargs = {"context": _TLS} if parts.scheme == "https" else {}
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname  # IPv6 literal
        conn = cls(host, parts.port, timeout=_remaining(deadline), **kwargs)
        conn._create_connection = _connect
        try:
            sent = {"User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "en-US,en;q=0.8",
                    "Accept-Encoding": "gzip, deflate", **(headers or {})}
            if json_body is not None:
                sent["Content-Type"] = "application/json"
            conn.request("GET" if json_body is None else "POST", urlunsplit(("", "", parts.path or "/", parts.query, "")),
                         body=json_body, headers=sent)
            sock = conn.sock  # the response may detach it from the connection
            resp = conn.getresponse()
            location = resp.getheader("Location")
            if resp.status in (301, 302, 303, 307, 308) and location and json_body is None:
                if len(redirects) >= MAX_REDIRECTS:
                    raise FetchError(f"stopped after {MAX_REDIRECTS} redirects (last: {url})")
                redirects.append(url)
                url = normalize(urljoin(url, location.strip()))
                continue
            body, truncated = _read(resp, sock, deadline, max_bytes)
            return Response(url, resp.status, resp.reason, resp.headers, body, truncated, redirects)
        except ssl.SSLCertVerificationError as e:
            raise FetchError(f"TLS certificate check failed for {parts.hostname}: {e.verify_message}") from None
        except (TimeoutError, socket.timeout):
            raise FetchError(f"timed out talking to {parts.hostname}") from None
        except (OSError, http.client.HTTPException) as e:
            raise FetchError(f"connection to {parts.hostname} failed: {type(e).__name__}: {e}") from None
        finally:
            conn.close()
