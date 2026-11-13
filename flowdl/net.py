"""Persistent HTTP connections for the download engine.

urllib opens a new TCP + TLS connection for every request. A download made of
many pieces then pays the connection set-up (one or more network round trips,
plus the TLS handshake) again and again. This module keeps one connection open
per download thread and sends request after request on it (HTTP keep-alive),
like IDM does. It follows redirects and honours the system proxy settings.
"""

from __future__ import annotations

import base64
import http.client
import os
import socket
import ssl
import urllib.parse
import urllib.request

REDIRECTS = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 10

_ssl_context: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    global _ssl_context
    if _ssl_context is None:
        _ssl_context = ssl.create_default_context()
    return _ssl_context


class HTTPError(Exception):
    def __init__(self, code: int, reason: str, url: str, retry_after: float | None = None):
        super().__init__(f"HTTP {code}: {reason}")
        self.code, self.reason, self.url = code, reason, url
        self.retry_after = retry_after  # seconds the server asked us to wait (429 / 503)


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        import email.utils
        import time
        when = email.utils.parsedate_to_datetime(value).timestamp()
        return max(0.0, when - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


SENSITIVE = ("cookie", "authorization")  # never sent to a different site after a redirect


class Response:
    """An open response. Read it with ``read1``; ``finished`` tells whether the
    connection can be reused for another request."""

    def __init__(self, raw: http.client.HTTPResponse, url: str):
        self.raw = raw
        self.url = url
        self.status = raw.status
        self.headers = raw.headers

    def read1(self, n: int) -> bytes:
        """Whatever has arrived (at most n bytes): low latency, but small pieces."""
        return self.raw.read1(n) if hasattr(self.raw, "read1") else self.raw.read(n)

    def read(self, n: int) -> bytes:
        """Exactly n bytes (less only at the end). Waiting for bigger pieces costs far less
        processor time than handling many small ones (the receive itself runs outside Python)."""
        return self.raw.read(n)

    @property
    def finished(self) -> bool:
        return self.raw.isclosed()


def _proxy_for(scheme: str, host: str) -> tuple[str, int, dict] | None:
    proxies = urllib.request.getproxies()
    proxy = proxies.get(scheme)
    if not proxy or urllib.request.proxy_bypass(host):
        return None
    if "://" not in proxy:
        proxy = "http://" + proxy
    p = urllib.parse.urlsplit(proxy)
    headers = {}
    if p.username:
        creds = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(creds.encode()).decode()
    return p.hostname, p.port or 80, headers


class Connection:
    """One keep-alive connection, re-opened automatically when the host changes."""

    def __init__(self, timeout: float = 30):
        self.timeout = timeout
        self.conn: http.client.HTTPConnection | None = None
        self.key: tuple | None = None
        self.via_proxy_http = False
        self.response: Response | None = None
        self.requests = 0  # requests sent on the current connection (for tests/stats)
        self.aborted = False  # cut on purpose by abort(): never re-send on its own
        self.opened = 0    # connections opened (TCP/TLS set-ups)

    def abort(self) -> None:
        """Wake a read blocked on this connection (called from another thread): the reading
        thread gets an error or the end of the data right away instead of waiting for the
        timeout. Used when the server stops sending without closing the connection."""
        self.aborted = True
        conn = self.conn
        sock = getattr(conn, "sock", None) if conn is not None else None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            if os.name == "nt":
                # On Windows shutting the socket down may leave the other thread's read
                # waiting; closing it does wake it (on POSIX we must not: the number could be
                # reused by another connection while that thread still reads from it).
                try:
                    sock.close()
                except OSError:
                    pass

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
        self.conn, self.key, self.response = None, None, None

    def _connect(self, scheme: str, host: str, port: int) -> None:
        self.close()
        proxy = _proxy_for(scheme, host)
        if scheme == "https":
            if proxy:
                conn = http.client.HTTPSConnection(proxy[0], proxy[1], timeout=self.timeout, context=ssl_context())
                conn.set_tunnel(host, port, headers=proxy[2] or None)
            else:
                conn = http.client.HTTPSConnection(host, port, timeout=self.timeout, context=ssl_context())
            self.via_proxy_http = False
        else:
            if proxy:
                conn = http.client.HTTPConnection(proxy[0], proxy[1], timeout=self.timeout)
                self.proxy_headers = proxy[2]
                self.via_proxy_http = True
            else:
                conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
                self.via_proxy_http = False
        self.conn, self.key = conn, (scheme, host, port)
        self.opened += 1
        self.requests = 0

    def get(self, url: str, headers: dict, start: int | None = None, end: int | None = None,
            cookies: dict | None = None) -> Response:
        """GET ``url`` (optionally a byte range), following redirects. Raises HTTPError for
        4xx/5xx answers. The previous response must have been read to the end; otherwise
        the connection is closed and a new one is opened.

        ``cookies`` maps a host to the Cookie header the browser sends to that host. Like a
        browser, each request only carries the cookies of its own host, and credentials given
        in ``headers`` are not passed on when a redirect leads to another host."""
        if self.response is not None and not self.response.finished:
            self.close()
        if self.aborted:  # cut on purpose while idle or before this request: start clean
            self.close()
            self.aborted = False
        first_host = (urllib.parse.urlsplit(url).hostname or "").lower()
        for _ in range(MAX_REDIRECTS + 1):
            parts = urllib.parse.urlsplit(url)
            scheme = parts.scheme.lower()
            if scheme not in ("http", "https"):
                raise ValueError(f"protocolo não suportado: {scheme}")
            port = parts.port or (443 if scheme == "https" else 80)
            host = parts.hostname or ""
            if self.key != (scheme, host, port):
                self._connect(scheme, host, port)
            path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
            target = url if self.via_proxy_http else path
            hdrs = {"Host": parts.netloc.rsplit("@", 1)[-1], "Accept-Encoding": "identity"}
            for k, v in headers.items():
                if k.lower() in SENSITIVE and host.lower() != first_host:
                    continue  # another site: do not hand it our credentials
                hdrs[k] = v
            if cookies is not None:
                hdrs = {k: v for k, v in hdrs.items() if k.lower() != "cookie"}
                if cookies.get(host.lower()):
                    hdrs["Cookie"] = cookies[host.lower()]
            if self.via_proxy_http:
                hdrs.update(getattr(self, "proxy_headers", {}))
            if start is not None:
                hdrs["Range"] = f"bytes={start}-{'' if end is None else end}"
            raw = self._send(target, hdrs)
            self.requests += 1
            if raw.status in REDIRECTS and raw.getheader("Location"):
                raw.read()  # small body; keeps the connection reusable
                url = urllib.parse.urljoin(url, raw.getheader("Location"))
                continue
            if raw.status >= 400:
                try:
                    raw.read(64 * 1024)
                finally:
                    if not raw.isclosed():
                        self.close()
                raise HTTPError(raw.status, raw.reason, url, parse_retry_after(raw.getheader("Retry-After")))
            self.response = Response(raw, url)
            return self.response
        raise HTTPError(310, "redirecionamentos a mais", url)

    def _send(self, target: str, headers: dict) -> http.client.HTTPResponse:
        for attempt in (1, 2):
            try:
                self.conn.request("GET", target, headers=headers)
                return self.conn.getresponse()
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError,
                    http.client.CannotSendRequest, http.client.BadStatusLine):
                # The server closed an idle keep-alive connection: reconnect once (not when
                # we cut it ourselves because it had stopped answering).
                if attempt == 2 or self.requests == 0 or self.aborted:
                    raise
                scheme, host, port = self.key
                self._connect(scheme, host, port)
        raise AssertionError("unreachable")
