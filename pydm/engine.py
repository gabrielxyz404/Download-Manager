"""Download engine: segmented (multi-connection) downloads with pause/resume.

How it works (same idea used by IDM):
  1. Probe the URL to learn its size, file name and whether the server
     accepts HTTP Range requests.
  2. If ranges are supported, split the file into N segments and download
     each segment on its own connection, writing directly into the right
     offset of a pre-allocated ``.part`` file.
  3. When a connection finishes its segment, it "steals" half of the largest
     remaining segment (dynamic segmentation), so all connections stay busy
     until the very end.
  4. Progress is saved to a ``.part.json`` file, so a paused / interrupted
     download can be resumed later, even after closing the app.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from email.message import Message

from .categories import category_for
from .limiter import GLOBAL_LIMITER, RateLimiter
from .status import Status  # noqa: F401 (re-exported)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
CHUNK_SIZE = 64 * 1024
MIN_SPLIT_SIZE = 512 * 1024  # do not split segments smaller than 2x this
MAX_RETRIES = 8
TIMEOUT = 30


class Segment:
    """Byte range [start, end] (inclusive). ``pos`` is the next byte to write."""

    def __init__(self, start: int, end: int, pos: int | None = None):
        self.start = start
        self.end = end
        self.pos = start if pos is None else pos
        self.lock = threading.Lock()
        self.active = False

    @property
    def remaining(self) -> int:
        return max(0, self.end - self.pos + 1)

    def to_list(self) -> list[int]:
        return [self.start, self.end, self.pos]


def _filename_from_headers(headers: Message, url: str) -> str:
    cd = headers.get("Content-Disposition", "")
    name = None
    m = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", cd, re.I)
    if m:
        name = urllib.parse.unquote(m.group(1).strip().strip('"'))
    else:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
        if m:
            name = m.group(1).strip()
    if not name:
        path = urllib.parse.urlparse(url).path
        name = urllib.parse.unquote(os.path.basename(path)) or "download"
    return sanitize_filename(name)


def sanitize_filename(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "download"


def unique_path(path: str) -> str:
    """Return ``path`` or ``name (1).ext`` style variant that does not exist yet."""
    if not os.path.exists(path) and not os.path.exists(path + ".part"):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while True:
        candidate = f"{base} ({i}){ext}"
        if not os.path.exists(candidate) and not os.path.exists(candidate + ".part"):
            return candidate
        i += 1


def _request(url: str, start: int | None = None, end: int | None = None,
             headers: dict | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    if start is not None:
        req.add_header("Range", f"bytes={start}-{'' if end is None else end}")
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def probe(url: str, headers: dict | None = None) -> dict:
    """Find out size, final URL, file name and range support for ``url``."""
    # A 1-byte range GET is more reliable than HEAD (many servers mishandle HEAD).
    with _request(url, 0, 0, headers) as resp:
        headers = resp.headers
        final_url = resp.geturl()
        size = None
        resumable = False
        if resp.status == 206:
            m = re.search(r"/(\d+)\s*$", headers.get("Content-Range", ""))
            if m:
                size = int(m.group(1))
                resumable = True
        elif headers.get("Content-Length"):
            size = int(headers["Content-Length"])
        return {
            "url": final_url,
            "size": size,
            "resumable": resumable,
            "filename": _filename_from_headers(headers, final_url),
        }


class DownloadTask:
    """A single download. Thread-safe to call start()/pause() from a GUI."""

    def __init__(self, url: str, dest_dir: str, connections: int = 8,
                 filename: str | None = None, task_id: str | None = None,
                 headers: dict | None = None, speed_limit: int = 0):
        self.id = task_id or f"{time.time_ns():x}"
        self.url = url
        self.dest_dir = dest_dir
        self.connections = max(1, int(connections))
        self.filename = filename
        self.path: str | None = None
        self.size: int | None = None
        self.resumable = False
        self.segments: list[Segment] = []
        self.status = Status.QUEUED
        self.error: str | None = None
        self.downloaded = 0
        self.speed = 0.0
        self.added_at = time.time()
        self.headers = dict(headers or {})  # e.g. Referer / Cookie sent by the browser
        self.limiter = RateLimiter(speed_limit)

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._samples: deque[tuple[float, int]] = deque()
        self._active_workers = 0
        self._worker_error: str | None = None
        self.on_done = None  # called (from the download thread) when it stops for any reason

    # ------------------------------------------------------------------ public
    @property
    def progress(self) -> float:
        if not self.size:
            return 1.0 if self.status == Status.COMPLETED else 0.0
        return min(1.0, self.downloaded / self.size)

    @property
    def eta(self) -> float | None:
        if not self.size or self.speed <= 0:
            return None
        return (self.size - self.downloaded) / self.speed

    @property
    def speed_limit(self) -> int:
        return int(self.limiter.rate)

    @speed_limit.setter
    def speed_limit(self, value: int) -> None:
        self.limiter.rate = max(0, int(value))

    @property
    def category(self) -> str:
        return category_for(self.filename or self.url)

    @property
    def active_connections(self) -> int:
        return self._active_workers

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running() or self.status == Status.COMPLETED:
            return
        self._stop.clear()
        self.error = None
        self._worker_error = None
        self.status = Status.CONNECTING
        self._thread = threading.Thread(target=self._run, name=f"dl-{self.id}", daemon=True)
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
            "id": self.id, "url": self.url, "dest_dir": self.dest_dir,
            "connections": self.connections, "filename": self.filename,
            "path": self.path, "size": self.size, "status": self.status,
            "downloaded": self.downloaded, "added_at": self.added_at,
            "headers": self.headers, "speed_limit": self.speed_limit,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DownloadTask":
        t = cls(d["url"], d["dest_dir"], d.get("connections", 8), d.get("filename"), d.get("id"),
                d.get("headers"), d.get("speed_limit", 0))
        t.path = d.get("path")
        t.size = d.get("size")
        t.downloaded = d.get("downloaded", 0)
        t.added_at = d.get("added_at", time.time())
        status = d.get("status", Status.PAUSED)
        if status == Status.COMPLETED:
            t.status = Status.COMPLETED
        elif status == Status.ERROR:
            t.status = Status.ERROR
        else:
            t.status = Status.PAUSED  # anything in-flight becomes paused on reload
        if t.path and status != Status.COMPLETED:
            t._load_state()
        return t

    # ---------------------------------------------------------------- internals
    @property
    def _part_path(self) -> str:
        return self.path + ".part"

    @property
    def _state_path(self) -> str:
        return self.path + ".part.json"

    def _load_state(self) -> bool:
        try:
            with open(self._state_path, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            return False
        if not os.path.exists(self._part_path):
            return False
        self.size = state.get("size")
        self.resumable = state.get("resumable", False)
        self.segments = [Segment(*s) for s in state.get("segments", [])]
        self.downloaded = sum(s.pos - s.start for s in self.segments)
        return True

    def _state_matches(self, path: str) -> bool:
        try:
            with open(path + ".part.json", encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            return False
        return state.get("url") == self.url and os.path.exists(path + ".part")

    def _save_state(self) -> None:
        if not self.path or not self.resumable or self.status == Status.COMPLETED:
            return
        state = {
            "url": self.url, "size": self.size, "resumable": self.resumable,
            "segments": [s.to_list() for s in self.segments],
        }
        tmp = self._state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, self._state_path)
        except OSError:
            pass

    def _run(self) -> None:
        try:
            self._prepare()
            if self._stop.is_set():
                self.status = Status.PAUSED
                return
            self.status = Status.DOWNLOADING
            if self.resumable and self.size:
                self._download_segmented()
            else:
                self._download_single()
        except Exception as exc:  # noqa: BLE001 - report any failure to the UI
            self.error = str(exc) or exc.__class__.__name__
            self.status = Status.ERROR
        finally:
            self.speed = 0.0
            self._save_state()
            if self.on_done:
                self.on_done(self)

    def _prepare(self) -> None:
        os.makedirs(self.dest_dir, exist_ok=True)
        if self.path and self.segments and os.path.exists(self._part_path):
            return  # resuming from saved state
        info = probe(self.url, self.headers)
        self.url = info["url"]
        self.size = info["size"]
        self.resumable = info["resumable"]
        if not self.filename:
            self.filename = info["filename"]
        if not self.path:
            candidate = os.path.join(self.dest_dir, self.filename)
            if self._state_matches(candidate):
                self.path = candidate  # continue an interrupted download of the same URL
            else:
                self.path = unique_path(candidate)
            self.filename = os.path.basename(self.path)
        if self._load_state() and self.size == info["size"]:
            return
        self.segments = []
        self.downloaded = 0

    # -- single connection (server does not support ranges / unknown size)
    def _download_single(self) -> None:
        self.downloaded = 0
        self._active_workers = 1
        try:
            with _request(self.url, headers=self.headers) as resp, open(self._part_path, "wb") as f:
                while not self._stop.is_set():
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    self.downloaded += len(chunk)
                    self._update_speed()
                    self._throttle(len(chunk))
        finally:
            self._active_workers = 0
        if self._stop.is_set():
            # Can't resume without range support: start over next time.
            self.status = Status.PAUSED
            self.downloaded = 0
            return
        if self.size is None:
            self.size = self.downloaded
        self._finish()

    # -- multiple connections
    def _download_segmented(self) -> None:
        if not self.segments:
            self._create_segments()
        if not os.path.exists(self._part_path):
            with open(self._part_path, "wb") as f:
                f.truncate(self.size)

        workers: list[threading.Thread] = []
        last_save = time.monotonic()

        def spawn(seg: Segment) -> None:
            seg.active = True
            with self._lock:
                self._active_workers += 1
            t = threading.Thread(target=self._worker, args=(seg,), daemon=True)
            workers.append(t)
            t.start()

        for seg in self.segments:
            if seg.remaining > 0:
                spawn(seg)

        while True:
            time.sleep(0.5)
            self.downloaded = sum(s.pos - s.start for s in self.segments)
            self._update_speed()
            if self._worker_error:
                self._stop.set()
            # Dynamic segmentation: keep `connections` workers busy.
            if not self._stop.is_set():
                while self._active_workers < self.connections:
                    new_seg = self._split_largest()
                    if new_seg is None:
                        break
                    spawn(new_seg)
            if time.monotonic() - last_save > 1.0:
                self._save_state()
                last_save = time.monotonic()
            if not any(t.is_alive() for t in workers):
                break

        self.downloaded = sum(s.pos - s.start for s in self.segments)
        self._save_state()
        if self._worker_error:
            raise RuntimeError(self._worker_error)
        if all(s.remaining == 0 for s in self.segments):
            self._finish()
        else:
            self.status = Status.PAUSED

    def _create_segments(self) -> None:
        n = max(1, min(self.connections, self.size // MIN_SPLIT_SIZE or 1))
        step = self.size // n
        self.segments = []
        for i in range(n):
            start = i * step
            end = self.size - 1 if i == n - 1 else (i + 1) * step - 1
            self.segments.append(Segment(start, end))

    def _split_largest(self) -> Segment | None:
        with self._lock:
            candidates = [s for s in self.segments if s.remaining >= 2 * MIN_SPLIT_SIZE]
            # Idle segments (left over from a previous session) are picked up as-is.
            idle = [s for s in self.segments if not s.active and s.remaining > 0]
            if idle:
                return idle[0]
            if not candidates:
                return None
            seg = max(candidates, key=lambda s: s.remaining)
            with seg.lock:
                if seg.remaining < 2 * MIN_SPLIT_SIZE:
                    return None
                mid = seg.pos + seg.remaining // 2
                new = Segment(mid, seg.end)
                seg.end = mid - 1
            self.segments.append(new)
            self.segments.sort(key=lambda s: s.start)
            return new

    def _worker(self, seg: Segment) -> None:
        retries = 0
        try:
            with open(self._part_path, "r+b") as f:
                while not self._stop.is_set() and seg.remaining > 0:
                    try:
                        with _request(self.url, seg.pos, seg.end, self.headers) as resp:
                            if resp.status != 206:
                                raise RuntimeError("O servidor parou de aceitar downloads em partes")
                            while not self._stop.is_set():
                                chunk = resp.read(CHUNK_SIZE)
                                if not chunk:
                                    break
                                with seg.lock:
                                    chunk = chunk[: seg.remaining]
                                    if chunk:
                                        f.seek(seg.pos)
                                        f.write(chunk)
                                        seg.pos += len(chunk)
                                    if seg.remaining == 0:
                                        break
                                retries = 0
                                self._throttle(len(chunk))
                    except (urllib.error.URLError, OSError, TimeoutError) as exc:
                        if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403, 404, 410):
                            raise RuntimeError(f"HTTP {exc.code}: {exc.reason}") from exc
                        retries += 1
                        if retries > MAX_RETRIES:
                            raise RuntimeError(f"Falha na conexão: {exc}") from exc
                        self._stop.wait(min(2 ** retries, 30))
        except Exception as exc:  # noqa: BLE001
            self._worker_error = str(exc)
        finally:
            seg.active = False
            with self._lock:
                self._active_workers -= 1

    def _throttle(self, nbytes: int) -> None:
        delay = max(GLOBAL_LIMITER.reserve(nbytes), self.limiter.reserve(nbytes))
        if delay > 0:
            self._stop.wait(delay)

    def _update_speed(self) -> None:
        now = time.monotonic()
        self._samples.append((now, self.downloaded))
        while self._samples and now - self._samples[0][0] > 3.0:
            self._samples.popleft()
        t0, b0 = self._samples[0]
        self.speed = (self.downloaded - b0) / (now - t0) if now > t0 else 0.0

    def _finish(self) -> None:
        final = self.path
        if os.path.exists(final):
            final = unique_path(final)
        os.replace(self._part_path, final)
        self.path = final
        self.filename = os.path.basename(final)
        try:
            os.remove(self._state_path)
        except OSError:
            pass
        self.downloaded = self.size or self.downloaded
        self.status = Status.COMPLETED


def format_size(n: float | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def format_time(sec: float | None) -> str:
    if sec is None:
        return "-"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s" if m else f"{s}s"
