import hashlib
import json
import os
import re
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flowdl import engine
from flowdl.engine import DownloadTask, Status
from flowdl.manager import DownloadManager

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
        self.assertGreaterEqual(Handler.range_requests, 2)  # several connections were used
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

    def test_parts_list_is_never_seen_empty_while_a_part_is_added(self):
        """Found in an audit: adding a part sorted the list in place, and CPython shows a list
        as EMPTY while it sorts it. Threads reading it without the lock (progress, "is it
        complete?", the window's map) could then see no parts at all."""
        import sys
        from flowdl.engine import Segment
        task = DownloadTask("http://127.0.0.1:1/x", tempfile.mkdtemp(), 8)
        task.segments = [Segment(i * 100, i * 100 + 99) for i in range(40)]
        stop, empty, reads = [False], [0], [0]

        def reader():
            while not stop[0]:
                reads[0] += 1
                if not task.segments or all(seg.remaining == 0 for seg in task.segments):
                    empty[0] += 1

        old = sys.getswitchinterval()
        sys.setswitchinterval(0.0005)
        t = threading.Thread(target=reader)
        t.start()
        try:
            for i in range(3000):
                with task._lock:
                    task._add_segment(Segment(10_000 + i, 10_000 + i))
        finally:
            stop[0] = True
            t.join()
            sys.setswitchinterval(old)
        self.assertGreater(reads[0], 100)
        self.assertEqual(empty[0], 0, f"{empty[0]} of {reads[0]} reads saw no parts")

    def test_expired_browser_address_on_the_last_try_falls_back_to_the_link(self):
        """Found in an audit: when the browser's final address answered "busy" (503) and then
        "forbidden" (403) on the LAST first-request try, the loop ended with no answer and the
        download failed with an internal error. It must fall back to the original link."""
        calls = []

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                calls.append(self.path)
                if self.path.startswith("/final"):
                    busy = sum(1 for c in calls if c.startswith("/final")) < 4
                    self.send_response(503 if busy else 403)
                    if busy:
                        self.send_header("Retry-After", "1")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
                a = int(m.group(1)) if m else 0
                b = int(m.group(2)) if m and m.group(2) else len(DATA) - 1
                b = min(b, len(DATA) - 1)
                self.send_response(206 if m else 200)
                if m:
                    self.send_header("Content-Range", f"bytes {a}-{b}/{len(DATA)}")
                self.send_header("Content-Length", str(b - a + 1))
                self.end_headers()
                self.wfile.write(DATA[a:b + 1])

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            task = DownloadTask(base + "/orig.bin", tempfile.mkdtemp(), 2, final_url=base + "/final.bin")
            task.start()
            task.wait(60)
            self.assertEqual(task.status, Status.COMPLETED, task.error)
            with open(task.path, "rb") as f:
                self.assertEqual(f.read(), DATA)
        finally:
            srv.shutdown()

    def test_temporary_copy_is_removed_even_if_windows_holds_it_for_a_moment(self):
        """Browser mode: once the browser has the file, our temporary copy (as big as the file)
        is deleted. On Windows the antivirus or the closing stream may hold it for a moment;
        the delete was tried once and the copy stayed forever. Now: tries again, and at the
        next start of the app."""
        from unittest import mock
        from flowdl import engine as eng
        from flowdl.manager import DownloadManager
        d = tempfile.mkdtemp()
        task = DownloadTask("http://127.0.0.1:1/f.bin", d, 2)
        task.path = os.path.join(d, "f.bin")
        for p in (task.path + ".part", task.path + ".part.json"):
            with open(p, "wb") as f:
                f.write(b"x" * 1000)
        real, held = os.remove, {"n": 0}

        def busy_remove(p):
            if held["n"] < 2:  # held twice, then free
                held["n"] += 1
                raise PermissionError(13, "in use")
            real(p)
        with mock.patch.object(eng.os, "remove", busy_remove):
            task.release_to_browser(os.path.join(d, "browser", "f.bin"))
            self.assertTrue(task.leftovers)
            end = time.monotonic() + 5
            while time.monotonic() < end and os.path.exists(os.path.join(d, "f.bin.part")):
                time.sleep(0.1)
        self.assertFalse(os.path.exists(os.path.join(d, "f.bin.part")))
        self.assertFalse(os.path.exists(os.path.join(d, "f.bin.part.json")))
        # held for longer than the app ran: cleaned at the next start
        with open(os.path.join(d, "g.bin.part"), "wb") as f:
            f.write(b"x")
        db = os.path.join(d, "db.json")
        with open(db, "w", encoding="utf-8") as f:
            json.dump({"tasks": [{**task.to_dict(), "leftovers": [os.path.join(d, "g.bin.part"),
                                                                  os.path.join(d, "keep.txt")]}]}, f)
        with open(os.path.join(d, "keep.txt"), "w") as f:
            f.write("not ours")
        DownloadManager(db_path=db)
        self.assertFalse(os.path.exists(os.path.join(d, "g.bin.part")))
        self.assertTrue(os.path.exists(os.path.join(d, "keep.txt")))  # only our temporary files

    def test_list_saved_from_many_threads_stays_whole_and_current(self):
        """Engine, bridge, timer and window threads all save the download list. Found in an
        audit: two saves at once could leave the file broken (and the next start then lost
        the whole list), or an older save could land after a newer one (a removed download
        came back)."""
        from flowdl.manager import DownloadManager
        d = tempfile.mkdtemp()
        db = os.path.join(d, "db.json")
        mgr = DownloadManager(db_path=db)
        for i in range(60):
            mgr.add(f"http://127.0.0.1:1/f{i}.bin", d, 2, start=False)
        stop, bad = [False], [0]

        def saver():
            while not stop[0]:
                mgr._last_saved = None  # force a real write every time
                mgr.save()

        def reader():
            while not stop[0]:
                try:
                    with open(db, encoding="utf-8") as f:
                        json.load(f)
                except ValueError:
                    bad[0] += 1
                except OSError:
                    pass
        threads = [threading.Thread(target=saver) for _ in range(4)] + [threading.Thread(target=reader)]
        for t in threads:
            t.start()
        time.sleep(1.5)
        removed = mgr.tasks[0]
        mgr.remove(removed)  # saves; the other savers must not bring it back
        time.sleep(0.5)
        stop[0] = True
        for t in threads:
            t.join()
        self.assertEqual(bad[0], 0)
        again = DownloadManager(db_path=db)
        self.assertEqual(len(again.tasks), 59)
        self.assertNotIn(removed.id, {t.id for t in again.tasks})

    def test_settings_file_is_never_left_half_written(self):
        from unittest import mock
        from flowdl.settings import Settings
        st = Settings()
        st.path = type(st.path)(os.path.join(tempfile.mkdtemp(), "settings.json"))
        st.global_limit_kb = 123
        st.save()
        real = type(st.path).write_text

        def crash(self_, *a, **k):  # the computer turns off in the middle of writing
            real(self_, "{ broken", encoding="utf-8")
            raise OSError("power cut")
        st.global_limit_kb = 456
        with mock.patch.object(type(st.path), "write_text", crash):
            st.save()
        with open(st.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["global_limit_kb"], 123)  # the last good version

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

    def test_checkpoint_never_records_unsynced_bytes(self):
        """Power-loss safety: progress is recorded only for data already forced to disk."""
        import json
        from unittest import mock
        task = DownloadTask(self.base + "/x.bin", self.tmp.name, 2)
        task.path, task.size, task.resumable = os.path.join(self.tmp.name, "x.bin"), 1000, True
        task.segments = [engine.Segment(0, 499, 100), engine.Segment(500, 999, 600)]
        order = []

        def fake_fsync(fd):
            order.append("fsync")
            task.segments[0].pos = 400  # more data arrives while the disk is syncing

        real_save = task._save_state
        with mock.patch.object(engine.os, "fsync", fake_fsync), \
                mock.patch.object(task, "_save_state", lambda segs=None: (order.append("save"), real_save(segs))):
            task._checkpoint(0)
        self.assertEqual(order, ["fsync", "save"])
        with open(task.path + ".part.json") as f:
            saved = json.load(f)["segments"]
        self.assertEqual(saved[0], [0, 499, 100])  # not 400: those bytes were not synced yet

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


