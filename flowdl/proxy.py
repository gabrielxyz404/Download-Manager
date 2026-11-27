"""Proxies chosen in the app (HTTP or SOCKS5, as other download managers offer) and HTTP
authentication (Basic and Digest) for sites and proxies.

The default stays the system's proxy (Windows settings). Chosen here instead:
- "none": always straight to the site;
- "http": an HTTP proxy (HTTPS goes through it with CONNECT);
- "socks5": a SOCKS5 proxy (RFC 1928, user/password RFC 1929), e.g. Tor at 127.0.0.1:9150.
  With remote DNS (the default) the proxy looks the name up: the site's name never goes to
  the local DNS (what Tor users want).
Addresses of this computer (localhost...) go straight, unless taken out of the list of
addresses without proxy.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import re
import socket
import threading
import urllib.parse
import urllib.request

MODES = ("system", "none", "http", "socks5")
ALWAYS_DIRECT = ("localhost", "127.0.0.1", "::1")
DEFAULT_BYPASS = list(ALWAYS_DIRECT)


class ProxyError(OSError):
    """The proxy refused or failed: said in words the user understands."""


class Route:
    """How to reach one site: through which proxy, with which credentials."""

    def __init__(self, kind: str, host: str, port: int, user: str = "", password: str = "",
                 remote_dns: bool = True, headers: dict | None = None):
        self.kind, self.host, self.port = kind, host, port
        self.user, self.password, self.remote_dns = user, password, remote_dns
        self.headers = headers or {}  # (system proxies: credentials found in the address)

    def __repr__(self) -> str:  # (no password)
        return f"Route({self.kind} {self.host}:{self.port})"


_config = {"mode": "system"}
_lock = threading.Lock()


def configure(mode: str = "system", host: str = "", port: int = 0, user: str = "", password: str = "",
              remote_dns: bool = True, bypass=None) -> None:
    """Set the app's proxy (from the settings). A mode without a host means no proxy."""
    mode = mode if mode in MODES else "system"
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 0
    with _lock:
        _config.clear()
        _config.update(mode=mode, host=(host or "").strip(), port=port if 0 < port < 65536 else 0,
                       user=user or "", password=password or "", remote_dns=bool(remote_dns),
                       bypass=[b.strip().lower() for b in (DEFAULT_BYPASS if bypass is None else bypass)
                               if isinstance(b, str) and b.strip()])


def current() -> dict:
    with _lock:
        return dict(_config)


def _bypassed(host: str, bypass: list) -> bool:
    host = (host or "").lower().strip("[]")
    for b in bypass:
        b = b.lstrip("*").lstrip(".") if b.startswith("*") else b
        if host == b or host.endswith("." + b):
            return True
    return False


def route(scheme: str, host: str) -> Route | None:
    """The proxy for this site, or None for a direct connection."""
    c = current()
    if c["mode"] == "none":
        return None
    if c["mode"] in ("http", "socks5"):
        if not c["host"] or not c["port"] or _bypassed(host, c["bypass"]):
            return None
        return Route(c["mode"], c["host"], c["port"], c["user"], c["password"], c["remote_dns"])
    if (host or "").lower() in ALWAYS_DIRECT:
        return None
    return _system_route(scheme, host)


def _system_route(scheme: str, host: str) -> Route | None:
    """The system's proxy (Windows settings / environment), as before."""
    proxies = urllib.request.getproxies()
    proxy = proxies.get(scheme)
    if not proxy or urllib.request.proxy_bypass(host):
        return None
    if "://" not in proxy:
        proxy = "http://" + proxy
    p = urllib.parse.urlsplit(proxy)
    kind = "socks5" if p.scheme.startswith("socks") else "http"
    user = urllib.parse.unquote(p.username or "")
    password = urllib.parse.unquote(p.password or "")
    return Route(kind, p.hostname or "", p.port or (1080 if kind == "socks5" else 80), user, password)


def url_for_tools() -> str | None:
    """The chosen proxy as an address, for yt-dlp (videos). None: the system's / none."""
    c = current()
    if c["mode"] not in ("http", "socks5") or not c["host"] or not c["port"]:
        return None
    scheme = "http" if c["mode"] == "http" else ("socks5h" if c["remote_dns"] else "socks5")
    auth = ""
    if c["user"]:
        auth = urllib.parse.quote(c["user"], safe="") + ":" + urllib.parse.quote(c["password"], safe="") + "@"
    host = f"[{c['host']}]" if ":" in c["host"] else c["host"]
    return f"{scheme}://{auth}{host}:{c['port']}"


