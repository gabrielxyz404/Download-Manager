"""Updates from the Releases page: found, checked against the published SHA-256, installed."""

import hashlib
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from flowdl import updater

INSTALLER = os.urandom(200_000)


class FakeGitHub(BaseHTTPRequestHandler):
    release = None   # JSON of the latest release, or None (404 = private / no releases)
    sums = b""

    def log_message(self, *a):
        pass

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        if self.path == "/latest":
            if FakeGitHub.release is None:
                self.send_error(404)
                return
            body = json.dumps(FakeGitHub.release(base)).encode()
        elif self.path == "/dl/FlowDownloads-Setup.exe":
            body = INSTALLER
        elif self.path == "/dl/SHA256SUMS.txt":
            body = FakeGitHub.sums
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def release(version):
    return lambda base: {"tag_name": "v" + version, "body": "Novidades: ícone ao lado do relógio.",
                         "html_url": base + "/page",
                         "assets": [{"name": "FlowDownloads-Setup.exe", "browser_download_url": base + "/dl/FlowDownloads-Setup.exe"},
                                    {"name": "SHA256SUMS.txt", "browser_download_url": base + "/dl/SHA256SUMS.txt"}]}


class UpdaterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.api = mock.patch.object(updater, "API", f"http://127.0.0.1:{cls.server.server_address[1]}/latest")
        cls.api.start()

    @classmethod
    def tearDownClass(cls):
        cls.api.stop()
        cls.server.shutdown()

    def setUp(self):
        FakeGitHub.sums = f"{hashlib.sha256(INSTALLER).hexdigest()}  FlowDownloads-Setup.exe\n".encode()

    def test_newer_version_found_downloaded_and_checked(self):
        FakeGitHub.release = release("9.9.9")
        update, msg = updater.check("0.8.0")
        self.assertIsNotNone(update, msg)
        self.assertEqual(update.version, "9.9.9")
        self.assertIn("9.9.9", msg)
        with tempfile.TemporaryDirectory() as d:
            path = updater.download(update, d)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), INSTALLER)

    def test_tampered_installer_is_never_saved_or_run(self):
        FakeGitHub.release = release("9.9.9")
        FakeGitHub.sums = ("0" * 64 + "  FlowDownloads-Setup.exe\n").encode()
        update, _ = updater.check("0.8.0")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(updater, "run_installer") as run:
            with self.assertRaises(ValueError):
                updater.download(update, d)
            self.assertEqual(os.listdir(d), [])
            run.assert_not_called()

    def test_already_newest(self):
        FakeGitHub.release = release("0.8.0")
        update, msg = updater.check("0.8.0")
        self.assertIsNone(update)
        self.assertIn("mais recente", msg)
        self.assertIsNone(updater.check("0.10.0")[0])  # 0.10 is newer than 0.8 (numbers, not text)

    def test_private_repository_is_said_plainly(self):
        FakeGitHub.release = None
        update, msg = updater.check("0.8.0")
        self.assertIsNone(update)
        self.assertIn("privado", msg)

    def test_silent_install_restarts_the_app(self):
        cmd = updater.install_command("C:\\\\x\\\\FlowDownloads-Setup-9.9.9.exe")
        self.assertIn("/VERYSILENT", cmd)
        self.assertIn("/UPDATE=1", cmd)


class UpdaterHousekeepingTests(unittest.TestCase):
    def test_old_installers_are_removed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for n in ("FlowDownloads-Setup-0.8.0.exe", "FlowDownloads-Setup-0.9.0.exe", "outro.txt"):
                open(os.path.join(d, n), "wb").close()
            updater.clean_old(d)
            self.assertEqual(os.listdir(d), ["outro.txt"])

    def test_installer_still_starts_when_leaving_the_job_is_not_allowed(self):
        from unittest import mock
        calls = []

        def popen(cmd, creationflags=0, **kw):
            calls.append(creationflags)
            if creationflags & 0x01000000:
                raise OSError(5, "Access is denied")
        with mock.patch.object(updater.os, "name", "nt"), \
                mock.patch.object(updater.subprocess, "Popen", popen), \
                mock.patch.object(updater.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True), \
                mock.patch.object(updater.subprocess, "DETACHED_PROCESS", 0x8, create=True):
            updater.run_installer("setup.exe")
        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[1] & 0x01000000)

if __name__ == "__main__":
    unittest.main()
