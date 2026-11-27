"""Persistent HTTP connections for the download engine.

urllib opens a new TCP + TLS connection for every request. A download made of
many pieces then pays the connection set-up (one or more network round trips,
plus the TLS handshake) again and again. This module keeps one connection open
per download thread and sends request after request on it (HTTP keep-alive),
like IDM does. It follows redirects and honours the system proxy settings.
"""

from __future__ import annotations

import http.client
import os
import socket
import threading
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
    def __init__(self, code: int, reason: str, url: str, retry_after: float | None = None,
                 content_range: str = ""):
        super().__init__(f"HTTP {code}: {reason}")
        self.code, self.reason, self.url = code, reason, url
        self.retry_after = retry_after  # seconds the server asked us to wait (429 / 503)
        self.content_range = content_range  # e.g. "bytes */0" with 416: the file is empty


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
        self.posted = False

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


HAPPY_DELAY = 0.25  # start the next address if the first has not answered by then


def happy_connect(address, timeout=None, source_address=None):
    """Like socket.create_connection, but the addresses (IPv6 and IPv4 mixed) are tried
    side by side, each one HAPPY_DELAY after the previous, and the first that connects wins
    ("Happy Eyeballs", as browsers do). Before: a broken IPv6 route made every new
    connection wait for the whole timeout (30 s) before trying IPv4."""
    host, port = address[0], address[1]
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if len({i[0] for i in infos}) < 2:  # one family only: the usual way
        return socket.create_connection(address, timeout, source_address)
    v6 = [i for i in infos if i[0] == socket.AF_INET6]
    v4 = [i for i in infos if i[0] != socket.AF_INET6]
    order = []
    while v6 or v4:  # alternate the families, the system's preferred one first
        for group in ((v6, v4) if infos[0][0] == socket.AF_INET6 else (v4, v6)):
            if group:
                order.append(group.pop(0))
    winner: list = []
    errors: list = []
    done = threading.Event()
    lock = threading.Lock()

    def attempt(info):
        family, kind, proto, _name, sockaddr = info
        sock = None
        try:
            sock = socket.socket(family, kind, proto)  # may fail: no IPv6 on this computer
            if timeout is not None:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
        except OSError as exc:
            if sock is not None:
                sock.close()
            with lock:
                errors.append(exc)
                if len(errors) == len(order):
                    done.set()
            return
        with lock:
            if winner:
                sock.close()  # another address was faster
                return
            winner.append(sock)
        done.set()

    for n, info in enumerate(order):
        threading.Thread(target=attempt, args=(info,), daemon=True, name="connect").start()
        if done.wait(HAPPY_DELAY if n < len(order) - 1 else None if timeout is None else timeout):
            break
    else:
        done.wait(timeout)
    with lock:
        if winner:
            return winner[0]
        if errors:
            raise errors[0]
    raise socket.timeout("timed out")


_PROXY_CACHE: dict = {}  # (scheme, host) -> (when, answer)


def _proxy_for(scheme: str, host: str):
    """The proxy for this address (a proxy.Route, or None). The system's is remembered for a
    minute: on Windows the check reads the registry and may look the name up in DNS, for every
    new connection. One chosen in the app is used as it is."""
    import time
    from . import proxy
    if proxy.current()["mode"] != "system":
        return proxy.route(scheme, host)
    key, now = (scheme, host), time.monotonic()
    cached = _PROXY_CACHE.get(key)
    if cached and now - cached[0] < 60:
        return cached[1]
    answer = proxy.route(scheme, host)
    if len(_PROXY_CACHE) > 200:
        _PROXY_CACHE.clear()
    _PROXY_CACHE[key] = (now, answer)
    return answer


_SITE_CREDS: dict = {}  # (host, user) -> proxy.Credentials: a site's user / password from its link


