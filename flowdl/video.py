"""Video/audio downloads from sites like YouTube, using the optional yt-dlp library.

Install with:  pip install yt-dlp
For resolutions above 360p on YouTube (separate video + audio streams) and for
MP3 conversion, ffmpeg is needed: the app installs it on request (install_ffmpeg).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque

from .categories import category_for
from . import vault
from .engine import Status, sanitize_filename

yt_dlp = None  # optional dependency, imported only when a video is requested (it costs ~25 MB)


# Video sites change often and yt-dlp follows every week. The app can fetch the newest
# yt-dlp from its official releases (checked against the SHA-256 the project publishes) and
# use it instead of the one it was installed with: no reinstall needed.
UPDATE_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
SUMS_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS"


def update_path():
    from .paths import config_dir
    return config_dir() / "yt-dlp.zip"


class _UpdatedFirst:
    """Import finder that looks for yt_dlp in the downloaded update before anywhere else.
    (In the installed .exe the bundled copy would otherwise always win, whatever sys.path says.)"""

    @staticmethod
    def find_spec(name, path=None, target=None):
        if name != "yt_dlp" and not name.startswith("yt_dlp."):
            return None
        import importlib.machinery
        where = str(update_path())
        if not os.path.isfile(where):
            return None
        return importlib.machinery.PathFinder.find_spec(name, path if path is not None else [where])


def _use_update() -> None:
    """A downloaded newer yt-dlp is used before the one the app was installed with."""
    if os.path.isfile(str(update_path())) and not any(isinstance(f, _UpdatedFirst) or f is _UpdatedFirst
                                                       for f in sys.meta_path):
        sys.meta_path.insert(0, _UpdatedFirst)


def available() -> bool:
    if yt_dlp is not None:
        return True
    _use_update()
    import importlib.util
    importlib.invalidate_caches()
    return importlib.util.find_spec("yt_dlp") is not None


def _lib():
    global yt_dlp
    if yt_dlp is None:
        _use_update()
        import yt_dlp as module  # type: ignore
        yt_dlp = module
    return yt_dlp


def _version_key(v: str | None) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", v or ""))


def current_version() -> str | None:
    if not available():
        return None
    try:
        return _lib().version.__version__
    except Exception:  # noqa: BLE001
        return None


def _fetch(url: str, timeout: float = 60) -> bytes:
    """(Through the app's proxy, like the downloads.)"""
    from . import net
    return net.fetch(url, 64 * 1024 * 1024, timeout=timeout)[2]


def update_yt_dlp() -> tuple[bool, str]:
    """Fetch the newest yt-dlp if it is newer than ours. Returns (ok, message)."""
    import hashlib
    import http.client
    import io
    import zipfile
    try:
        sums = _fetch(SUMS_URL, 30).decode("utf-8", "replace")
        want = next((line.split()[0].lower() for line in sums.splitlines()
                     if line.split()[1:] == ["yt-dlp"]), None)
        if not want:
            return False, "não encontrei o checksum oficial do yt-dlp"
        data = _fetch(UPDATE_URL)
    except (OSError, http.client.HTTPException) as exc:
        return False, f"sem ligação ao GitHub ({exc})"
    if hashlib.sha256(data).hexdigest() != want:
        return False, "o ficheiro descarregado não confere com o checksum oficial: não foi usado"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            text = z.read("yt_dlp/version.py").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError):
        return False, "o ficheiro do yt-dlp não é o esperado: não foi usado"
    m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", text)
    new = m.group(1) if m else ""
    have = current_version()
    if have and _version_key(have) >= _version_key(new):
        return True, f"o suporte a vídeos já está na versão mais recente ({have})"
    path = update_path()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError as exc:
        return False, f"não consegui gravar a atualização ({exc})"
    _reload()
    return True, f"suporte a vídeos atualizado para a versão {new}"


def _reload() -> None:
    """Use the new yt-dlp from now on (drops the one already loaded)."""
    global yt_dlp
    for name in [n for n in sys.modules if n == "yt_dlp" or n.startswith("yt_dlp.")]:
        del sys.modules[name]
    yt_dlp = None
    _use_update()


