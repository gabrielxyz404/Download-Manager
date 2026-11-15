"""Links and login cookies: per-site cookies, expired signed links, cookies on disk."""

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flowdl import vault
from flowdl.engine import DownloadTask, Status
from flowdl.manager import DownloadManager

DATA = os.urandom(6 * 1024 * 1024)


class FileHost(BaseHTTPRequestHandler):
    """The file server ("site B", reached as localhost). Signed links: /f?token=N is valid for
    `uses` requests, like short-lived addresses from GitHub or cloud storage."""
    protocol_version = "HTTP/1.1"
    seen = []
    tokens = {}
    uses = 10 ** 9
    delay = 0.0

    def log_message(self, *a):
        pass

    def do_GET(self):
        cls = type(self)
        cls.seen.append({"path": self.path, "cookie": self.headers.get("Cookie"),
                         "auth": self.headers.get("Authorization")})
        token = self.path.split("token=")[-1]
        left = cls.tokens.get(token, 0)
        if left <= 0:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        cls.tokens[token] = left - 1
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
            for i in range(0, len(body), 64 * 1024):
                self.wfile.write(body[i:i + 64 * 1024])
                if cls.delay:
                    import time
                    time.sleep(cls.delay)
        except OSError:
            pass


class LinkSite(BaseHTTPRequestHandler):
    """The site with the download link ("site A", 127.0.0.1): each visit hands out a fresh
    signed address on the file server."""
    protocol_version = "HTTP/1.1"
    seen = []
    counter = 0
    file_port = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        cls = type(self)
        cls.seen.append({"cookie": self.headers.get("Cookie")})
        cls.counter += 1
        token = f"t{cls.counter}"
        FileHost.tokens[token] = FileHost.uses
        self.send_response(302)
        self.send_header("Location", f"http://localhost:{cls.file_port}/f?token={token}")
        self.send_header("Content-Length", "0")
        self.end_headers()


def serve(handler):
    s = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    s.daemon_threads = True
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


class SecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = serve(FileHost)
        cls.site = serve(LinkSite)
        LinkSite.file_port = cls.files.server_address[1]
        cls.link = f"http://127.0.0.1:{cls.site.server_address[1]}/download?id=7"

    @classmethod
    def tearDownClass(cls):
        cls.files.shutdown()
        cls.site.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        FileHost.seen, FileHost.tokens, FileHost.uses, FileHost.delay = [], {}, 10 ** 9, 0.0
        LinkSite.seen, LinkSite.counter = [], 0

    def run_task(self, task):
        task.start()
        task.wait(60)
        self.assertEqual(task.status, Status.COMPLETED, task.error)
        with open(task.path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).digest(), hashlib.sha256(DATA).digest())

    def test_cookies_and_password_stay_with_their_site(self):
        """C: the link site's login must not be sent to the file server it redirects to."""
        task = DownloadTask(self.link, self.tmp, 4, headers={"Authorization": "Basic c2VjcmV0"},
                            cookies={"127.0.0.1": "session=abc"})
        self.run_task(task)
        self.assertTrue(LinkSite.seen and all(r["cookie"] == "session=abc" for r in LinkSite.seen))
        self.assertTrue(FileHost.seen)
        self.assertTrue(all(r["cookie"] is None and r["auth"] is None for r in FileHost.seen), FileHost.seen)

    def test_each_site_gets_its_own_cookies(self):
        task = DownloadTask(self.link, self.tmp, 4,
                            cookies={"127.0.0.1": "session=abc", "localhost": "cdn=xyz"})
        self.run_task(task)
        self.assertTrue(all(r["cookie"] == "cdn=xyz" for r in FileHost.seen))

    def test_old_cookie_header_belongs_to_the_link_site(self):
        task = DownloadTask(self.link, self.tmp, 2, headers={"Cookie": "session=abc"})
        self.assertEqual(task.cookies, {"127.0.0.1": "session=abc"})
        self.assertNotIn("Cookie", task.headers)

    def test_signed_address_expires_during_download(self):
        """A: each signed address works for 3 requests; the download asks the original link
        for fresh ones and completes intact."""
        FileHost.uses = 3
        self.run_task(DownloadTask(self.link, self.tmp, 4))
        self.assertGreater(LinkSite.counter, 1, "it should have asked the link again")

    def test_browser_final_address_already_expired(self):
        """A: the browser's final address no longer works when the app starts."""
        task = DownloadTask(self.link, self.tmp, 4,
                            final_url=f"http://localhost:{LinkSite.file_port}/f?token=velho")
        self.run_task(task)
        self.assertEqual(FileHost.seen[0]["path"], "/f?token=velho")  # tried the browser's address first

    def test_cookies_not_in_plain_text_on_disk(self):
        """E"""
        db = os.path.join(self.tmp, "db.json")
        mgr = DownloadManager(db_path=db)
        mgr.add(self.link, self.tmp, 2, start=False, cookies={"127.0.0.1": "session=SEGREDO123"})
        mgr.save()
        with open(db, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("SEGREDO123", text)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(db).st_mode), 0o600)
        again = DownloadManager(db_path=db)
        self.assertEqual(again.tasks[0].cookies, {"127.0.0.1": "session=SEGREDO123"})

    def test_vault_roundtrip(self):
        value = {"example.com": "a=1; b=2"}
        self.assertEqual(vault.unprotect(vault.protect(value)), value)
        self.assertIsNone(vault.protect({}))
        self.assertEqual(vault.unprotect("lixo", {}), {})




