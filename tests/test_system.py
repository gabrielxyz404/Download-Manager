"""Things the operating system does for us: keep awake, "came from the internet" mark, disk errors."""

import errno
import os
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from flowdl import system
from flowdl.engine import DownloadTask
from flowdl.status import Status


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class MarkOfTheWebTests(unittest.TestCase):
    def test_text_windows_expects(self):
        text = system.zone_identifier("https://user:senha@cdn.site.com/a.exe?t=1#x", "https://site.com/p")
        self.assertTrue(text.startswith("[ZoneTransfer]\r\nZoneId=3\r\n"))
        self.assertIn("HostUrl=https://cdn.site.com/a.exe?t=1", text)
        self.assertIn("ReferrerUrl=https://site.com/p", text)
        self.assertNotIn("senha", text)  # never the password part of an address

    @unittest.skipUnless(os.name == "nt", "a marca só existe no Windows (NTFS)")
    def test_finished_download_is_marked_on_windows(self):
        with tempfile.TemporaryDirectory() as site, tempfile.TemporaryDirectory() as dest:
            with open(os.path.join(site, "programa.exe"), "wb") as f:
                f.write(os.urandom(300_000))
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=site))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                task = DownloadTask(f"http://127.0.0.1:{server.server_address[1]}/programa.exe", dest, 4)
                task.start()
                task.wait(30)
                self.assertEqual(task.status, Status.COMPLETED, task.error)
                with open(task.path + ":Zone.Identifier", encoding="ascii") as f:
                    self.assertIn("ZoneId=3", f.read())
            finally:
                server.shutdown()


class DiskTests(unittest.TestCase):
    def test_plain_messages(self):
        self.assertIn("Disco cheio", system.disk_message(OSError(errno.ENOSPC, "No space"), "D:\\x"))
        self.assertIn("Sem permissão", system.disk_message(OSError(errno.EACCES, "denied"), "C:\\y"))
        self.assertIn("já não existe", system.disk_message(OSError(errno.ENOENT, "gone"), "E:\\z"))

    def test_not_enough_space_pauses_and_resumes_later(self):
        """Not enough free space: the download pauses with a clear message (not an error),
        and simply continues with Resume once there is room."""
        with tempfile.TemporaryDirectory() as site, tempfile.TemporaryDirectory() as dest:
            data = os.urandom(3 * 1024 * 1024)
            with open(os.path.join(site, "grande.bin"), "wb") as f:
                f.write(data)
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=site))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                task = DownloadTask(f"http://127.0.0.1:{server.server_address[1]}/grande.bin", dest, 4)
                with mock.patch("flowdl.system.free_space", return_value=1000):
                    task.start()
                    task.wait(30)
                self.assertEqual(task.status, Status.PAUSED)
                self.assertIn("Falta espaço", task.error)
                task.start()  # room again
                task.wait(30)
                self.assertEqual(task.status, Status.COMPLETED, task.error)
                with open(task.path, "rb") as f:
                    self.assertEqual(f.read(), data)
            finally:
                server.shutdown()


class AwakeTests(unittest.TestCase):
    def test_does_nothing_outside_windows_and_stops_when_idle(self):
        busy = [True]
        awake = system.Awake(lambda: busy[0])
        awake.poke()
        if os.name != "nt":
            self.assertIsNone(awake._thread)
        busy[0] = False
        if awake._thread is not None:
            awake._thread.join(5)
            self.assertFalse(awake._thread.is_alive())


class SparseFileTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "NTFS")
    def test_part_file_is_sparse_on_windows(self):
        import ctypes
        import tempfile
        from flowdl import system
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.part")
            with open(p, "wb") as f:
                self.assertTrue(system.make_sparse(f))
                f.truncate(64 * 1024 * 1024)
                f.seek(60 * 1024 * 1024)
                f.write(b"x")  # far into the file: no zero-filling of what is before
            attrs = ctypes.windll.kernel32.GetFileAttributesW(p)
            self.assertTrue(attrs & system.FILE_ATTRIBUTE_SPARSE_FILE)

    def test_elsewhere_nothing_happens(self):
        import tempfile
        from flowdl import system
        if os.name == "nt":
            self.skipTest("Windows")
        with tempfile.TemporaryFile() as f:
            self.assertFalse(system.make_sparse(f))

if __name__ == "__main__":
    unittest.main()
