import http.cookiejar
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from flowdl import video
from flowdl.bridge import BrowserBridge
from flowdl.engine import DownloadTask, Status
from flowdl.manager import DownloadManager

HAVE_TOOLS = video.available() and shutil.which("ffmpeg") is not None


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def copyfile(self, source, outputfile):
        try:
            super().copyfile(source, outputfile)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client paused / closed the connection


@unittest.skipUnless(HAVE_TOOLS, "yt-dlp e ffmpeg são necessários")
class VideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = tempfile.mkdtemp()
        ff = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25",
              "-f", "lavfi", "-i", "sine=frequency=440", "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-shortest"]
        subprocess.run(ff + [os.path.join(cls.site, "clip.mp4")], check=True)
        subprocess.run(ff + ["-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
                             os.path.join(cls.site, "live.m3u8")], check=True)
        with open(os.path.join(cls.site, "watch.html"), "w") as f:
            f.write('<html><head><title>Meu video</title></head><body>'
                    '<video src="clip.mp4" controls></video></body></html>')
        handler = partial(Quiet, directory=cls.site)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.site, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, task, timeout=60):
        task.start()
        task.wait(timeout)
        self.assertEqual(task.status, Status.COMPLETED, task.error)
        self.assertTrue(os.path.exists(task.path))
        return task

    def test_options_from_html_page(self):
        info = video.get_options(self.base + "/watch.html")
        labels = [o["label"] for o in info["options"]]
        self.assertEqual(labels, ["Melhor qualidade", "Só áudio (MP3)"])

    def test_download_video_from_page(self):
        t = self._run(video.VideoTask(self.base + "/watch.html", self.tmp.name, "b", title="Meu video"))
        self.assertTrue(t.path.endswith(".mp4"))
        self.assertGreater(os.path.getsize(t.path), 10_000)

    def test_download_hls_stream(self):
        t = self._run(video.VideoTask(self.base + "/live.m3u8", self.tmp.name, "b"))
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                              t.path], capture_output=True, text=True).stdout
        self.assertAlmostEqual(float(out), 4.0, delta=1.0)

    def test_audio_only_mp3(self):
        t = self._run(video.VideoTask(self.base + "/clip.mp4", self.tmp.name, "ba/b", audio_only=True))
        self.assertTrue(t.path.endswith(".mp3"), t.path)

    def test_pause_then_resume(self):
        t = video.VideoTask(self.base + "/clip.mp4", self.tmp.name, "b", speed_limit=20_000)
        t.start()
        time.sleep(1.5)
        t.pause()
        t.wait(10)
        self.assertEqual(t.status, Status.PAUSED)
        t.speed_limit = 0
        self._run(t)

    def test_persist_roundtrip(self):
        t = video.VideoTask(self.base + "/x", self.tmp.name, "b", True, "T", quality="Só áudio")
        d = json.loads(json.dumps(t.to_dict()))
        t2 = video.VideoTask.from_dict(d)
        self.assertEqual((t2.format, t2.audio_only, t2.quality, t2.status), ("b", True, "Só áudio", Status.PAUSED))


class OptionTests(unittest.TestCase):
    # Shaped like YouTube: high resolutions are video-only, 360p has audio built in.
    INFO = {"title": "Clip", "webpage_url": "https://www.youtube.com/watch?v=x", "formats": [
        {"format_id": "18", "height": 360, "vcodec": "avc1", "acodec": "mp4a"},
        {"format_id": "137", "height": 1080, "vcodec": "avc1", "acodec": "none"},
        {"format_id": "136", "height": 720, "vcodec": "avc1", "acodec": "none"},
        {"format_id": "140", "vcodec": "none", "acodec": "mp4a"},
    ]}

    def test_with_ffmpeg_all_resolutions(self):
        opts = video.build_options(self.INFO, ffmpeg=True)
        self.assertEqual([o["label"] for o in opts["options"]],
                         ["Vídeo 1080p", "Vídeo 720p", "Vídeo 360p", "Só áudio (MP3)"])
        self.assertFalse(opts["limited"])

    def test_without_ffmpeg_only_ready_made_files(self):
        opts = video.build_options(self.INFO, ffmpeg=False)
        self.assertEqual([o["label"] for o in opts["options"]], ["Vídeo 360p", "Só áudio (M4A)"])
        self.assertTrue(opts["limited"])