class FileNameTests(unittest.TestCase):
    def test_windows_device_names_and_very_long_names(self):
        """A server could name its file "con.txt" or "NUL": on Windows those open a device
        (or fail at the end). Names over 255 characters fail too. Both are made safe."""
        from flowdl.engine import MAX_NAME, sanitize_filename
        for name in ("con.txt", "CON", "nul.tar.gz", "COM1.zip", "lpt9", "Aux.pdf"):
            self.assertTrue(sanitize_filename(name).startswith("_"), name)
        for name in ("console.txt", "comic.zip", "null.bin"):
            self.assertEqual(sanitize_filename(name), name)
        long = sanitize_filename("a" * 400 + ".zip")
        self.assertLessEqual(len(long), MAX_NAME)
        self.assertTrue(long.endswith(".zip"))
        self.assertEqual(sanitize_filename("..\\..\\x/../evil.exe"), "evil.exe")
        self.assertEqual(sanitize_filename(" . "), "download")


class RefusedLinkTests(unittest.TestCase):
    def test_gives_up_when_fresh_addresses_never_work(self):
        """No endless loop: if every fresh address is refused, the download stops with a message."""
        files, site = serve(FileHost), serve(LinkSite)
        LinkSite.file_port = files.server_address[1]
        FileHost.seen, FileHost.tokens, FileHost.uses, FileHost.delay = [], {}, 10 ** 9, 0.05
        try:
            task = DownloadTask(f"http://127.0.0.1:{site.server_address[1]}/d", tempfile.mkdtemp(), 4)
            task.start()
            import time
            deadline = time.monotonic() + 30
            while task.downloaded == 0 and time.monotonic() < deadline:
                time.sleep(0.05)  # wait for real progress (slow machines start later)
            self.assertGreater(task.downloaded, 0)
            FileHost.uses = 0  # from now on every address is refused
            FileHost.tokens = {k: 0 for k in FileHost.tokens}
            task.wait(60)
            self.assertEqual(task.status, Status.ERROR)
            self.assertIn("deixou de funcionar", task.error)
        finally:
            FileHost.delay = 0.0
            files.shutdown()
            site.shutdown()


class BrowserIdentityTests(unittest.TestCase):
    def test_browser_identity_is_passed_on_safely(self):
        from flowdl.bridge import _download_item
        from flowdl.service import browser_request
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/142.0 Safari/537.36"
        item = _download_item({"url": "https://a.com/x.zip", "user_agent": ua, "accept_language": "pt-BR,pt"})
        headers, _ = browser_request(item)
        self.assertEqual(headers["User-Agent"], ua)
        self.assertEqual(headers["Accept-Language"], "pt-BR,pt")
        # nothing that could smuggle another header in
        bad = _download_item({"url": "https://a.com/x.zip", "user_agent": "x\r\nCookie: roubado=1"})
        self.assertNotIn("User-Agent", browser_request(bad)[0])
        self.assertNotIn("User-Agent", browser_request(_download_item({"url": "https://a.com/x.zip"}))[0])

    def test_other_browser_headers_only_from_the_list(self):
        from flowdl.bridge import _download_item
        from flowdl.service import browser_request
        item = _download_item({"url": "https://a.com/x.zip", "browser_headers": {
            "sec-ch-ua": '"Brave";v="142"', "sec-fetch-mode": "navigate", "accept": "text/html,*/*",
            "range": "bytes=0-1", "host": "evil.com", "cookie": "a=1", "authorization": "Bearer x",
            "sec-fetch-site": "same\r\nX-Evil: 1"}})
        headers, _ = browser_request(item)
        self.assertEqual(headers["Sec-Ch-Ua"], '"Brave";v="142"')
        self.assertEqual(headers["Sec-Fetch-Mode"], "navigate")
        self.assertEqual(headers["Accept"], "text/html,*/*")
        for name in ("Range", "Host", "Cookie", "Authorization", "Sec-Fetch-Site"):
            self.assertNotIn(name, headers)


class SameFileTests(unittest.TestCase):
    """A download taken over from the browser: the app must be getting the same file."""

    def test_a_page_instead_of_the_file_is_refused(self):
        from flowdl.engine import _check_expected
        info = {"type": "text/html", "size": 900}
        with self.assertRaises(RuntimeError):
            _check_expected({"size": 0, "type": "application/zip"}, info)
        _check_expected({"size": 0, "type": "text/html"}, info)  # the browser was saving a page too
        _check_expected(None, info)  # not from the browser: nothing to compare

    def test_a_different_size_is_refused(self):
        from flowdl.engine import _check_expected
        with self.assertRaises(RuntimeError):
            _check_expected({"size": 5000, "type": ""}, {"type": "application/zip", "size": 4000})
        _check_expected({"size": 5000, "type": ""}, {"type": "application/zip", "size": 5000})
        _check_expected({"size": 0, "type": ""}, {"type": "application/zip", "size": 4000})  # unknown

    def test_expectation_from_the_extension(self):
        from flowdl.bridge import _download_item
        item = _download_item({"url": "https://a.com/x.zip", "browser_size": 1234, "browser_type": "application/zip"})
        self.assertEqual(item["expect"], {"size": 1234, "type": "application/zip"})
        self.assertIsNone(_download_item({"url": "https://a.com/x.zip"})["expect"])
        self.assertIsNone(_download_item({"url": "https://a.com/x.zip", "browser_size": "lixo"})["expect"])


if __name__ == "__main__":
    unittest.main()
