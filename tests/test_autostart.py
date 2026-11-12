import os
import sys
import tempfile
import unittest

from flowdl import autostart


@unittest.skipUnless(autostart.supported(), "sistema sem arranque automático suportado")
class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.tmp  # Linux: keep the test away from the real config
        self.was_enabled = autostart.is_enabled()

    def tearDown(self):
        autostart.set_enabled(self.was_enabled)
        if self.old is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.old

    def test_enable_and_disable(self):
        autostart.set_enabled(True)
        self.assertTrue(autostart.is_enabled())
        self.assertIn("--background", autostart._command())
        if sys.platform.startswith("win"):
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                value, _ = winreg.QueryValueEx(key, "FlowDownloads")
            self.assertIn("--background", value)
        autostart.set_enabled(False)
        self.assertFalse(autostart.is_enabled())


if __name__ == "__main__":
    unittest.main()
