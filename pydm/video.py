"""Video/audio downloads from sites like YouTube, using the optional yt-dlp library.

Install with:  pip install yt-dlp
For resolutions above 360p on YouTube (separate video + audio streams) and for
MP3 conversion, ffmpeg must also be installed (Windows: winget install ffmpeg).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque

from .categories import category_for
from .engine import Status, sanitize_filename

try:  # optional dependency
    import yt_dlp  # type: ignore
except ImportError:  # pragma: no cover - depends on the machine
    yt_dlp = None


def available() -> bool:
    return yt_dlp is not None


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _base_opts(cookiefile: str | None = None, headers: dict | None = None) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True}
    if cookiefile and os.path.exists(cookiefile):
        opts["cookiefile"] = cookiefile
    if headers:
        opts["http_headers"] = {k: v for k, v in headers.items() if k.lower() != "cookie"}
    return opts


def get_options(url: str, cookiefile: str | None = None, headers: dict | None = None) -> dict:
    """Ask yt-dlp which qualities exist. Returns {title, options: [{label, format, audio_only}]}."""
    if not available():
        raise RuntimeError("yt-dlp não está instalado")
    with yt_dlp.YoutubeDL(_base_opts(cookiefile, headers)) as ydl:
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


class VideoTask:
    """Same interface as engine.DownloadTask, but downloads through yt-dlp."""

    kind = "video"

    def __init__(self, url: str, dest_dir: str, fmt: str = "bv*+ba/b", audio_only: bool = False,
                 title: str | None = None, connections: int = 8, task_id: str | None = None,
                 headers: dict | None = None, cookiefile: str | None = None, speed_limit: int = 0,
                 quality: str = ""):
        self.id = task_id or f"v{time.time_ns():x}"
        self.url = url
        self.dest_dir = dest_dir
        self.format = fmt
        self.audio_only = audio_only
        self.quality = quality
        self.title = title
        self.connections = max(1, int(connections))
        self.headers = dict(headers or {})
        self.cookiefile = cookiefile
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
            "speed_limit": self.speed_limit, "quality": self.quality, "filename": self.filename,
            "path": self.path, "size": self.size, "downloaded": self.downloaded,
            "status": self.status, "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "VideoTask":
        t = cls(d["url"], d["dest_dir"], d.get("format", "bv*+ba/b"), d.get("audio_only", False),
                d.get("title"), d.get("connections", 8), d.get("id"), d.get("headers"),
                d.get("cookiefile"), d.get("speed_limit", 0), d.get("quality", ""))
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

    def _name_template(self) -> str:
        if self.title:  # title shown in the browser tab / chosen by the user
            return sanitize_filename(self.title)[:150].replace("%", "%%") + ".%(ext)s"
        return "%(title).150B.%(ext)s"

    def _run(self) -> None:
        os.makedirs(self.dest_dir, exist_ok=True)
        opts = _base_opts(self.cookiefile, self.headers)
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
            with yt_dlp.YoutubeDL(opts) as ydl:
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
            self.speed = 0.0
            self._eta = None