class StalledServer(BaseHTTPRequestHandler):
    """Every 3rd answer stops sending half-way and keeps the connection open (a throttling
    server or a flaky line), like the downloads that froze at 0 B/s on a user's computer."""
    protocol_version = "HTTP/1.1"
    answers = 0
    hang_every = 3  # 0: never goes silent (just a line of `rate` per connection)
    rate = 3_000_000
    data = None  # None: BIG (defined further down)
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def do_GET(self):
        data = StalledServer.data or BIG
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        start = int(m.group(1)) if m else 0
        end = int(m.group(2)) if m and m.group(2) else len(data) - 1
        with StalledServer.lock:
            StalledServer.answers += 1
            hang = bool(StalledServer.hang_every) and StalledServer.answers % StalledServer.hang_every == 0
        self.send_response(206 if m else 200)
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        sent = 0
        try:
            for i in range(start, end + 1, 64 * 1024):
                piece = data[i:min(i + 64 * 1024, end + 1)]
                self.wfile.write(piece)
                sent += len(piece)
                time.sleep(len(piece) / StalledServer.rate)
                if hang and sent > 512 * 1024:
                    time.sleep(120)  # silent, connection left open
                    return
        except OSError:
            pass


def _until(cond, secs, step=0.1):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


class OutageServer(BaseHTTPRequestHandler):
    """After ~1 MB, the server (or the line) goes silent for EVERY connection for a while,
    new requests included, then comes back - what froze a 2 GB download at 0 B/s while the
    app opened hundreds of connections (each stuck one "rescued" by a new one that got stuck
    too). Counts the connections that are open at the same time."""
    protocol_version = "HTTP/1.1"
    outage = 20.0
    outage_from = None  # set when the first MB was sent
    sent = 0
    open_now = peak = 0
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    @classmethod
    def reset(cls):
        cls.outage_from, cls.sent, cls.open_now, cls.peak = None, 0, 0, 0

    @classmethod
    def wait_if_out(cls):
        while True:
            with cls.lock:
                t0 = cls.outage_from
            if t0 is None or time.monotonic() - t0 >= cls.outage:
                return
            time.sleep(0.2)

    def handle(self):
        cls = OutageServer
        with cls.lock:
            cls.open_now += 1
            cls.peak = max(cls.peak, cls.open_now)
        try:
            super().handle()
        finally:
            with cls.lock:
                cls.open_now -= 1

    def do_GET(self):
        cls = OutageServer
        data = BIG
        cls.wait_if_out()
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        start = int(m.group(1)) if m else 0
        end = int(m.group(2)) if m and m.group(2) else len(data) - 1
        self.send_response(206 if m else 200)
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        try:
            for i in range(start, end + 1, 64 * 1024):
                cls.wait_if_out()
                piece = data[i:min(i + 64 * 1024, end + 1)]
                self.wfile.write(piece)
                time.sleep(len(piece) / 1_000_000)
                with cls.lock:
                    cls.sent += len(piece)
                    if cls.outage_from is None and cls.sent > 1_000_000:
                        cls.outage_from = time.monotonic()
        except OSError:
            pass