class CookieAndResumeTests(unittest.TestCase):
    def test_cookiefile_readable_by_cookiejar(self):
        with tempfile.TemporaryDirectory() as d:
            path = video.write_cookiefile([
                {"domain": ".youtube.com", "hostOnly": False, "path": "/", "secure": True,
                 "expirationDate": 2000000000, "name": "SID", "value": "abc"},
                {"domain": "site.com", "hostOnly": True, "path": "/", "secure": False,
                 "name": "s", "value": "1"},
            ], os.path.join(d, "c", "cookies.txt"))
            jar = http.cookiejar.MozillaCookieJar(path)
            jar.load(ignore_discard=True, ignore_expires=True)
            self.assertEqual({c.name for c in jar}, {"SID", "s"})

    def test_auto_resume_after_close(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "db.json")
            mgr = DownloadManager(db_path=db)
            running = DownloadTask("http://127.0.0.1:9/a.bin", d)
            paused = DownloadTask("http://127.0.0.1:9/b.bin", d)
            paused.status = Status.PAUSED
            mgr.tasks = [running, paused]
            running.status = Status.DOWNLOADING
            mgr._resume_ids = {running.id}  # what shutdown() records
            mgr.save()
            again = DownloadManager(db_path=db, auto_resume=True)
            statuses = {t.url[-5:]: t.status for t in again.tasks}
            # a.bin was active -> resumes (it will fail to connect, but it was queued/started)
            self.assertNotEqual(statuses["a.bin"], Status.PAUSED)
            self.assertEqual(statuses["b.bin"], Status.PAUSED)
            for t in again.tasks:
                t.pause()
                t.wait(5)
            no_auto = DownloadManager(db_path=db + ".x")
            self.assertEqual(no_auto.tasks, [])

    def test_bridge_video_endpoint(self):
        got = []
        bridge = BrowserBridge(got.append, port=0).start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{bridge.port}/video", method="POST",
                headers={"X-FlowDL": "1", "Content-Type": "application/json"},
                data=json.dumps({"page": "https://www.youtube.com/watch?v=x", "title": "T",
                                 "media": [{"url": "https://cdn/a.mp4", "size": 5}, {"url": "blob:x"}],
                                 "cookie_jar": [{"name": "a", "value": "b", "domain": ".youtube.com"}]}).encode())
            self.assertEqual(urllib.request.urlopen(req, timeout=5).status, 200)
        finally:
            bridge.stop()
        self.assertEqual(got[0]["kind"], "video")
        self.assertEqual([m["url"] for m in got[0]["media"]], ["https://cdn/a.mp4"])
        self.assertEqual(got[0]["cookie_jar"][0]["name"], "a")




class VideoSupportUpdateTests(unittest.TestCase):
    """The newest yt-dlp from its official releases, used without reinstalling the app."""

    def setUp(self):
        import hashlib
        import http.server
        import io
        import pathlib
        import threading
        import zipfile
        from unittest import mock
        from flowdl import video
        self.video = video
        self.dir = tempfile.TemporaryDirectory()
        self.files = {}

        def zipapp(version):
            buf = io.BytesIO()
            buf.write(b"#!/usr/bin/env python3\n")
            with zipfile.ZipFile(buf, "a") as z:
                z.writestr("yt_dlp/__init__.py", "from . import version\n")
                z.writestr("yt_dlp/version.py", f"__version__ = '{version}'\n")
            return buf.getvalue()
        self.zipapp = zipapp
        self.hash = lambda data: hashlib.sha256(data).hexdigest()
        files = self.files

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = files.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        target = pathlib.Path(self.dir.name) / "yt-dlp.zip"
        self.target = target
        self.patches = [mock.patch.object(video, "UPDATE_URL", base + "/yt-dlp"),
                        mock.patch.object(video, "SUMS_URL", base + "/SHA2-256SUMS"),
                        mock.patch.object(video, "update_path", lambda: target),
                        mock.patch.object(video, "current_version", lambda: "2026.1.1")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        import sys
        for p in self.patches:
            p.stop()
        sys.meta_path[:] = [f for f in sys.meta_path if f is not self.video._UpdatedFirst]
        for name in [n for n in sys.modules if n == "yt_dlp" or n.startswith("yt_dlp.")]:
            del sys.modules[name]  # the real one is imported again by whoever needs it
        self.video.yt_dlp = None
        self.server.shutdown()
        self.dir.cleanup()

    def serve(self, data, published_hash=None):
        self.files["/yt-dlp"] = data
        self.files["/SHA2-256SUMS"] = (f"{published_hash or self.hash(data)}  yt-dlp\n"
                                       f"{'0' * 64}  yt-dlp.exe\n").encode()

    def test_newer_version_is_used_without_reinstalling(self):
        self.serve(self.zipapp("2099.12.31"))
        ok, msg = self.video.update_yt_dlp()
        self.assertTrue(ok, msg)
        self.assertIn("2099.12.31", msg)
        self.assertTrue(self.target.exists())
        self.assertEqual(self.video._lib().version.__version__, "2099.12.31")  # in use right away

    def test_file_that_does_not_match_the_official_checksum_is_refused(self):
        self.serve(self.zipapp("2099.12.31"), published_hash="f" * 64)
        ok, msg = self.video.update_yt_dlp()
        self.assertFalse(ok)
        self.assertIn("não confere", msg)
        self.assertFalse(self.target.exists())  # never saved, never imported

    def test_already_up_to_date(self):
        self.serve(self.zipapp("2025.1.1"))
        ok, msg = self.video.update_yt_dlp()
        self.assertTrue(ok)
        self.assertIn("mais recente", msg)
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