# ffmpeg (for 1080p and more on YouTube, and MP3): installed by the app on request, in its own
# folder (no administrator, no change to the system PATH), from the official Windows builds
# of the FFmpeg-Builds project, checked against the SHA-256 published with them.
FFMPEG_BUILD = "ffmpeg-master-latest-win64-gpl.zip"
FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/" + FFMPEG_BUILD
FFMPEG_SUMS_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/checksums.sha256"
FFMPEG_MAX = 400 * 1024 * 1024  # (the build is ~200 MB: anything far bigger is not it)
FFMPEG_EXPECTED = 200 * 1024 * 1024  # (for the progress shown while it downloads)
FFMPEG_PROGRAMS = ("ffmpeg.exe", "ffprobe.exe")
WINDOWS = os.name == "nt"  # (the builds are for Windows)


def ffmpeg_home():
    from .paths import config_dir
    return config_dir() / "ffmpeg"


def _app_ffmpeg() -> str | None:
    """The app's own copy (installed with install_ffmpeg), if any."""
    path = ffmpeg_home() / "ffmpeg.exe"
    return str(path) if WINDOWS and path.is_file() else None


def ffmpeg_dir() -> str | None:
    """Folder of the ffmpeg in use: the app's own copy first, then the system's."""
    own = _app_ffmpeg()
    if own:
        return os.path.dirname(own)
    found = shutil.which("ffmpeg")
    return os.path.dirname(found) if found else None


def has_ffmpeg() -> bool:
    return ffmpeg_dir() is not None


def ffmpeg_state() -> str:
    if _app_ffmpeg():
        return "instalado pela app"
    return "encontrado no sistema" if shutil.which("ffmpeg") else "não instalado"


def install_ffmpeg(progress=None) -> tuple[bool, str]:
    """Download ffmpeg + ffprobe into the app's folder. ``progress(done, total)`` is called
    while downloading. Returns (ok, message). Nothing is kept unless the checksum matches."""
    import hashlib
    import http.client
    import tempfile
    import zipfile
    from .system import free_space
    if not WINDOWS:
        return False, "a instalação automática do ffmpeg só existe no Windows"
    home = ffmpeg_home()
    home.mkdir(parents=True, exist_ok=True)
    room = free_space(str(home))
    if room is not None and room < 600 * 1024 * 1024:  # (the archive + the two programs)
        return False, "pouco espaço livre no disco para instalar o ffmpeg (são precisos ~600 MB)"
    try:
        sums = _fetch(FFMPEG_SUMS_URL, 30).decode("utf-8", "replace")
    except (OSError, http.client.HTTPException) as exc:
        return False, f"sem ligação ao GitHub ({exc})"
    want = next((line.split()[0].lower() for line in sums.splitlines()
                 if line.split()[1:] == [FFMPEG_BUILD]), None)
    if not want or not re.fullmatch(r"[0-9a-f]{64}", want):
        return False, "não encontrei o checksum oficial do ffmpeg: não foi instalado"
    fd, zip_path = tempfile.mkstemp(suffix=".zip", dir=str(home))
    out = os.fdopen(fd, "wb")  # (owned at once: closed even when the download cannot start)
    try:
        digest = hashlib.sha256()
        from . import net
        done = [0]

        def piece(chunk: bytes) -> None:
            done[0] += len(chunk)
            digest.update(chunk)
            out.write(chunk)
            if progress:
                progress(done[0], FFMPEG_EXPECTED)
        try:
            with out:  # (through the app's proxy, like the downloads)
                net.fetch(FFMPEG_URL, FFMPEG_MAX, timeout=60, on_chunk=piece)
        except ValueError:
            return False, "o ficheiro do ffmpeg é maior do que o esperado: não foi instalado"
        except (OSError, http.client.HTTPException) as exc:
            return False, f"o download do ffmpeg falhou ({exc})"
        if digest.hexdigest() != want:
            return False, "o ficheiro descarregado não confere com o checksum oficial: não foi instalado"
        try:
            with zipfile.ZipFile(zip_path) as z:
                # Only the two programs, by exact place (<build>/bin/<name>): never any other
                # path from the archive.
                members = {}
                for info in z.infolist():
                    parts = info.filename.split("/")
                    if len(parts) == 3 and parts[1] == "bin" and parts[2] in FFMPEG_PROGRAMS \
                            and ".." not in parts[0] and "\\" not in info.filename:
                        members[parts[2]] = info
                if set(members) != set(FFMPEG_PROGRAMS):
                    return False, "o ficheiro do ffmpeg não é o esperado: não foi instalado"
                for name, info in members.items():
                    tmp = home / (name + ".tmp")
                    with z.open(info) as src, open(tmp, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    tmp.replace(home / name)
        except (zipfile.BadZipFile, OSError) as exc:
            return False, f"não consegui extrair o ffmpeg ({exc})"
    finally:
        out.close()
        try:
            os.remove(zip_path)
        except OSError:
            pass
    version = ffmpeg_version()
    if not version:
        remove_ffmpeg()
        return False, "o ffmpeg descarregado não arrancou: foi removido"
    return True, f"ffmpeg instalado ({version})"


def ffmpeg_version() -> str | None:
    """First line of `ffmpeg -version` (also proves that it runs)."""
    folder = ffmpeg_dir()
    if not folder:
        return None
    exe = os.path.join(folder, "ffmpeg.exe" if WINDOWS else "ffmpeg")
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    line = out.stdout.decode("utf-8", "replace").splitlines()[:1]
    return line[0][:80] if out.returncode == 0 and line else None


def remove_ffmpeg() -> None:
    for name in FFMPEG_PROGRAMS:
        try:
            (ffmpeg_home() / name).unlink()
        except OSError:
            pass


def install_command() -> list[str]:
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):  # pip prefers the console interpreter
        exe = exe[:-5] + ".exe"
    return [exe, "-m", "pip", "install", "--user", "-U", "yt-dlp"]


