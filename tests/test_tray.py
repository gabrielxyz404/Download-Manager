"""Icon next to the Windows clock: menu actions, hover text, notifications."""

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from flowdl import tray
from flowdl.limiter import GLOBAL_LIMITER
from flowdl.status import Status


class FakeTask:
    def __init__(self, status, running=False, speed=0.0, name="a.zip", error=None):
        self.status, self._running, self.speed, self.filename, self.error = status, running, speed, name, error

    def is_running(self):
        return self._running


class ServiceTrayTests(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"APPDATA": self.cfg.name, "XDG_CONFIG_HOME": self.cfg.name})
        self.env.start()
        from flowdl.service import Service
        self.svc = Service()
        self.svc._manager = mock.Mock(tasks=[])
        self.rate = GLOBAL_LIMITER.rate

    def tearDown(self):
        GLOBAL_LIMITER.rate = self.rate
        self.env.stop()
        self.cfg.cleanup()

    def test_hover_text(self):
        self.assertIn("à espera", self.svc.tray_status())
        self.svc._manager.tasks = [FakeTask(Status.DOWNLOADING, True, 3 * 1024 * 1024),
                                   FakeTask(Status.DOWNLOADING, True, 1024 * 1024)]
        self.assertEqual(self.svc.tray_status(), "Flow Downloads — 2 a baixar · 4.0 MB/s")

    def test_menu_actions(self):
        self.assertEqual(self.svc.tray_action("show"), "show")
        self.assertEqual(self.svc.tray_action("quit"), "quit")
        self.svc.tray_action("pause_all")
        self.svc._manager.pause_all.assert_called_once()
        self.svc.tray_action("resume_all")
        self.svc._manager.resume_all.assert_called_once()

    def test_speed_limit_switch(self):
        self.svc.settings.global_limit_kb = 500
        GLOBAL_LIMITER.rate = 500 * 1024
        self.svc.tray_action("limit")
        self.assertEqual(GLOBAL_LIMITER.rate, 0)
        self.assertFalse(self.svc.settings.limit_on)
        self.svc.tray_action("limit")
        self.assertEqual(GLOBAL_LIMITER.rate, 500 * 1024)
        self.assertTrue(self.svc.tray_menu_state()["limit_on"])

    def test_notification_once_per_download(self):
        self.svc.tray = mock.Mock()
        done = FakeTask(Status.COMPLETED, name="jogo.zip")
        self.svc.tray_notify_done(done)
        self.svc.tray_notify_done(done)
        self.svc.tray.notify.assert_called_once()
        self.assertIn("jogo.zip", self.svc.tray.notify.call_args[0][1])
        failed = FakeTask(Status.ERROR, name="x.iso", error="HTTP 404")
        self.svc.tray_notify_done(failed)
        self.assertTrue(self.svc.tray.notify.call_args[1].get("warning"))

    def test_background_loop_handles_the_icon(self):
        self.svc.events.put(("tray", "show"))
        self.assertEqual(self.svc.wait(), "main")
        self.svc.events.put(("tray", "quit"))
        self.assertIsNone(self.svc.wait())
        self.assertTrue(self.svc.exit_requested)


@unittest.skipUnless(tray.supported(), "o ícone só existe no Windows")
class WindowsTrayTests(unittest.TestCase):
    def test_real_icon_menu_and_notification(self):
        import ctypes
        got = []
        t = tray.Tray(got.append, status=lambda: "Flow Downloads — 1 a baixar · 2.0 MB/s",
                      menu_state=lambda: {"running": True, "paused": False, "limit_kb": 0, "limit_on": False})
        self.assertTrue(t.start())
        try:
            self.assertTrue(t.hwnd)
            self.assertTrue(ctypes.windll.user32.FindWindowW("FlowDownloadsTray", None))
            t.update("Flow Downloads — teste")
            t.notify("Download concluído", "teste.zip")
            send = ctypes.windll.user32.SendMessageW
            send(t.hwnd, tray.WM_COMMAND, tray.PAUSE_ALL, 0)
            send(t.hwnd, tray.CALLBACK, 0, tray.WM_LBUTTONDBLCLK)  # double-click on the icon
            self.assertEqual(got, ["pause_all", "show"])
        finally:
            t.stop()
        self.assertFalse(t._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
