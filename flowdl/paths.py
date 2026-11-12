"""Where Flow Downloads keeps its files. Kept dependency-free so the background service stays tiny."""

from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))  # running as the installed .exe (PyInstaller)


def install_dir() -> Path:
    """Folder of the program: the .exe's folder when installed, the project folder otherwise."""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_download_dir() -> str:
    return str(Path.home() / "Downloads")


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    path = Path(base) / "FlowDownloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def background_command() -> list[str]:
    """Command that starts Flow Downloads in the background (no window)."""
    if FROZEN:
        return [sys.executable, "--background"]
    exe = sys.executable
    if sys.platform.startswith("win") and exe.lower().endswith("python.exe"):
        exe = exe[:-10] + "pythonw.exe"  # no console window
    return [exe, str(install_dir() / "Flow Downloads.pyw"), "--background"]
