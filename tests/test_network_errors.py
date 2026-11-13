"""F / G: certificate errors stop at once with a clear message; Retry-After is obeyed."""

import os
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flowdl.engine import CERT_ERROR, DownloadTask, Status

DATA = os.urandom(3 * 1024 * 1024)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    busy_left = 0        # answer 429 + Retry-After this many times
    retry_after = "2"
    times = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        cls = type(self)
        cls.times.append(time.monotonic())
        if cls.busy_left > 0:
            cls.busy_left -= 1
            self.send_response(429)
            self.send_header("Retry-After", cls.retry_after)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        start = int(m.group(1)) if m else 0
        end = int(m.group(2)) if m and m.group(2) else len(DATA) - 1
        body = DATA[start:end + 1]
        self.send_response(206 if m else 200)
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(DATA)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


class NetworkErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        Handler.busy_left, Handler.times, Handler.retry_after = 0, [], "2"

    def serve(self, tls=False):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        if tls:
            cert, key = os.path.join(self.tmp, "c.pem"), os.path.join(self.tmp, "k.pem")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=site-falso", "-keyout", key, "-out", cert],
                           check=True, capture_output=True)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f"{'https' if tls else 'http'}://127.0.0.1:{server.server_address[1]}/f.bin"

    @unittest.skipUnless(shutil.which("openssl"), "precisa do openssl para criar o certificado")
    def test_bad_certificate_fails_fast_with_clear_message(self):
        url = self.serve(tls=True)
        task = DownloadTask(url, self.tmp, 4)
        t0 = time.monotonic()
        task.start()
        task.wait(30)
        self.assertEqual(task.status, Status.ERROR)
        self.assertEqual(task.error, CERT_ERROR)
        self.assertLess(time.monotonic() - t0, 5, "should not keep retrying")

    def test_busy_server_retry_after_is_obeyed(self):
        url = self.serve()
        Handler.busy_left = 1  # the very first request is told to come back in 2 s
        task = DownloadTask(url, self.tmp, 4)
        task.start()
        task.wait(30)
        self.assertEqual(task.status, Status.COMPLETED, task.error)
        self.assertGreaterEqual(Handler.times[1] - Handler.times[0], 1.9)
        with open(task.path, "rb") as f:
            self.assertEqual(f.read(), DATA)

    def test_huge_retry_after_is_capped_and_pause_stops_waiting(self):
        url = self.serve()
        Handler.busy_left, Handler.retry_after = 1, "100000"
        task = DownloadTask(url, self.tmp, 4)
        task.start()
        time.sleep(1)
        task.pause()  # waiting for the server: pausing must work at once
        task.wait(5)
        self.assertFalse(task.is_running())
        self.assertEqual(task.status, Status.PAUSED)

    def test_network_blip_on_first_request(self):
        """The first request used to fail the whole download on any hiccup."""
        url = self.serve()
        Handler.busy_left, Handler.retry_after = 2, "0"  # two 'busy' answers, no wait asked
        task = DownloadTask(url, self.tmp, 4)
        task.start()
        task.wait(30)
        self.assertEqual(task.status, Status.COMPLETED, task.error)


if __name__ == "__main__":
    unittest.main()
