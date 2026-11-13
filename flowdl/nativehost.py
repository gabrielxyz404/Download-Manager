"""The browser can start Flow Downloads when it is closed (Chromium "native messaging").

The extension asks the browser to run us with its address as the first argument; we start
the app in the background, fully separate from the browser, answer, and end. Only our
extension may do this: the browser checks the "allowed_origins" below.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys

HOST_NAME = "com.flowdownloads.app"
EXTENSION_ID = "iomfaojagjphjdphkipgejgebijmpfph"  # fixed by the "key" in the extension's manifest
ORIGIN = f"chrome-extension://{EXTENSION_ID}/"

# Where Chromium browsers look for native messaging hosts of the current user (Windows registry).
REGISTRY_KEYS = [
    r"Software\Google\Chrome\NativeMessagingHosts",
    r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts",
    r"Software\Microsoft\Edge\NativeMessagingHosts",
    r"Software\Chromium\NativeMessagingHosts",
]


def called_by_browser(argv: list[str]) -> bool:
    return any(a.startswith("chrome-extension://") for a in argv[1:])


def manifest(path: str) -> dict:
    return {"name": HOST_NAME, "description": "Flow Downloads", "path": path, "type": "stdio",
            "allowed_origins": [ORIGIN]}


def host_program() -> str:
    """What the browser runs: the .exe itself; from the source folder, a small script."""
    from .paths import FROZEN, config_dir, install_dir
    if FROZEN:
        return sys.executable
    exe = sys.executable
    pyw = str(install_dir() / "Flow Downloads.pyw")
    if os.name == "nt":
        if exe.lower().endswith("pythonw.exe"):
            exe = exe[:-11] + "python.exe"
        script = config_dir() / "native-host.bat"
        text = f'@echo off\r\n"{exe}" "{pyw}" %*\r\n'
    else:
        script = config_dir() / "native-host.sh"
        text = f'#!/bin/sh\nexec "{exe}" "{pyw}" "$@"\n'
    if not script.exists() or script.read_text(encoding="utf-8", errors="replace") != text:
        script.write_text(text, encoding="utf-8")
        if os.name != "nt":
            os.chmod(script, 0o755)
    return str(script)


def register() -> bool:
    """Windows: tell the Chromium browsers (current user only, no admin) how to start us.
    Cheap and idempotent: only writes what changed. Returns True if anything was written."""
    if os.name != "nt":
        return False
    import winreg
    from .paths import config_dir
    path = config_dir() / "native-host.json"
    text = json.dumps(manifest(host_program()), indent=1)
    changed = False
    try:
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            changed = True
    except OSError:
        return False
    for base in REGISTRY_KEYS:
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + "\\" + HOST_NAME) as key:
                try:
                    current = winreg.QueryValueEx(key, "")[0]
                except OSError:
                    current = None
                if current != str(path):
                    winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(path))
                    changed = True
        except OSError:
            pass
    return changed


def _reply(message: dict) -> None:
    """Native messaging answer: 4-byte length + JSON (only if the browser gave us a pipe)."""
    out = getattr(sys.stdout, "buffer", None)
    if out is None:
        return
    try:
        data = json.dumps(message).encode()
        out.write(struct.pack("<I", len(data)) + data)
        out.flush()
    except (OSError, ValueError):
        pass


def serve() -> int:
    """Run by the browser: start the app in the background (separate from the browser, so it
    keeps running when the browser closes) and end at once."""
    from .paths import background_command
    flags = 0
    if os.name == "nt":
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                 | 0x01000000)  # CREATE_BREAKAWAY_FROM_JOB: not ended with the browser's helper
    env = dict(os.environ)
    env.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    started = False
    for f in ((flags, 0) if flags else (0,)):
        try:
            subprocess.Popen(background_command(), creationflags=f, close_fds=True, env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=os.name != "nt")
            started = True
            break
        except OSError:
            continue  # the browser's job may not allow leaving it: try plainly
    _reply({"ok": started})
    return 0 if started else 1
