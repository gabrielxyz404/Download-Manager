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

import http.client
import json
import os
import re
import threading
import time
import urllib.error
import uuid
import urllib.parse
import urllib.request
from collections import deque
from email.message import Message

from . import net, vault
from .categories import category_for
from .limiter import GLOBAL_LIMITER, RateLimiter
from .status import Status  # noqa: F401 (re-exported)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
CHUNK_SIZE = 256 * 1024
MIN_CHUNK = 64 * 1024      # each connection reads pieces of ~0.25 s of data...
MAX_CHUNK = 1024 * 1024    # ...between 64 KB and 1 MB (few, big pieces = little processor use)
MIN_SPLIT_SIZE = 512 * 1024  # do not split segments smaller than 2x this
MAX_RETRIES = 8
FIRST_REQUEST = 2 * 1024 * 1024  # the first request already fetches this much
REQUEST_SECONDS = 2.0     # each later request asks for ~2 s worth of data...
MIN_REQUEST = 1024 * 1024  # ...between 1 MB...
MAX_REQUEST = 32 * 1024 * 1024  # ...and 32 MB, on a connection that stays open
START_CONNECTIONS = 2     # adaptive mode starts small (4 for big files)...
START_CONNECTIONS_BIG = 4
BIG_FILE = 64 * 1024 * 1024
RAMP_INTERVAL = 2.0       # ...measures the speed every 2 s...
RAMP_MIN_GAIN = 1.15      # ...and doubles the connections while that gives 15%+ more speed
LIMIT_CODES = (429, 503)  # "too many requests / busy": the server limits connections per user
RECHECK_INTERVAL = 8.0    # after the test, re-check every 8 s...
RECHECK_DROP = 0.7        # ...and test more connections if the speed fell below 70% of the best
SLOW_FACTOR = 0.3         # a connection at <30% of the median speed...
SLOW_MIN_AGE = 5.0        # ...for 5 s or more is reconnected (at most MAX_SWAPS times)
MAX_SWAPS = 2
ENDGAME_MIN_PIECE = 32 * 1024   # near the end, help connections that still need...
ENDGAME_MIN_ETA = 1.5           # ...more than 1.5 s to finish their piece
TIMEOUT = 30


class FileChanged(Exception):
    """The file on the server is not the version we started downloading."""


def validator_of(headers) -> str | None:
    """What identifies this version of the file for If-Range. Last-Modified first (like XDM:
    mirrors of one file have different ETags); otherwise a strong ETag (weak ones, W/..., are
    not allowed in If-Range)."""
    lm = headers.get("Last-Modified")
    if lm:
        return lm
    etag = headers.get("ETag") or ""
    return etag if etag.startswith('"') else None


class Segment:
    """Byte range [start, end] (inclusive). ``pos`` is the next byte to write."""

    def __init__(self, start: int, end: int, pos: int | None = None):
        self.start = start
        self.end = end
        self.pos = start if pos is None else pos
        self.lock = threading.Lock()
        self.active = False
        self.inflight_end = -1  # last byte of the request currently on its way

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
    """One-off request with urllib (used by the grabber-style helpers and tests)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    if start is not None:
        req.add_header("Range", f"bytes={start}-{'' if end is None else end}")
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def _info_from_response(resp: net.Response) -> dict:
    headers = resp.headers
    size = None
    resumable = False
    if resp.status == 206:
        m = re.search(r"/(\d+)\s*$", headers.get("Content-Range", ""))
        if m:
            size = int(m.group(1))
            resumable = True
    elif headers.get("Content-Length"):
        size = int(headers["Content-Length"])
    return {"url": resp.url, "size": size, "resumable": resumable,
            "filename": _filename_from_headers(headers, resp.url), "validator": validator_of(headers)}


def _headers(extra: dict | None) -> dict:
    return {"User-Agent": USER_AGENT, **(extra or {})}


def host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def split_cookies(url: str, headers: dict | None, cookies: dict | None) -> tuple[dict, dict]:
    """Cookies are kept per site ({host: "a=1; b=2"}), never as a plain header: a header
    would follow redirects to other sites. An old-style Cookie header belongs to ``url``'s site."""
    headers = dict(headers or {})
    cookies = {k.lower(): v for k, v in (cookies or {}).items() if v}
    for k in [k for k in headers if k.lower() == "cookie"]:
        value = headers.pop(k)
        if value:
            cookies.setdefault(host_of(url), value)
    return headers, cookies


