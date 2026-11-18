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

    def test_turning_a_sub_site_back_on_keeps_the_bigger_rule(self):
        """Found in an audit: turning capture back on for drive.google.com (in the popup)
        removed the whole "google.com" rule. Now that one site gets an exception."""
        from flowdl.capture import set_site_skipped, skipped_site
        st = Settings()
        st.capture_skip_sites = ["google.com"]
        self.assertTrue(skipped_site(st, "https://drive.google.com/x.zip"))
        self.assertTrue(set_site_skipped(st, "drive.google.com", False))
        self.assertFalse(skipped_site(st, "https://drive.google.com/x.zip"))
        self.assertTrue(skipped_site(st, "https://mail.google.com/x.zip"))  # the rule stays
        self.assertIn("google.com", st.capture_skip_sites)
        self.assertFalse(set_site_skipped(st, "drive.google.com", False))  # nothing changes
        self.assertTrue(set_site_skipped(st, "drive.google.com", True))  # off again
        self.assertTrue(skipped_site(st, "https://drive.google.com/x.zip"))
        self.assertEqual(st.capture_skip_sites, ["google.com"])

    def test_generic_file_type_is_taken_whatever_the_list(self):
        """Found in an audit: "application/octet-stream" (a link with no name) mapped to ".bin",
        so removing "bin" from the list stopped all such downloads."""
        st = Settings()
        st.capture_types = ["zip", "exe"]
        for mime in ("application/octet-stream", "binary/octet-stream; charset=x", "application/force-download"):
            self.assertTrue(should_capture(st, "https://a/get?id=7", mime=mime), mime)
        self.assertFalse(should_capture(st, "https://a/get?id=7", mime="application/pdf"))

    def test_unknown_type_sent_as_a_file_is_taken(self):
        """Seen by the user: testfile.org's 5 GB "testfile.org-5GB.dat" stayed with the browser
        ("tipo fora da lista da app") while the 1 GB file went to the app."""
        dat = "https://testfile.org/files-5GB"
        self.assertTrue(should_capture(S, dat, filename="testfile.org-5GB.dat", mime="application/octet-stream"))
        self.assertTrue(should_capture(S, "https://a.com/disco.vhdx", mime="binary/octet-stream"))
        # ...not when the server says what it is, nor for small everyday files
        self.assertFalse(should_capture(S, dat, filename="testfile.org-5GB.dat", mime="text/plain"))
        self.assertFalse(should_capture(S, "https://a.com/notas.txt", mime="application/octet-stream"))
        self.assertFalse(should_capture(S, "https://a.com/foto.jpg", mime="application/octet-stream"))
        # a type the user removed from the list stays with the browser
        class NoPdf(S):
            capture_types = [t for t in DEFAULT_TYPES if t != "pdf"]
        self.assertFalse(should_capture(NoPdf, "https://a.com/doc.pdf", mime="application/octet-stream"))

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


class SiteSwitchTests(unittest.TestCase):
    def test_turn_a_site_off_and_on(self):
        from flowdl.capture import set_site_skipped, skipped_site

        class St(S):
            capture_skip_sites: list = []
        st = St()
        self.assertTrue(set_site_skipped(st, "www.exemplo.com.br", True))
        self.assertEqual(st.capture_skip_sites, ["exemplo.com.br"])  # the whole site, www. or not
        self.assertTrue(skipped_site(st, "https://cdn.exemplo.com.br/a.zip"))
        self.assertFalse(should_capture(st, "https://www.exemplo.com.br/a.zip"))
        self.assertFalse(set_site_skipped(st, "exemplo.com.br", True))  # already there
        self.assertTrue(set_site_skipped(st, "exemplo.com.br", False))
        self.assertEqual(st.capture_skip_sites, [])
        self.assertTrue(should_capture(st, "https://www.exemplo.com.br/a.zip"))
        for bad in ("", "a b", "x.com/caminho"):
            self.assertFalse(set_site_skipped(st, bad, True))


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
