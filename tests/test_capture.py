"""L: which browser downloads the app takes over."""

import json
import os
import tempfile
import unittest
import urllib.request

from flowdl.capture import DEFAULT_TYPES, host_matches, should_capture
from flowdl.manager import DownloadManager
from flowdl.service import Service
from flowdl.settings import Settings


class S:  # settings stand-in
    capture_types = list(DEFAULT_TYPES)
    capture_all_types = False
    capture_skip_sites: list = []


class CaptureRuleTests(unittest.TestCase):
    def test_types_like_idm(self):
        self.assertTrue(should_capture(S, "https://a.com/x.zip"))
        self.assertTrue(should_capture(S, "https://a.com/f", filename="Setup.EXE"))
        self.assertFalse(should_capture(S, "https://a.com/notas.txt"))
        self.assertFalse(should_capture(S, "https://a.com/foto.jpg"))

    def test_script_links_decided_by_content_type(self):
        self.assertTrue(should_capture(S, "https://a.com/download.php?f=1", mime="application/octet-stream"))
        self.assertFalse(should_capture(S, "https://a.com/download.php?f=1", mime="text/html"))
        self.assertTrue(should_capture(S, "https://a.com/get.php", filename="jogo.iso"))

    def test_name_not_known_yet_uses_server_content_type(self):
        """The browser often reports the download before it knows the file name."""
        self.assertTrue(should_capture(S, "https://a.com/get?id=7", mime="application/x-zip-compressed"))
        self.assertTrue(should_capture(S, "https://a.com/get?id=7", mime="application/vnd.rar"))
        self.assertTrue(should_capture(S, "https://a.com/get?id=7", mime="video/mp4"))
        self.assertFalse(should_capture(S, "https://a.com/get?id=7", mime="text/html"))
        self.assertFalse(should_capture(S, "https://a.com/get?id=7", mime="text/plain"))
        self.assertFalse(should_capture(S, "https://a.com/get?id=7", mime="image/png"))
        self.assertFalse(should_capture(S, "https://a.com/get?id=7", mime="application/json"))

        class NoZip(S):
            capture_types = [t for t in DEFAULT_TYPES if t != "zip"]
        self.assertFalse(should_capture(NoZip, "https://a.com/get?id=7", mime="application/x-zip-compressed"))

    def test_final_address_name(self):
        self.assertTrue(should_capture(S, "https://a.com/go?id=9", final_url="https://cdn.a.com/file.7z"))

    def test_all_types_and_skipped_sites(self):
        class All(S):
            capture_all_types = True
        self.assertTrue(should_capture(All, "https://a.com/notas.txt"))

        class Skip(S):
            capture_skip_sites = ["banco.pt", "*.intranet.local"]
        self.assertFalse(should_capture(Skip, "https://www.banco.pt/extrato.pdf"))
        self.assertFalse(should_capture(Skip, "https://files.intranet.local/a.zip"))
        self.assertTrue(should_capture(Skip, "https://outro.pt/a.zip"))
        self.assertTrue(host_matches("banco.pt", "banco.pt"))
        self.assertFalse(host_matches("banco.pt", "meubanco.pt"))  # not a sub-domain


class CheckEndpointTests(unittest.TestCase):
    def test_extension_asks_the_app(self):
        tmp = tempfile.mkdtemp()
        settings = Settings()
        settings.path = type(settings.path)(os.path.join(tmp, "s.json"))
        svc = Service(settings, DownloadManager(db_path=os.path.join(tmp, "db.json")), port=0)
        svc.start_bridge()
        try:
            def check(**payload):
                req = urllib.request.Request(f"http://127.0.0.1:{svc.bridge.port}/check", method="POST",
                                             data=json.dumps(payload).encode(),
                                             headers={"X-FlowDL": "1", "Content-Type": "application/json"})
                return json.loads(urllib.request.urlopen(req, timeout=5).read())["capture"]
            self.assertTrue(check(url="https://a.com/x.zip"))
            self.assertFalse(check(url="https://a.com/x.txt"))
            settings.capture_all_types = True
            self.assertTrue(check(url="https://a.com/x.txt"))
        finally:
            svc.stop_bridge()


if __name__ == "__main__":
    unittest.main()