# ------------------------------------------------------------------ SOCKS5 (RFC 1928 / 1929)
def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ProxyError("o proxy fechou a ligação")
        data += chunk
    return data


SOCKS_ERRORS = {
    1: "falha geral do proxy", 2: "o proxy não permite esta ligação", 3: "rede inacessível",
    4: "o site está inacessível", 5: "ligação recusada pelo site", 6: "o tempo esgotou",
    7: "comando não suportado", 8: "tipo de endereço não suportado",
}


def socks5_connect(r: Route, host: str, port: int, timeout=None) -> socket.socket:
    """A socket to host:port through a SOCKS5 proxy (ready for HTTP or TLS on top). With the
    name looked up here, each of its addresses in turn (seen on Windows: "localhost" is ::1
    first, and the site listened on 127.0.0.1 only)."""
    try:
        ipaddress.ip_address(host.strip("[]"))
        targets = [host]
    except ValueError:
        if r.remote_dns:
            targets = [host]
        else:
            targets = list(dict.fromkeys(i[4][0] for i in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)))[:4]
    for n, target in enumerate(targets):
        try:
            return _socks5_once(r, target, port, timeout)
        except ProxyError as exc:
            if n == len(targets) - 1 or getattr(exc, "code", 0) not in (3, 4, 5):
                raise
    raise ProxyError("o proxy não conseguiu chegar ao site")


def _socks5_once(r: Route, host: str, port: int, timeout=None) -> socket.socket:
    from .net import happy_connect
    sock = happy_connect((r.host, r.port), timeout)
    try:
        methods = b"\x00\x02" if r.user else b"\x00"
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        ver, method = _recv_exact(sock, 2)
        if ver != 5:
            raise ProxyError("isto não é um proxy SOCKS5")
        if method == 0x02:
            user, pw = r.user.encode()[:255], r.password.encode()[:255]
            sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(pw)]) + pw)
            if _recv_exact(sock, 2)[1] != 0:
                raise ProxyError("o proxy recusou o utilizador ou a senha")
        elif method != 0x00:
            raise ProxyError("o proxy pede autenticação: indique utilizador e senha"
                             if not r.user else "o proxy não aceita este tipo de autenticação")
        try:
            ip = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            ip = None
        if ip is None:  # the proxy looks the name up (Tor: nothing leaks to the local DNS)
            name = host.encode("idna")[:255]
            addr = b"\x03" + bytes([len(name)]) + name
        else:
            addr = (b"\x01" if ip.version == 4 else b"\x04") + ip.packed
        sock.sendall(b"\x05\x01\x00" + addr + port.to_bytes(2, "big"))
        ver, rep, _rsv, atyp = _recv_exact(sock, 4)
        if rep != 0:
            exc = ProxyError(f"o proxy recusou a ligação: {SOCKS_ERRORS.get(rep, f'erro {rep}')}")
            exc.code = rep
            raise exc
        size = {1: 4, 4: 16}.get(atyp)
        if size is None:
            size = _recv_exact(sock, 1)[0]
        _recv_exact(sock, size + 2)  # the address the proxy used (not needed)
        return sock
    except BaseException:
        sock.close()
        raise


# ------------------------------------------------------------------ HTTP authentication
def parse_challenge(header: str) -> tuple[str, dict]:
    """ 'Digest realm="x", nonce="y", qop="auth"' -> ("digest", {...}). """
    scheme, _, rest = (header or "").strip().partition(" ")
    params = {}
    for m in re.finditer(r'([\w-]+)\s*=\s*("((?:[^"\\]|\\.)*)"|[^,\s]+)', rest):
        params[m.group(1).lower()] = m.group(3).replace('\\"', '"') if m.group(3) is not None else m.group(2)
    return scheme.lower(), params


def pick_challenge(headers: list[str]) -> tuple[str, dict] | None:
    """Digest if offered (the password never travels), else Basic."""
    found = [parse_challenge(h) for h in headers if h]
    for want in ("digest", "basic"):
        for scheme, params in found:
            if scheme == want:
                return scheme, params
    return None


class Credentials:
    """A user and password for one site or proxy, answering its challenges. Digest keeps the
    last challenge so the next requests are answered without being refused first."""

    def __init__(self, user: str, password: str):
        self.user, self.password = user, password
        self.scheme: str | None = None
        self.params: dict = {}
        self.nc = 0
        self._lock = threading.Lock()

    def learn(self, scheme: str, params: dict) -> None:
        with self._lock:
            self.scheme, self.params, self.nc = scheme, params, 0

    def header(self, method: str, uri: str) -> str | None:
        with self._lock:
            if self.scheme == "basic":
                raw = f"{self.user}:{self.password}".encode()
                return "Basic " + base64.b64encode(raw).decode()
            if self.scheme != "digest":
                return None
            self.nc += 1
            return digest_header(self.user, self.password, method, uri, self.params, self.nc)


