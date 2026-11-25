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

import base64
import http.client
import json
import ssl
import os
import random
import zlib
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
MB = 1024 * 1024
CHUNK_SIZE = 256 * 1024
MIN_CHUNK = 64 * 1024      # each connection reads pieces of ~0.25 s of data...
MAX_CHUNK = 1024 * 1024    # ...between 64 KB and 1 MB (few, big pieces = little processor use)
MIN_SPLIT_SIZE = 512 * 1024  # do not split segments smaller than 2x this
MAX_RETRIES = 8
NETWORK_PATIENCE = 30 * 60  # no way to the server at all: keep trying this long
FIRST_REQUEST = 2 * 1024 * 1024  # the first request already fetches this much
# Each later request asks for ~15 s worth of data, between 4 MB (a new connection must not
# stop to ask again at once) and 256 MB, on a connection that stays open. Few requests: some
# servers count them and stop a client that asks too often (seen on thinkbroadband: "429 Too
# Many Requests" after ~120 requests in 20 s, then nothing for 75 s, with 2 s requests).
REQUEST_SECONDS = 15.0
MIN_REQUEST = 4 * 1024 * 1024
MAX_REQUEST = 256 * 1024 * 1024
MIRROR_SECONDS = 2.0  # browser-list downloads: shorter (see the worker)...
# ...but never more than ~REQUEST_BUDGET pieces for the whole file: a server that counts
# requests (thinkbroadband: 429 after ~120) must not see hundreds for one big file.
REQUEST_BUDGET = 64
MIRROR_REQUEST = 1024 * 1024  # browser-list downloads: shorter requests (see the worker)
START_CONNECTIONS = 2     # adaptive mode starts small (4 for big files)...
START_CONNECTIONS_BIG = 8  # (was 4: fast lines waited seconds for the speed to build up)
BIG_FILE = 64 * 1024 * 1024
MEMORY_MIN_SIZE = 8 * 1024 * 1024  # downloads at least this big teach us about their server
RAMP_INTERVAL = 2.0       # ...measures the speed every 2 s...
RAMP_MIN_GAIN = 1.15      # ...and doubles the connections while that gives 15%+ more speed
LIMIT_CODES = (429, 503)  # "too many requests / busy": the server limits connections per user
RECHECK_INTERVAL = 8.0    # after the test, re-check every 8 s...
RECHECK_DROP = 0.7        # ...and test more connections if the speed fell below 70% of the best
SLOW_FACTOR = 0.3         # a connection at <30% of the median speed...
SLOW_MIN_AGE = 5.0        # ...for 5 s or more is reconnected (at most MAX_SWAPS times)
MAX_SWAPS = 2
STALL_SECONDS = 6.0       # a connection that receives nothing for this long is replaced at once
STALL_ALONE_SECONDS = 4.0 # ...or for this long while the other connections are receiving
ENDGAME_MIN_PIECE = 512 * 1024  # near the end, help connections that still need...
ENDGAME_KEEP = 64 * 1024        # (what the slow one keeps, at least)
ENDGAME_MIN_ETA = 3.0           # ...more than 3 s to finish their piece (smaller pieces cost a
                                # new request each, on a fresh connection a slow start too)
TIMEOUT = 30
SINGLE_TRIES = 3          # a server that cannot continue mid-file: start over this many times
CHECKPOINT_S = 5.0        # progress forced onto the disk this often (a power cut loses at most this)
PROBE_OK_BYTES = 256 * 1024  # a fresh connection that got this much really works (not a trickle)
# Every connection silent this long: the server (or the line) stopped. (Not shorter than
# STALL_SECONDS: tried 4 s, and a server that stalls each answer after a while became 8x
# slower - the stop was declared before the usual swap of a silent connection could act.)
OUTAGE_SILENT_S = STALL_SECONDS
OUTAGE_SHED_S = 1.0       # a stop longer than this: close the connections above the new number
OUTAGE_CALM_S = 90.0      # after the server stopped every connection: fewer connections for this long
RESCUE_EXTRA = 2          # a stuck piece may add this many connections over the maximum (while
                          # the stuck ones, which leave once they wake, are at most the maximum)


CERT_ERROR = ("O certificado de segurança (HTTPS) deste site não é válido ou não é de confiança. "
              "Tentar de novo não resolve.")
TRANSIENT_CODES = (408, 425, 429, 500, 502, 503, 504)  # worth asking again
LIMITED_QUIET_S = 90.0  # after a 429: no replacing / reopening connections for this long
MAX_RETRY_AFTER = 120  # never wait longer than this for a server's "try again in N s"
FIRST_TRIES = 4
MAX_SAME_REFUSALS = 5  # the same, still valid address refused again and again with no progress
MAX_FRUITLESS_REFRESHES = 3  # fresh addresses in a row with no progress at all: give up


def is_cert_error(exc: BaseException) -> bool:
    """The site's certificate / name could not be verified: retrying cannot help."""
    while exc is not None:
        if isinstance(exc, ssl.SSLCertVerificationError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


class _Finished(Exception):
    """A late connection found the download already finished: it just leaves."""


class DiskProblem(Exception):
    """The disk, not the network: full, no permission, folder gone. The download pauses with a
    plain message (its progress is kept) instead of being retried as a network error."""


def _disk(exc: OSError, path: str) -> DiskProblem:
    from .system import disk_message
    return DiskProblem(disk_message(exc, os.path.dirname(os.path.abspath(path)) or path))


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
    from .categories import name_from_url
    cd = headers.get("Content-Disposition", "")
    name = None
    m = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", cd, re.I)
    if m:
        charset = (m.group(1).strip() or "utf-8").lower()
        raw = m.group(2).strip().strip('"')
        try:
            name = urllib.parse.unquote(raw, encoding=charset, errors="strict")
        except (LookupError, UnicodeDecodeError):
            name = urllib.parse.unquote(raw, encoding="latin-1")
    else:
        # A quoted name may hold ";" and \" (escaped quotes); an unquoted one ends at ";".
        m = (re.search(r'filename\s*=\s*"((?:[^"\\]|\\.)*)"', cd, re.I)
             or re.search(r'filename\s*=\s*([^";]+)', cd, re.I))
        if m:
            # Only \" and \\ are escapes; other backslashes are kept (old servers send Windows
            # paths like C:\dir\name.exe: only the last part is used, further down).
            name = re.sub(r'\\(["\\])', r"\1", m.group(1)).strip()
            try:  # headers are read as Latin-1: a name sent in UTF-8 ("ç" as 2 bytes) is fixed here
                name = name.encode("latin-1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass  # it really was Latin-1 (or plain ASCII): keep it
    if not name:
        name = name_from_url(url) or "download"
    return sanitize_filename(name)


# Names Windows keeps for devices, whatever the extension ("con.txt" opens the console).
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in "123456789¹²³"),
             *(f"LPT{i}" for i in "123456789¹²³")}
MAX_NAME = 200  # Windows allows 255 per name; room for ".part.json" and " (12)"


