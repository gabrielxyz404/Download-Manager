"""Updates from the project's Releases page on GitHub.

Checks the latest release (at most once a day, or when asked), downloads its installer,
checks it against the SHA-256 published with it (a file that does not match is never run)
and installs it silently; the installer starts the app again. Loaded only when used.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = "gabrielxyz404/Download-Manager"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
INSTALLER = "FlowDownloads-Setup.exe"
SUMS = "SHA256SUMS.txt"
CHECK_EVERY = 24 * 3600


def version_key(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def _get(url: str, timeout: float = 30, accept: str = "application/octet-stream") -> bytes:
    from .engine import USER_AGENT
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(200 * 1024 * 1024)


class Update:
    def __init__(self, version: str, installer_url: str, sums_url: str | None, notes: str, page: str):
        self.version, self.installer_url, self.sums_url = version, installer_url, sums_url
        self.notes, self.page = notes, page


def check(current: str | None = None) -> tuple[Update | None, str]:
    """(update or None, message for the user)."""
    from . import __version__
    current = current or __version__
    try:
        data = json.loads(_get(API, 20, "application/vnd.github+json").decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, ("Não consegui ver a página de versões (o repositório no GitHub é privado, "
                          "ou ainda não há versões publicadas).")
        return None, f"Não consegui procurar atualizações (HTTP {exc.code})."
    except (OSError, ValueError) as exc:
        return None, f"Sem ligação ao GitHub ({exc})."
    latest = str(data.get("tag_name", "")).lstrip("v")
    if not latest or version_key(latest) <= version_key(current):
        return None, f"Está na versão mais recente ({current})."
    assets = {a.get("name"): a.get("browser_download_url") for a in data.get("assets") or []}
    if INSTALLER not in assets:
        return None, f"A versão {latest} não tem instalador."
    return (Update(latest, assets[INSTALLER], assets.get(SUMS), str(data.get("body") or "")[:2000],
                   str(data.get("html_url") or RELEASES_PAGE)),
            f"Há uma versão nova: {latest} (tem a {current}).")


def due(settings) -> bool:
    return time.time() - float(getattr(settings, "update_checked", 0) or 0) > CHECK_EVERY


def download(update: Update, folder) -> str:
    """The installer, checked against the published SHA-256. Raises ValueError if it does not
    match (it is then deleted and never run)."""
    if not update.sums_url:
        raise ValueError("a versão não publica o SHA-256 do instalador")
    sums = _get(update.sums_url, 30).decode("utf-8", "replace")
    want = next((line.split()[0].lower() for line in sums.splitlines()
                 if line.split()[1:] and line.split()[-1].lstrip("*") == INSTALLER), None)
    if not want:
        raise ValueError("o SHA-256 do instalador não está publicado")
    data = _get(update.installer_url, 600)
    if hashlib.sha256(data).hexdigest() != want:
        raise ValueError("o instalador descarregado não confere com o SHA-256 publicado: não foi usado")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"FlowDownloads-Setup-{update.version}.exe")
    with open(path, "wb") as f:
        f.write(data)
    return path


def install_command(installer: str) -> list[str]:
    """Silent install over the current one; the installer starts the app again (/UPDATE=1)."""
    return [installer, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE=1"]


def run_installer(installer: str) -> None:
    """Start the installer on its own (it outlives the app it replaces). Raises OSError if
    it cannot be started at all (the caller tells the user)."""
    def start(flags):
        subprocess.Popen(install_command(installer), creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name != "nt":
        start(0)
        return
    detached = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        start(detached | 0x01000000)  # CREATE_BREAKAWAY_FROM_JOB: survives the app's end
    except OSError:
        # Started from a "job" that forbids leaving it (e.g. by the browser): without that flag.
        start(detached)


def clean_old(folder, keep: str | None = None) -> None:
    """Remove installers of earlier updates (tens of MB each)."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        path = os.path.join(folder, name)
        if name.startswith("FlowDownloads-Setup") and name.endswith(".exe") and path != keep:
            try:
                os.remove(path)
            except OSError:
                pass  # still running (the update that is installing right now)


def can_install() -> bool:
    """Only the installed program updates itself (not a copy run from the source folder)."""
    from .paths import FROZEN
    return FROZEN and sys.platform.startswith("win")
