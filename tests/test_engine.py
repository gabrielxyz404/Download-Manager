import hashlib
import os
import re
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pydm import engine
from pydm.engine import DownloadTask, Status
from pydm.manager import DownloadManager

DATA = os.urandom(5 * 1024 * 1024 + 123)


class Handler(BaseHTTPRequestHandler):
    supports_range = True
    delay = 0.0  # seconds per 64 KB, to make downloads slow enough to pause
    range_requests = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        cls = type(self)
        start, end = 0, len(DATA) - 1
        rng = self.headers.get("Range")
        partial = False
        if rng and cls.supports_range and self.path != "/norange.bin":
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else end
            partial = True
            Handler.range_requests += 1
        body = DATA[start:end + 1]
        self.send_response(206 if partial else 200)
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(DATA)}")
            self.send_header("Accept-Ranges", "bytes")
        if self.path.startswith("/cd"):
            self.send_header("Content-Disposition", 'attachment; filename="relatorio final.zip"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            for i in range(0, len(body), 64 * 1024):
                self.wfile.write(body[i:i + 64 * 1024])
                if cls.delay:
                    time.sleep(cls.delay)
        except (BrokenPipeError, ConnectionResetError):
            pass


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        Handler.delay = 0.0
        Handler.range_requests = 0

    def tearDown(self):
        self.tmp.cleanup()

    def assertFileOk(self, path):
        with open(path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), hashlib.sha256(DATA).hexdigest())

    def test_probe(self):
        info = engine.probe(self.base + "/file.bin")
        self.assertEqual(info["size"], len(DATA))
        self.assertTrue(info["resumable"])
        self.assertEqual(info["filename"], "file.bin")

    def test_segmented_download(self):
        task = DownloadTask(self.base + "/file.bin", self.tmp.name, connections=6)
        task.start()
        task.wait(30)
        self.assertEqual(task.status, Status.COMPLETED, task.error)
        self.assertFileOk(task.path)
        self.assertGreaterEqual(Handler.range_requests, 6)
        self.assertFalse(os.path.exists(task.path + ".part"))
        self.assertFalse(os.path.exists(task.path + ".part.json"))

    def test_filename_from_content_disposition_and_unique(self):
        t1 = DownloadTask(self.base + "/cd", self.tmp.name, 4)
        t1.start(); t1.wait(30)
        t2 = DownloadTask(self.base + "/cd", self.tmp.name, 4)
        t2.start(); t2.wait(30)
        self.assertEqual(os.path.basename(t1.path), "relatorio final.zip")
        self.assertEqual(os.path.basename(t2.path), "relatorio final (1).zip")
        self.assertFileOk(t2.path)

    def test_no_range_server_single_connection(self):
        task = DownloadTask(self.base + "/norange.bin", self.tmp.name, 8)
        task.start()
        task.wait(30)
        self.assertEqual(task.status, Status.COMPLETED, task.error)
        self.assertFalse(task.resumable)
        self.assertFileOk(task.path)

    def test_pause_and_resume_in_new_task(self):
        Handler.delay = 0.2
        url = self.base + "/big.bin"
        task = DownloadTask(url, self.tmp.name, 4)
        task.start()
        time.sleep(1.0)
        task.pause()
        task.wait(10)
        self.assertEqual(task.status, Status.PAUSED)
        self.assertGreater(task.downloaded, 0)
        self.assertLess(task.downloaded, len(DATA))
        self.assertTrue(os.path.exists(task.path + ".part.json"))
        paused_at = task.downloaded

        # Simulate restarting the program: a fresh task for the same URL continues.
        Handler.delay = 0.0
        again = DownloadTask(url, self.tmp.name, 4)
        again.start()
        time.sleep(0.05)
        again.wait(30)
        self.assertEqual(again.status, Status.COMPLETED, again.error)
        self.assertEqual(again.path, task.path)
        self.assertFileOk(again.path)
        self.assertGreater(paused_at, 0)

    def test_dynamic_segmentation_splits(self):
        task = DownloadTask(self.base + "/file.bin", self.tmp.name, 3)
        task.size = len(DATA)
        task._create_segments()
        task.segments[0].pos = task.segments[0].end + 1  # first segment finished
        for s in task.segments[1:]:
            s.active = True
        new = task._split_largest()
        self.assertIsNotNone(new)
        segs = sorted(task.segments, key=lambda s: s.start)
        for a, b in zip(segs, segs[1:]):
            self.assertEqual(a.end + 1, b.start)
        self.assertEqual(segs[-1].end, len(DATA) - 1)

    def test_http_404_reports_error(self):
        class NotFound(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_error(404)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), NotFound)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            task = DownloadTask(f"http://127.0.0.1:{srv.server_address[1]}/x", self.tmp.name)
            task.start(); task.wait(10)
            self.assertEqual(task.status, Status.ERROR)
            self.assertIn("404", task.error)
        finally:
            srv.shutdown()

    def test_manager_queue_and_persistence(self):
        db = os.path.join(self.tmp.name, "db.json")
        mgr = DownloadManager(max_active=1, db_path=db)
        Handler.delay = 0.005
        a = mgr.add(self.base + "/a.bin", self.tmp.name, 4)
        b = mgr.add(self.base + "/b.bin", self.tmp.name, 4)
        self.assertTrue(a.is_running())
        self.assertEqual(b.status, Status.QUEUED)
        mgr.pause_all()
        self.assertTrue(os.path.exists(db))

        Handler.delay = 0.0
        mgr2 = DownloadManager(max_active=2, db_path=db)
        self.assertEqual(len(mgr2.tasks), 2)
        for t in mgr2.tasks:
            mgr2.resume(t)
        deadline = time.time() + 30
        while time.time() < deadline and any(t.status != Status.COMPLETED for t in mgr2.tasks):
            mgr2.tick()
            time.sleep(0.2)
        for t in mgr2.tasks:
            self.assertEqual(t.status, Status.COMPLETED, t.error)
            self.assertFileOk(t.path)


if __name__ == "__main__":
    unittest.main()