def install_yt_dlp() -> tuple[bool, str]:
    """Install/update yt-dlp with pip. Returns (ok, output)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(install_command(), capture_output=True, text=True, creationflags=flags)
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]


def write_cookiefile(cookies: list[dict], path: str) -> str | None:
    """Write browser cookies (chrome.cookies format) as a Netscape cookies.txt file."""
    if not cookies:
        return None
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        domain = c.get("domain", "")
        host_only = c.get("hostOnly", False)
        if not host_only and not domain.startswith("."):
            domain = "." + domain
        lines.append("\t".join([
            domain,
            "FALSE" if host_only else "TRUE",
            c.get("path", "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(int(c.get("expirationDate", 0) or 0)),
            c.get("name", ""),
            c.get("value", ""),
        ]))
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


class temp_cookiefile:
    """A cookies.txt that exists only while yt-dlp needs it (readable by this user only)."""

    def __init__(self, jar: list | None, legacy_path: str | None = None):
        self.jar, self.legacy_path, self.path = jar, legacy_path, None

    def __enter__(self) -> str | None:
        if not self.jar:
            return self.legacy_path if self.legacy_path and os.path.exists(self.legacy_path) else None
        import tempfile
        fd, self.path = tempfile.mkstemp(prefix="flowdl-", suffix=".txt")  # created with mode 0600
        os.close(fd)
        return write_cookiefile(self.jar, self.path)

    def __exit__(self, *exc) -> None:
        if self.path:
            try:
                os.remove(self.path)
            except OSError:
                pass


def _base_opts(cookiefile: str | None = None, headers: dict | None = None) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True}
    if cookiefile and os.path.exists(cookiefile):
        opts["cookiefile"] = cookiefile
    if headers:
        opts["http_headers"] = {k: v for k, v in headers.items() if k.lower() != "cookie"}
    from . import proxy
    if proxy.current()["mode"] == "none":
        opts["proxy"] = ""  # (straight, even if the system has one)
    elif proxy.url_for_tools():
        opts["proxy"] = proxy.url_for_tools()  # the proxy chosen in the app (SOCKS5 / HTTP)
    own = _app_ffmpeg()
    if own:
        opts["ffmpeg_location"] = os.path.dirname(own)  # (the app's copy is not on the PATH)
    return opts


def get_options(url: str, cookiefile: str | None = None, headers: dict | None = None,
                cookie_jar: list | None = None) -> dict:
    """Ask yt-dlp which qualities exist. Returns {title, options: [{label, format, audio_only}]}."""
    if not available():
        raise RuntimeError("yt-dlp não está instalado")
    with temp_cookiefile(cookie_jar, cookiefile) as path, _lib().YoutubeDL(_base_opts(path, headers)) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist" and info.get("entries"):
        info = next(e for e in info["entries"] if e)
    return build_options(info, has_ffmpeg(), url)


def build_options(info: dict, ffmpeg: bool, url: str = "") -> dict:
    """Turn yt-dlp's format list into simple choices: 1080p, 720p, ..., audio only."""
    formats = info.get("formats") or [info]

    def height(f):
        return f.get("height") or 0

    def has(f, key):
        return f.get(key) not in (None, "none")

    with_video = [f for f in formats if has(f, "vcodec") or height(f)]
    progressive = [f for f in with_video if has(f, "acodec")]
    usable = with_video if ffmpeg else progressive
    heights = sorted({height(f) for f in usable if height(f)}, reverse=True)

    options = []
    for h in heights:
        fmt = f"bv*[height<={h}]+ba/b[height<={h}]" if ffmpeg else f"b[height<={h}]"
        options.append({"label": f"Vídeo {h}p", "format": fmt, "audio_only": False})
    if not options:
        options.append({"label": "Melhor qualidade", "format": "bv*+ba/b" if ffmpeg else "b",
                        "audio_only": False})
    if ffmpeg:  # ffmpeg can extract the audio track from any video
        options.append({"label": "Só áudio (MP3)", "format": "ba/b", "audio_only": True})
    elif any(has(f, "acodec") and not has(f, "vcodec") for f in formats):
        options.append({"label": "Só áudio (M4A)", "format": "ba", "audio_only": True})
    return {
        "title": info.get("title") or "video",
        "url": info.get("webpage_url") or url,
        "duration": info.get("duration"),
        "options": options,
        "limited": not ffmpeg and len(progressive) < len(with_video),
    }