def sanitize_filename(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        return "download"
    if name.split(".")[0].strip().upper() in _RESERVED:
        name = "_" + name
    if len(name) > MAX_NAME:  # keep the extension: it decides how the file opens
        stem, ext = os.path.splitext(name)
        ext = ext if len(ext) <= 16 else ""
        name = stem[:MAX_NAME - len(ext)].rstrip(" .") + ext
    return name


_NAME_LOCK = threading.Lock()  # choosing a free file name and claiming it is one step


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
    elif (headers.get("Content-Length") or "").strip().isdigit():
        size = int(headers["Content-Length"])
    if (headers.get("Content-Encoding") or "identity").strip().lower() not in ("identity", ""):
        # Compressed by the server: the sizes and ranges are those of the compressed data, not
        # of the file. One stream, unpacked as it comes (see _download_single).
        size, resumable = None, False
    return {"url": resp.url, "size": size, "resumable": resumable,
            "filename": _filename_from_headers(headers, resp.url), "validator": validator_of(headers),
            "type": (headers.get("Content-Type") or "").split(";")[0].strip().lower()}


def _check_expected(expect: dict | None, info: dict) -> None:
    """For a download taken over from the browser: is the app getting the same file? Some
    sites answer anyone else with a page (a protection check, "your download starts in 5 s",
    a login form) instead of the file; that must not be saved as the download."""
    if not expect:
        return
    want_type = (expect.get("type") or "").lower()
    if info["type"] in ("text/html", "application/xhtml+xml") and want_type not in ("text/html", "application/xhtml+xml"):
        raise RuntimeError("o site respondeu à app com uma página em vez do ficheiro")
    want_size = expect.get("size") or 0
    if want_size > 0 and info["size"] and info["size"] != want_size:
        raise RuntimeError(f"o site deu à app outro ficheiro ({info['size']} bytes em vez de {want_size})")


def _honest_agent() -> str:
    from . import __version__
    return f"FlowDownloads/{__version__}"


HONEST_USER_AGENT = _honest_agent()


def _headers(extra: dict | None) -> dict:
    return {"User-Agent": USER_AGENT, **(extra or {})}


_NUM = (int, float)
# The kind of each saved field. A damaged or hand-edited list may hold anything: a field of
# the wrong kind takes its default (before: the window would not open, or no save worked).
RECORD_KINDS = {
    "url": str, "dest_dir": str, "connections": int, "filename": (str, type(None)), "id": str,
    "headers": dict, "speed_limit": _NUM, "final_url": (str, type(None)), "path": (str, type(None)),
    "size": (int, type(None)), "downloaded": int, "added_at": _NUM, "mirror": bool,
    "stream_token": (str, type(None)), "rid": (str, type(None)), "awaiting_browser": bool,
    "expected_hash": (str, type(None)), "check": str, "active_seconds": _NUM, "peak_speed": _NUM,
    "first_byte_after": (int, float, type(None)), "leftovers": list, "status": str,
    "title": (str, type(None)), "format": str, "audio_only": bool, "quality": str,
    "cookiefile": (str, type(None)),
}


def clean_record(d: dict) -> dict:
    """A saved download with every field of the right kind (wrong ones dropped)."""
    if not isinstance(d, dict) or not isinstance(d.get("url"), str) or not isinstance(d.get("dest_dir"), str):
        raise ValueError("damaged download entry")
    out = {}
    for k, v in d.items():
        kind = RECORD_KINDS.get(k)
        if kind is not None:
            if isinstance(v, bool) and kind is not bool and not (isinstance(kind, tuple) and bool in kind):
                continue  # (True is an int in Python: not a size or a time)
            if not isinstance(v, kind):
                continue
            if isinstance(v, float) and (v != v or abs(v) == float("inf")):
                continue
            if isinstance(v, int) and not isinstance(v, bool) and v < 0 and k != "first_byte_after":
                continue
        out[k] = v
    return out


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:  # a broken address ("http://[::1/x"): no site
        return ""


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
        self.accepted = resp is not None  # the server accepted a request on this connection
        self.started = self.last_swap = self.last_data = time.monotonic()
        self.quiet_swaps = 0  # replaced for silence with no data in between (backs off)
        self.rescued_at = 0.0  # when its piece was last handed to a fresh connection
        self.retired = False  # its piece went to a fresh connection: leave instead of taking more
        self.shed = False  # closed during a long stop: more connections than the server takes
        self.rescuer = False  # it was opened to take a stuck connection's piece
        self.answering = False  # the server has started answering the current request
        self.alive = True
        self.got_data = False
        self.reconnect = False
        self.swaps = 0
        self.total = 0
        self._samples: deque[tuple[float, int]] = deque([(self.started, 0)])

    def count(self, n: int) -> None:
        self.total += n
        now = time.monotonic()
        if n:
            self.last_data = now
            self.quiet_swaps = 0
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
        self._answer_time = 0.0  # how long the server takes to start answering a request
        self._disk_problem: DiskProblem | None = None
        self._naps = threading.Event()  # set: connections stop waiting (pause / file complete)
        self._refreshes = 0  # fresh addresses obtained for an expired link
        self._same_refusals = 0  # refusals of an address that the original link still leads to
        self._progress_at_same = -1
        self._limit_hit = 0  # >0: the server refused a connection; value = connections it accepts
        self._streaming = 0  # connections currently receiving data
        self._last_rx = 0.0  # when any connection last received data
        self._net_down = False  # connections are waiting for a way to the server
        self.outages = 0  # times every connection went silent together (this download)
        self._last_outage_at = 0.0
        self._punishing_host = False
        self._few_requests = False  # the server said "too many requests" (429): whole pieces
        self._last_429 = 0.0
        self._target = 1 << 30  # connections wanted now (set by the download loop)
        self._in_outage = False  # every connection stopped together (no cutting / reopening)
        self._outage_started: float | None = None
        self._tail = False  # the last few seconds of work: stuck pieces are always rescued
        self._accepted_any = True  # the server accepted a connection in this run
        self._wake = threading.Event()  # set when a connection finishes: react at once
        self.server_limit: int | None = None  # connections the server accepts, when it refuses more
        self.connection_note = ""  # shown in the list: speed test / limit found
        self.reconnections = 0  # slow connections replaced
        # Diagnostic ("Copiar diagnóstico"): what happened, second by second, in the last run.
        self.requests = 0  # range requests sent (this run)
        self._diag: deque = deque(maxlen=900)  # one row per second (the last 15 min)
        self._diag_events: deque = deque(maxlen=300)
        self._diag_note = ""
        self._diag_at = 0.0
        # Where the link led (after redirects): signed links there may expire; self.url, the
        # link the user/browser started from, is asked again for a fresh one when that happens.
        self._final_url: str | None = final_url if final_url and final_url != url else None
        self._refresh_lock = threading.Lock()
        # A file made by a form (POST): {"body": bytes, "type": content type}, sent again
        # exactly as the browser did. If its answer IS the file, the download uses one
        # connection (repeating a form for pieces could make another file or repeat an
        # action); if it leads to an ordinary address, that address is downloaded as usual.
        self.post: dict | None = None
        self._post_direct = False
        self._progress_at_refresh = -1
        self._fruitless_refreshes = 0
        self.validator: str | None = None  # this version of the file (see validator_of)
        self._file_changed = False
        self.restarted_changed = False  # the file changed on the server and we started over
        self._first = None
        self._workers: list[_Worker] = []
        # Browser mode: the browser also shows this download (it receives the bytes from the
        # app while they arrive) and saves the final file itself.
        self.mirror = False
        self.expect: dict | None = None  # what the browser received (size, type), when taken over
        self.mark_internet = True  # Windows "came from the internet" mark on the finished file
        self.expected_hash: str | None = None  # checksum the user gave: verified when it finishes
        self.check = ""  # what the end-of-download actions found (checksum, antivirus...)
        # Measured, shown in the details panel (and kept): to compare with the browser
        self.active_seconds = 0.0    # time spent downloading (all sessions together)
        self.peak_speed = 0.0        # best speed over ~3 s
        self.first_byte_after = None  # seconds from the start to the first byte (last session)
        self._run_started = 0.0
        self._run_from = 0
        self.after_ran = False
        self.host_memory = None    # HostMemory: what each server taught us (set by the manager)
        self.server_addresses = 0  # how many addresses the server has (connections are spread)
        self._settled_at = 0
        self.stream_token: str | None = None
        self.rid: str | None = None  # the extension's request id for this download
        self.awaiting_browser = False  # finished here, waiting for the browser to take it
        self.leftovers: list = []  # our temporary copies the system did not let us delete yet
        self.on_done = None  # called (from the download thread) when it stops for any reason
        self.on_change = None  # called when the file name / place is known (to save the list)
        # Picks the folder from the real file name (category folders); set by the manager for
        # downloads without a folder chosen by hand. Not saved: used only before the file exists.
        self.choose_folder = None

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
        if self.is_running() and self._stop.is_set():
            # Paused a moment ago and still closing (saving its state): resume right after,
            # instead of ignoring the click.
            self._thread.join(10)
        if self.is_running() or self.status == Status.COMPLETED:
            return
        self._stop.clear()
        self._naps.clear()
        self.error = None
        self._worker_error = None
        self._disk_problem = None
        self.status = Status.CONNECTING
        self._thread = threading.Thread(target=self._run, name=f"dl-{self.id}", daemon=True)
        self._thread.start()

    def _settle(self) -> None:
        """Before reopening a connection we cut: on a server that stopped us before, wait a
        moment (a little different for each connection), so it counts the old one as gone
        before the new one arrives. Seen in a simulation: near the end every connection was
        cut and reopened at once, the server saw more than its limit for an instant, stopped
        everyone again - and so on, for minutes."""
        if self._touchy():
            self._nap(0.4 + random.random() * 0.8)

    def _touchy(self) -> bool:
        """The server stopped every connection recently: careful with new connections.
        Only for a while (a short Wi-Fi drop must not leave the rest of a long download
        without its usual end-of-download help)."""
        return bool(self.outages) and time.monotonic() - self._last_outage_at < OUTAGE_CALM_S

    def _nap(self, seconds: float) -> None:
        """A connection waiting before it tries again. Woken at once on pause, or when the
        file is complete (it must not keep the finished download waiting)."""
        self._naps.wait(seconds)

    def pause(self) -> None:
        if self.is_running():
            self._stop.set()
            self._naps.set()
            # Wake every connection now: one waiting on a server that stopped sending would
            # otherwise hold the pause (and the file, on Windows) for its 30 s timeout.
            with self._lock:
                conns = [w.conn for w in self._workers if w.alive and w.conn is not None]
            for conn in conns:
                conn.abort()
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
            "cookies": vault.protect_cached(self.cookies), "final_url": self._final_url,
            "mirror": self.mirror, "stream_token": self.stream_token, "rid": self.rid,
            "awaiting_browser": self.awaiting_browser,
            "expected_hash": self.expected_hash, "check": self.check,
            "leftovers": [p for p in self.leftovers if os.path.exists(p)],
            "active_seconds": round(self.active_seconds, 2), "peak_speed": round(self.peak_speed),
            "first_byte_after": self.first_byte_after,
            # (encrypted like the cookies: a form may carry personal data; dropped once done)
            "post": vault.protect_cached({"body": base64.b64encode(self.post["body"]).decode(),
                                          "type": self.post.get("type") or "",
                                          "origin": self.post.get("origin") or ""})
            if self.post and self.status != Status.COMPLETED else None,
            "post_direct": self._post_direct,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DownloadTask":
        d = clean_record(d)
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
        t.expected_hash = d.get("expected_hash")
        t.check = d.get("check", "")
        t.active_seconds = d.get("active_seconds", 0.0)
        t.leftovers = [p for p in d.get("leftovers", []) if isinstance(p, str)
                       and p.endswith((".part", ".part.json"))]
        t.peak_speed = d.get("peak_speed", 0.0)
        post = vault.unprotect(d.get("post"), None)
        if isinstance(post, dict) and isinstance(post.get("body"), str):
            try:
                t.post = {"body": base64.b64decode(post["body"]), "type": str(post.get("type") or ""),
                          "origin": str(post.get("origin") or "")}
            except ValueError:
                t.post = None
        t._post_direct = bool(d.get("post_direct")) and t.post is not None
        t.first_byte_after = d.get("first_byte_after")
        t.after_ran = d.get("status") == Status.COMPLETED  # never again for an old download
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

    def _prepare_patiently(self) -> None:
        """Resuming with no way to the server yet (e.g. the app resumed downloads when the
        computer started, before the Wi-Fi was up): wait for the line instead of failing at
        once (before: "Erro", and the download stayed stopped). A new link that fails is
        still reported at once (a typo must not wait half an hour)."""
        started = time.monotonic()
        while True:
            try:
                return self._prepare()
            except (OSError, http.client.HTTPException) as exc:
                if (isinstance(exc, net.HTTPError) or self.downloaded <= 0 or self._stop.is_set()
                        or is_cert_error(exc) or time.monotonic() - started > NETWORK_PATIENCE):
                    raise
                self.connection_note = "sem ligação ao servidor: a tentar de novo"
                self._stop.wait(30)
                if self._stop.is_set():
                    raise RuntimeError("pausado") from exc

    def _run(self) -> None:
        self._run_started = time.monotonic()
        self._run_from = self.downloaded
        self.first_byte_after = None
        self.outages = 0  # (counted per run: an old stop says nothing about a resumed download)
        self.requests = 0
        self._diag.clear()
        self._diag_events.clear()
        self._diag_note = ""
        self._diag_at = 0.0
        self._event(f"início: {self.downloaded // MB} MB já baixados, máx. {self.connections} ligações")
        try:
            for attempt in (1, 2):
                try:
                    self._prepare_patiently()
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
        except DiskProblem as exc:
            self.error = str(exc)
            self.status = Status.PAUSED  # nothing lost: Resume once there is room / a folder
        except Exception as exc:  # noqa: BLE001 - report any failure to the UI
            if self._stop.is_set() and not self._worker_error:
                self.status = Status.PAUSED  # paused while it was waiting to try again
            else:
                self.error = str(exc) or exc.__class__.__name__
                self.status = Status.ERROR
        finally:
            self._event(f"fim: {self.status}" + (f" ({self.error})" if self.error else ""))
            self.speed = 0.0
            self.active_seconds += time.monotonic() - self._run_started
            self._save_state()
            if self.on_done:
                self.on_done(self)

    def _event(self, text: str) -> None:
        t = time.monotonic() - self._run_started if self._run_started else 0.0
        self._diag_events.append((t, text))

    def _diag_row(self, now: float, target: int) -> None:
        """One line per second of the run (see diagnostic())."""
        if now - self._diag_at < 1.0:
            return
        self._diag_at = now
        if self.connection_note != self._diag_note:
            self._diag_note = self.connection_note
            if self.connection_note:
                self._event(f"nota: {self.connection_note}")
        with self._lock:
            alive = sum(1 for w in self._workers if w.alive)
            receiving = sum(1 for w in self._workers if w.alive and now - w.last_data < 1.0)
        self._diag.append((now - self._run_started, self.downloaded // MB, self.speed / MB, alive,
                           receiving, target, self.requests, self._in_outage))

    def diagnostic(self) -> str:
        """What happened in the last run, as text to paste in a message: second by second
        (MB done, speed, open connections, receiving ones, connections wanted, requests sent)
        and the events (stops, connections cut / replaced, errors). No address or cookies:
        only the server's name."""
        from . import __version__
        lines = [f"Flow Downloads {__version__} - diagnóstico",
                 f"servidor: {host_of(self._current_url())}  tamanho: "
                 f"{(self.size or 0) / MB:.0f} MB  estado: {self.status}"
                 + ("  (na lista do navegador)" if self.mirror else "")
                 + (f"  erro: {self.error}" if self.error else ""),
                 f"ligações máx.: {self.connections}  paragens: {self.outages}  "
                 f"substituídas: {self.reconnections}  pedidos: {self.requests}  "
                 f"limite do servidor: {self.server_limit}",
                 "", "eventos (segundos desde o início):"]
        lines += [f"{t:7.1f}  {text}" for t, text in list(self._diag_events)]
        lines += ["", "   seg      MB    MB/s  abertas  a receber  alvo  pedidos  parado"]
        lines += [f"{t:6.0f}  {mb:6d}  {sp:6.1f}  {alive:7d}  {rx:9d}  {tg:4d}  {rq:7d}  {'sim' if out else ''}"
                  for t, mb, sp, alive, rx, tg, rq, out in list(self._diag)]
        return "\n".join(lines)

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
        try:
            os.makedirs(self.dest_dir, exist_ok=True)
        except OSError as exc:
            raise _disk(exc, os.path.join(self.dest_dir, "x")) from exc
        self._first = None
        if self.path and self.segments and os.path.exists(self._part_path):
            return  # resuming from saved state
        if not self.path:
            # An interrupted download of this same link in this folder? Resume it without
            # asking the server anything (the name usually comes from the link).
            from .categories import name_from_url
            guess = os.path.join(self.dest_dir, self.filename or sanitize_filename(name_from_url(self.url)))
            if self._state_matches(guess):
                self.path = guess
                self.filename = os.path.basename(guess)
                if self._load_state() and self.segments:
                    return
        # The first request already asks for the first piece of the file: its answer tells
        # us size / name / range support AND its data is kept (no wasted round trip).
        conn = net.Connection(TIMEOUT)
        for attempt in range(1, FIRST_TRIES + 1):
            try:
                if self.post and (self._post_direct or self._final_url is None):
                    # (the range only reaches the address the form leads to, if any: see net)
                    resp = conn.get(self.url, self._headers_for(self.url), 0, FIRST_REQUEST - 1,
                                    cookies=self.cookies, post=self.post)
                    self._first_peer = conn.peer
                    self._post_direct = resp.posted
                    self._event("formulário: o ficheiro veio na própria resposta (uma ligação)" if resp.posted
                                else "formulário: levou a um endereço normal (várias ligações)")
                    break
                resp = conn.get(self._current_url(), self._headers_for(self._current_url()), 0,
                                FIRST_REQUEST - 1, cookies=self.cookies)
                self._first_peer = conn.peer  # the server address that answered
                break
            except net.HTTPError as exc:
                conn.close()
                if exc.code == 416 and exc.content_range.replace(" ", "").endswith("/0"):
                    # An empty file: servers that follow the rules (S3...) cannot send "the
                    # first bytes" of nothing. Ask for the whole (empty) file instead.
                    resp = conn.get(self._current_url(), self._headers_for(self._current_url()),
                                    cookies=self.cookies)
                    self._first_peer = conn.peer
                    break
                if self._final_url and exc.code in (401, 403, 404, 410):
                    # The browser's final address expired already: start again from the link.
                    self._final_url = None
                    continue
                if exc.code == 403 and not any(k.lower() == "user-agent" for k in self.headers):
                    # Our default browser-like name, without a real browser behind it, looks like
                    # a disguised robot to some sites (seen: SourceForge answers 403 to any
                    # browser name from a program, and 206 to an honest one). Say who we are,
                    # once; kept for this download (and its later resumes).
                    self.headers = {**self.headers, "User-Agent": HONEST_USER_AGENT}
                    continue
                if exc.code not in TRANSIENT_CODES or attempt == FIRST_TRIES:
                    raise
                self._stop.wait(min(exc.retry_after or 2 ** attempt, MAX_RETRY_AFTER))
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                if is_cert_error(exc):
                    raise RuntimeError(CERT_ERROR) from exc
                if attempt == FIRST_TRIES:
                    raise
                self._stop.wait(2 ** attempt)  # a network blip: try again
            if self._stop.is_set():
                raise RuntimeError("pausado")  # _run reports it as paused, not as an error
        else:
            # Every try used up without an answer (e.g. the browser's expired address was
            # dropped on the last try): one more try with the original link, then give up
            # with the server's real answer (before: an internal error, "resp" unset).
            resp = conn.get(self._current_url(), self._headers_for(self._current_url()), 0,
                            FIRST_REQUEST - 1, cookies=self.cookies)
            self._first_peer = conn.peer
        info = _info_from_response(resp)
        try:
            _check_expected(self.expect, info)
        except RuntimeError:
            conn.close()
            raise
        self._final_url = info["url"]
        if self._post_direct:
            # The form's own answer: one connection, no ranges, from the start on a restart.
            self._final_url = None
            info["resumable"] = False
            self.segments = []
        if self.segments and self.validator and info["validator"] and info["validator"] != self.validator:
            # an interrupted download of an older version of this file
            conn.close()
            raise FileChanged()
        self.validator = self.validator or info["validator"]
        self.size = info["size"]
        self.resumable = info["resumable"]
        if not self.filename:
            self.filename = info["filename"]
        if not self.path and self.choose_folder:
            self.dest_dir = self.choose_folder(self.filename, self.url)
            os.makedirs(self.dest_dir, exist_ok=True)
        if not self.path:
            candidate = os.path.join(self.dest_dir, self.filename)
            # Chosen and claimed in one step: downloads started together that get the same
            # name (same.bin) each pick a free one - they used to write into the same file.
            with _NAME_LOCK:
                if self._state_matches(candidate):
                    self.path = candidate  # continue an interrupted download of the same URL
                else:
                    self.path = unique_path(candidate)
                    try:
                        open(self.path + ".part", "ab").close()  # the name is taken from now on
                    except OSError:
                        pass  # the real error (no room, no folder) is reported when writing
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

    def _refresh_url(self, failed_url: str) -> bool:
        """Signed download links (GitHub, cloud storage...) expire after a while. Ask the
        original link again for a fresh one. Only once per expired address: connections that
        hit the same expired address meanwhile just use the new one.

        Returns True when the original link leads to the very same address and it answers:
        then the address had not expired (the server refused that one request, e.g. too many
        connections for a moment) and the caller just tries again a little later."""
        with self._refresh_lock:
            if self._current_url() != failed_url:
                return False  # another connection already got a fresh address
            if self.post:
                # (asking the form again could make another file: not behind the user's back)
                raise RuntimeError("O endereço dado pelo formulário expirou: baixe outra vez no site")
            done = sum(s.pos - s.start for s in self.segments)
            info = probe(self.url, self.headers, self.cookies)
            if info["size"] != self.size or (self.validator and info["validator"]
                                             and info["validator"] != self.validator):
                self._file_changed = True
                raise FileChanged()
            if info["url"] == failed_url:
                if done > self._progress_at_same:
                    self._same_refusals = 0
                else:
                    self._same_refusals += 1
                    if self._same_refusals > MAX_SAME_REFUSALS:
                        raise RuntimeError("O site recusa a ligação da app (HTTP 403)")
                self._progress_at_same = done
                return True
            if done > self._progress_at_refresh:
                self._fruitless_refreshes = 0  # the last fresh address was useful
            else:
                self._fruitless_refreshes += 1
                if self._fruitless_refreshes > MAX_FRUITLESS_REFRESHES:
                    raise RuntimeError("O link deixou de funcionar (o site recusa novos endereços)")
            self._progress_at_refresh = done
            self._final_url = info["url"]
            self._refreshes += 1
            return False

    def _check_room(self, needed: int | None) -> None:
        """Before writing a new file: is there room for it? Say so now, not at 97%."""
        if not needed:
            return
        from .system import free_space
        folder = os.path.dirname(os.path.abspath(self._part_path))
        free = free_space(folder)
        if free is not None and free < needed:
            raise DiskProblem(f"Falta espaço em {folder}: precisa de {format_size(needed)}, "
                              f"há {format_size(free)} livres. Liberte espaço e clique em Retomar")

    # -- single connection (server does not support ranges / unknown size)
    def _download_single(self) -> None:
        try:
            self._check_room(self.size)
        except DiskProblem:
            if self._first:
                self._first[0].close()
                self._first = None
            raise
        self.downloaded = 0
        self._active_workers = 1
        conn, resp = self._first or (net.Connection(TIMEOUT), None)
        self._first = None
        decoded = False
        try:
            for attempt in range(1, SINGLE_TRIES + 1):
                got = expected = 0
                try:
                    if resp is None or resp.status == 206:  # need the whole body in one answer
                        if self._post_direct:
                            resp = conn.get(self.url, self._headers_for(self.url), cookies=self.cookies,
                                            post=self.post)
                        else:
                            resp = conn.get(self._current_url(), self._headers_for(self._current_url()),
                                            cookies=self.cookies)
                    # A server may compress the file although we asked it not to (the browser
                    # would unpack it): unpack it too, or the saved file is the compressed one.
                    encoding = (resp.headers.get("Content-Encoding") or "").strip().lower()
                    inflate = (zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding in ("gzip", "x-gzip")
                               else zlib.decompressobj() if encoding == "deflate" else None)
                    decoded = inflate is not None
                    length = resp.headers.get("Content-Length")
                    expected = int(length) if length and length.strip().isdigit() else 0
                    self.downloaded = 0
                    with open(self._part_path, "wb") as f:
                        while not self._stop.is_set():
                            chunk = resp.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            got += len(chunk)
                            data = inflate.decompress(chunk) if inflate else chunk
                            try:
                                f.write(data)
                            except OSError as exc:
                                raise _disk(exc, self._part_path) from exc
                            self.downloaded += len(data)
                            self._update_speed()
                            self._throttle(len(chunk))
                        if inflate and not self._stop.is_set():
                            tail = inflate.flush()
                            f.write(tail)
                            self.downloaded += len(tail)
                    if not self._stop.is_set() and expected and got < expected:
                        # The connection ended before the size the server announced: a cut-off
                        # file must never be taken for the whole one.
                        raise http.client.IncompleteRead(b"", expected - got)
                    break
                except DiskProblem:
                    raise
                except (OSError, http.client.HTTPException, zlib.error) as exc:
                    if isinstance(exc, net.HTTPError) or self._stop.is_set():
                        raise
                    conn.close()
                    resp = None
                    if attempt == SINGLE_TRIES:
                        have = f"{format_size(got)} de {format_size(expected)}" if expected else format_size(got)
                        raise RuntimeError(
                            f"A ligação caiu antes do fim ({have}). Este servidor não deixa continuar "
                            "a meio: \"Tentar de novo\" recomeça do início") from exc
                    self._nap(2 * attempt)  # this server cannot continue: start over, after a pause
        finally:
            conn.close()
            self._active_workers = 0
        if decoded and not self._stop.is_set():
            self.size = self.downloaded  # the announced size was the compressed one
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
            try:
                self._check_room(self.size)
            except DiskProblem:
                if first is not None:
                    first[0].close()
                raise
            try:
                with open(self._part_path, "wb") as f:
                    # Windows: a sparse file, so writing far into it does not first make Windows
                    # fill everything before that point with zeros (several connections write
                    # at different places; on a hard disk that zero-filling cost seconds).
                    from .system import make_sparse
                    make_sparse(f)
                    f.truncate(self.size)
            except OSError as exc:
                raise _disk(exc, self._part_path) from exc

        threads: list[threading.Thread] = []
        last_save = time.monotonic()
        complete_since = None
        sync_fd = os.open(self._part_path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        # One file handle shared by all connections (writes take turns; the disk is far faster
        # than the network). The download can then end the moment every byte is there, without
        # waiting for a straggling connection: on Windows a file still open in another thread
        # cannot be renamed, which kept finished downloads waiting.
        self._fh = open(self._part_path, "r+b", buffering=0)
        self._fh_lock = threading.Lock()
        self._rescued: list = []
        self._workers = []

        # A server name with several addresses (CDNs): spread the connections over them, so
        # one busy machine or a per-address limit does not hold every connection back.
        url = self._current_url()
        parts = urllib.parse.urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        ips = [] if net._proxy_for(parts.scheme, parts.hostname or "") else net.addresses(parts.hostname or "", port)
        # Only addresses of the kind (IPv4 / IPv6) that the first connection really reached:
        # many computers list IPv6 addresses that do not work, and a connection sent there
        # would wait for nothing. The address that worked goes first.
        working = getattr(self, "_first_peer", None) if first is not None else None
        if working is None:
            ips = []  # nothing proven yet: let the system choose, as before
        else:
            ips = [working] + [ip for ip in ips if ip != working and (":" in ip) == (":" in working)]
        self.server_addresses = len(ips)
        turn = [1]  # the first connection (the initial request) is already on the first address

        def spawn(seg: Segment, conn=None, resp=None, rescuer: bool = False) -> None:
            if conn is None and len(ips) > 1 and host_of(self._current_url()) == (parts.hostname or "").lower():
                conn = net.Connection(TIMEOUT)
                conn.address = ((parts.hostname or "").lower(), ips[turn[0] % len(ips)])
                turn[0] += 1
            w = _Worker(seg, conn, resp)
            w.rescuer = rescuer
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
        known = self.host_memory.get(host_of(self._current_url())) if self.host_memory else None
        # (Stopped us again and again on an earlier download, a few hours ago at most.)
        self._punishing_host = bool(known and known.get("limit") and known.get("outage"))
        self._few_requests = bool(known and known.get("few_requests"))
        if known:
            self._event(f"memória do servidor: {known.get('conns')} ligações, limite {known.get('limit')}"
                        + (", já nos parou (pedidos longos)" if self._punishing_host else "")
                        + (", conta os pedidos (pedidos longos)" if self._few_requests else ""))
        if known:
            # This server taught us before: start where it paid off (and within its limit),
            # still measuring and adapting from there.
            if known.get("limit"):
                cap = min(cap, known["limit"])
            target = max(target, min(cap, known.get("conns", target)))
            ramping = target < cap
            self.connection_note = f"começa com {target} (conhecido)" if not ramping else "testando velocidade"
        self._settled_at = 0
        limit_refreshes = self._refreshes
        soft_cap = cap           # lowered after the server stopped for every connection
        outage_since = None      # when every connection went silent (None: data flowing)
        flowing_at = 0.0         # last moment the speed was at least 10% of the best
        calm_since = 0.0         # when data came back after the last such stop

        if first is not None:
            seg0 = self.segments[0]
            seg0.active = True
            seg0.inflight_end = min(seg0.end, int(re.match(r"bytes \d+-(\d+)/",
                                                           first[1].headers["Content-Range"]).group(1)))
            spawn(seg0, *first)
        # Resuming (no first answer yet in this run): one connection until the server has
        # accepted one - a server that allows few connections answers extra ones with 403,
        # which would look like an expired link and stop the download for good.
        self._accepted_any = first is not None
        allowed = lambda: target if self._accepted_any else 1  # noqa: E731
        for seg in self.segments:
            if seg.remaining > 0 and not seg.active and self._active_workers < allowed():
                seg.active = True
                spawn(seg)
        # The other first connections start now, not after the first half-second check (in
        # browser mode the file is one piece, so the loop above starts only one).
        while self._active_workers < allowed() and not self._stop.is_set():
            new_seg = self._split_largest()
            if new_seg is None:
                break
            spawn(new_seg)

        while True:
            self._wake.wait(0.5)
            self._wake.clear()
            self.downloaded = sum(s.pos - s.start for s in self.segments)
            self._update_speed()
            if self._worker_error or self._file_changed or self._disk_problem:
                self._stop.set()
            now = time.monotonic()
            if self._limit_hit:
                self.server_limit = max(1, self._limit_hit)
                self._limit_hit = 0
                limit_refreshes = self._refreshes
                target, ramping = self.server_limit, False
                self.connection_note = f"servidor aceita {self.server_limit}"
            elif self.server_limit is not None and self._refreshes != limit_refreshes:
                # The "limit" was the link expiring (refusals just before a fresh address):
                # forget it and test more connections again with the new address.
                self.server_limit = None
                ramping, ramp_speed = target < cap, None
                ramp_at = now + RAMP_INTERVAL
                self.connection_note = "testando velocidade" if ramping else ""
            elif ramping and now >= ramp_at and outage_since is None:
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
                    self._settled_at = self._active_workers
                    self.connection_note = f"{self._active_workers} conexões bastam"
                    recheck_at = now + RECHECK_INTERVAL
            elif (not ramping and self.server_limit is None and target < min(cap, soft_cap)
                  and recheck_at and now >= recheck_at):
                # Speed dropped a lot since the best moment? Test more connections again -
                # but not when it dropped to (almost) nothing: that is the server or the line
                # stopping, and more connections only get stuck too (seen: 8 -> 21 at 0 B/s).
                current = self._recent_speed(3.0)
                recheck_at = now + RECHECK_INTERVAL
                if best_speed and best_speed * 0.2 < current < best_speed * RECHECK_DROP:
                    ramp_speed, ramping = current, True
                    target = min(cap, target * 2)
                    ramp_at = now + RAMP_INTERVAL
                    self.connection_note = "testando velocidade"
            if self._samples and now - self._samples[0][0] >= 2.5:
                # Over a full window only: a short one right at the start (the first answer
                # arriving at once) gave a huge "best", and everything after looked like a stop.
                best_speed = max(best_speed, self._recent_speed(3.0))
            # Every connection silent for a while: the server (or the line) stopped. Some servers
            # do that to clients with "too many" connections, and on some lines many connections
            # stall together: use half as many for a while, as a browser-like client would.
            # (Not only total silence: such a server often lets a trickle through between stops.)
            recent = self._recent_speed(3.0)
            if not best_speed or recent >= best_speed * 0.1:
                flowing_at = now
            slowed = flowing_at and now - flowing_at > STALL_ALONE_SECONDS
            silent = now - self._last_rx if self._last_rx else 0.0
            self._in_outage = outage_since is not None
            self._outage_started = outage_since
            with self._lock:
                busy = [w for w in self._workers if w.alive and w.seg is not None
                        and w.seg.active and w.seg.remaining > 0]
            busy_now = len(busy)
            # The very end: what is left would take a few seconds at the best speed. Stuck pieces
            # there are rescued at once - never taken for the server stopping.
            left = sum(seg.remaining for seg in self.segments)
            # (Not on a server that already stopped every connection once: there, everyone
            # silent at the end is that server stopping again, and rescuing / reopening every
            # connection at once is what keeps it stopped - seen in a simulation, for minutes.)
            self._tail = left <= max(4 * 1024 * 1024, best_speed * 4) and not self._touchy()
            if self._tail and outage_since is not None:
                outage_since = None
            # Only while most connections still have work, and not at the very end.
            if ((silent > OUTAGE_SILENT_S or slowed) and busy_now >= max(2, target // 2) and not self._tail
                    and outage_since is None and not all(s.remaining == 0 for s in self.segments)):
                outage_since = now
                self.outages += 1
                self._last_outage_at = now
                self._event(f"paragem do servidor #{self.outages}: {self._active_workers} ligações, "
                            f"nada recebido há {silent:.0f} s")
                if target > 2:
                    target = soft_cap = max(2, target // 2)
                    ramping = False
                    self.connection_note = f"o servidor parou: {target} ligações"
            elif outage_since is not None and ((best_speed and recent >= best_speed * 0.3)
                                               or busy_now < max(2, target // 2)):
                outage_since = None  # data flowing again (or only the last pieces are left)
                calm_since = now
                self._event(f"voltou a receber: {self._active_workers} ligações")
            # (Known at once, in this same pass: the check below cuts silent connections, and
            # both fire at 6 s - before, every connection was sometimes cut and reopened in the
            # very pass that found the server had stopped us, straight back over its limit.)
            self._in_outage = outage_since is not None
            self._outage_started = outage_since
            if soft_cap < cap and outage_since is None and calm_since and now - calm_since > OUTAGE_CALM_S:
                soft_cap = cap  # a long calm: more connections may be tried again
                recheck_at = now
            if self._net_down:  # (said over the speed-test notes while nothing gets through)
                self.connection_note = "sem ligação ao servidor: a tentar de novo"
            self._target = target  # connections above this leave when they get the chance
            self._diag_row(now, target)
            if not self._stop.is_set():
                self._replace_slow_connections(now, target)
                while self._rescued:  # pieces taken from stuck connections: a fresh one each
                    spawn(self._rescued.pop(), rescuer=True)
                # Dynamic segmentation: keep `target` connections busy (not while the server has
                # stopped every connection: new ones would only add to what it punishes).
                while self._active_workers < allowed() and (not self._in_outage or self._active_workers == 0):
                    new_seg = self._split_largest()
                    if new_seg is None:
                        break
                    spawn(new_seg)
            if all(s.remaining == 0 for s in self.segments):
                self._naps.set()  # complete: no connection may keep it waiting
            if time.monotonic() - last_save > CHECKPOINT_S:
                self._checkpoint(sync_fd)
                last_save = time.monotonic()
            if not any(t.is_alive() for t in threads):
                break
            if all(s.remaining == 0 for s in self.segments):
                complete_since = complete_since or time.monotonic()
                if time.monotonic() - complete_since > 1.0:
                    break  # every byte is here: do not wait for a connection that is stuck
            else:
                complete_since = None

        with self._fh_lock:
            self._fh.close()  # a straggler that wakes up later finds it closed and just leaves
        self.downloaded = sum(s.pos - s.start for s in self.segments)
        if not all(s.remaining == 0 for s in self.segments):
            self._checkpoint(sync_fd)  # paused / stopped: where to continue (complete: not needed)
        os.close(sync_fd)
        if self.host_memory and all(s.remaining == 0 for s in self.segments) and self.size >= MEMORY_MIN_SIZE:
            # Remember for next time (only downloads long enough to have measured something).
            learned = self.server_limit or self._settled_at or target
            outage_limit = None
            if self.outages >= 2:  # stopped again and again with more: use fewer for a while
                outage_limit = soft_cap
                learned = min(learned, soft_cap)
            self.host_memory.put(host_of(self._current_url()), learned, self.server_limit, outage_limit)
        self._workers = []
        if self._file_changed:
            raise FileChanged()
        if self._disk_problem:
            raise self._disk_problem
        if self._worker_error:
            raise RuntimeError(self._worker_error)
        if all(s.remaining == 0 for s in self.segments):
            self._finish()
        else:
            self.status = Status.PAUSED

    def _create_segments(self, first_len: int = 0) -> None:
        n = max(1, min(self.connections, self.size // MIN_SPLIT_SIZE or 1))
        if self.mirror:
            # Shown in the browser's list, which can only receive the file from its start:
            # one piece, and new connections take the work right after the others
            # (_split_largest), so the browser's progress keeps up with the app's.
            n = 1
        step = self.size // n
        # Built aside and then put in place in one step: other threads (the window's map of
        # the parts, the progress) read the list without a lock and must never see it half done.
        segments = []
        for i in range(n):
            start = i * step
            end = self.size - 1 if i == n - 1 else (i + 1) * step - 1
            segments.append(Segment(start, end))
        if first_len > segments[0].end + 1:
            # The first answer already covers more than the first piece: grow piece 0 so
            # that connection is not cut, and shrink / drop the pieces it overlaps.
            seg0 = segments[0]
            seg0.end = min(first_len, self.size) - 1
            rest = []
            for s in segments[1:]:
                if s.end <= seg0.end:
                    continue
                if s.start <= seg0.end:
                    s.start = s.pos = seg0.end + 1
                rest.append(s)
            segments = [seg0] + rest
        self.segments = segments

    def _split_largest(self) -> Segment | None:
        """Work for a free connection: an idle piece, or the second half of the largest
        piece still to download (never cutting a request already on its way)."""
        with self._lock:
            idle = [s for s in self.segments if not s.active and s.remaining > 0]
            if idle:
                idle[0].active = True
                return idle[0]
            if self._in_outage:
                # The server stopped every connection: splitting work now would only cut
                # requests and open new connections (what such servers punish). Wait.
                return None
            if self.mirror and not self._few_requests:
                head = self._split_near_start()
                if head is not None:
                    return head
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

    def _split_near_start(self) -> Segment | None:
        """Browser-list downloads: give a free connection the work closest to the start of the
        file. The connection ahead keeps a few seconds of work and the new one takes what
        follows, so all connections work side by side near the start and the part the browser
        can already receive grows at the full speed. Called with self._lock held."""
        per_conn = self.speed / max(1, self._active_workers)
        # (2 s of work, but at least 1/REQUEST_BUDGET of the file: 2 s pieces of a 5 GB file
        # meant hundreds of requests, and thinkbroadband answers "too many requests" to that)
        keep = int(min(MAX_REQUEST, max(MIN_SPLIT_SIZE, per_conn * MIRROR_SECONDS,
                                        (self.size or 0) // REQUEST_BUDGET)))
        for s in sorted(self.segments, key=lambda x: x.start):
            with s.lock:
                free_from = max(s.pos, s.inflight_end + 1) if s.active else s.pos
                free = s.end - free_from + 1
                if free < keep + 2 * MIN_SPLIT_SIZE:
                    continue
                new = Segment(free_from + keep, s.end)
                s.end = free_from + keep - 1
            return self._add_segment(new)
        return None

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
            if remaining >= ENDGAME_MIN_PIECE + ENDGAME_KEEP and eta > best_eta:
                best, best_eta, best_speed = s, eta, speed
        if best is None:
            return None
        share = best_speed / (best_speed + max(fast, best_speed))  # slow one keeps this fraction
        with best.lock:
            if best.remaining < ENDGAME_MIN_PIECE + ENDGAME_KEEP:
                return None
            # The slow one keeps only what it finishes as soon as a fast one would (however
            # little); the piece taken is worth a new request (ENDGAME_MIN_PIECE at least).
            keep = max(ENDGAME_KEEP, min(best.remaining // 2, int(best.remaining * share)))
            keep = min(keep, best.remaining - ENDGAME_MIN_PIECE)
            mid = best.pos + keep
            new = Segment(mid, best.end)
            best.end = mid - 1
        return self._add_segment(new)

    def _add_segment(self, new: Segment) -> Segment:
        new.active = True
        # A new sorted list, put in place in one step. Sorting in place made the list look
        # EMPTY to threads reading it meanwhile (CPython empties a list while sorting it): the
        # download could then believe it was complete, or save an empty progress.
        self.segments = sorted(self.segments + [new], key=lambda s: s.start)
        return new

    def _shed_extra_connections(self, now: float, target: int | None) -> None:
        """The server stopped every connection, and still sends nothing after a few seconds:
        close the connections above the new (halved) number. Some servers only send again once
        fewer connections are open (seen: thinkbroadband, 13 open, 0 B/s for ever, while the
        app waited for them to wake up). Their pieces go back to the connections that stay."""
        started = self._outage_started
        if not self._in_outage or started is None or now - started < OUTAGE_SHED_S or not target:
            return
        with self._lock:
            # Retired ones (stuck, their piece already given to a fresh connection) are of no
            # use and still count at the server: always closed, and not counted as staying.
            leaving = [w for w in self._workers if w.alive and not w.shed and w.retired]
            staying = [w for w in self._workers if w.alive and not w.shed and not w.retired]
        if leaving:
            self._event(f"fechadas {len(leaving)} ligações presas já substituídas")
        for w in leaving:
            w.shed = w.reconnect = True
            if w.conn is not None:
                w.conn.abort()
        extra = len(staying) - max(1, target)
        if extra <= 0:
            return
        # Never the ones receiving right now (a probe that got through); then the ones that
        # brought the least go: the best connections stay.
        self._event(f"fechadas {extra} ligações a mais (fica com {max(1, target)})")
        for w in sorted(staying, key=lambda x: (now - x.last_data < 1.0, x.total))[:extra]:
            w.shed = w.retired = w.reconnect = True
            if w.conn is not None:
                w.conn.abort()

    def _replace_slow_connections(self, now: float, target: int | None = None) -> None:
        """A connection much slower than the others is often stuck on a slow server node.
        Ask it to reconnect (a new connection may land on a faster one)."""
        with self._lock:
            live = [w for w in self._workers if w.alive and w.got_data and now - w.started > SLOW_MIN_AGE]
        # Stalled: nothing received for a while (the server or the line stopped sending
        # without closing). Cut it at once, even if every connection is stalled: waiting for
        # the 30 s timeout froze the whole download.
        with self._lock:
            # (worker, its piece) read once: a worker may drop its piece at any moment
            busy = [(w, w.seg) for w in self._workers if w.alive and w.seg is not None and w.seg.active]
        receiving = sum(1 for w, _ in busy if now - w.last_data < 1.0)
        self._shed_extra_connections(now, target)
        if self._last_429 and now - self._last_429 < LIMITED_QUIET_S:
            # The server just said "too many requests": every replacement or reopened
            # connection is one more request for it to count (seen: more 429s, and the stop
            # went on). Wait: it sends again on the same connections when its block ends.
            return
        # When every connection seems stopped, ONE fresh connection tells whether the server
        # stopped (it gets nothing either: then wait) or only some connections are stuck (it
        # gets data at once: rescue goes on, one at a time). Never a crowd of new ones.
        with self._lock:
            probing = any(x.alive and x.rescuer and x.total < PROBE_OK_BYTES for x in self._workers)
        # Everyone has work and nobody receives: the line or the server stopped for all of us.
        # A fresh connection would get stuck too (rescuing then multiplied the connections, up
        # to hundreds at 0 B/s): only cut-and-reconnect, with back-off. When only a few
        # connections are left with work (the end of the download), rescuing them is right.
        silent_for = now - self._last_rx if self._last_rx else 0.0
        outage = not self._tail and receiving == 0 and (len(busy) >= max(2, target or self.connections)
                                     or (len(busy) >= 2 and silent_for > STALL_SECONDS))
        for w, seg in busy:
            if seg.remaining == 0 and not w.reconnect and now - w.last_data > 1.0:
                # Its piece was finished by another connection (end-game help) while it waits
                # for an answer it no longer needs: free it now instead of after the timeout.
                w.reconnect = True
                if w.conn is not None:
                    w.conn.abort()
                continue
            # Silent while the others receive: abnormal, act fast. Everyone silent (e.g. the
            # line dropped for a moment): give it longer. A connection replaced again and again
            # without receiving anything (a server slow to answer) waits twice as long each time.
            base = STALL_ALONE_SECONDS if receiving >= 2 else STALL_SECONDS
            if not w.answering:
                base = max(base, 3 * self._answer_time)
            limit = min(base * 2 ** w.quiet_swaps, TIMEOUT)
            quiet = now - w.last_data
            # After the server stopped us once: never above the number it took (a server that
            # punishes "too many" stopped again for the extra rescuers, near the end).
            touchy = self._touchy()
            room = min(self.connections, self._target) if touchy else self.connections
            extra = 0 if touchy else RESCUE_EXTRA
            # Rescue only outside an outage and within the maximum + RESCUE_EXTRA (and as many
            # stuck ones still leaving): never more connections than that.
            with self._lock:
                # (+ the fresh ones already promised in this pass: seen, 3 rescued at once with
                # 8 open -> 11, over the maximum)
                pending = len(self._rescued)
                working = sum(1 for x in self._workers if x.alive and not x.retired) + pending
                alive = sum(1 for x in self._workers if x.alive and not (x.retired and x.reconnect)) + pending
            # A rescuer that never received anything is not rescued in turn (only reconnected,
            # with back-off): otherwise every silent spell added a new generation of connections.
            if (quiet > base and now - w.rescued_at > base and not outage and not w.retired
                    and (not self._in_outage or not probing)
                    and not (w.rescuer and w.total < PROBE_OK_BYTES)
                    and working < room + extra
                    and alive < room + extra):  # (open ones, stuck included: never above the maximum)
                # Hand what it still has to receive to a fresh connection now, whether or not
                # the stuck one wakes up (on Windows it may not until its timeout). If it does
                # wake, its piece is empty and it simply takes other work. No back-off here:
                # the piece must keep moving even when this connection is not cut again yet.
                with seg.lock:
                    rest = Segment(seg.pos, seg.end) if seg.remaining > 0 else None
                    if rest is not None:
                        seg.end = seg.pos - 1
                if rest is not None:
                    w.rescued_at = now
                    w.retired = True  # when it wakes, it leaves: its replacement took its place
                    # ...and it is closed now: left open, stuck ones piled up at the server (seen
                    # on Windows: 15 open with a maximum of 8, and a server that stops everyone
                    # above its limit never sent again).
                    w.reconnect = True
                    self.reconnections += 1  # (a stuck connection replaced)
                    self._event(f"ligação parada há {quiet:.0f} s: substituída ({rest.remaining // MB} MB passam a outra)")
                    if w.conn is not None:
                        w.conn.abort()
                    with self._lock:
                        self._rescued.append(self._add_segment(rest))
                    if self._in_outage:
                        # Only ONE probe while the server has stopped everyone (before: one per
                        # silent connection in the same pass - straight back to the number the
                        # server had just stopped us for).
                        probing = True
            if self._in_outage and quiet < TIMEOUT:
                # The server stopped every connection: cutting them and opening new ones only
                # adds connections at the server (the old ones linger there), which is what
                # some servers punish. Wait: data usually comes back on the same connections.
                continue
            if quiet > limit and now - w.last_swap > limit and not w.reconnect:
                # Cut it and reconnect (waiting twice as long each time it brings nothing, so a
                # server that is merely slow to answer is not hammered).
                w.reconnect = True
                w.last_data = now
                w.quiet_swaps += 1
                self.reconnections += 1
                self._event(f"ligação parada há {quiet:.0f} s: cortada e reaberta")
                if w.conn is not None:
                    w.conn.abort()
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
        req_url = self.url
        conn = w.conn = w.conn or net.Connection(TIMEOUT)
        resp = w.resp
        seg = w.seg
        if resp is None:
            self._settle()  # (a server that stopped us before: see _settle)
        try:
            # Unbuffered: every chunk goes straight to the operating system, so nothing
            # sits in a Python buffer that would be lost if the app is killed.
            f = self._fh
            if True:
                while not self._stop.is_set():
                    if w.shed:
                        break  # closed during a long stop: its piece goes back to the others
                    if seg.remaining == 0 and resp is None:
                        seg.active = False
                        if w.retired:
                            break  # its piece went to a fresh connection: do not add one more
                        if self._active_workers > self._target and not self._in_outage:
                            break  # fewer connections wanted now (the server stopped us): leave
                        seg = w.seg = self._split_largest()  # keep the connection, take new work
                        if seg is None:
                            break
                        continue
                    try:
                        if resp is None:
                            size = w.request_size()
                            # (a new connection: the others' speed as a first guess)
                            est = w.speed() or self.speed / max(1, self._active_workers)
                            if self.mirror and est > 0:
                                # Browser-list downloads: short requests, so the next
                                # connection can take the work right after this one (a long
                                # request cannot be split and pushes it far from the start).
                                # Not before the connection's speed is known: tiny first
                                # requests kept new connections slow (seen: a 5 GB file started
                                # at ~6 MB/s and took 16 s longer than the browser).
                                size = min(size, max(MIRROR_REQUEST, int(est * MIRROR_SECONDS),
                                                     (self.size or 0) // REQUEST_BUDGET))
                            if self._few_requests or ((self._touchy() or self._punishing_host) and not self.mirror):
                                # A server that stopped us: one long request per connection (its
                                # whole piece), as other download managers do - many requests
                                # look like a robot to such servers (seen: thinkbroadband).
                                # (Not in browser-list mode unless the server said "too many
                                # requests": the browser reads from the start, and long requests
                                # there made it wait - 23 s -> 33 s simulated.)
                                size = seg.end - seg.pos + 1
                            req_end = min(seg.end, seg.pos + size - 1)
                            with seg.lock:
                                seg.inflight_end = req_end
                            req_url = self._current_url()
                            hdrs = self._headers_for(req_url)
                            if self.validator:
                                hdrs["If-Range"] = self.validator  # only bytes of the same version
                            w.answering = False
                            sent = w.last_data = time.monotonic()  # waiting for the answer starts now
                            self.requests += 1
                            resp = conn.get(req_url, hdrs, seg.pos, req_end, cookies=self.cookies)
                            w.answering = True
                            w.last_data = now_ = time.monotonic()
                            # How long this server takes to start answering (slow ones exist):
                            # a connection waiting for an answer is not cut before ~3x that.
                            self._answer_time = max(now_ - sent, self._answer_time * 0.9)
                            w.accepted = True
                            if not self._accepted_any:
                                self._accepted_any = True
                                self._wake.set()  # the others may start now
                            if resp.status != 206:
                                if self.validator:
                                    # If-Range said no: the file on the server is a new version
                                    self._file_changed = True
                                    raise FileChanged()
                                raise RuntimeError("O servidor parou de aceitar downloads em partes")
                            # The part must be the one asked for, of a file of the same size: a
                            # server that answers another part, or a file replaced meanwhile
                            # without If-Range support, would mix bytes from both silently.
                            m = re.match(r"\s*bytes\s+(\d+)-(\d+)/(\d+|\*)", resp.headers.get("Content-Range", ""))
                            if m and m.group(3) != "*" and self.size and int(m.group(3)) != self.size:
                                self._file_changed = True
                                raise FileChanged()
                            if not m or int(m.group(1)) != seg.pos:
                                raise http.client.HTTPException(
                                    f"o servidor enviou outra parte do ficheiro ({resp.headers.get('Content-Range')!r})")
                            self._final_url = resp.url
                        got_this = 0  # bytes this answer brought
                        while not self._stop.is_set():
                            with seg.lock:
                                want = min(seg.end, seg.inflight_end) - seg.pos + 1
                            if want <= 0:
                                break
                            # Whatever has arrived (up to a chunk): a slow but living connection
                            # shows progress at once, so it is never taken for a stalled one.
                            # (Measured: no more processor time than waiting for whole chunks.)
                            chunk = resp.read1(min(w.chunk_size(), want))
                            if not chunk:
                                break
                            with seg.lock:
                                last = min(seg.end, seg.inflight_end)
                                n = max(0, min(len(chunk), last - seg.pos + 1))
                                if n:
                                    with self._fh_lock:
                                        if f.closed:  # the download already ended without us
                                            raise _Finished()
                                        try:
                                            f.seek(seg.pos)
                                            f.write(chunk[:n])
                                        except OSError as exc:
                                            raise _disk(exc, self._part_path) from exc
                                    seg.pos += n
                                request_done = seg.pos > last
                            got_this += n
                            w.count(n)
                            self._last_rx = w.last_data
                            self._net_down = False
                            retries = 0
                            if not w.got_data:
                                w.got_data = True
                                with self._lock:
                                    self._streaming += 1
                                if self.first_byte_after is None:
                                    self.first_byte_after = time.monotonic() - self._run_started
                                self._wake.set()  # progress known at once (the browser waits for it)
                            self._throttle(n)
                            if request_done or w.reconnect:
                                break
                        with seg.lock:
                            seg.inflight_end = -1
                            unfinished = seg.remaining > 0
                        if not resp.finished:
                            conn.close()  # stopped mid-answer: this connection can't be reused
                        resp = None
                        if got_this == 0 and unfinished and not w.reconnect and not self._stop.is_set():
                            # An answer that ended without bringing anything (a server that lies
                            # about the length): wait before asking again, and give up in the
                            # end - it used to ask again thousands of times a second, for ever.
                            retries += 1
                            if retries > MAX_RETRIES:
                                raise RuntimeError("O servidor deixou de enviar o resto do ficheiro")
                            self._nap(min(2 ** retries, 30))
                            continue
                        if w.reconnect:
                            conn.close()
                            if w.shed:
                                break
                            self._settle()
                            w.reconnect = False
                            w.swaps += 1
                            w.last_swap = time.monotonic()
                            w.reset_speed()
                    except (net.HTTPError, OSError, http.client.HTTPException) as exc:
                        with seg.lock:
                            seg.inflight_end = -1
                        resp = None
                        conn.close()
                        if w.shed:
                            break
                        if w.reconnect and not isinstance(exc, net.HTTPError):
                            # We cut this connection ourselves (it had stopped receiving): just
                            # open a new one now, no waiting and no failed try counted -
                            # unless fewer connections are wanted now: then it leaves, and its
                            # piece goes back to the others.
                            if self._active_workers > self._target and not self._in_outage:
                                break
                            self._settle()
                            w.reconnect = False
                            w.swaps += 1
                            w.last_swap = w.last_data = time.monotonic()
                            w.reset_speed()
                            continue
                        code = exc.code if isinstance(exc, net.HTTPError) else None
                        self._event(f"erro numa ligação: {'' if code else type(exc).__name__ + ' '}"
                                    f"{str(exc)[:80]}"
                                    + (f" (esperar {exc.retry_after:.0f} s)" if getattr(exc, "retry_after", None) else ""))
                        refused = isinstance(exc, ConnectionRefusedError)
                        if conn.address and not isinstance(exc, net.HTTPError):
                            conn.address = None  # that address failed: let the system choose
                        if is_cert_error(exc):
                            raise RuntimeError(CERT_ERROR) from exc
                        wait = getattr(exc, "retry_after", None)
                        if code == 429:
                            self._last_429 = time.monotonic()
                            # "Too many requests": from now on one long request per connection
                            # (and remembered for this server). Asking again soon only adds to
                            # its count: wait longer than for other errors.
                            if not self._few_requests:
                                self._few_requests = True
                                self._event("o servidor conta os pedidos: passa a pedidos longos")
                                if self.host_memory:
                                    self.host_memory.note_few_requests(host_of(self._current_url()))
                            if wait is None:
                                retries += 1
                                if retries > MAX_RETRIES and time.monotonic() - (self._last_rx or self._run_started) > NETWORK_PATIENCE:
                                    raise RuntimeError(f"HTTP 429: {exc.reason}") from exc
                                self._nap(min(10.0 * retries, 60.0))
                                continue
                        if code in TRANSIENT_CODES and wait is not None:
                            # The server said when to come back: do exactly that, without using
                            # up a retry or concluding it limits connections.
                            self._nap(min(wait, MAX_RETRY_AFTER))
                            continue
                        if (code in LIMIT_CODES or refused) and not w.got_data and self._active_workers > 1:
                            # The server does not want one more connection from us. It may still
                            # be counting one we just closed, so try once more after a second;
                            # if refused again, that is its limit: hand the piece back (another
                            # connection will take it) instead of failing.
                            refusals += 1
                            if refusals == 1:
                                self._nap(1.0)
                                continue
                            self._limit_hit = max(self._limit_hit, self._streaming)
                            return
                        with self._lock:
                            others = sum(1 for x in self._workers if x is not w and x.alive and x.accepted)
                        if code == 403 and others >= 1 and req_url == self._current_url():
                            # Other connections are receiving from this very address: it has
                            # not expired. Some hosts answer 403 (not 429/503) to connections
                            # beyond their limit: treat it as that limit.
                            refusals += 1
                            if refusals == 1:
                                self._nap(1.0)
                                continue
                            self._limit_hit = max(self._limit_hit, self._streaming)
                            return
                        if code == 403 and not self._accepted_any and self.downloaded > 0 and refusals < 3:
                            # Resuming right after a pause: the server may still count the
                            # connections we just closed and refuse even the first new one.
                            # Not an expired link yet: wait a moment and ask again.
                            refusals += 1
                            self._nap(float(refusals))
                            continue
                        if code in (401, 403, 410) and req_url != self._current_url():
                            continue  # another connection already replaced the expired address
                        if code in (401, 403, 410):
                            # The address probably expired: get a fresh one (the whole download
                            # gives up only after several fresh addresses brought no progress).
                            try:
                                if self._refresh_url(req_url):  # same address, still good
                                    refusals += 1
                                    self._nap(min(3.0, 0.5 * refusals))
                                continue
                            except FileChanged:
                                raise
                            except Exception as refresh_error:  # noqa: BLE001
                                msg = str(refresh_error)
                                if "deixou de funcionar" not in msg:
                                    msg = f"O link deixou de funcionar ({msg or exc})"
                                raise RuntimeError(msg) from exc
                        if code == 416:  # the range is gone: the file got smaller, i.e. it changed
                            self._file_changed = True
                            raise FileChanged()
                        if code in (401, 403, 404, 410):
                            raise RuntimeError(f"HTTP {exc.code}: {exc.reason}") from exc
                        if code is None and time.monotonic() - (self._last_rx or self._run_started) > 10:
                            self._net_down = True  # (the window says so while it waits)
                        retries += 1
                        if retries > MAX_RETRIES:
                            # No way through at all (no internet, router restarting, the
                            # server's machine down): keep trying, every 30 s, for as long as
                            # NETWORK_PATIENCE without anything received - before, the download
                            # stopped with an error after ~2.5 min and stayed stopped once the
                            # line came back. A server's own answer (HTTP error) is not this.
                            since = self._last_rx or self._run_started
                            if code is None and time.monotonic() - since < NETWORK_PATIENCE:
                                self._net_down = True
                                self.connection_note = "sem ligação ao servidor: a tentar de novo"
                                self._nap(30)
                                continue
                            raise RuntimeError(f"Falha na conexão: {exc}") from exc
                        self._nap(min(2 ** retries, 30))
        except FileChanged:
            self._file_changed = True  # the download loop starts over with the new version
        except DiskProblem as exc:
            self._disk_problem = exc
        except _Finished:
            pass
        except Exception as exc:  # noqa: BLE001
            if not self._stop.is_set():  # an error caused by pausing (connection cut) is no error
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
        if now - t0 >= 2.0:  # a full window: no start-up spikes
            self.peak_speed = max(self.peak_speed, self.speed)
        if self.first_byte_after is None and self.downloaded > self._run_from:
            self.first_byte_after = now - self._run_started

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
        """The browser saved the file: remove our temporary copy. On Windows it may still be
        held for a moment (the antivirus scanning it, the stream closing): try again for a
        minute instead of leaving a copy as big as the file behind."""
        left = [p for p in (self._part_path, self._state_path) if not _remove_quietly(p)]
        self.leftovers = left  # kept in the list: removed at the next start if still there
        if left:
            def retry():
                for wait in (0.5, 1, 2, 4, 8, 15, 30):
                    time.sleep(wait)
                    left[:] = [p for p in left if not _remove_quietly(p)]
                    if not left:
                        return
            threading.Thread(target=retry, name="cleanup", daemon=True).start()
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
        try:
            os.replace(self._part_path, final)
        except OSError as exc:
            raise _disk(exc, final) from exc
        self.path = final
        self.filename = os.path.basename(final)
        if self.mark_internet:
            # Like a file saved by the browser: Windows still warns before running it.
            from .system import mark_from_internet
            mark_from_internet(final, self._current_url(), _headers(self.headers).get("Referer"))
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


def _remove_quietly(path: str) -> bool:
    """Delete a file; True when it is gone (or was never there)."""
    try:
        os.remove(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def format_time(sec: float | None) -> str:
    if sec is None:
        return "-"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s" if m else f"{s}s"
