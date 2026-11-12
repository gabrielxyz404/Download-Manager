import datetime as dt
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pydm import grabber
from pydm.bridge import BrowserBridge
from pydm.categories import category_for
from pydm.engine import DownloadTask, Status
from pydm.limiter import RateLimiter
from pydm.scheduler import Scheduler

PAGE = """<html><body>
<a href="/files/setup.exe">setup</a>
<a href="video.mp4#t=3">video</a>
<a href="https://other.example/doc.pdf">doc</a>
<img src="/img/a.png" srcset="/img/a@2x.png 2x">
<a href="/sub.html">sub</a>
</body></html>"""
SUB = '<a href="/deep/song.mp3">song</a>'
BLOB = b"x" * 300_000


class Handler(BaseHTTPRequestHandler):
    seen_headers: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path in ("/", "/sub.html"):
            body = (PAGE if self.path == "/" else SUB).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            Handler.seen_headers.append(dict(self.headers))
            body = BLOB
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_categories(self):
        self.assertEqual(category_for("filme.MKV"), "Vídeos")
        self.assertEqual(category_for("a.zip?x=1"), "Compactados")
        self.assertEqual(category_for("x.unknown"), "Outros")

    def test_limiter_paces_bytes(self):
        lim = RateLimiter(100_000)
        total_wait = sum(lim.reserve(10_000) for _ in range(10))
        self.assertGreater(total_wait, 4.0)  # delays grow: 0.1 + 0.2 + ... + 1.0 s
        self.assertEqual(RateLimiter(0).reserve(10_000), 0)

    def test_speed_limited_download(self):
        with tempfile.TemporaryDirectory() as d:
            t = DownloadTask(self.base + "/f.bin", d, 2, speed_limit=200_000)
            t0 = time.monotonic()
            t.start(); t.wait(20)
            self.assertEqual(t.status, Status.COMPLETED, t.error)
            self.assertGreater(time.monotonic() - t0, 1.0)  # 300 KB at 200 KB/s

    def test_headers_forwarded(self):
        Handler.seen_headers = []
        with tempfile.TemporaryDirectory() as d:
            t = DownloadTask(self.base + "/h.bin", d, 1, headers={"Cookie": "s=1", "Referer": "http://x/"})
            t.start(); t.wait(20)
        self.assertEqual(t.status, Status.COMPLETED, t.error)
        self.assertTrue(all(h.get("Cookie") == "s=1" for h in Handler.seen_headers))

    def test_grabber(self):
        found = {i["name"]: i for i in grabber.grab(self.base + "/")}
        self.assertIn("setup.exe", found)
        self.assertIn("video.mp4", found)
        self.assertIn("doc.pdf", found)
        self.assertIn("a@2x.png", found)
        self.assertNotIn("song.mp3", found)
        deep = {i["name"] for i in grabber.grab(self.base + "/", {"Músicas"}, depth=1)}
        self.assertEqual(deep, {"song.mp3"})

    def test_scheduler_fires_once(self):
        s = Scheduler({"start_enabled": True, "start_time": "02:00", "days": [0]})
        monday = dt.datetime(2026, 11, 9, 2, 0, 10)
        self.assertEqual(s.due(monday), ["start"])
        self.assertEqual(s.due(monday + dt.timedelta(seconds=20)), [])
        self.assertEqual(s.due(dt.datetime(2026, 11, 10, 2, 0)), [])  # Tuesday not selected

    def test_bridge(self):
        got = []
        bridge = BrowserBridge(got.append, port=0).start()
        base = f"http://127.0.0.1:{bridge.port}"
        try:
            def post(headers, payload):
                req = urllib.request.Request(base + "/add", data=json.dumps(payload).encode(),
                                             headers=headers, method="POST")
                try:
                    return urllib.request.urlopen(req, timeout=5).status
                except urllib.error.HTTPError as e:
                    return e.code

            ok = {"X-PyDM": "1", "Content-Type": "application/json", "Origin": "chrome-extension://abc"}
            self.assertEqual(post(ok, {"url": "https://a/b.zip", "cookies": "k=v"}), 200)
            self.assertEqual(post({"Content-Type": "application/json"}, {"url": "https://a/c"}), 403)
            self.assertEqual(post({**ok, "Origin": "https://evil.example"}, {"url": "https://a/d"}), 403)
            self.assertEqual(post(ok, {"url": "javascript:alert(1)"}), 400)
            self.assertEqual([g["url"] for g in got], ["https://a/b.zip"])
            self.assertEqual(got[0]["cookies"], "k=v")
        finally:
            bridge.stop()


if __name__ == "__main__":
    unittest.main()