class _Paused(Exception):
    pass


_NAME_LOCK = threading.Lock()


class VideoTask:
    """Same interface as engine.DownloadTask, but downloads through yt-dlp."""

    kind = "video"

    def __init__(self, url: str, dest_dir: str, fmt: str = "bv*+ba/b", audio_only: bool = False,
                 title: str | None = None, connections: int = 8, task_id: str | None = None,
                 headers: dict | None = None, cookiefile: str | None = None, speed_limit: int = 0,
                 quality: str = "", cookie_jar: list | None = None):
        self.id = task_id or "v" + uuid.uuid4().hex[:15]
        self.url = url
        self.dest_dir = dest_dir
        self.format = fmt
        self.audio_only = audio_only
        self.quality = quality
        self.title = title
        self.connections = max(1, int(connections))
        self.headers = dict(headers or {})
        self.cookiefile = cookiefile  # old versions kept a cookies.txt; now cookie_jar instead
        self.cookie_jar = list(cookie_jar or [])  # browser cookies, protected on disk (vault)
        self._speed_limit = int(speed_limit)
        self.filename: str | None = sanitize_filename(title) if title else None
        self.path: str | None = None
        self.size: int | None = None
        self.downloaded = 0
        self.speed = 0.0
        self.status = Status.QUEUED
        self.error: str | None = None
        self.resumable = True
        self.added_at = time.time()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._streams: dict[str, tuple[int, int]] = {}
        self._eta: float | None = None
        self._samples: deque = deque()
        self.on_done = None

    # ------------------------------------------------------------- interface
    @property
    def progress(self) -> float:
        if self.status == Status.COMPLETED:
            return 1.0
        return min(1.0, self.downloaded / self.size) if self.size else 0.0

    @property
    def eta(self) -> float | None:
        return self._eta

    @property
    def active_connections(self) -> int:
        return 1 if self.is_running() and self.status == Status.DOWNLOADING else 0

    @property
    def category(self) -> str:
        if self.status == Status.COMPLETED and self.filename:
            return category_for(self.filename)
        return "Músicas" if self.audio_only else "Vídeos"

    @property
    def speed_limit(self) -> int:
        return self._speed_limit

    @speed_limit.setter
    def speed_limit(self, value: int) -> None:
        self._speed_limit = max(0, int(value))  # applied on next start/resume

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running() or self.status == Status.COMPLETED:
            return
        if not available():
            self.status, self.error = Status.ERROR, "instale o yt-dlp (pip install yt-dlp)"
            return
        self._stop.clear()
        self.error = None
        self.status = Status.CONNECTING
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def pause(self) -> None:
        if self.is_running():
            self._stop.set()
        elif self.status in (Status.QUEUED, Status.CONNECTING):
            self.status = Status.PAUSED

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "id": self.id, "url": self.url, "dest_dir": self.dest_dir,
            "format": self.format, "audio_only": self.audio_only, "title": self.title,
            "connections": self.connections, "headers": self.headers, "cookiefile": self.cookiefile,
            "cookie_jar": vault.protect_cached(self.cookie_jar),
            "speed_limit": self.speed_limit, "quality": self.quality, "filename": self.filename,
            "path": self.path, "size": self.size, "downloaded": self.downloaded,
            "status": self.status, "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "VideoTask":
        from .engine import clean_record
        d = clean_record(d)
        t = cls(d["url"], d["dest_dir"], d.get("format", "bv*+ba/b"), d.get("audio_only", False),
                d.get("title"), d.get("connections", 8), d.get("id"), d.get("headers"),
                d.get("cookiefile"), d.get("speed_limit", 0), d.get("quality", ""),
                vault.unprotect(d.get("cookie_jar"), []))
        t.filename, t.path = d.get("filename") or t.filename, d.get("path")
        t.size, t.downloaded = d.get("size"), d.get("downloaded", 0)
        t.added_at = d.get("added_at", t.added_at)
        status = d.get("status")
        t.status = status if status in (Status.COMPLETED, Status.ERROR) else Status.PAUSED
        return t

    # -------------------------------------------------------------- internals
    def _hook(self, d: dict) -> None:
        if self._stop.is_set():
            raise _Paused()
        if d.get("status") not in ("downloading", "finished"):
            return
        self.status = Status.DOWNLOADING
        name = d.get("filename") or d.get("tmpfilename") or "?"
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        if d["status"] == "finished":
            total = total or done
            done = total
        self._streams[name] = (int(done), int(total))
        self.downloaded = sum(s[0] for s in self._streams.values())
        self.size = sum(s[1] for s in self._streams.values()) or None
        self.speed = float(d.get("speed") or 0)
        self._eta = d.get("eta")
        if name != "?":
            # "Title.f137.mp4" -> "Title.mp4" (yt-dlp names each stream with its format id)
            self.filename = re.sub(r"\.f\d+(?=\.\w+$)", "", os.path.basename(name))

    _reserved: set = set()  # names taken by videos being downloaded right now

    def _name_template(self) -> str:
        if self.title:  # title shown in the browser tab / chosen by the user
            first = sanitize_filename(self.title)[:150]
            base = getattr(self, "_reserved_name", None)
            if base is None and self.filename and self.filename != sanitize_filename(self.title):
                # resuming (the real name, with its extension, came from an earlier run): the
                # same name, so it continues instead of starting over
                base = os.path.splitext(self.filename)[0]
            if base is None:
                base = self._free_name(first)
            return base.replace("%", "%%") + ".%(ext)s"
        return "%(title).150B.%(ext)s"

    def _free_name(self, base: str) -> str:
        """The same video in another quality, or two streams with the same tab title: a new
        name ("Title (2)"). Before, yt-dlp said "already downloaded" and the download was
        marked finished pointing at the OTHER file."""
        import glob
        name, n = base, 1
        with _NAME_LOCK:
            while (name in VideoTask._reserved
                   or glob.glob(os.path.join(glob.escape(self.dest_dir), glob.escape(name) + ".*"))):
                n += 1
                name = f"{base} ({n})"
            VideoTask._reserved.add(name)
            self._reserved_name = name
        return name

    def _run(self) -> None:
        os.makedirs(self.dest_dir, exist_ok=True)
        self._cookies = temp_cookiefile(self.cookie_jar, self.cookiefile)
        opts = _base_opts(self._cookies.__enter__(), self.headers)
        opts.update({
            "format": self.format,
            "outtmpl": os.path.join(self.dest_dir, self._name_template()),
            "progress_hooks": [self._hook],
            "continuedl": True,
            "concurrent_fragment_downloads": self.connections,
            "retries": 10,
            "fragment_retries": 10,
            "windowsfilenames": True,
        })
        if self.speed_limit:
            opts["ratelimit"] = self.speed_limit
        if has_ffmpeg():
            if self.audio_only:
                opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                           "preferredquality": "192"}]
            else:
                opts["merge_output_format"] = "mp4"
        try:
            with _lib().YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=True)
            if info.get("_type") == "playlist" and info.get("entries"):
                info = next(e for e in info["entries"] if e)
            downloads = info.get("requested_downloads") or []
            path = downloads[0].get("filepath") if downloads else None
            path = path or info.get("filepath") or info.get("_filename")
            if path and os.path.exists(path):
                self.path = path
                self.filename = os.path.basename(path)
                self.size = os.path.getsize(path)
            self.downloaded = self.size or self.downloaded
            self.status = Status.COMPLETED
        except Exception as exc:  # noqa: BLE001 - yt-dlp raises many types
            if self._stop.is_set():
                self.status = Status.PAUSED
            else:
                msg = str(exc).replace("ERROR: ", "").strip()
                self.error = msg.splitlines()[0][:200] if msg else exc.__class__.__name__
                self.status = Status.ERROR
        finally:
            self._cookies.__exit__(None, None, None)
            self.speed = 0.0
            self._eta = None
            if self.on_done:
                self.on_done(self)
