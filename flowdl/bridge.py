"""Tiny local HTTP server that receives downloads from the browser extension.

Written directly on sockets (instead of http.server) so the background process
stays small: it sleeps in accept() and uses no CPU until the browser calls.

Listens only on 127.0.0.1. Requests must carry the ``X-FlowDL`` header and must
not come from a regular web page origin, so websites cannot push downloads.
"""

from __future__ import annotations

import json
import secrets
import socket
import threading
from typing import Callable

DEFAULT_PORT = 17890
MAX_HEADER = 64 * 1024
MAX_BODY = 2_000_000
REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden"}


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


def _download_item(data: dict) -> dict:
    url = str(data.get("url", ""))
    if not url.startswith(("http://", "https://", "ftp://")):
        raise ValueError("url inválida")
    final = str(data.get("final_url") or "")
    by_host = data.get("cookies_by_host") or {}
    item = {"url": url, "filename": data.get("filename") or None,
            "referrer": data.get("referrer") or None, "cookies": data.get("cookies") or None,
            # where the link led in the browser (may be a short-lived signed address)
            "final_url": final if final.startswith(("http://", "https://")) else None,
            # the Cookie header the browser sent to each site, as observed by the extension
            "cookies_by_host": {str(k).lower(): str(v) for k, v in by_host.items() if v}
            if isinstance(by_host, dict) else {}}
    if data.get("mirror"):
        # The extension wants to show this download in the browser too: it will ask about
        # this request id until the download starts (or is cancelled in the app).
        item["rid"] = secrets.token_urlsafe(12)
    return item


def handle_request(method: str, path: str, headers: dict, body: bytes,
                   on_add: Callable[[dict], None], api=None) -> tuple[int, dict]:
    origin = headers.get("origin", "")
    if origin.startswith(("http://", "https://")) or headers.get("x-flowdl") != "1":
        return 403, {"ok": False}
    if method == "GET" and path == "/ping":
        return 200, {"app": "flowdl", "ok": True}
    if api is not None and (path.startswith("/request/") or path in ("/control", "/mirror-done")):
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
            conn.sendall(f"HTTP/1.1 {code} {REASONS.get(code, '')}\r\nContent-Type: application/json\r\n"
                         f"Content-Length: {len(out)}\r\nConnection: close\r\n\r\n".encode() + out)
        except OSError:
            pass
        finally:
            conn.close()
