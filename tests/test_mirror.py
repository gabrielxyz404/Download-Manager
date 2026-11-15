import os
import socket
import tempfile
import threading
import time
import unittest

from flowdl import mirror
from flowdl.status import Status


class FakeTask:
    """A download whose file is pre-allocated with zeros and filled in over time."""

    def __init__(self, path, data):
        self.path, self.size, self.data = path, len(data), data
        self.filename, self.stream_token, self.added_at = "x.bin", "tok", time.time()
        self.status = Status.DOWNLOADING
        self.prefix = 0
        self.part = path + ".part"
        with open(self.part, "wb") as f:
            f.truncate(self.size)  # zeros, like the real engine

    def available_prefix(self):
        return self.prefix

    def data_path(self):
        return self.part

    def grow(self, step, pause):
        with open(self.part, "r+b", buffering=0) as f:
            while self.prefix < self.size:
                n = min(step, self.size - self.prefix)
                f.seek(self.prefix)
                f.write(self.data[self.prefix:self.prefix + n])
                self.prefix += n
                time.sleep(pause)
        self.status = Status.COMPLETED


class MirrorTests(unittest.TestCase):
    def receive(self, task, headers=None):
        a, b = socket.socketpair()
        t = threading.Thread(target=mirror.serve, args=(a, task, headers or {}, lambda: True), daemon=True)
        t.start()
        got = b""
        b.settimeout(30)
        while True:
            chunk = b.recv(1 << 20)
            if not chunk:
                break
            got += chunk
        t.join(5)
        head, body = got.split(b"\r\n\r\n", 1)
        return head.decode(), body

    def test_browser_receives_exact_bytes_while_file_fills_in(self):
        """Regression: a buffered reader returned read-ahead zeros for bytes written later."""
        with tempfile.TemporaryDirectory() as d:
            data = os.urandom(3_000_000)
            task = FakeTask(os.path.join(d, "x.bin"), data)
            threading.Thread(target=task.grow, args=(3000, 0.0005), daemon=True).start()
            head, body = self.receive(task)
            self.assertIn("200 OK", head)
            self.assertIn('filename="x.bin"', head)
            self.assertEqual(len(body), len(data))
            self.assertTrue(body == data, "the browser got different bytes")

    def test_browser_resume_with_range(self):
        with tempfile.TemporaryDirectory() as d:
            data = os.urandom(200_000)
            task = FakeTask(os.path.join(d, "x.bin"), data)
            task.grow(200_000, 0)
            head, body = self.receive(task, {"range": "bytes=150000-"})
            self.assertIn("206 Partial Content", head)
            self.assertIn("Content-Range: bytes 150000-199999/200000", head)
            self.assertEqual(body, data[150000:])

    def test_unknown_size_is_sent_as_it_arrives(self):
        """Found in an audit: a file without a size from the server was held until the whole
        download finished, and given up after 10 minutes (the browser showed "failed")."""
        with tempfile.TemporaryDirectory() as d:
            data = os.urandom(400_000)
            task = FakeTask(os.path.join(d, "x.bin"), data)
            task.size = None
            got_early = []

            def grow():
                with open(task.part, "r+b", buffering=0) as f:
                    for i in range(0, len(data), 40_000):
                        f.seek(i)
                        f.write(data[i:i + 40_000])
                        task.prefix = i + len(data[i:i + 40_000])
                        time.sleep(0.05)
                task.status = Status.COMPLETED
            a, b = socket.socketpair()
            threading.Thread(target=mirror.serve, args=(a, task, {"range": "bytes=0-"}, lambda: True),
                             daemon=True).start()
            threading.Thread(target=grow, daemon=True).start()
            b.settimeout(30)
            got = b""
            while True:
                chunk = b.recv(1 << 20)
                if not chunk:
                    break
                got += chunk
                if task.status != Status.COMPLETED and b"\r\n\r\n" in got:
                    got_early.append(len(got))
            head, body = got.split(b"\r\n\r\n", 1)
            head = head.decode()
            self.assertIn("200 OK", head)  # no size: no partial answer
            self.assertNotIn("Content-Length", head)
            self.assertTrue(got_early, "nothing arrived before the download ended")
            self.assertEqual(body, data)

    def test_range_at_the_end_is_answered_416(self):
        with tempfile.TemporaryDirectory() as d:
            data = os.urandom(1000)
            task = FakeTask(os.path.join(d, "x.bin"), data)
            task.grow(1000, 0)
            head, body = self.receive(task, {"range": "bytes=1000-"})
            self.assertIn("416", head)
            self.assertEqual(body, b"")

    def test_non_ascii_names(self):
        value = mirror.content_disposition("relatório final.zip")
        self.assertIn("filename*=UTF-8''relat%C3%B3rio%20final.zip", value)


if __name__ == "__main__":
    unittest.main()