def digest_header(user: str, password: str, method: str, uri: str, p: dict, nc: int,
                  cnonce: str | None = None) -> str:
    """RFC 7616 (MD5, SHA-256, and their -sess forms; qop auth)."""
    algorithm = (p.get("algorithm") or "MD5").upper()
    name = {"MD5": "md5", "MD5-SESS": "md5", "SHA-256": "sha256", "SHA-256-SESS": "sha256"}.get(algorithm)
    if name is None:
        raise ProxyError(f"autenticação Digest com {algorithm}: não suportada")
    h = lambda s: hashlib.new(name, s.encode()).hexdigest()
    realm, nonce = p.get("realm", ""), p.get("nonce", "")
    cnonce = cnonce or os.urandom(8).hex()
    ha1 = h(f"{user}:{realm}:{password}")
    if algorithm.endswith("-SESS"):
        ha1 = h(f"{ha1}:{nonce}:{cnonce}")
    ha2 = h(f"{method}:{uri}")
    qops = [q.strip() for q in (p.get("qop") or "").split(",") if q.strip()]
    nc_text = f"{nc:08x}"
    if "auth" in qops:
        response = h(f"{ha1}:{nonce}:{nc_text}:{cnonce}:auth:{ha2}")
        extra = f', qop=auth, nc={nc_text}, cnonce="{cnonce}"'
    else:
        response = h(f"{ha1}:{nonce}:{ha2}")
        extra = ""
    q = lambda v: v.replace("\\", "\\\\").replace('"', '\\"')
    out = (f'Digest username="{q(user)}", realm="{q(realm)}", nonce="{q(nonce)}", uri="{q(uri)}", '
           f'algorithm={algorithm}, response="{response}"{extra}')
    if p.get("opaque"):
        out += f', opaque="{q(p["opaque"])}"'
    return out


_PROXY_CREDS: dict = {}  # (proxy host, port, user) -> Credentials


def proxy_credentials(r: Route) -> Credentials | None:
    if not r.user:
        return None
    key = (r.host, r.port, r.user)
    with _lock:
        creds = _PROXY_CREDS.get(key)
        if creds is None or creds.password != r.password:
            # (nothing sent before the proxy asks: then Digest if it offers it - the password
            # never travels - else Basic; a review found Basic was always sent first)
            creds = _PROXY_CREDS[key] = Credentials(r.user, r.password)
        return creds


def _read_head(sock: socket.socket) -> tuple[int, list[tuple[str, str]], bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ProxyError("o proxy fechou a ligação")
        data += chunk
        if len(data) > 65536:
            raise ProxyError("resposta do proxy grande demais")
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    m = re.match(r"HTTP/\d\.\d\s+(\d{3})", lines[0])
    if not m:
        raise ProxyError("resposta do proxy inválida")
    headers = [(k.strip().lower(), v.strip()) for k, _, v in (line.partition(":") for line in lines[1:])]
    return int(m.group(1)), headers, rest


def http_tunnel(r: Route, host: str, port: int, timeout=None) -> socket.socket:
    """A socket to host:port through an HTTP proxy (CONNECT), answering its Basic / Digest
    challenge, ready for TLS on top."""
    from .net import happy_connect
    target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    creds = proxy_credentials(r)
    for attempt in (1, 2):
        sock = happy_connect((r.host, r.port), timeout)
        try:
            lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
            for k, v in r.headers.items():
                lines.append(f"{k}: {v}")
            auth = creds.header("CONNECT", target) if creds else None
            if auth:
                lines.append(f"Proxy-Authorization: {auth}")
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace"))
            status, headers, _rest = _read_head(sock)
            if 200 <= status < 300:
                return sock
            if status == 407 and creds and attempt == 1:
                found = pick_challenge([v for k, v in headers if k == "proxy-authenticate"])
                if found:
                    creds.learn(*found)
                    sock.close()
                    continue
            if status == 407:
                raise ProxyError("o proxy pede autenticação: verifique o utilizador e a senha"
                                 if creds else "o proxy pede autenticação: indique utilizador e senha")
            raise ProxyError(f"o proxy recusou a ligação (HTTP {status})")
        except BaseException:
            sock.close()
            raise
    raise ProxyError("o proxy recusou a autenticação")