def probe(url: str, headers: dict | None = None, cookies: dict | None = None) -> dict:
    """Find out size, final URL, file name and range support for ``url``."""
    headers, cookies = split_cookies(url, headers, cookies)
    conn = net.Connection(TIMEOUT)
    try:
        resp = conn.get(url, _headers(headers), 0, 0, cookies=cookies)
        return _info_from_response(resp)
    finally:
        conn.close()


class _Worker:
    """State of one connection: what it is downloading and how fast."""

    def __init__(self, seg: "Segment", conn=None, resp=None):
        self.seg, self.conn, self.resp = seg, conn, resp
        self.started = self.last_swap = time.monotonic()
        self.alive = True
        self.got_data = False
        self.reconnect = False
        self.swaps = 0
        self.total = 0
        self._samples: deque[tuple[float, int]] = deque([(self.started, 0)])

    def count(self, n: int) -> None:
        self.total += n
        now = time.monotonic()
        self._samples.append((now, self.total))
        while len(self._samples) > 2 and now - self._samples[0][0] > 3.0:
            self._samples.popleft()

    def speed(self) -> float:
        now = time.monotonic()
        t0, b0 = self._samples[0]
        return (self.total - b0) / (now - t0) if now - t0 > 0.2 else 0.0

    def reset_speed(self) -> None:
        self._samples = deque([(time.monotonic(), self.total)])

    def request_size(self) -> int:
        return int(max(MIN_REQUEST, min(MAX_REQUEST, self.speed() * REQUEST_SECONDS)))

    def chunk_size(self) -> int:
        return int(max(MIN_CHUNK, min(MAX_CHUNK, self.speed() * 0.25)))


