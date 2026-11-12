"""Local HTTP server that receives downloads from the browser extension.

Listens only on 127.0.0.1. Requests must carry the ``X-PyDM`` header and must
not come from a regular web page origin, so websites cannot push downloads.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

DEFAULT_PORT = 17890


class _Handler(BaseHTTPRequestHandler):
    on_add: Callable[[dict], None] = staticmethod(lambda item: None)

    def log_message(self, *args):
        pass

    def _allowed(self) -> bool:
        origin = self.headers.get("Origin", "")
        if origin.startswith(("http://", "https://")):
            return False
        return self.headers.get("X-PyDM") == "1"

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/ping" and self._allowed():
            self._reply(200, {"app": "pydm", "ok": True})
        else:
            self._reply(403, {"ok": False})

    def do_POST(self):
        if self.path not in ("/add", "/video") or not self._allowed():
            self._reply(403, {"ok": False})
            return
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 2_000_000)
            data = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/video":
                item = self._video_item(data)
                type(self).on_add(item)
                self._reply(200, {"ok": True})
                return
            url = str(data.get("url", ""))
            if not url.startswith(("http://", "https://", "ftp://")):
                raise ValueError("url inválida")
            item = {
                "url": url,
                "filename": data.get("filename") or None,
                "referrer": data.get("referrer") or None,
                "cookies": data.get("cookies") or None,
            }
        except (ValueError, TypeError, AttributeError) as exc:
            self._reply(400, {"ok": False, "error": str(exc)})
            return
        type(self).on_add(item)
        self._reply(200, {"ok": True})

    @staticmethod
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


class BrowserBridge:
    def __init__(self, on_add: Callable[[dict], None], port: int = DEFAULT_PORT):
        handler = type("Handler", (_Handler,), {"on_add": staticmethod(on_add)})
        self.server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> "BrowserBridge":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
