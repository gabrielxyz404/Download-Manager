"""Tiny local HTTP server that receives downloads from the browser extension.

Written directly on sockets (instead of http.server) so the background process
stays small: it sleeps in accept() and uses no CPU until the browser calls.

Listens only on 127.0.0.1. Requests must carry the ``X-FlowDL`` header and must
come from our extension (or a program on this computer), so websites cannot push downloads.
"""

from __future__ import annotations

import json
import secrets
import socket
import threading
from typing import Callable

DEFAULT_PORT = 17890
# The oldest extension that understands this app (original + final link, per-site cookies,
# form downloads left to the browser, the download shown in the browser's list).
MIN_EXTENSION_VERSION = "0.5.0"


def version_tuple(text: str | None) -> tuple:
    try:
        return tuple(int(p) for p in (text or "").split("-")[0].split("."))
    except ValueError:
        return ()


def extension_ok(headers: dict) -> bool:
    """Extensions before 0.5.0 send no version: those count as too old."""
    return version_tuple(headers.get("x-flowdl-ext")) >= version_tuple(MIN_EXTENSION_VERSION)
MAX_HEADER = 64 * 1024
MAX_BODY = 2_000_000
REASONS = {200: "OK", 204: "No Content", 400: "Bad Request", 403: "Forbidden"}


def _video_item(data: dict) -> dict:
    page = str(data.get("page", ""))
    if not page.startswith(("http://", "https://")):
        raise ValueError("página inválida")
    media = []
    for m in data.get("media") or []:
        url = str(m.get("url", ""))
        if url.startswith(("http://", "https://")):
            media.append({"url": url, "mime": str(m.get("mime", "")),
                          "size": int(m.get("size") or 0), "hls": bool(m.get("hls"))})
    jar = [c for c in data.get("cookie_jar") or [] if isinstance(c, dict)]
    return {"kind": "video", "page": page, "title": str(data.get("title", ""))[:300],
            "media": media[:30], "cookie_jar": jar[:300]}


def _header_value(value) -> str | None:
    """A header value from the extension: one line of printable text, or nothing."""
    text = str(value or "").strip()
    if not text or len(text) > 512 or any(c in text for c in "\r\n\0"):
        return None
    return text


# Other headers the browser sent with the download that the app repeats (protected sites
# compare them). Nothing that changes what is asked (Range, Host, cookies...).
BROWSER_HEADERS = ("accept", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-fetch-dest",
                   "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user", "upgrade-insecure-requests", "dnt")


def _browser_headers(value) -> dict:
    if not isinstance(value, dict):
        return {}
    out = {}
    for name, v in value.items():
        name = str(name).lower()
        clean = _header_value(v)
        if name in BROWSER_HEADERS and clean:
            out["-".join(p.capitalize() for p in name.split("-"))] = clean
    return out


def _expected(size, mime) -> dict | None:
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        size = 0
    mime = (_header_value(mime) or "").split(";")[0].strip().lower()
    if size <= 0 and not mime:
        return None
    return {"size": max(0, size), "type": mime}


def _download_item(data: dict) -> dict:
    url = str(data.get("url", ""))
    if not url.startswith(("http://", "https://")):  # (no FTP: the engine speaks HTTP only)
        raise ValueError("url inválida")
    final = str(data.get("final_url") or "")
    by_host = data.get("cookies_by_host") or {}
    item = {"url": url, "filename": data.get("filename") or None,
            "referrer": data.get("referrer") or None, "cookies": data.get("cookies") or None,
            # where the link led in the browser (may be a short-lived signed address)
            "final_url": final if final.startswith(("http://", "https://")) else None,
            # the Cookie header the browser sent to each site, as observed by the extension
            "cookies_by_host": {str(k).lower(): str(v) for k, v in by_host.items() if v}
            if isinstance(by_host, dict) else {},
            # the browser's own identity: sites behind Cloudflare & co. tie their cookies to it
            "user_agent": _header_value(data.get("user_agent")),
            "accept_language": _header_value(data.get("accept_language")),
            "browser_headers": _browser_headers(data.get("browser_headers")),
            # what the browser itself received: the app must get the same file
            "expect": _expected(data.get("browser_size"), data.get("browser_type"))}
    if data.get("mirror"):
        # The extension wants to show this download in the browser too: it will ask about
        # this request id until the download starts (or is cancelled in the app).
        item["rid"] = secrets.token_urlsafe(12)
    return item


def origin_allowed(origin: str) -> bool:
    """Only our extension (its ID is fixed by the "key" in its manifest) or a program on this
    computer (no Origin at all) may talk to the app. Not "null": a web page can send that
    (a sandboxed iframe, a data: page) and would otherwise be let in."""
    from .nativehost import EXTENSION_ID
    return origin == "" or origin.rstrip("/") == f"chrome-extension://{EXTENSION_ID}"


