"""What happens when a download finishes: checksum, antivirus, open, extract, a command.

Loaded only when a download finishes (nothing of this stays in memory in the background).
Everything is off by default except the checksum the user typed for a download.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
import zipfile

ALGOS = {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}


def algo_for(expected: str) -> str | None:
    """MD5 / SHA-1 / SHA-256 / SHA-512, told apart by the length of the hex text."""
    text = (expected or "").strip().lower()
    if not text or any(c not in "0123456789abcdef" for c in text):
        return None
    return ALGOS.get(len(text))


def clean_hash(text: str) -> str:
    """Accepts "sha256:abc...", "ABC... nome.zip" (the format of .sha256 files) or just the hex."""
    text = (text or "").strip()
    if ":" in text.split()[0] if text else False:
        text = text.split(":", 1)[1]
    return text.split()[0].lower() if text.split() else ""


def file_hash(path: str, algo: str, stop=None) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            if stop is not None and stop():
                raise InterruptedError
            h.update(chunk)
    return h.hexdigest()


def verify(path: str, expected: str) -> tuple[bool, str]:
    """(ok, message) for the user."""
    want = clean_hash(expected)
    algo = algo_for(want)
    if not algo:
        return False, "checksum inválido (use MD5, SHA-1, SHA-256 ou SHA-512 em hexadecimal)"
    got = file_hash(path, algo)
    name = algo.upper().replace("SHA", "SHA-")
    if got == want:
        return True, f"✔ {name} confere"
    return False, f"⚠ {name} NÃO confere: o ficheiro está diferente do esperado"


# --------------------------------------------------------------------- antivirus
def defender() -> str | None:
    """Windows Defender's command-line scanner, if present."""
    if os.name != "nt":
        return None
    candidates = []
    platform = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Microsoft", "Windows Defender", "Platform")
    try:
        for v in sorted(os.listdir(platform), reverse=True):
            candidates.append(os.path.join(platform, v, "MpCmdRun.exe"))
    except OSError:
        pass
    candidates.append(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Windows Defender", "MpCmdRun.exe"))
    return next((c for c in candidates if os.path.exists(c)), None)


def scan(path: str) -> str:
    exe = defender()
    if not exe:
        return "antivírus do Windows não encontrado"
    try:
        r = subprocess.run([exe, "-Scan", "-ScanType", "3", "-File", path, "-DisableRemediation"],
                           capture_output=True, timeout=600, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"não consegui analisar ({exc})"
    if r.returncode == 2:
        return "⚠ o antivírus encontrou uma ameaça neste ficheiro"
    return "✔ antivírus: sem ameaças" if r.returncode == 0 else f"antivírus terminou com o código {r.returncode}"


_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


# ----------------------------------------------------------------------- extract
def extract_zip(path: str) -> str:
    """Into a folder next to the zip, named after it. Paths that try to leave that folder
    are refused by zipfile; there must be room for everything first."""
    if not zipfile.is_zipfile(path):
        return ""
    base = os.path.splitext(path)[0]
    target, n = base, 1
    while os.path.exists(target):
        n += 1
        target = f"{base} ({n})"
    try:
        with zipfile.ZipFile(path) as z:
            if any(i.flag_bits & 0x1 for i in z.infolist()):
                return "zip com palavra-passe: não extraído"
            need = sum(i.file_size for i in z.infolist())
            from .system import free_space
            free = free_space(os.path.dirname(path))
            if free is not None and need > free:
                return "sem espaço para extrair o zip"
            z.extractall(target)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        return f"não consegui extrair ({exc})"
    return f"extraído para {os.path.basename(target)}"


# ----------------------------------------------------------------- open / command
def open_file(path: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def command_for(template: str, path: str, url: str = "") -> list[str] | str:
    """The user's command with {ficheiro} {pasta} {nome} {url} filled in (each one quoted, so
    names with spaces or symbols are passed as one argument and never run as commands)."""
    values = {"ficheiro": path, "pasta": os.path.dirname(path), "nome": os.path.basename(path), "url": url}
    if os.name == "nt":
        filled = template
        for k, v in values.items():
            filled = filled.replace("{" + k + "}", subprocess.list2cmdline([v]))
        return filled
    parts = shlex.split(template)
    return [p.format_map(_Keep(values)) for p in parts]


class _Keep(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def run_command(template: str, path: str, url: str = "") -> str:
    try:
        subprocess.Popen(command_for(template, path, url), creationflags=_NO_WINDOW)
    except (OSError, ValueError) as exc:
        return f"o comando falhou ({exc})"
    return ""


# ------------------------------------------------------------------------ all of it
def run(task, settings) -> list[str]:
    """Everything chosen for a finished download, in order. Returns notes for the user."""
    path = task.path
    if not path or not os.path.isfile(path):
        return []
    notes = []
    if getattr(task, "expected_hash", None):
        ok, msg = verify(path, task.expected_hash)
        notes.append(msg)
        if not ok:
            return notes  # do not open / extract / run anything on a file that is not the right one
    if getattr(settings, "after_scan", False):
        verdict = scan(path)
        notes.append(verdict)
        if verdict.startswith("⚠"):
            return notes
    if getattr(settings, "after_extract", False) and path.lower().endswith(".zip"):
        msg = extract_zip(path)
        if msg:
            notes.append(msg)
    if getattr(settings, "after_command", ""):
        msg = run_command(settings.after_command, path, getattr(task, "url", ""))
        if msg:
            notes.append(msg)
    if getattr(settings, "after_open", False):
        try:
            open_file(path)
        except OSError:
            pass
    return notes
