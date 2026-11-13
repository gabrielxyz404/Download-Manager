"""Small things the operating system does for us (kept tiny: imported only when needed).

- keep the computer awake while downloads run (Windows sleeps on its own otherwise);
- mark finished files as "came from the internet" so Windows still warns before running them,
  exactly as for a file saved by the browser;
- plain-language messages for disk problems.
"""

from __future__ import annotations

import errno
import os
import threading
import time
import urllib.parse

# ------------------------------------------------------------------ keep awake
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class Awake:
    """While ``busy()`` is true, ask Windows not to go to sleep. The request belongs to the
    thread that made it, so one small thread holds it and ends when the downloads end."""

    def __init__(self, busy) -> None:
        self.busy = busy
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def poke(self) -> None:
        if os.name != "nt":
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._hold, name="awake", daemon=True)
            self._thread.start()

    def _hold(self) -> None:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        try:
            while self.busy():
                time.sleep(3)
        finally:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)  # sleep is allowed again


# ------------------------------------------------------------- mark of the web
def zone_identifier(url: str, referrer: str | None = None) -> str:
    """The text Windows keeps next to a downloaded file (the "Mark of the Web"): zone 3 =
    Internet. Addresses go without any user:password part."""
    def clean(u: str | None) -> str | None:
        if not u:
            return None
        p = urllib.parse.urlsplit(u)
        if p.scheme not in ("http", "https", "ftp"):
            return None
        host = p.hostname or ""
        if p.port:
            host += f":{p.port}"
        return urllib.parse.urlunsplit((p.scheme, host, p.path, p.query, ""))
    lines = ["[ZoneTransfer]", "ZoneId=3"]
    if clean(referrer):
        lines.append("ReferrerUrl=" + clean(referrer))
    if clean(url):
        lines.append("HostUrl=" + clean(url))
    return "\r\n".join(lines) + "\r\n"


def mark_from_internet(path: str, url: str, referrer: str | None = None) -> bool:
    """Windows only (NTFS keeps it in a hidden stream of the file; other disks just skip it)."""
    if os.name != "nt":
        return False
    try:
        with open(path + ":Zone.Identifier", "w", encoding="ascii", errors="replace") as f:
            f.write(zone_identifier(url, referrer))
        return True
    except OSError:
        return False  # FAT32 / exFAT pen drives have no place for it


# ------------------------------------------------------------- disk problems
DISK_FULL = {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}
NO_PERMISSION = {errno.EACCES, errno.EPERM, errno.EROFS}


def disk_message(exc: OSError, folder: str) -> str:
    """What went wrong with the disk, said plainly, with what to do."""
    winerror = getattr(exc, "winerror", None)
    if exc.errno in DISK_FULL or winerror in (39, 112):
        return f"Disco cheio em {folder}: liberte espaço e clique em Retomar"
    if exc.errno in NO_PERMISSION or winerror in (5, 19):
        return f"Sem permissão para gravar em {folder}: escolha outra pasta"
    if exc.errno == errno.ENOENT or winerror in (2, 3):
        return f"A pasta {folder} já não existe (disco removido?): ligue-o ou escolha outra pasta"
    return f"Erro ao gravar em {folder}: {exc.strerror or exc}"


def free_space(folder: str) -> int | None:
    import shutil
    try:
        return shutil.disk_usage(folder).free
    except OSError:
        return None
