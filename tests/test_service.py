import json
import sys
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


def _until(cond, secs, step=0.05):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


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

    def test_browser_download_without_confirmation_shows_progress_window(self):
        self.settings.confirm_browser = False
        post(self.port, "/add", {"url": self.base + "/progresso.bin"})
        self.assertEqual(self.wait_mode(), "dialog")  # only the small progress window
        self.assertEqual(self.svc.pending[0]["kind"], "progress")

    def test_silent_browser_download_needs_no_window(self):
        self.settings.confirm_browser = False
        self.settings.show_progress_window = False
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

    def test_browser_download_starts_at_once_even_with_the_queue_full(self):
        """The user clicked it now: it starts now, whatever the limit of simultaneous downloads
        (the limit is for the queue). Before, it waited in the queue while the browser was told
        "ok" and dropped its own download - lost if the site refused the app later."""
        Handler.delay = 0.05
        self.svc.manager.max_active = 1
        self.settings.show_progress_window = False
        first = self.svc.manager.add(self.base + "/fila.bin", self.tmp.name, 2)
        self.assertTrue(_until(first.is_running, 5))
        self.svc.requests["r1"] = {"state": "waiting"}
        self.svc.add_from_browser({"url": self.base + "/agora.bin", "rid": "r1", "mirror": True})
        task = self.svc._task_for("r1")
        self.assertTrue(_until(task.is_running, 3), task.status)
        self.assertTrue(first.is_running())  # both run: 2 > the limit of 1
        # the browser is told "started" only once data really arrived
        self.assertTrue(_until(lambda: task.downloaded > 0, 10))
        self.assertEqual(self.svc._verdict(task), "ok")
        Handler.delay = 0.0

    def test_has_it_started_is_answered_within_the_limit(self):
        """The browser stops an extension whose request takes over 30 s (and the browser's own
        paused download is then forgotten). Found in an audit: the answer could take 20 + 20 s."""
        from flowdl.status import Status
        old = service_mod.REQUEST_WAIT_S
        service_mod.REQUEST_WAIT_S = 2
        try:
            class T:
                id, downloaded, status, error = "t1", 0, Status.CONNECTING, None

                def is_running(self):
                    return True
            self.svc.manager.tasks.append(T())
            self.svc.requests["r"] = {"state": "waiting"}
            threading.Timer(1.8, lambda: self.svc._set_request("r", state="started", task="t1",
                                                                stream="/s")).start()
            t0 = time.monotonic()
            code, body = self.svc.api("GET", "/request/r", {})
            self.assertLess(time.monotonic() - t0, 2.6)
            self.assertEqual(body["state"], "waiting")  # "ask again"
        finally:
            service_mod.REQUEST_WAIT_S = old
            self.svc.manager.tasks[:] = [t for t in self.svc.manager.tasks if not isinstance(t, T)]

    def test_extension_giving_up_stops_a_pending_confirmation(self):
        """Found in an audit: after the extension stopped waiting (30 min) the browser carried
        on, but confirming the app's window later downloaded the file a second time."""
        self.svc.requests["r9"] = {"state": "waiting"}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/control",
                                     data=json.dumps({"rid": "r9", "action": "cancel"}).encode(),
                                     headers={"X-FlowDL": "1", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
        self.assertEqual(self.svc.request_state("r9"), "cancelled")
        self.assertFalse(self.svc.busy())

    def test_browser_saved_path_is_checked_and_marked_with_the_real_site(self):
        """/mirror-done tells where the browser saved the file. Found in an audit: any path was
        taken (and later hashed / extracted / opened); and the file's "came from the internet"
        mark named the app (127.0.0.1) instead of the real site."""
        from unittest import mock
        from flowdl.engine import DownloadTask
        task = DownloadTask("https://site.example/f.zip", self.tmp.name, 2,
                            headers={"Referer": "https://site.example/page"})
        task.size, task.status, task.stream_token, task.rid = 5, "Concluído", "tok", "rm"
        task.path = os.path.join(self.tmp.name, "f.zip")
        self.svc.manager.tasks.append(task)
        self.svc.requests["rm"] = {"state": "started", "task": task.id}
        marked = []
        with mock.patch("flowdl.system.mark_from_internet", lambda p, u, r=None: marked.append((p, u, r))):
            self.svc.api("POST", "/mirror-done", {"rid": "rm", "path": "C:/Windows/notepad.exe"})
            self.assertEqual(task.path, os.path.join(self.tmp.name, "f.zip"))  # not taken
            self.assertEqual(marked, [])
            task.stream_token = "tok"
            saved = os.path.join(self.tmp.name, "browser", "f.zip")
            os.makedirs(os.path.dirname(saved))
            with open(saved, "wb") as f:
                f.write(b"12345")
            self.svc.api("POST", "/mirror-done", {"rid": "rm", "path": saved})
        self.assertEqual(task.path, saved)
        self.assertEqual(marked, [(saved, "https://site.example/f.zip", "https://site.example/page")])
        self.assertIsNone(task.stream_token)  # the address the browser used is closed

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


class SingleInstanceTests(unittest.TestCase):
    def test_only_one_copy_runs_even_without_the_browser_link(self):
        """Found in an audit: the app only asked the browser link (the local port) whether
        another copy was running. With that link off, or two starts at the same moment, two
        copies ran and wrote the same files. Now an exclusive lock decides."""
        import subprocess
        d = tempfile.mkdtemp()
        cfg = os.path.join(d, "FlowDownloads")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"browser_integration": False, "dest_dir": d, "show_tray": False,
                       "check_updates": False}, f)
        env = {**os.environ, "XDG_CONFIG_HOME": d, "APPDATA": d}
        code = ("import sys; sys.path.insert(0, %r); from flowdl.service import run; run(background=True)"
                % os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        first = subprocess.Popen([sys.executable, "-c", code], env=env)
        try:
            time.sleep(2.5)
            self.assertIsNone(first.poll(), "the first copy must keep running")
            second = subprocess.Popen([sys.executable, "-c", code], env=env)
            self.assertEqual(second.wait(20), 0)  # left by itself
            self.assertIsNone(first.poll())
        finally:
            first.kill()
            first.wait(10)
        # the lock goes with the process: a new copy can start afterwards
        lock = service_mod._instance_lock(path=__import__("pathlib").Path(cfg) / "instance.lock")
        self.assertTrue(lock)
        self.assertIsNone(service_mod._instance_lock(path=__import__("pathlib").Path(cfg) / "instance.lock"))
        lock.close()


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


class BrowserListTests(unittest.TestCase):
    def test_request_is_known_before_the_add_answer(self):
        """Regression: the extension asked about its request before the main thread had
        registered it, got 'unknown' and gave up (the download never showed in the browser)."""
        tmp = tempfile.mkdtemp()
        settings = Settings()
        settings.path = type(settings.path)(os.path.join(tmp, "s.json"))
        svc = Service(settings, DownloadManager(db_path=os.path.join(tmp, "db.json")), port=0)
        svc.start_bridge()
        try:
            body = json.dumps({"url": "http://127.0.0.1:9/x.zip", "mirror": True}).encode()
            req = urllib.request.Request(f"http://127.0.0.1:{svc.bridge.port}/add", data=body, method="POST",
                                         headers={"X-FlowDL": "1", "Content-Type": "application/json"})
            rid = json.loads(urllib.request.urlopen(req, timeout=5).read())["rid"]
            # nobody is processing the event queue yet (main thread "busy")
            self.assertEqual(svc.requests[rid]["state"], "waiting")
        finally:
            svc.stop_bridge()


class NoConsoleTests(unittest.TestCase):
    def test_restart_works_without_console(self):
        """Regression (Windows .exe): sys.stdout is None without a console, and the restart
        to free memory crashed on sys.stdout.flush() before starting the new process."""
        from unittest import mock
        svc = Service(Settings(), DownloadManager(db_path=os.path.join(tempfile.mkdtemp(), "db.json")), port=0)
        svc.slim_enabled = True
        with mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None), \
                mock.patch.object(service_mod.os, "execv") as execv, \
                mock.patch.object(service_mod.subprocess, "Popen") as popen, \
                mock.patch.object(service_mod.os, "_exit") as _exit, \
                mock.patch.object(svc, "can_slim_down", return_value=True):
            svc.restart_slim()
        self.assertTrue(execv.called or popen.called)