class PenaltyServer(BaseHTTPRequestHandler):
    """A server (or a home router) that stops sending to EVERY connection for a while when a
    client opens more than LIMIT at once - what a user saw: speed down to 0 three times,
    connections going up to 21, and the download much slower than the browser alone."""
    protocol_version = "HTTP/1.1"
    LIMIT, STALL = 4, 5.0
    open_now = peak_after = stalls = 0
    stall_until = 0.0
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    @classmethod
    def reset(cls):
        cls.open_now = cls.peak_after = cls.stalls = 0
        cls.stall_until = 0.0

    def setup(self):
        super().setup()
        cls = PenaltyServer
        with cls.lock:
            cls.open_now += 1
            if cls.stalls:
                cls.peak_after = max(cls.peak_after, cls.open_now)
            if cls.open_now > cls.LIMIT and time.monotonic() >= cls.stall_until:
                cls.stall_until = time.monotonic() + cls.STALL
                cls.stalls += 1

    def finish(self):
        try:
            super().finish()
        finally:
            with PenaltyServer.lock:
                PenaltyServer.open_now -= 1

    def do_GET(self):
        data = BIG
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        start = int(m.group(1)) if m else 0
        end = min(int(m.group(2)) if m and m.group(2) else len(data) - 1, len(data) - 1)
        self.send_response(206 if m else 200)
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            for i in range(start, end + 1, 64 * 1024):
                while time.monotonic() < PenaltyServer.stall_until:
                    time.sleep(0.1)
                self.wfile.write(data[i:min(i + 64 * 1024, end + 1)])
                time.sleep(0.15)  # ~430 KB/s per connection: the app ramps up past the limit
        except OSError:
            pass