def handle_request(method: str, path: str, headers: dict, body: bytes,
                   on_add: Callable[[dict], None], api=None) -> tuple[int, dict]:
    origin = headers.get("origin", "")
    if not origin_allowed(origin) or headers.get("x-flowdl") != "1":
        return 403, {"ok": False}
    if method == "GET" and path == "/ping":
        from . import __version__
        return 200, {"app": "flowdl", "ok": True, "version": __version__,
                     "extension_ok": extension_ok(headers), "min_extension": MIN_EXTENSION_VERSION}
    if api is not None and (path.startswith("/request/") or path in ("/control", "/mirror-done", "/check", "/site")):
        try:
            data = json.loads(body or b"{}") if method == "POST" else {}
        except ValueError:
            return 400, {"ok": False}
        return api(method, path, data)
    if method != "POST" or path not in ("/add", "/video", "/show"):
        return 403, {"ok": False}
    try:
        if path == "/show":
            item = {"kind": "show"}
        else:
            data = json.loads(body or b"{}")
            item = _video_item(data) if path == "/video" else _download_item(data)
    except (ValueError, TypeError, AttributeError) as exc:
        return 400, {"ok": False, "error": str(exc)}
    item["extension_ok"] = extension_ok(headers)  # lets the app suggest an update
    on_add(item)
    return 200, {"ok": True, **({"rid": item["rid"]} if "rid" in item else {})}


class BrowserBridge:
    def __init__(self, on_add: Callable[[dict], None], port: int = DEFAULT_PORT, api=None, on_stream=None):
        self.on_add = on_add
        self.api = api              # request status / control / browser finished
        self.on_stream = on_stream  # sends a download's bytes to the browser
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: never share the port
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            # POSIX: allow re-binding right after a restart while old connections are in
            # TIME_WAIT (it does not let two programs listen on the port at the same time).
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind(("127.0.0.1", port))
            self.sock.listen(16)
        except OSError:
            self.sock.close()
            raise
        self.port = self.sock.getsockname()[1]
        self._running = False
        self._clients = 0  # requests being answered right now
        self._clients_cond = threading.Condition()
        self._thread = threading.Thread(target=self._serve, name="bridge", daemon=True)

    def start(self) -> "BrowserBridge":
        self._running = True
        self._thread.start()
        return self

    def stop(self) -> None:
        self._running = False
        try:  # wake the blocked accept() so the thread can exit
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
        except OSError:
            pass
        self._thread.join(2)
        self.sock.close()
        with self._clients_cond:  # let requests already received finish (their events count)
            self._clients_cond.wait_for(lambda: self._clients == 0, timeout=3)

    def _serve(self) -> None:
        while self._running:
            try:
                conn, _addr = self.sock.accept()  # sleeps here: no CPU while idle
            except OSError:
                return
            if not self._running:
                conn.close()
                return
            threading.Thread(target=self._client_counted, args=(conn,), daemon=True).start()

    def _client_counted(self, conn: socket.socket) -> None:
        with self._clients_cond:
            self._clients += 1
        try:
            self._client(conn)
        finally:
            with self._clients_cond:
                self._clients -= 1
                self._clients_cond.notify_all()

    def _client(self, conn: socket.socket) -> None:
        cors = ""
        try:
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk or len(data) > MAX_HEADER:
                    conn.close()
                    return
                data += chunk
            head, body = data.split(b"\r\n\r\n", 1)
            lines = head.decode("latin-1").split("\r\n")
            method, path, _version = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            origin = headers.get("origin", "")
            if origin and origin_allowed(origin):
                # Only the extension (chrome-extension://...) is answered with these, never a
                # web page. Browsers may check before a POST to this computer (a "preflight",
                # including the private / local network check) and need to see them.
                cors = (f"Access-Control-Allow-Origin: {origin}\r\nVary: Origin\r\n" if origin else "")
            else:
                cors = ""
            if method == "OPTIONS":
                allowed = bool(cors)
                conn.sendall((f"HTTP/1.1 {204 if allowed else 403} {'No Content' if allowed else 'Forbidden'}\r\n"
                              + (cors + "Access-Control-Allow-Methods: GET, POST\r\n"
                                 "Access-Control-Allow-Headers: Content-Type, X-FlowDL, X-FlowDL-Ext\r\n"
                                 "Access-Control-Allow-Private-Network: true\r\n"
                                 "Access-Control-Allow-Local-Network: true\r\n"
                                 "Access-Control-Max-Age: 600\r\n" if allowed else "")
                              + "Content-Length: 0\r\nConnection: close\r\n\r\n").encode())
                conn.close()
                return
            if method == "GET" and path.startswith("/stream/") and self.on_stream:
                # The token in the address is the key (the browser can't send our header).
                self.on_stream(conn, path.split("?")[0], headers)  # closes the connection
                return
            length = min(int(headers.get("content-length") or 0), MAX_BODY)
            while len(body) < length:
                chunk = conn.recv(min(65536, length - len(body)))
                if not chunk:
                    break
                body += chunk
            code, payload = handle_request(method, path.split("?")[0], headers, body[:length],
                                           self.on_add, self.api)
        except (OSError, ValueError):
            code, payload = 400, {"ok": False}
        try:
            out = json.dumps(payload).encode()
            conn.sendall(f"HTTP/1.1 {code} {REASONS.get(code, '')}\r\nContent-Type: application/json\r\n{cors}"
                         f"Content-Length: {len(out)}\r\nConnection: close\r\n\r\n".encode() + out)
        except OSError:
            pass
        finally:
            conn.close()
