"""A ZIP's contents known before its download ends (its end is asked for first)."""
import io
import os
import re
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flowdl import zipindex
from flowdl.engine import ZIP_TAIL, DownloadTask, Status


def make_zip(n=40, size=200_000, zip64=False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for i in range(n):
            info = zipfile.ZipInfo(f"pasta/ficheiro_{i:03d}.bin" if i % 2 else f"ção_{i}.txt")
            with z.open(info, "w", force_zip64=zip64) as f:
                f.write(os.urandom(size))
    return buf.getvalue()


class ListingTests(unittest.TestCase):
    def check(self, data):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            want = [(i.filename, i.file_size) for i in z.infolist()]
        tail = data[-ZIP_TAIL:]
        got = zipindex.read_listing(tail, len(data) - len(tail), len(data))
        self.assertEqual(got["count"], len(want))
        self.assertEqual(got["names"], want)
        self.assertEqual(got["uncompressed"], sum(s for _n, s in want))

    def test_plain_zip(self):
        self.check(make_zip())

    def test_zip64(self):
        self.check(make_zip(5, 1000, zip64=True))

    def test_not_a_zip_or_index_not_in_the_end(self):
        self.assertIsNone(zipindex.read_listing(os.urandom(5000), 0, 5000))
        data = make_zip(3000, 300)  # a big table of contents: not all in the last 64 KB
        tail = data[-65536:]
        self.assertIsNone(zipindex.read_listing(tail, len(data) - len(tail), len(data)))


class SlowZip(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    data = b""

    def log_message(self, *a):
        pass

    def do_GET(self):
        data = SlowZip.data
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        a = int(m.group(1)) if m else 0
        b = min(int(m.group(2)) if m and m.group(2) else len(data) - 1, len(data) - 1)
        self.send_response(206 if m else 200)
        if m:
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(b - a + 1))
        self.end_headers()
        try:
            for i in range(a, b + 1, 65536):
                self.wfile.write(data[i:min(i + 65536, b + 1)])
                time.sleep(0.03)
        except OSError:
            pass


class DownloadTests(unittest.TestCase):
    def test_contents_known_long_before_the_end(self):
        SlowZip.data = make_zip(60, 150_000)  # ~9 MB
        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowZip)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        tmp = tempfile.TemporaryDirectory()
        try:
            task = DownloadTask(f"http://127.0.0.1:{server.server_address[1]}/pacote.zip", tmp.name, 4)
            task.start()
            known_at = None
            while task.is_running():
                if task.zip_listing and known_at is None:
                    known_at = task.progress
                time.sleep(0.05)
            self.assertEqual(task.status, Status.COMPLETED, task.error)
            with open(task.path, "rb") as f:
                self.assertEqual(f.read(), SlowZip.data)
            self.assertIsNotNone(known_at, "contents never known during the download")
            self.assertLess(known_at, 0.6)
            self.assertEqual(task.zip_listing["count"], 60)
        finally:
            server.shutdown()
            tmp.cleanup()



class SummaryTests(unittest.TestCase):
    def test_details_line(self):
        try:
            from flowdl.gui import zip_summary
        except ImportError:
            self.skipTest("sem tkinter")
        data = make_zip(5, 1000)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.zip")
            with open(path, "wb") as f:
                f.write(data)

            class T:
                status, zip_listing = Status.COMPLETED, None
            t = T()
            t.path = path
            line = zip_summary(t)
            self.assertTrue(line.startswith("5 ficheiros"), line)
            self.assertIn("e mais 2", line)
            t2 = T()
            t2.path, t2.status = path.replace(".zip", ".bin"), Status.COMPLETED
            self.assertEqual(zip_summary(t2), "")

if __name__ == "__main__":
    unittest.main()
