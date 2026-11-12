import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from flowdl import service as service_mod
from flowdl.manager import DownloadManager
from flowdl.service import Service
from flowdl.settings import Settings
from tests.test_engine import Handler


def post(port, path, payload):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode(),
                                 headers={"X-FlowDL": "1", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status


class ServiceTests(unittest.TestCase):
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
        Handler.delay = 0.0
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()
        self.settings.path = type(self.settings.path)(os.path.join(self.tmp.name, "settings.json"))
        self.settings.dest_dir = self.tmp.name
        self.settings.scheduler = {}
        self.svc = Service(self.settings, DownloadManager(db_path=os.path.join(self.tmp.name, "db.json")),
                           port=0)
        self.assertTrue(self.svc.start_bridge())
        self.port = self.svc.bridge.port

    def tearDown(self):
        self.svc.exit_requested = True
        self.svc.shutdown()
        self.tmp.cleanup()

    def wait_mode(self, timeout=10):
        result = []
        t = threading.Thread(target=lambda: result.append(self.svc.wait()), daemon=True)
        t.start()
        t.join(timeout)
        self.assertFalse(t.is_alive(), "wait() did not return")
        return result[0]

    def test_silent_browser_download_needs_no_window(self):
        self.settings.confirm_browser = False
        self.settings.scheduler = {}
        self.svc.scheduler.on_complete = "close"  # returns from wait() once everything finished
        post(self.port, "/add", {"url": self.base + "/silent.bin"})
        self.assertIsNone(self.wait_mode())  # handled entirely in the background
        self.assertEqual(len(self.svc.manager.tasks), 1)
        self.assertEqual(self.svc.manager.tasks[0].status, "Concluído")

    def test_browser_download_with_confirmation_opens_dialog(self):
        self.settings.confirm_browser = True
        post(self.port, "/add", {"url": self.base + "/x.zip", "referrer": "https://site/"})
        self.assertEqual(self.wait_mode(), "dialog")
        self.assertEqual(self.svc.pending[0]["url"], self.base + "/x.zip")
        self.assertEqual(self.svc.manager.tasks, [])

    def test_video_link_opens_dialog(self):
        post(self.port, "/video", {"page": "https://www.youtube.com/watch?v=abc", "media": []})
        self.assertEqual(self.wait_mode(), "dialog")
        self.assertEqual(self.svc.pending[0]["kind"], "video")

    def test_show_request_and_single_instance(self):
        self.assertTrue(service_mod._existing_instance(self.port))
        self.assertEqual(self.wait_mode(), "main")

    def test_idle_wait_uses_no_cpu(self):
        t = threading.Thread(target=self.svc.wait, daemon=True)
        cpu0 = time.process_time()
        t.start()
        time.sleep(2)
        self.assertLess(time.process_time() - cpu0, 0.05)
        self.svc.events.put(("quit", None))
        t.join(5)

    def test_bridge_stop_is_quick(self):
        t0 = time.monotonic()
        self.svc.stop_bridge()
        self.assertLess(time.monotonic() - t0, 1.5)


if __name__ == "__main__":
    unittest.main()


class RestartTests(unittest.TestCase):
    def test_port_can_be_reused_right_after_restart(self):
        """Regression: after a restart the port was still held by connections in TIME_WAIT."""
        from flowdl.bridge import BrowserBridge
        got = []
        first = BrowserBridge(got.append, port=0).start()
        port = first.port
        for _ in range(3):  # leave some closed connections behind
            post(port, "/show", {})
        first.stop()
        second = BrowserBridge(got.append, port=port).start()  # must not raise EADDRINUSE
        try:
            self.assertEqual(post(port, "/show", {}), 200)
        finally:
            second.stop()

    def test_two_instances_cannot_listen_together(self):
        from flowdl.bridge import BrowserBridge
        first = BrowserBridge(lambda i: None, port=0).start()
        try:
            with self.assertRaises(OSError):
                BrowserBridge(lambda i: None, port=first.port)
        finally:
            first.stop()