def _site_credentials(parts):
    if not parts.username:
        return None
    from . import proxy
    user = urllib.parse.unquote(parts.username)
    password = urllib.parse.unquote(parts.password or "")
    key = ((parts.hostname or "").lower(), user)
    creds = _SITE_CREDS.get(key)
    if creds is None or creds.password != password:
        if len(_SITE_CREDS) > 100:
            _SITE_CREDS.clear()
        creds = _SITE_CREDS[key] = proxy.Credentials(user, password)
    return creds


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
        self.address: tuple[str, str] | None = None  # (host, ip): use this address of the host
        self.peer: str | None = None  # the address the last request went to
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
                # On Windows shutting the socket down leaves the other thread's read waiting;
                # really closing it (closesocket) wakes it. socket.close() would only mark it,
                # since the HTTP response still holds it open: close the handle itself, which
                # also marks the socket object invalid so it is never closed twice. (Not on
                # POSIX: shutdown already wakes the read there.)
                try:
                    real_close = getattr(sock, "_real_close", None)
                    (real_close or sock.close)()
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
        from . import proxy as proxies
        self.close()
        proxy = _proxy_for(scheme, host)
        self.route = proxy
        self.via_proxy_http = bool(proxy and proxy.kind == "http" and scheme == "http")
        if self.via_proxy_http:
            # Plain HTTP through an HTTP proxy: the request names the whole address.
            conn = http.client.HTTPConnection(proxy.host, proxy.port, timeout=self.timeout)
        elif scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=self.timeout, context=ssl_context())
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
        if proxy and proxy.kind == "socks5":
            # The socket comes through the SOCKS proxy; TLS (and the certificate check for the
            # site's name) on top of it, as without a proxy.
            conn._create_connection = lambda addr, timeout=None, source=None, r=proxy: \
                proxies.socks5_connect(r, host, port, timeout)
        elif proxy and not self.via_proxy_http:
            # HTTPS through an HTTP proxy: our own CONNECT (it answers Basic and Digest).
            conn._create_connection = lambda addr, timeout=None, source=None, r=proxy: \
                proxies.http_tunnel(r, host, port, timeout)
        if not proxy and self.address and self.address[0] == host.lower():
            # Connect to one chosen address of this server (it has several): the name is still
            # used for the Host header and the HTTPS certificate check, exactly as before.
            ip = self.address[1]
            conn._create_connection = lambda addr, timeout=None, source=None: socket.create_connection(
                (ip, addr[1]), timeout, source)
        elif not proxy:
            conn._create_connection = happy_connect
        self.conn, self.key = conn, (scheme, host, port)
        self.opened += 1
        self.requests = 0

    def get(self, url: str, headers: dict, start: int | None = None, end: int | None = None,
            cookies: dict | None = None, post: dict | None = None) -> Response:
        """GET ``url`` (optionally a byte range), following redirects. Raises HTTPError for
        4xx/5xx answers. The previous response must have been read to the end; otherwise
        the connection is closed and a new one is opened.

        ``cookies`` maps a host to the Cookie header the browser sends to that host. Like a
        browser, each request only carries the cookies of its own host, and credentials given
        in ``headers`` are not passed on when a redirect leads to another host.

        ``post`` ({"body": bytes, "type": content type}): send a form's POST instead (no
        range). After a 301/302/303 the browser asks the new address with a plain GET, and
        so do we; 307/308 repeat the POST."""
        if self.response is not None and not self.response.finished:
            self.close()
        if self.aborted:  # cut on purpose while idle or before this request: start clean
            self.close()
            self.aborted = False
        from . import proxy as proxies
        first_host = (urllib.parse.urlsplit(url).hostname or "").lower()
        auth_tries = 0
        for _ in range(MAX_REDIRECTS + 3):
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
            route = getattr(self, "route", None)
            if self.via_proxy_http and route is not None:
                hdrs.update(route.headers)
                pcreds = proxies.proxy_credentials(route)
                if pcreds:
                    hdrs["Proxy-Authorization"] = pcreds.header("POST" if post else "GET", target)
            creds = _site_credentials(parts) if host.lower() == first_host else None
            if creds and not any(k.lower() == "authorization" for k in hdrs):
                answer = creds.header("POST" if post else "GET", target)
                if answer:
                    hdrs["Authorization"] = answer
            if start is not None and post is None:
                hdrs["Range"] = f"bytes={start}-{'' if end is None else end}"
            if post is not None:
                hdrs["Content-Type"] = post.get("type") or "application/x-www-form-urlencoded"
                if post.get("origin"):
                    hdrs["Origin"] = post["origin"]
            raw = self._send(target, hdrs, post["body"] if post is not None else None)
            self.requests += 1
            # A user / password: the site's (from the link) or the proxy's. Asked for: answer
            # its challenge (Basic or Digest) and ask once more.
            if raw.status in (401, 407) and auth_tries < 2:
                who = creds if raw.status == 401 else (proxies.proxy_credentials(route)
                                                       if self.via_proxy_http and route else None)
                found = proxies.pick_challenge(raw.headers.get_all(
                    "WWW-Authenticate" if raw.status == 401 else "Proxy-Authenticate") or [])
                if who is not None and found is not None and (
                        auth_tries == 0 or found[1].get("stale", "").lower() == "true"):
                    who.learn(*found)
                    auth_tries += 1
                    raw.read()
                    continue
            if raw.status in REDIRECTS and raw.getheader("Location"):
                raw.read()  # small body; keeps the connection reusable
                url = urllib.parse.urljoin(url, raw.getheader("Location"))
                if raw.status not in (307, 308):
                    post = None  # (a browser follows with GET)
                continue
            if raw.status >= 400:
                try:
                    raw.read(64 * 1024)
                finally:
                    if not raw.isclosed():
                        self.close()
                raise HTTPError(raw.status, raw.reason, url, parse_retry_after(raw.getheader("Retry-After")),
                                raw.getheader("Content-Range") or "")
            self.response = Response(raw, url)
            self.response.posted = post is not None  # (the form's answer itself, not a GET)
            return self.response
        raise HTTPError(310, "redirecionamentos a mais", url)

    def _send(self, target: str, headers: dict, body: bytes | None = None) -> http.client.HTTPResponse:
        for attempt in (1, 2):
            try:
                self.conn.request("GET" if body is None else "POST", target, body=body, headers=headers)
                try:  # which of the server's addresses we reached (before it may be closed)
                    self.peer = self.conn.sock.getpeername()[0] if self.conn.sock else None
                except OSError:
                    self.peer = None
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


def addresses(host: str, port: int) -> list[str]:
    """The different addresses a server name has (big servers / CDNs have several)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return []
    out = []
    for family, *_rest, sockaddr in infos:
        ip = sockaddr[0]
        if ip not in out:
            out.append(ip)
    return out
