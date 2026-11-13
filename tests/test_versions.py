"""D / H: a file that changes on the server must never end up as a mix of two versions."""

import hashlib
import os
import re
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flowdl.engine import DownloadTask, Status, validator_of

V1 = os.urandom(12 * 1024 * 1024)
V2 = os.urandom(12 * 1024 * 1024)
V3_SMALL = os.urandom(3 * 1024 * 1024)


class VersionedServer(BaseHTTPRequestHandler):
    """Serves the current version. Honours If-Range like real servers: when it does not
    match the current version, the whole new file is sent (200) instead of the range."""
    protocol_version = "HTTP/1.1"
    versions = {"v1": (V1, "Mon, 01 Jan 2024 00:00:00 GMT"), "v2": (V2, "Tue, 02 Jan 2024 00:00:00 GMT"),
                "v3": (V3_SMALL, "Wed, 03 Jan 2024 00:00:00 GMT")}
    current = "v1"
    delay = 0.0
    switch_after = None  # switch to v2 after this many requests
    requests = 0
    send_validator = True

    def log_message(self, *a):
        pass

    def do_GET(self):
        cls = type(self)
        cls.requests += 1
        if cls.switch_after and cls.requests > cls.switch_after:
            cls.current = cls.switch_to
        data, lm = cls.versions[cls.current]
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        if_range = self.headers.get("If-Range")
        if m and if_range and if_range != lm:
            m = None  # changed: whole new file
        if m:
            start = int(m.group(1))
            if start >= len(data):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
            body = data[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        if cls.send_validator:
            self.send_header("Last-Modified", lm)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            for i in range(0, len(body), 64 * 1024):
                self.wfile.write(body[i:i + 64 * 1024])
                if cls.delay:
                    time.sleep(cls.delay)
        except OSError:
            pass


def sha(b):
    return hashlib.sha256(b).hexdigest()


class VersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), VersionedServer)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/ficheiro.bin"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        S = VersionedServer
        S.current, S.delay, S.switch_after, S.requests, S.send_validator, S.switch_to = "v1", 0.0, None, 0, True, "v2"

    def result(self, task):
        with open(task.path, "rb") as f:
            data = f.read()
        mixed = len({d for d in (V1, V2, V3_SMALL) if data[:65536] == d[:65536]} ^
                    {d for d in (V1, V2, V3_SMALL) if data[-65536:] == d[-65536:]})
        self.assertFalse(mixed and data not in (V1, V2, V3_SMALL), "two versions mixed in one file")
        return data

    def test_changes_during_download(self):
        VersionedServer.switch_after, VersionedServer.delay = 2, 0.01  # changes after 2 requests
        task = DownloadTask(self.url, self.tmp, 4)
        task.start()
        task.wait(60)
        self.assertEqual(task.status, Status.COMPLETED, task.error)
        self.assertEqual(sha(self.result(task)), sha(V2), "must be exactly the new version")
        self.assertTrue(task.restarted_changed)

    def test_changes_while_paused(self):
        """Paused today, resumed tomorrow after the site updated the file."""
        VersionedServer.delay = 0.05
        task = DownloadTask(self.url, self.tmp, 4)
        task.start()
        time.sleep(1.0)
        task.pause()
        task.wait(10)
        self.assertEqual(task.status, Status.PAUSED)
        self.assertTrue(0 < task.downloaded < len(V1))
        VersionedServer.current, VersionedServer.delay = "v2", 0.0
        again = DownloadTask(self.url, self.tmp, 4)  # like reopening the app
        again.start()
        again.wait(60)
        self.assertEqual(again.status, Status.COMPLETED, again.error)
        self.assertEqual(sha(self.result(again)), sha(V2))

    def test_file_shrinks(self):
        """H: the new version is smaller; ranges past its end answer 416."""
        VersionedServer.switch_after, VersionedServer.switch_to, VersionedServer.delay = 2, "v3", 0.01
        task = DownloadTask(self.url, self.tmp, 4)
        task.start()
        task.wait(60)
        self.assertEqual(task.status, Status.COMPLETED, task.error)
        self.assertEqual(sha(self.result(task)), sha(V3_SMALL))

    def test_unchanged_file_is_not_restarted(self):
        VersionedServer.delay = 0.01
        task = DownloadTask(self.url, self.tmp, 4)
        task.start()
        task.wait(60)
        self.assertEqual(sha(self.result(task)), sha(V1))
        self.assertFalse(task.restarted_changed)

    def test_server_without_dates_still_works(self):
        VersionedServer.send_validator = False
        task = DownloadTask(self.url, self.tmp, 4)
        task.start()
        task.wait(60)
        self.assertEqual(task.status, Status.COMPLETED, task.error)
        self.assertEqual(sha(self.result(task)), sha(V1))

    def test_validator_choice(self):
        self.assertEqual(validator_of({"Last-Modified": "x", "ETag": '"e"'}), "x")
        self.assertEqual(validator_of({"ETag": '"forte"'}), '"forte"')
        self.assertIsNone(validator_of({"ETag": 'W/"fraca"'}))


if __name__ == "__main__":
    unittest.main()