class StallTests(unittest.TestCase):
    def test_connections_that_go_silent_are_replaced_quickly(self):
        ThreadingHTTPServer.request_queue_size = 64
        server = ThreadingHTTPServer(("127.0.0.1", 0), StalledServer)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        tmp = tempfile.TemporaryDirectory()
        try:
            task = DownloadTask(f"http://127.0.0.1:{server.server_address[1]}/f.bin", tmp.name, 8)
            t0 = time.monotonic()
            task.start()
            timeline = []  # (second, MB so far, swaps, connections): printed if too slow
            while task.is_running() and time.monotonic() - t0 < 90:
                time.sleep(1)
                timeline.append((round(time.monotonic() - t0), round(task.downloaded / 1e6, 1),
                                 task.reconnections, task.active_connections))
            task.wait(5)
            took = time.monotonic() - t0
            self.assertEqual(task.status, Status.COMPLETED, task.error)
            with open(task.path, "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).digest(), hashlib.sha256(BIG).digest())
            # Before: each silent connection blocked for the 30 s timeout (and the end waited
            # for a connection whose piece another one had finished).
            self.assertLess(took, 30, f"{took:.0f}s {timeline}")
            self.assertGreater(task.reconnections, 0)
        finally:
            server.shutdown()
            tmp.cleanup()

    def test_outage_for_every_connection_does_not_multiply_connections(self):
        """Seen on a user's PC (thinkbroadband 2 GB): the download stopped at 0 B/s showing 280,
        then 400 connections. When EVERY connection goes silent, handing each stuck piece to a
        new connection only adds connections that get stuck too, doubling every few seconds.
        The number of connections must stay near the maximum, and the download must go on
        when the server comes back. (Stuck connections that cannot be woken, as on Windows.)"""
        from unittest import mock
        from flowdl import net
        OutageServer.reset()
        ThreadingHTTPServer.request_queue_size = 256
        server = ThreadingHTTPServer(("127.0.0.1", 0), OutageServer)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        tmp = tempfile.TemporaryDirectory()
        try:
            with mock.patch.object(net.Connection, "abort", lambda self: None):
                task = DownloadTask(f"http://127.0.0.1:{server.server_address[1]}/f.bin", tmp.name, 8)
                t0 = time.monotonic()
                task.start()
                peak, timeline = 0, []
                while task.is_running() and time.monotonic() - t0 < 120:
                    time.sleep(0.5)
                    peak = max(peak, task.active_connections)
                    timeline.append((round(time.monotonic() - t0), task.active_connections,
                                     round(task.downloaded / 1e6, 1)))
                task.wait(5)
            self.assertEqual(task.status, Status.COMPLETED, f"{task.error} {timeline}")
            with open(task.path, "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).digest(), hashlib.sha256(BIG).digest())
            self.assertLessEqual(peak, 12, timeline)  # 8 + a few being replaced, never hundreds
            # open at the server: 8 working + 2 extra + stuck ones still leaving (2 x 8 + 2);
            # before the fix it passed 100 in 45 s and kept growing
            self.assertLessEqual(OutageServer.peak, 18, f"servidor: {OutageServer.peak} {timeline}")
        finally:
            server.shutdown()
            tmp.cleanup()

    def test_pause_is_immediate_even_while_the_server_is_silent(self):
        """Pausing used to wait for every connection's 30 s timeout when the server had stopped
        sending (and on Windows kept the file locked meanwhile). It must stop at once, as
        "paused" (not an error), and continue to a correct file afterwards."""
        OutageServer.reset()
        OutageServer.outage = 8.0
        ThreadingHTTPServer.request_queue_size = 64
        server = ThreadingHTTPServer(("127.0.0.1", 0), OutageServer)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        tmp = tempfile.TemporaryDirectory()
        try:
            task = DownloadTask(f"http://127.0.0.1:{server.server_address[1]}/f.bin", tmp.name, 4)
            task.start()
            self.assertTrue(_until(lambda: OutageServer.outage_from is not None, 20))
            time.sleep(1.0)  # every connection is now waiting on a silent server
            t0 = time.monotonic()
            task.pause()
            task.wait(10)
            took = time.monotonic() - t0
            self.assertFalse(task.is_running())
            self.assertLess(took, 3, f"{took:.1f}s")
            self.assertEqual(task.status, Status.PAUSED, task.error)
            time.sleep(max(0.0, OutageServer.outage_from + OutageServer.outage - time.monotonic()))
            task.start()
            task.wait(60)
            self.assertEqual(task.status, Status.COMPLETED, task.error)
            with open(task.path, "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).digest(), hashlib.sha256(BIG).digest())
        finally:
            OutageServer.outage = 20.0
            server.shutdown()
            tmp.cleanup()

    def test_server_that_stops_everyone_gets_fewer_connections_not_more(self):
        """Every connection stopping together must never make the app open more (it used to
        read the drop to 0 as "more connections will help" and doubled them); it uses half as
        many instead, so a server or router that punishes many connections is calmed."""
        PenaltyServer.reset()
        ThreadingHTTPServer.request_queue_size = 64
        server = ThreadingHTTPServer(("127.0.0.1", 0), PenaltyServer)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        tmp = tempfile.TemporaryDirectory()
        try:
            task = DownloadTask(f"http://127.0.0.1:{server.server_address[1]}/f.bin", tmp.name, 16)
            task.start()
            peak_app = 0
            t0 = time.monotonic()
            while task.is_running() and time.monotonic() - t0 < 120:
                time.sleep(0.2)
                if PenaltyServer.stalls:
                    peak_app = max(peak_app, task.active_connections)
            task.wait(5)
            self.assertEqual(task.status, Status.COMPLETED, task.error)
            with open(task.path, "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).digest(), hashlib.sha256(BIG).digest())
            self.assertGreaterEqual(task.outages, 1)
            # Before: still downloading after 2 min, with 30-40 connections open at the server
            # and the server stopping again every few seconds.
            self.assertLessEqual(peak_app, 16 + 2, "more connections than allowed after the server stopped")
            self.assertLessEqual(PenaltyServer.stalls, 8, "kept provoking the server")
        finally:
            server.shutdown()
            tmp.cleanup()

    def test_finishes_even_if_a_stuck_connection_cannot_be_woken(self):
        """Seen on Windows: every byte was there, but one stuck connection could not be woken
        and the download waited for it until its 30 s timeout. It must end at once anyway."""
        from unittest import mock
        from flowdl import net
        ThreadingHTTPServer.request_queue_size = 64
        server = ThreadingHTTPServer(("127.0.0.1", 0), StalledServer)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        tmp = tempfile.TemporaryDirectory()
        try:
            with mock.patch.object(net.Connection, "abort", lambda self: None):  # cannot be woken
                task = DownloadTask(f"http://127.0.0.1:{server.server_address[1]}/f.bin", tmp.name, 8)
                t0 = time.monotonic()
                task.start()
                task.wait(60)
                took = time.monotonic() - t0
            self.assertEqual(task.status, Status.COMPLETED, task.error)
            with open(task.path, "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).digest(), hashlib.sha256(BIG).digest())
            self.assertLess(took, 25, f"{took:.0f}s")
        finally:
            server.shutdown()
            tmp.cleanup()

    def test_browser_list_download_keeps_up_from_the_start(self):
        """Downloads shown in the browser's list: the browser can only receive the file from
        its start. Before, connections spread over the whole file and the browser showed a
        fraction of the app's progress ("4 days left") while the app was far ahead."""
        StalledServer.hang_every, StalledServer.rate, StalledServer.data = 0, 1_000_000, BIG * 4
        ThreadingHTTPServer.request_queue_size = 64
        server = ThreadingHTTPServer(("127.0.0.1", 0), StalledServer)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        tmp = tempfile.TemporaryDirectory()
        try:
            task = DownloadTask(f"http://127.0.0.1:{server.server_address[1]}/f.bin", tmp.name, 8)
            task.mirror = True
            task.start()
            ratios = []
            while task.is_running():
                time.sleep(0.25)
                if task.downloaded > 1024 * 1024 and task.is_running():
                    ratios.append(task.available_prefix() / task.downloaded)
            self.assertEqual(task.status, Status.COMPLETED, task.error)
            with open(task.data_path(), "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).digest(), hashlib.sha256(BIG * 4).digest())
            # (before: 0.43 on average; then 0.59-0.61 with long requests that could not be
            # split; with short requests near the start, 0.73)
            self.assertGreater(sum(ratios) / len(ratios), 0.65, [round(r, 2) for r in ratios])
        finally:
            StalledServer.hang_every, StalledServer.rate, StalledServer.data = 3, 3_000_000, None
            server.shutdown()
            tmp.cleanup()


class HostMemoryTests(unittest.TestCase):
    def test_remember_and_forget(self):
        from flowdl.hostmemory import HostMemory
        import pathlib
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "hosts.json"
            mem = HostMemory(path)
            self.assertIsNone(mem.get("exemplo.com"))
            mem.put("Exemplo.com", 8, None)
            mem.put("lento.com", 2, 2)
            again = HostMemory(path)  # read back from disk
            self.assertEqual(again.get("exemplo.com")["conns"], 8)
            self.assertEqual(again.get("lento.com")["limit"], 2)
            again._load()["exemplo.com"]["time"] -= 31 * 24 * 3600
            self.assertIsNone(again.get("exemplo.com"))  # a month later: test again
            path.write_text("estragado")
            self.assertIsNone(HostMemory(path).get("lento.com"))  # a damaged file is just ignored

    def test_second_download_from_the_same_server_starts_where_it_paid_off(self):
        from flowdl.hostmemory import HostMemory
        import pathlib
        server = ThreadingHTTPServer(("127.0.0.1", 0), AdaptiveServer)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                mem = HostMemory(pathlib.Path(d) / "hosts.json")
                AdaptiveServer.mode, AdaptiveServer.size = "per_connection", 12 * 1024 * 1024
                AdaptiveServer.active = AdaptiveServer.peak = AdaptiveServer.refused = 0
                notes = []
                for i in range(2):
                    task = DownloadTask(f"http://127.0.0.1:{server.server_address[1]}/m{i}.bin", d, 8)
                    task.host_memory = mem
                    task.start()
                    seen = set()
                    while task.is_running():
                        seen.add(task.connection_note)
                        time.sleep(0.05)
                    self.assertEqual(task.status, Status.COMPLETED, task.error)
                    notes.append(seen)
                self.assertIn("testando velocidade", notes[0])
                self.assertEqual(mem.get("127.0.0.1")["conns"], 8)
                self.assertTrue(any("começa com 8" in (n or "") for n in notes[1]), notes[1])
        finally:
            server.shutdown()


class ManyAddressesTests(unittest.TestCase):
    """A server name with several addresses (CDNs): connections are spread over them."""

    def serve_on(self, ip, port=0):
        seen = []

        class H(Handler):
            def do_GET(self):
                seen.append(self.headers.get("Host"))
                super().do_GET()
        srv = ThreadingHTTPServer((ip, port), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, seen

    def test_connections_go_to_every_address_and_the_file_is_intact(self):
        from unittest import mock
        from flowdl import net
        a, seen_a = self.serve_on("127.0.0.1")
        port = a.server_address[1]
        try:
            b, seen_b = self.serve_on("127.0.0.2", port)
        except OSError:
            a.shutdown()
            self.skipTest("127.0.0.2 não disponível")
        tmp = tempfile.TemporaryDirectory()
        try:
            with mock.patch.object(net, "addresses", return_value=["127.0.0.1", "127.0.0.2"]):
                task = DownloadTask(f"http://127.0.0.1:{port}/grande.bin", tmp.name, 8)
                task.start()
                task.wait(60)
            self.assertEqual(task.status, Status.COMPLETED, task.error)
            with open(task.path, "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).digest(), hashlib.sha256(DATA).digest())
            self.assertTrue(seen_a and seen_b, (len(seen_a), len(seen_b)))
            self.assertTrue(all(h == f"127.0.0.1:{port}" for h in seen_b))  # the name, not the address
            self.assertEqual(task.server_addresses, 2)
        finally:
            a.shutdown()
            b.shutdown()
            tmp.cleanup()

    def test_a_dead_address_does_not_break_the_download(self):
        from unittest import mock
        from flowdl import net
        a, _ = self.serve_on("127.0.0.1")
        port = a.server_address[1]
        tmp = tempfile.TemporaryDirectory()
        try:
            with mock.patch.object(net, "addresses", return_value=["127.0.0.1", "127.0.0.3"]):  # .3: nobody there
                task = DownloadTask(f"http://127.0.0.1:{port}/grande.bin", tmp.name, 8)
                task.start()
                task.wait(60)
            self.assertEqual(task.status, Status.COMPLETED, task.error)
            with open(task.path, "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).digest(), hashlib.sha256(DATA).digest())
        finally:
            a.shutdown()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()


BIG = DATA * 3  # ~15 MB


class AdaptiveServer(BaseHTTPRequestHandler):
    """Configurable server for the connection speed test.

    mode "per_connection": each connection is limited (a CDN): more connections = faster.
    mode "shared": the total is limited (slow line): more connections do not help.
    mode "max2": the server refuses (503) more than 2 connections at once.
    """
    mode = "per_connection"
    size = 0
    active = 0
    peak = 0
    refused = 0
    lock = threading.Lock()
    shared = None  # RateLimiter for "shared"

    def log_message(self, *a):
        pass

    def do_GET(self):
        cls = type(self)
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        start = int(m.group(1)) if m else 0
        end = int(m.group(2)) if m and m.group(2) else cls.size - 1
        with cls.lock:
            if cls.mode in ("max2", "max2_403") and cls.active >= 2 and end > start:
                cls.refused += 1
                self.send_error(503 if cls.mode == "max2" else 403)
                return
            cls.active += 1
            cls.peak = max(cls.peak, cls.active)
        try:
            body = BIG[start:end + 1]
            self.send_response(206 if m else 200)
            if m:
                self.send_header("Content-Range", f"bytes {start}-{end}/{cls.size}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for i in range(0, len(body), 16 * 1024):
                piece = body[i:i + 16 * 1024]
                self.wfile.write(piece)
                if cls.mode == "shared":
                    time.sleep(cls.shared.reserve(len(piece)))
                else:
                    time.sleep(len(piece) / 250_000)  # 250 KB/s per connection
        except OSError:
            pass
        finally:
            with cls.lock:
                cls.active -= 1


class AdaptiveConnectionTests(unittest.TestCase):
    """The server speed test: how many connections are worth opening."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), AdaptiveServer)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        from flowdl.limiter import RateLimiter
        self.tmp = tempfile.TemporaryDirectory()
        AdaptiveServer.active = AdaptiveServer.peak = AdaptiveServer.refused = 0
        AdaptiveServer.shared = RateLimiter(1_000_000)

    def tearDown(self):
        self.tmp.cleanup()

    def run_download(self, mode, size, connections=8):
        AdaptiveServer.mode, AdaptiveServer.size = mode, size
        task = DownloadTask(f"{self.base}/{mode}.bin", self.tmp.name, connections)
        notes = set()
        self.peak = self.streaming_peak = 0
        task.start()
        while task.is_running():
            notes.add(task.connection_note)
            self.peak = max(self.peak, task.active_connections)
            self.streaming_peak = max(self.streaming_peak, task._streaming)
            time.sleep(0.05)
        self.assertEqual(task.status, Status.COMPLETED, task.error)
        with open(task.path, "rb") as f:
            self.assertEqual(f.read(), BIG[:size])
        return task, notes

    def test_fast_server_ramps_up_to_the_maximum(self):
        task, notes = self.run_download("per_connection", 12 * 1024 * 1024)
        self.assertIn("testando velocidade", notes)
        self.assertEqual(self.peak, 8)  # 2 -> 4 -> 8: every step was faster, never more than 8

    def test_saturated_line_stops_adding_connections(self):
        task, notes = self.run_download("shared", 10 * 1024 * 1024, connections=16)
        # No point in 16 connections on a 1 MB/s line: the speed test concludes that fewer are
        # enough. (On a slow, noisy machine - seen on Windows CI - the first measurement, taken
        # while the connections are still opening, may be low, so the ramp may take one more
        # step before concluding: the maximum leaves room for that; the conclusion is what matters.)
        enough = [int(m.group(1)) for n in notes if (m := re.search(r"(\d+) conex\w+ bastam", n or ""))]
        self.assertTrue(enough, notes)
        self.assertLess(min(enough), 16, notes)

    def test_server_that_refuses_extra_connections_with_403(self):
        """Some file hosts answer 403 (not 429/503) to connections beyond their limit. That is
        not an expired link: the download must finish with the connections it accepts."""
        task, notes = self.run_download("max2_403", 3 * 1024 * 1024)
        self.assertGreater(AdaptiveServer.refused, 0)
        self.assertIsNone(task.error)

    def test_server_connection_limit_is_respected_without_failing(self):
        # The server refuses (503) every connection beyond 2. The download must still finish,
        # intact and without error, by giving the refused pieces to the accepted connections.
        task, notes = self.run_download("max2", 3 * 1024 * 1024)
        self.assertGreater(AdaptiveServer.refused, 0)
        self.assertIsNone(task.error)
        if task.server_limit is not None:  # shown to the user as "servidor aceita N"
            self.assertIn(f"servidor aceita {task.server_limit}", notes)
            self.assertLess(task.server_limit, 8)


class KeepAliveServer(BaseHTTPRequestHandler):
    """HTTP/1.1 server that counts TCP connections and requests (like a real CDN)."""
    protocol_version = "HTTP/1.1"
    connections = 0
    requests = 0
    first_range = None
    slow_ports: set = set()
    slow_first = 0  # make the first N connections very slow (a bad CDN node)
    fast_rate = 2_000_000
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def setup(self):
        super().setup()
        cls = type(self)
        with cls.lock:
            cls.connections += 1
            self.slow = cls.connections <= cls.slow_first

    def do_GET(self):
        cls = type(self)
        with cls.lock:
            cls.requests += 1
            if cls.first_range is None:
                cls.first_range = self.headers.get("Range")
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        start = int(m.group(1)) if m else 0
        end = int(m.group(2)) if m and m.group(2) else len(BIG) - 1
        body = BIG[start:end + 1]
        self.send_response(206 if m else 200)
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(BIG)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            for i in range(0, len(body), 64 * 1024):
                self.wfile.write(body[i:i + 64 * 1024])
                time.sleep(64 * 1024 / (20_000 if self.slow else cls.fast_rate))
        except OSError:
            pass


class ConnectionReuseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), KeepAliveServer)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        KeepAliveServer.connections = KeepAliveServer.requests = KeepAliveServer.slow_first = 0
        KeepAliveServer.fast_rate = 2_000_000
        KeepAliveServer.first_range = None

    def tearDown(self):
        self.tmp.cleanup()

    def download(self, connections=8):
        task = DownloadTask(self.base + "/grande.bin", self.tmp.name, connections)
        peak = 0
        task.start()
        while task.is_running():
            peak = max(peak, task.active_connections)
            time.sleep(0.05)
        self.assertEqual(task.status, Status.COMPLETED, task.error)
        with open(task.path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).digest(), hashlib.sha256(BIG).digest())
        return task, peak

    def test_connections_are_reused_for_many_pieces(self):
        task, peak = self.download()
        # many pieces (requests) went over few connections: no new handshake per piece
        self.assertGreater(KeepAliveServer.requests, KeepAliveServer.connections)
        self.assertLessEqual(KeepAliveServer.connections, task.connections + 1)

    def test_first_request_already_downloads_data(self):
        self.download()
        self.assertEqual(KeepAliveServer.first_range, "bytes=0-2097151")  # not a 1-byte probe

    def test_big_files_start_with_four_connections(self):
        task = DownloadTask(self.base + "/grande.bin", self.tmp.name, 8)
        import flowdl.engine as eng
        old = eng.BIG_FILE
        eng.BIG_FILE = 1024 * 1024  # treat our 15 MB test file as "big"
        try:
            task.start()
            time.sleep(1.0)  # before the first speed check (2 s)
            self.assertEqual(task.active_connections, 4)
        finally:
            eng.BIG_FILE = old
            task.pause()
            task.wait(10)

    def test_slow_connection_is_replaced(self):
        KeepAliveServer.slow_first = 1  # the very first connection lands on a slow node
        # A long enough download for the check (a connection slow for 5 s+) to run, with margin
        # for slower machines (Windows CI): ~20 s here, while the slow node alone would take minutes.
        KeepAliveServer.fast_rate = 150_000
        t0 = time.monotonic()
        task, _ = self.download()
        self.assertGreaterEqual(task.reconnections, 1)
        self.assertLess(time.monotonic() - t0, 60)

    def test_endgame_does_not_wait_for_a_slow_connection(self):
        """The first piece (2 MB) lands on a 20 KB/s connection: alone it would take ~100 s.
        The other connections take over half of what it still has to receive, repeatedly."""
        KeepAliveServer.slow_first = 1
        t0 = time.monotonic()
        self.download()
        self.assertLess(time.monotonic() - t0, 15)


class IdTests(unittest.TestCase):
    def test_ids_are_unique_when_added_at_once(self):
        """Regression (found on Windows): ids taken from the clock repeated."""
        from flowdl.video import VideoTask
        ids = {DownloadTask("http://x/a", "/tmp").id for _ in range(2000)}
        ids |= {VideoTask("http://x/v", "/tmp").id for _ in range(500)}
        self.assertEqual(len(ids), 2500)


class BadServer(BaseHTTPRequestHandler):
    """A server with one bad habit at a time (set before each test). Found by testing the
    engine against 86 kinds of server behaviour."""
    protocol_version = "HTTP/1.1"
    data = b""
    mode = ""
    hits = 0
    names = {}  # path -> Content-Disposition name

    def log_message(self, *a):
        pass

    def do_GET(self):
        cls = BadServer
        cls.hits += 1
        path = self.path.split("?")[0]
        data = cls.names.get(path, (None, cls.data))[1]
        name = cls.names.get(path, (None, None))[0]
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        if cls.mode == "empty416" and m:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if cls.mode in ("truncate_norange", "truncate_once") or not m:
            body = data
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            if name:
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.end_headers()
            if cls.mode == "truncate_norange" or (cls.mode == "truncate_once" and cls.hits <= 2):
                self.wfile.write(body[:len(body) // 3])  # then the connection drops
                self.close_connection = True
                return
            self.wfile.write(body)
            return
        if cls.mode == "new_version" and cls.hits > 3:
            data = cls.data2  # the file was replaced on the server
        a = int(m.group(1))
        b = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
        total = len(data)
        body = data[a:b + 1]
        start = a
        if cls.mode == "shift":
            start = max(0, a - 4096)
            body = data[start:start + len(body)]
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{start + len(body) - 1}/{total}")
        if cls.mode == "short_body":
            self.send_header("Content-Length", str(max(0, len(body) - 3000)))
            self.end_headers()
            self.wfile.write(body[:max(0, len(body) - 3000)])
            return
        self.send_header("Content-Length", str(len(body)))
        if name:
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()
        try:
            for i in range(0, len(body), 65536):
                self.wfile.write(body[i:i + 65536])
                if cls.mode in ("new_version", "same_name"):
                    time.sleep(0.003)
        except OSError:
            pass


class BadServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ThreadingHTTPServer.request_queue_size = 64
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), BadServer)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        BadServer.data, BadServer.mode, BadServer.hits, BadServer.names = os.urandom(3 * 1024 * 1024), "", 0, {}
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def run_task(self, path="/f.bin", conns=4, secs=60):
        task = DownloadTask(self.base + path, self.tmp.name, conns)
        task.start()
        task.wait(secs)
        return task

    def test_cut_off_file_is_never_taken_as_complete(self):
        """A server without ranges that drops the connection before the announced size: the
        file used to be saved, cut off, as finished (100%)."""
        BadServer.mode = "truncate_norange"
        task = self.run_task()
        self.assertEqual(task.status, Status.ERROR)
        self.assertIn("caiu antes do fim", task.error)
        self.assertFalse(task.path and os.path.exists(task.path))
        BadServer.mode, BadServer.hits = "truncate_once", 0  # it drops only the first time: tried again
        task = self.run_task("/g.bin")
        self.assertEqual(task.status, Status.COMPLETED, task.error)
        with open(task.path, "rb") as f:
            self.assertEqual(f.read(), BadServer.data)

    def test_wrong_part_from_the_server_is_never_written(self):
        """Parts that start elsewhere than asked used to be written where asked: a corrupt
        file marked finished."""
        BadServer.mode = "shift"
        task = DownloadTask(self.base + "/s.bin", self.tmp.name, 4)
        task.start()
        time.sleep(4)
        task.pause()
        task.wait(10)
        self.assertNotEqual(task.status, Status.COMPLETED)

    def test_file_replaced_on_the_server_is_not_mixed(self):
        """The server replaced the file (another size) and ignores If-Range: the download starts
        over with the new version instead of mixing both."""
        BadServer.data2 = os.urandom(3 * 1024 * 1024 + 777)
        BadServer.mode = "new_version"
        task = self.run_task(conns=4)
        self.assertEqual(task.status, Status.COMPLETED, task.error)
        with open(task.path, "rb") as f:
            self.assertEqual(f.read(), BadServer.data2)

    def test_answer_without_the_rest_does_not_flood_the_server(self):
        """Answers shorter than they say: it asked again thousands of times a second."""
        BadServer.mode = "short_body"
        task = DownloadTask(self.base + "/h.bin", self.tmp.name, 2)
        task.start()
        time.sleep(5)
        hits = BadServer.hits
        task.pause()
        task.wait(10)
        self.assertLess(hits, 60, f"{hits} requests in 5 s")

    def test_empty_file_on_a_strict_server(self):
        """Servers like S3 answer 416 ("bytes */0") to a range on an empty file."""
        BadServer.mode, BadServer.data = "empty416", b""
        task = self.run_task("/vazio.txt")
        self.assertEqual(task.status, Status.COMPLETED, task.error)
        self.assertEqual(os.path.getsize(task.path), 0)

    def test_names_with_semicolon_quotes_and_windows_paths(self):
        from email.message import Message
        from flowdl.engine import _filename_from_headers
        for cd, want in (('attachment; filename="a;b.zip"', "a;b.zip"),
                         (r'attachment; filename="say \"hi\".txt"', "say _hi_.txt"),
                         (r'attachment; filename="C:\Windows\evil.exe"', "evil.exe"),
                         ("attachment; filename=plain.zip; size=3", "plain.zip")):
            msg = Message()
            msg["Content-Disposition"] = cd
            self.assertEqual(_filename_from_headers(msg, "http://h/x"), want, cd)

    def test_downloads_with_the_same_name_at_once_get_their_own_files(self):
        """They used to pick the same free name and write into one file."""
        BadServer.mode = "same_name"
        BadServer.names = {f"/get{i}": ("same.bin", os.urandom(1024 * 1024 + i)) for i in range(4)}
        tasks = [DownloadTask(f"{self.base}/get{i}", self.tmp.name, 2) for i in range(4)]
        for t in tasks:
            t.start()
        for t in tasks:
            t.wait(60)
        paths = {t.path for t in tasks}
        self.assertEqual(len(paths), 4)
        for i, t in enumerate(tasks):
            self.assertEqual(t.status, Status.COMPLETED, t.error)
            with open(t.path, "rb") as f:
                self.assertEqual(f.read(), BadServer.names[f"/get{i}"][1])
