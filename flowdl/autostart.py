"""Start Flow Downloads in the background when the user logs in (Windows / Linux)."""

from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path

from .paths import background_command

APP_NAME = "FlowDownloads"


def supported() -> bool:
    return sys.platform.startswith("win") or sys.platform.startswith("linux")


def _command() -> str:
    return subprocess.list2cmdline(background_command()) if sys.platform.startswith("win") \
        else " ".join(f'"{part}"' for part in background_command())


def _desktop_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / "flow-downloads.desktop"


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
        # CreateKey opens the key, or creates it: on a new Windows profile it may not exist yet.
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
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
        path.write_text("[Desktop Entry]\nType=Application\nName=Flow Downloads\n"
                        f"Exec={_command()}\nX-GNOME-Autostart-enabled=true\n", encoding="utf-8")
    elif path.exists():
        path.unlink()
