"""Start PyDM in the background when the user logs in (Windows / Linux)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "PyDM"
LAUNCHER = Path(__file__).resolve().parent.parent / "PyDM.pyw"


def supported() -> bool:
    return sys.platform.startswith("win") or sys.platform.startswith("linux")


def _command() -> str:
    exe = sys.executable
    if sys.platform.startswith("win") and exe.lower().endswith("python.exe"):
        exe = exe[:-10] + "pythonw.exe"  # no console window
    return f'"{exe}" "{LAUNCHER}" --background'


def _desktop_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / "pydm.desktop"


def is_enabled() -> bool:
    if sys.platform.startswith("win"):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                winreg.QueryValueEx(key, APP_NAME)
            return True
        except OSError:
            return False
    return _desktop_file().exists()


def set_enabled(enabled: bool) -> None:
    if sys.platform.startswith("win"):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except OSError:
                    pass
        return
    path = _desktop_file()
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[Desktop Entry]\nType=Application\nName=PyDM\n"
                        f"Exec={_command()}\nX-GNOME-Autostart-enabled=true\n", encoding="utf-8")
    elif path.exists():
        path.unlink()