class DownloadTask:
    """A single download. Thread-safe to call start()/pause() from a GUI."""

    def __init__(self, url: str, dest_dir: str, connections: int = 8,
                 filename: str | None = None, task_id: str | None = None,
                 headers: dict | None = None, speed_limit: int = 0,
                 cookies: dict | None = None, final_url: str | None = None):
        # Random id: ids from the clock collided on Windows (coarse clock) when several
        # downloads were added at once.
        self.id = task_id or uuid.uuid4().hex[:16]
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
        # Referer etc. sent by the browser; login cookies kept per site (see split_cookies)
        self.headers, self.cookies = split_cookies(url, headers, cookies)
        self.limiter = RateLimiter(speed_limit)

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._samples: deque[tuple[float, int]] = deque()
        self._active_workers = 0
        self._worker_error: str | None = None
        self._limit_hit = 0  # >0: the server refused a connection; value = connections it accepts
        self._streaming = 0  # connections currently receiving data
        self._wake = threading.Event()  # set when a connection finishes: react at once
        self.server_limit: int | None = None  # connections the server accepts, when it refuses more
        self.connection_note = ""  # shown in the list: speed test / limit found
        self.reconnections = 0  # slow connections replaced
        # Where the link led (after redirects): signed links there may expire; self.url, the
        # link the user/browser started from, is asked again for a fresh one when that happens.
        self._final_url: str | None = final_url if final_url and final_url != url else None
        self._refresh_lock = threading.Lock()
        self.validator: str | None = None  # this version of the file (see validator_of)
        self._file_changed = False
        self.restarted_changed = False  # the file changed on the server and we started over
        self._first = None
        self._workers: list[_Worker] = []
        # Browser mode: the browser also shows this download (it receives the bytes from the
        # app while they arrive) and saves the final file itself.
        self.mirror = False
        self.stream_token: str | None = None
        self.rid: str | None = None  # the extension's request id for this download
        self.awaiting_browser = False  # finished here, waiting for the browser to take it
        self.on_done = None  # called (from the download thread) when it stops for any reason
        self.on_change = None  # called when the file name / place is known (to save the list)

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
            "cookies": vault.protect(self.cookies), "final_url": self._final_url,
            "mirror": self.mirror, "stream_token": self.stream_token, "rid": self.rid,
            "awaiting_browser": self.awaiting_browser,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DownloadTask":
        t = cls(d["url"], d["dest_dir"], d.get("connections", 8), d.get("filename"), d.get("id"),
                d.get("headers"), d.get("speed_limit", 0), vault.unprotect(d.get("cookies"), {}),
                d.get("final_url"))
        t.path = d.get("path")
        t.size = d.get("size")
        t.downloaded = d.get("downloaded", 0)
        t.added_at = d.get("added_at", time.time())
        t.mirror = d.get("mirror", False)
        t.stream_token = d.get("stream_token")
        t.rid = d.get("rid")
        t.awaiting_browser = d.get("awaiting_browser", False)
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
        self.validator = state.get("validator")
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

    def _checkpoint(self, sync_fd: int) -> None:
        """Power-loss safe progress save: note the positions, force the data written so far
        onto the disk, and only then record those positions. The record can never claim
        bytes that were not really stored."""
        snapshot = [s.to_list() for s in self.segments]
        try:
            os.fsync(sync_fd)
        except OSError:
            return
        self._save_state(snapshot)

    def _save_state(self, segments: list | None = None) -> None:
        if not self.path or not self.resumable or self.status == Status.COMPLETED:
            return
        state = {
            "url": self.url, "size": self.size, "resumable": self.resumable,
            "segments": segments if segments is not None else [s.to_list() for s in self.segments],
            "validator": self.validator,
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
            for attempt in (1, 2):
                try:
                    self._prepare()
                    if self.on_change:
                        self.on_change(self)
                    if self._stop.is_set():
                        self.status = Status.PAUSED
                        return
                    self.status = Status.DOWNLOADING
                    if self.resumable and self.size:
                        self._download_segmented()
                    else:
                        self._download_single()
                    break
                except FileChanged:
                    if attempt == 2:
                        raise RuntimeError("O ficheiro está sempre a mudar no servidor")
                    # Never mix two versions: drop what we have and start over with the new one.
                    self._discard_progress()
                    self.restarted_changed = True
        except Exception as exc:  # noqa: BLE001 - report any failure to the UI
            self.error = str(exc) or exc.__class__.__name__
            self.status = Status.ERROR
        finally:
            self.speed = 0.0
            self._save_state()
            if self.on_done:
                self.on_done(self)

    def _discard_progress(self) -> None:
        for p in (self._part_path, self._state_path):
            try:
                os.remove(p)
            except OSError:
                pass
        self.segments, self.downloaded, self.size = [], 0, None
        self.validator, self._final_url, self._file_changed = None, None, False
        self._worker_error = None
        self._stop.clear()

    def _prepare(self) -> None:
        os.makedirs(self.dest_dir, exist_ok=True)
        self._first = None
        if self.path and self.segments and os.path.exists(self._part_path):
            return  # resuming from saved state
        if not self.path:
            # An interrupted download of this same link in this folder? Resume it without
            # asking the server anything (the name usually comes from the link).
            guess = os.path.join(self.dest_dir, self.filename or sanitize_filename(
                urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(self.url).path))))
            if self._state_matches(guess):
                self.path = guess
                self.filename = os.path.basename(guess)
                if self._load_state() and self.segments:
                    return
        # The first request already asks for the first piece of the file: its answer tells
        # us size / name / range support AND its data is kept (no wasted round trip).
        conn = net.Connection(TIMEOUT)
        try:
            try:
                resp = conn.get(self._current_url(), self._headers_for(self._current_url()), 0, FIRST_REQUEST - 1,
                                cookies=self.cookies)
            except net.HTTPError as exc:
                if not self._final_url or exc.code not in (401, 403, 404, 410):
                    raise
                # The browser's final address expired already: start again from the link.
                self._final_url = None
                resp = conn.get(self.url, _headers(self.headers), 0, FIRST_REQUEST - 1, cookies=self.cookies)
        except Exception:
            conn.close()
            raise
        info = _info_from_response(resp)
        self._final_url = info["url"]
        if self.segments and self.validator and info["validator"] and info["validator"] != self.validator:
            # an interrupted download of an older version of this file
            conn.close()
            raise FileChanged()
        self.validator = self.validator or info["validator"]
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
            conn.close()  # we already have these bytes
            return
        self.segments = []
        self.downloaded = 0
        self._first = (conn, resp)

    def _current_url(self) -> str:
        return self._final_url or self.url

    def _headers_for(self, url: str) -> dict:
        """Credentials given for the link (Authorization) go to the link's own site only, also
        when we ask the address it led to directly (cookies are per site already)."""
        headers = _headers(self.headers)
        if host_of(url) != host_of(self.url):
            headers = {k: v for k, v in headers.items() if k.lower() not in net.SENSITIVE}
        return headers

    def _refresh_url(self, failed_url: str) -> None:
        """Signed download links (GitHub, cloud storage...) expire after a while. Ask the
        original link again for a fresh one. Only once per expired address: connections that
        hit the same expired address meanwhile just use the new one."""
        with self._refresh_lock:
            if self._current_url() != failed_url:
                return  # another connection already got a fresh address
            info = probe(self.url, self.headers, self.cookies)
            if info["size"] != self.size or (self.validator and info["validator"]
                                             and info["validator"] != self.validator):
                self._file_changed = True
                raise FileChanged()
            self._final_url = info["url"]

    # -- single connection (server does not support ranges / unknown size)
    def _download_single(self) -> None:
        self.downloaded = 0
        self._active_workers = 1
        conn, resp = self._first or (net.Connection(TIMEOUT), None)
        self._first = None
        try:
            if resp is None or resp.status == 206:  # need the whole body in one answer
                resp = conn.get(self._current_url(), self._headers_for(self._current_url()), cookies=self.cookies)
            with open(self._part_path, "wb") as f:
                while not self._stop.is_set():
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    self.downloaded += len(chunk)
                    self._update_speed()
                    self._throttle(len(chunk))
        finally:
            conn.close()
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
        first = self._first
        self._first = None
        if not self.segments:
            first_len = 0
            if first is not None:
                m = re.match(r"bytes (\d+)-(\d+)/", first[1].headers.get("Content-Range", ""))
                first_len = int(m.group(2)) + 1 if m and int(m.group(1)) == 0 else 0
            self._create_segments(first_len)
        if not os.path.exists(self._part_path):
            with open(self._part_path, "wb") as f:
                f.truncate(self.size)

        threads: list[threading.Thread] = []
        last_save = time.monotonic()
        sync_fd = os.open(self._part_path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        self._workers = []

        def spawn(seg: Segment, conn=None, resp=None) -> None:
            w = _Worker(seg, conn, resp)
            with self._lock:
                self._active_workers += 1
                self._workers.append(w)
            t = threading.Thread(target=self._worker, args=(w,), daemon=True)
            threads.append(t)
            t.start()

        # Server speed test: start with a few connections (more for big files) and double
        # them while the total speed keeps growing. Stop when more connections stop helping
        # (the line or the server is saturated) or when the server refuses them (its limit).
        # Keep measuring: if the speed drops later, try more connections again.
        cap = self.connections
        start_n = START_CONNECTIONS_BIG if self.size >= BIG_FILE else START_CONNECTIONS
        target = min(start_n, cap)
        ramping = target < cap
        ramp_at = time.monotonic() + RAMP_INTERVAL
        ramp_speed: float | None = None
        best_speed = 0.0
        recheck_at = 0.0
        self.connection_note = "testando velocidade" if ramping else ""
        self.server_limit = None

        if first is not None:
            seg0 = self.segments[0]
            seg0.active = True
            seg0.inflight_end = min(seg0.end, int(re.match(r"bytes \d+-(\d+)/",
                                                           first[1].headers["Content-Range"]).group(1)))
            spawn(seg0, *first)
        for seg in self.segments:
            if seg.remaining > 0 and not seg.active and self._active_workers < target:
                seg.active = True
                spawn(seg)

        while True:
            self._wake.wait(0.5)
            self._wake.clear()
            self.downloaded = sum(s.pos - s.start for s in self.segments)
            self._update_speed()
            if self._worker_error or self._file_changed:
                self._stop.set()
            now = time.monotonic()
            if self._limit_hit:
                self.server_limit = max(1, self._limit_hit)
                self._limit_hit = 0
                target, ramping = self.server_limit, False
                self.connection_note = f"servidor aceita {self.server_limit}"
            elif ramping and now >= ramp_at:
                current = self._recent_speed(1.5)
                if ramp_speed is None or current > ramp_speed * RAMP_MIN_GAIN:
                    ramp_speed = current
                    target = min(cap, target * 2)
                    ramp_at = now + RAMP_INTERVAL
                    ramping = target < cap
                    if not ramping:
                        self.connection_note = ""
                else:
                    ramping = False  # more connections did not make it faster
                    self.connection_note = f"{self._active_workers} conexões bastam"
                    recheck_at = now + RECHECK_INTERVAL
            elif (not ramping and self.server_limit is None and target < cap
                  and recheck_at and now >= recheck_at):
                # Speed dropped a lot since the best moment? Test more connections again.
                current = self._recent_speed(3.0)
                recheck_at = now + RECHECK_INTERVAL
                if best_speed and current < best_speed * RECHECK_DROP:
                    ramp_speed, ramping = current, True
                    target = min(cap, target * 2)
                    ramp_at = now + RAMP_INTERVAL
                    self.connection_note = "testando velocidade"
            best_speed = max(best_speed, self._recent_speed(3.0))
            if not self._stop.is_set():
                self._replace_slow_connections(now)
                # Dynamic segmentation: keep `target` connections busy.
                while self._active_workers < target:
                    new_seg = self._split_largest()
                    if new_seg is None:
                        break
                    spawn(new_seg)
            if time.monotonic() - last_save > 2.0:
                self._checkpoint(sync_fd)
                last_save = time.monotonic()
            if not any(t.is_alive() for t in threads):
                break

        self.downloaded = sum(s.pos - s.start for s in self.segments)
        self._checkpoint(sync_fd)
        os.close(sync_fd)
        self._workers = []
        if self._file_changed:
            raise FileChanged()
        if self._worker_error:
            raise RuntimeError(self._worker_error)
        if all(s.remaining == 0 for s in self.segments):
            self._finish()
        else:
            self.status = Status.PAUSED

    def _create_segments(self, first_len: int = 0) -> None:
        n = max(1, min(self.connections, self.size // MIN_SPLIT_SIZE or 1))
        step = self.size // n
        self.segments = []
        for i in range(n):
            start = i * step
            end = self.size - 1 if i == n - 1 else (i + 1) * step - 1
            self.segments.append(Segment(start, end))
        if first_len > self.segments[0].end + 1:
            # The first answer already covers more than the first piece: grow piece 0 so
            # that connection is not cut, and shrink / drop the pieces it overlaps.
            seg0 = self.segments[0]
            seg0.end = min(first_len, self.size) - 1
            rest = []
            for s in self.segments[1:]:
                if s.end <= seg0.end:
                    continue
                if s.start <= seg0.end:
                    s.start = s.pos = seg0.end + 1
                rest.append(s)
            self.segments = [seg0] + rest

    def _split_largest(self) -> Segment | None:
        """Work for a free connection: an idle piece, or the second half of the largest
        piece still to download (never cutting a request already on its way)."""
        with self._lock:
            idle = [s for s in self.segments if not s.active and s.remaining > 0]
            if idle:
                idle[0].active = True
                return idle[0]
            best, best_free = None, 0
            for s in self.segments:
                with s.lock:
                    free_from = max(s.pos, s.inflight_end + 1) if s.active else s.pos
                    free = s.end - free_from + 1
                if free >= 2 * MIN_SPLIT_SIZE and free > best_free:
                    best, best_free = s, free
            if best is not None:
                with best.lock:
                    free_from = max(best.pos, best.inflight_end + 1) if best.active else best.pos
                    free = best.end - free_from + 1
                    if free >= 2 * MIN_SPLIT_SIZE:
                        mid = free_from + free // 2
                        new = Segment(mid, best.end)
                        best.end = mid - 1
                        return self._add_segment(new)
            return self._endgame_split()

    def _endgame_split(self) -> Segment | None:
        """Near the end, help the slowest connection by taking over most of what it still has
        to receive, even inside its current request (it stops at the new boundary). It keeps
        only the share it can finish in the same time as a fast connection. This avoids the
        classic wait at 99% for one slow connection. Called with self._lock held."""
        best, best_eta, best_speed = None, ENDGAME_MIN_ETA, 1.0
        fast = max([w.speed() for w in self._workers] + [1.0])
        for w in self._workers:
            s = w.seg
            if s is None or not s.active:
                continue
            with s.lock:
                remaining = s.remaining
            speed = max(w.speed(), 1.0)
            eta = remaining / speed
            if remaining >= 2 * ENDGAME_MIN_PIECE and eta > best_eta:
                best, best_eta, best_speed = s, eta, speed
        if best is None:
            return None
        share = best_speed / (best_speed + max(fast, best_speed))  # slow one keeps this fraction
        with best.lock:
            if best.remaining < 2 * ENDGAME_MIN_PIECE:
                return None
            keep = max(ENDGAME_MIN_PIECE, min(best.remaining // 2, int(best.remaining * share)))
            mid = best.pos + keep
            new = Segment(mid, best.end)
            best.end = mid - 1
        return self._add_segment(new)

    def _add_segment(self, new: Segment) -> Segment:
        new.active = True
        self.segments.append(new)
        self.segments.sort(key=lambda s: s.start)
        return new

    def _replace_slow_connections(self, now: float) -> None:
        """A connection much slower than the others is often stuck on a slow server node.
        Ask it to reconnect (a new connection may land on a faster one)."""
        with self._lock:
            live = [w for w in self._workers if w.alive and w.got_data and now - w.started > SLOW_MIN_AGE]
        if len(live) < 3:
            return
        speeds = sorted(w.speed() for w in live)
        median = speeds[len(speeds) // 2]
        for w in live:
            if (w.speed() < median * SLOW_FACTOR and w.swaps < MAX_SWAPS
                    and now - w.last_swap > SLOW_MIN_AGE and not w.reconnect):
                w.reconnect = True
                self.reconnections += 1

    def _worker(self, w: "_Worker") -> None:
        retries = 0
        refusals = 0
        refreshed_without_data = False  # asked for a fresh address and got nothing since
        req_url = self.url
        conn = w.conn or net.Connection(TIMEOUT)
        resp = w.resp
        seg = w.seg
        try:
            # Unbuffered: every chunk goes straight to the operating system, so nothing
            # sits in a Python buffer that would be lost if the app is killed.
            with open(self._part_path, "r+b", buffering=0) as f:
                while not self._stop.is_set():
                    if seg.remaining == 0 and resp is None:
                        seg.active = False
                        seg = w.seg = self._split_largest()  # keep the connection, take new work
                        if seg is None:
                            break
                        continue
                    try:
                        if resp is None:
                            req_end = min(seg.end, seg.pos + w.request_size() - 1)
                            with seg.lock:
                                seg.inflight_end = req_end
                            req_url = self._current_url()
                            hdrs = self._headers_for(req_url)
                            if self.validator:
                                hdrs["If-Range"] = self.validator  # only bytes of the same version
                            resp = conn.get(req_url, hdrs, seg.pos, req_end, cookies=self.cookies)
                            if resp.status != 206:
                                if self.validator:
                                    # If-Range said no: the file on the server is a new version
                                    self._file_changed = True
                                    raise FileChanged()
                                raise RuntimeError("O servidor parou de aceitar downloads em partes")
                            self._final_url = resp.url
                        while not self._stop.is_set():
                            with seg.lock:
                                want = min(seg.end, seg.inflight_end) - seg.pos + 1
                            if want <= 0:
                                break
                            chunk = resp.read(min(w.chunk_size(), want))
                            if not chunk:
                                break
                            with seg.lock:
                                last = min(seg.end, seg.inflight_end)
                                n = max(0, min(len(chunk), last - seg.pos + 1))
                                if n:
                                    f.seek(seg.pos)
                                    f.write(chunk[:n])
                                    seg.pos += n
                                request_done = seg.pos > last
                            w.count(n)
                            retries = 0
                            refreshed_without_data = False
                            if not w.got_data:
                                w.got_data = True
                                with self._lock:
                                    self._streaming += 1
                            self._throttle(n)
                            if request_done or w.reconnect:
                                break
                        with seg.lock:
                            seg.inflight_end = -1
                        if not resp.finished:
                            conn.close()  # stopped mid-answer: this connection can't be reused
                        resp = None
                        if w.reconnect:
                            conn.close()
                            w.reconnect = False
                            w.swaps += 1
                            w.last_swap = time.monotonic()
                            w.reset_speed()
                    except (net.HTTPError, OSError, http.client.HTTPException) as exc:
                        with seg.lock:
                            seg.inflight_end = -1
                        resp = None
                        conn.close()
                        code = exc.code if isinstance(exc, net.HTTPError) else None
                        refused = isinstance(exc, ConnectionRefusedError)
                        if (code in LIMIT_CODES or refused) and not w.got_data and self._active_workers > 1:
                            # The server does not want one more connection from us. It may still
                            # be counting one we just closed, so try once more after a second;
                            # if refused again, that is its limit: hand the piece back (another
                            # connection will take it) instead of failing.
                            refusals += 1
                            if refusals == 1:
                                self._stop.wait(1.0)
                                continue
                            self._limit_hit = max(self._limit_hit, self._streaming)
                            return
                        if code in (401, 403, 410) and req_url != self._current_url():
                            continue  # another connection already replaced the expired address
                        if code in (401, 403, 410) and not refreshed_without_data:
                            refreshed_without_data = True  # the address probably expired
                            try:
                                self._refresh_url(req_url)
                                continue
                            except FileChanged:
                                raise
                            except Exception:  # noqa: BLE001
                                pass
                        if code == 416:  # the range is gone: the file got smaller, i.e. it changed
                            self._file_changed = True
                            raise FileChanged()
                        if code in (401, 403, 404, 410):
                            raise RuntimeError(f"HTTP {exc.code}: {exc.reason}") from exc
                        retries += 1
                        if retries > MAX_RETRIES:
                            raise RuntimeError(f"Falha na conexão: {exc}") from exc
                        self._stop.wait(min(2 ** retries, 30))
        except FileChanged:
            self._file_changed = True  # the download loop starts over with the new version
        except Exception as exc:  # noqa: BLE001
            self._worker_error = str(exc) or exc.__class__.__name__
        finally:
            conn.close()
            if seg is not None:
                with seg.lock:
                    seg.inflight_end = -1
                seg.active = False
            w.alive = False
            with self._lock:
                self._active_workers -= 1
                if w.got_data:
                    self._streaming -= 1
            self._wake.set()

    def _throttle(self, nbytes: int) -> None:
        delay = max(GLOBAL_LIMITER.reserve(nbytes), self.limiter.reserve(nbytes))
        if delay > 0:
            self._stop.wait(delay)

    def _recent_speed(self, window: float) -> float:
        """Average speed over the last ``window`` seconds."""
        now = time.monotonic()
        recent = [(t, b) for t, b in self._samples if now - t <= window]
        if len(recent) < 2 or recent[-1][0] <= recent[0][0]:
            return self.speed
        return (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0])

    def _update_speed(self) -> None:
        now = time.monotonic()
        self._samples.append((now, self.downloaded))
        while self._samples and now - self._samples[0][0] > 3.0:
            self._samples.popleft()
        t0, b0 = self._samples[0]
        self.speed = (self.downloaded - b0) / (now - t0) if now > t0 else 0.0

    def available_prefix(self) -> int:
        """How many bytes from the start of the file are already downloaded, with no gap
        (what can be handed to the browser right now)."""
        if self.status == Status.COMPLETED:
            return self.size or self.downloaded
        if not self.resumable:
            return self.downloaded
        with self._lock:
            for s in sorted(self.segments, key=lambda s: s.start):
                if s.pos <= s.end:
                    return s.pos
        return self.size or 0

    def data_path(self) -> str | None:
        """Where the downloaded bytes are right now."""
        if self.path and os.path.exists(self._part_path):
            return self._part_path
        return self.path

    def finalize_local(self) -> None:
        """Browser mode, but the browser never took the file: keep it in our folder."""
        if not self.awaiting_browser:
            return
        self.awaiting_browser = False
        self.mirror = False
        self._finish()

    def release_to_browser(self, browser_path: str | None = None) -> None:
        """The browser saved the file: remove our temporary copy."""
        for p in (self._part_path, self._state_path):
            try:
                os.remove(p)
            except OSError:
                pass
        self.awaiting_browser = False
        if browser_path:
            self.path = browser_path
            self.filename = os.path.basename(browser_path)

    def _finish(self) -> None:
        if self.mirror:
            # The browser is receiving the bytes and saves the final file itself; ours is
            # only a temporary copy, removed once the browser has everything.
            try:
                os.remove(self._state_path)
            except OSError:
                pass
            self.downloaded = self.size or self.downloaded
            self.awaiting_browser = True
            self.status = Status.COMPLETED
            return
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
