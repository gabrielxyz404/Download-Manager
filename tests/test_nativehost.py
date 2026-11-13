"""The browser can start the app when it is closed (native messaging)."""

import io
import json
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

from flowdl import nativehost


class NativeHostTests(unittest.TestCase):
    def test_recognises_the_browser_call(self):
        self.assertTrue(nativehost.called_by_browser(
            ["Flow Downloads.exe", "chrome-extension://iomfaojagjphjdphkipgejgebijmpfph/", "--parent-window=0"]))
        self.assertFalse(nativehost.called_by_browser(["Flow Downloads.exe", "--background"]))
        self.assertFalse(nativehost.called_by_browser(["Flow Downloads.exe", "https://x.com/a.zip"]))

    def test_only_our_extension_may_start_it(self):
        m = nativehost.manifest("C:\\\\x\\\\Flow Downloads.exe")
        self.assertEqual(m["allowed_origins"], ["chrome-extension://iomfaojagjphjdphkipgejgebijmpfph/"])
        self.assertEqual(m["type"], "stdio")

    def test_extension_id_matches_its_key(self):
        """The ID the app allows must be the one the browser gives the extension (from its key)."""
        import base64
        import hashlib
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "browser-extension", "manifest.json"), encoding="utf-8") as f:
            key = json.load(f)["key"]
        digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
        self.assertEqual("".join(chr(ord("a") + int(c, 16)) for c in digest), nativehost.EXTENSION_ID)

    def test_serve_starts_the_app_and_answers_in_the_browser_format(self):
        out = io.BytesIO()
        fake_stdout = mock.Mock(buffer=out)
        with mock.patch("flowdl.paths.background_command", return_value=[sys.executable, "-c", "pass"]), \
                mock.patch.object(sys, "stdout", fake_stdout):
            self.assertEqual(nativehost.serve(), 0)
        data = out.getvalue()
        (length,) = struct.unpack("<I", data[:4])
        self.assertEqual(json.loads(data[4:4 + length]), {"ok": True})

    def test_serve_without_a_pipe_still_starts(self):
        with mock.patch("flowdl.paths.background_command", return_value=[sys.executable, "-c", "pass"]), \
                mock.patch.object(sys, "stdout", None):
            self.assertEqual(nativehost.serve(), 0)

    @unittest.skipUnless(os.name == "nt", "registo do Windows")
    def test_registers_for_the_chromium_browsers_on_windows(self):
        import winreg
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"APPDATA": d}):
            try:
                nativehost.register()
                self.assertFalse(nativehost.register())  # nothing changed: nothing written again
                for base in nativehost.REGISTRY_KEYS:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + "\\" + nativehost.HOST_NAME) as k:
                        path = winreg.QueryValueEx(k, "")[0]
                    with open(path, encoding="utf-8") as f:
                        m = json.load(f)
                    self.assertEqual(m["name"], "com.flowdownloads.app")
                    self.assertTrue(os.path.exists(m["path"]))
            finally:
                for base in nativehost.REGISTRY_KEYS:
                    try:
                        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base + "\\" + nativehost.HOST_NAME)
                    except OSError:
                        pass


if __name__ == "__main__":
    unittest.main()
