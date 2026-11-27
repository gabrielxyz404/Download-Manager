"""FTP / FTPS downloads, with resume and several connections (like other download managers).

Browsers no longer open ftp:// links, so these come pasted or from the grabber. The task has
the same interface as engine.DownloadTask (list, details, pause / resume, saved list).

- Size (SIZE) and version (MDTM) of the file: a file that changed on the server while paused
  is downloaded again from the start (never two versions mixed).
- Resume and pieces: REST <offset> before RETR. FTP has no "up to" byte: a connection reads
  until the end of its piece and then drops the transfer (ABOR).
- Several connections (2-4: FTP servers often limit connections per address). A "421 too many
  connections" is taken as the server's limit: fewer connections, not an error.
- No SIZE / REST: one connection, from the start, as for HTTP servers without ranges.
"""

from __future__ import annotations

import ftplib
import json
import os
import socket
import ssl
import threading
import time
import urllib.parse
import uuid
from collections import deque

from . import vault
from .categories import category_for
from .engine import (_NAME_LOCK, CHECKPOINT_S, GLOBAL_LIMITER, Segment, Status, _retry_in_use,
                     sanitize_filename, unique_path)
from .limiter import RateLimiter

FTP_CONNECTIONS = 4  # at most (servers often limit connections per address)
MIN_PIECE = 2 * 1024 * 1024  # pieces smaller than this are not split again
CHUNK = 256 * 1024
TIMEOUT = 30
MAX_TRIES = 8


class _TooMany(ftplib.error_temp):
    """421: the server takes no more connections from us."""


def split_url(url: str) -> dict:
    p = urllib.parse.urlsplit(url)
    if p.scheme.lower() not in ("ftp", "ftps") or not p.hostname:
        raise ValueError("link FTP inválido")
    return {"tls": p.scheme.lower() == "ftps", "host": p.hostname, "port": p.port or 21,
            "user": urllib.parse.unquote(p.username or "") or "anonymous",
            "password": urllib.parse.unquote(p.password or "") if p.username else "anonymous@",
            "path": urllib.parse.unquote(p.path or "/")}


def clean_url(url: str) -> str:
    """The link without its password (that one is kept encrypted, apart)."""
    p = urllib.parse.urlsplit(url)
    if p.password is None:
        return url
    host = p.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{p.username}@{host}" + (f":{p.port}" if p.port else "")
    return urllib.parse.urlunsplit((p.scheme, netloc, p.path, p.query, ""))


class FtpTask:
    kind = "ftp"

    def __init__(self, url: str, dest_dir: str, connections: int = 4, filename: str | None = None,
                 task_id: str | None = None, speed_limit: int = 0, password: str | None = None):
        info = split_url(url)  # (raises for a broken link)
        self.id = task_id or "f" + uuid.uuid4().hex[:15]
        self._password = password if password is not None else info["password"]
        self.url = clean_url(url)
        self.dest_dir = dest_dir
        self.connections = max(1, min(FTP_CONNECTIONS, int(connections)))
        self.filename = sanitize_filename(filename) if filename else (
            sanitize_filename(os.path.basename(info["path"].rstrip("/"))) or "download")
        self.path: str | None = None
        self.size: int | None = None
        self.downloaded = 0
        self.speed = 0.0
        self.status = Status.QUEUED
        self.error: str | None = None
        self.resumable = False
        self.validator: str | None = None  # "<size>|<MDTM>": this version of the file
        self.segments: list[Segment] = []
        self.added_at = time.time()
        self.limiter = RateLimiter(speed_limit)
        self.server_limit: int | None = None
        self.connection_note = ""
        self.restarted_changed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._samples: deque = deque()
        self._active = 0
        self._socks: list = []
        self.on_done = None
        self.on_change = None
        # (as for HTTP: Windows' "came from the internet" mark, and the end-of-download actions)
        self.mark_internet = True
        self.after_ran = False
        self.expected_hash: str | None = None
        self.check = ""

    # ------------------------------------------------------------- interface
    @property
    def progress(self) -> float:
        if self.status == Status.COMPLETED:
            return 1.0
        return min(1.0, self.downloaded / self.size) if self.size else 0.0

    @property
    def eta(self) -> float | None:
        if not self.size or self.speed <= 0:
            return None
        return max(0.0, (self.size - self.downloaded) / self.speed)

    @property
    def active_connections(self) -> int:
        return self._active

    @property
    def category(self) -> str:
        return category_for(self.filename or self.url)

    @property
    def speed_limit(self) -> int:
        return int(self.limiter.rate)

    @speed_limit.setter
    def speed_limit(self, value: int) -> None:
        self.limiter.rate = max(0, int(value))

    @property
    def _part_path(self) -> str:
        return (self.path or os.path.join(self.dest_dir, self.filename)) + ".part"

    @property
    def _state_path(self) -> str:
        return self._part_path + ".json"

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running() and self._stop.is_set():
            self._thread.join(10)
        if self.is_running() or self.status == Status.COMPLETED:
            return
        self._stop.clear()
        self.error = None
        self.status = Status.CONNECTING
        self._thread = threading.Thread(target=self._run, name=f"ftp-{self.id}", daemon=True)
        self._thread.start()

    def pause(self) -> None:
        if self.is_running():
            self._stop.set()
            with self._lock:
                socks = list(self._socks)
            for s in socks:  # wake connections waiting for data
                try:
                    s.close()
                except OSError:
                    pass
        elif self.status in (Status.QUEUED, Status.CONNECTING):
            self.status = Status.PAUSED

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def diagnostic(self) -> str:
        return (f"FTP {urllib.parse.urlsplit(self.url).hostname}  tamanho: {self.size}  estado: {self.status}"
                + (f"  erro: {self.error}" if self.error else "")
                + f"\nligações: {self.connections}  limite do servidor: {self.server_limit}")

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "id": self.id, "url": self.url, "dest_dir": self.dest_dir,
            "connections": self.connections, "filename": self.filename, "path": self.path,
            "size": self.size, "downloaded": self.downloaded, "status": self.status,
            "added_at": self.added_at, "speed_limit": self.speed_limit,
            "check": self.check, "expected_hash": self.expected_hash,
            # (anonymous needs nothing; a real password only encrypted, and not once done)
            "secret": vault.protect_cached(self._password)
            if self._password and self._password != "anonymous@" and self.status != Status.COMPLETED else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FtpTask":
        password = vault.unprotect(d.get("secret"), None) if d.get("secret") else None
        t = cls(d["url"], d["dest_dir"], d.get("connections", 4), d.get("filename"), d.get("id"),
                d.get("speed_limit", 0), password if isinstance(password, str) else None)
        t.path, t.size = d.get("path"), d.get("size")
        t.downloaded = d.get("downloaded", 0) if isinstance(d.get("downloaded"), int) else 0
        t.added_at = d.get("added_at", t.added_at)
        t.after_ran = d.get("status") == Status.COMPLETED  # (never again for an old download)
        t.check = d.get("check", "") if isinstance(d.get("check"), str) else ""
        t.expected_hash = d.get("expected_hash") if isinstance(d.get("expected_hash"), str) else None
        status = d.get("status")
        t.status = status if status in (Status.COMPLETED, Status.ERROR) else Status.PAUSED
        return t

    # ------------------------------------------------------------- connections
    def _login_patiently(self) -> ftplib.FTP:
        """The first connection: a server that says "421 too many" (ours, just closed, may
        still count) is asked again a few times before giving up."""
        for attempt in range(6):
            try:
                return self._login()
            except _TooMany:
                if attempt == 5 or self._stop.is_set():
                    raise
                self._stop.wait(5)
        raise _TooMany("421")

    def _login(self) -> ftplib.FTP:
        info = split_url(self.url)
        cls = ftplib.FTP_TLS if info["tls"] else ftplib.FTP
        kwargs = {"context": ssl.create_default_context()} if info["tls"] else {}
        ftp = cls(timeout=TIMEOUT, **kwargs)
        try:
            ftp.connect(info["host"], info["port"])
            user = info["user"]
            ftp.login(user, self._password if user != "anonymous" else (self._password or "anonymous@"))
            if info["tls"]:
                ftp.prot_p()  # (the file itself encrypted too, not only the password)
            ftp.voidcmd("TYPE I")
        except ftplib.error_temp as exc:
            ftp.close()
            if str(exc).startswith("421"):
                raise _TooMany(str(exc)) from exc
            raise
        except BaseException:
            ftp.close()
            raise
        return ftp

    # ------------------------------------------------------------- the download
    def _run(self) -> None:
        try:
            for attempt in (1, 2):
                self._prepare()
                if self._stop.is_set():
                    self.status = Status.PAUSED
                    return
                self.status = Status.DOWNLOADING
                if self._changed:
                    if attempt == 2:
                        raise RuntimeError("O ficheiro está sempre a mudar no servidor")
                    continue
                if self.resumable and self.size:
                    self._download_pieces()
                else:
                    self._download_whole()
                break
            if self._stop.is_set():
                self.status = Status.PAUSED
                return
            self._finish()
        except (ftplib.error_perm, ftplib.error_temp, ftplib.error_proto, OSError, EOFError, RuntimeError) as exc:
            if self._stop.is_set():
                self.status = Status.PAUSED
            else:
                self.error = _message(exc)
                self.status = Status.ERROR
        finally:
            self.speed = 0.0
            self._active = 0
            if self.on_done:
                self.on_done(self)

    def _prepare(self) -> None:
        """Size, version and resume support; the saved progress if it is the same file."""
        self._changed = False
        info = split_url(self.url)
        ftp = self._login_patiently()
        try:
            try:
                size = ftp.size(info["path"])
            except (ftplib.error_perm, ftplib.error_temp):
                size = None
            try:
                mdtm = ftp.sendcmd("MDTM " + info["path"]).split(" ", 1)[1].strip()
            except (ftplib.error_perm, ftplib.error_temp, IndexError):
                mdtm = ""
            try:
                rest_ok = ftp.sendcmd("REST 0").startswith("350")
            except (ftplib.error_perm, ftplib.error_temp):
                rest_ok = False
        finally:
            try:
                ftp.quit()
            except (OSError, EOFError, ftplib.Error):
                ftp.close()
        version = f"{size}|{mdtm}"
        os.makedirs(self.dest_dir, exist_ok=True)
        if not self.path:
            with _NAME_LOCK:
                self.path = unique_path(os.path.join(self.dest_dir, self.filename))
                open(self.path + ".part", "ab").close()
            self.filename = os.path.basename(self.path)
            if self.on_change:
                self.on_change(self)
        saved = self._load_state()
        if saved and saved.get("validator") != version and saved.get("validator"):
            self._discard()  # another version of the file: start again
            self.restarted_changed = True
            saved = None
        self.size, self.validator = size, version
        self.resumable = bool(size and rest_ok)
        if saved and self.resumable:
            self.segments = [Segment(a, b, p) for a, b, p in saved["segments"]]
        else:
            self.segments = []
        if not self.segments and self.resumable:
            n = max(1, min(self.connections, self.size // MIN_PIECE or 1))
            step = -(-self.size // n)
            self.segments = [Segment(i, min(i + step, self.size) - 1) for i in range(0, self.size, step)]
            with open(self._part_path, "r+b") as f:
                f.truncate(self.size)
        self.downloaded = sum(s.pos - s.start for s in self.segments)

    def _load_state(self) -> dict | None:
        try:
            with open(self._state_path, encoding="utf-8") as f:
                state = json.load(f)
            if isinstance(state, dict) and isinstance(state.get("segments"), list) and os.path.exists(self._part_path):
                return state
        except (OSError, ValueError):
            pass
        return None

    def _save_state(self) -> None:
        if not self.resumable:
            return
        fh = getattr(self, "_fh", None)
        if fh is not None:
            # The bytes onto the disk first, then the positions (a power cut must never leave a
            # record that claims bytes that were not stored - as the HTTP engine does).
            try:
                with self._fh_lock:
                    if not fh.closed:
                        os.fsync(fh.fileno())
            except OSError:
                return
        tmp = self._state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"validator": self.validator, "segments": [s.to_list() for s in self.segments]}, f)
            os.replace(tmp, self._state_path)
        except OSError:
            pass

    def _discard(self) -> None:
        for p in (self._part_path, self._state_path):
            try:
                os.remove(p)
            except OSError:
                pass
        open(self._part_path, "ab").close()

    def _count(self, n: int) -> None:
        now = time.monotonic()
        with self._lock:
            self.downloaded += n
            self._samples.append((now, self.downloaded))
            while self._samples and now - self._samples[0][0] > 3:
                self._samples.popleft()
            if len(self._samples) > 1 and now > self._samples[0][0]:
                self.speed = (self._samples[-1][1] - self._samples[0][1]) / (now - self._samples[0][0])
        delay = max(GLOBAL_LIMITER.reserve(n), self.limiter.reserve(n))
        if delay > 0:
            self._stop.wait(delay)

    def _download_whole(self) -> None:
        """One connection, from the start (no size or no REST)."""
        info = split_url(self.url)
        self.downloaded = 0
        ftp = self._login_patiently()
        self._active = 1
        try:
            sock = ftp.transfercmd("RETR " + info["path"])
            with self._lock:
                self._socks.append(sock)
            with _retry_in_use(lambda: open(self._part_path, "wb")) as f:
                while not self._stop.is_set():
                    data = sock.recv(CHUNK)
                    if not data:
                        break
                    f.write(data)
                    self._count(len(data))
            sock.close()
            if not self._stop.is_set():
                ftp.voidresp()
        finally:
            with self._lock:
                self._socks.clear()
            ftp.close()
        if self._stop.is_set():
            return
        if self.size and self.downloaded != self.size:
            raise RuntimeError("O servidor FTP enviou o ficheiro incompleto")
        self.size = self.downloaded

    def _download_pieces(self) -> None:
        self._fh = _retry_in_use(lambda: open(self._part_path, "r+b", buffering=0))
        self._fh_lock = threading.Lock()
        self._errors: list = []
        threads: list[threading.Thread] = []
        try:
            def spawn(seg):
                seg.active = True
                t = threading.Thread(target=self._worker, args=(seg,), daemon=True)
                threads.append(t)
                t.start()

            for seg in [s for s in self.segments if s.remaining > 0][:self.connections]:
                spawn(seg)
            last_save = time.monotonic()
            while True:
                time.sleep(0.2)
                with self._lock:
                    alive = sum(1 for t in threads if t.is_alive())
                    self._active = alive
                allowed = min(self.connections, self.server_limit or self.connections)
                while alive < allowed and not self._stop.is_set() and not self._errors:
                    seg = self._next_piece()
                    if seg is None:
                        break
                    spawn(seg)
                    alive += 1
                if time.monotonic() - last_save > CHECKPOINT_S:
                    self._save_state()
                    last_save = time.monotonic()
                if self._errors and not alive:
                    raise self._errors[0]
                if not alive and (self._stop.is_set() or all(s.remaining == 0 for s in self.segments)):
                    break
                if not alive and self._next_piece(peek=True) is None and any(s.remaining for s in self.segments):
                    # (every connection refused: try again in a moment)
                    self._stop.wait(2)
        finally:
            with self._fh_lock:
                self._fh.close()
            self.downloaded = sum(s.pos - s.start for s in self.segments)
            self._save_state()

    def _next_piece(self, peek: bool = False) -> Segment | None:
        """An idle piece, or the second half of the biggest one still downloading."""
        with self._lock:
            idle = [s for s in self.segments if not s.active and s.remaining > 0]
            if idle:
                if not peek:
                    idle[0].active = True
                return idle[0]
            best = max((s for s in self.segments if s.remaining >= 2 * MIN_PIECE), key=lambda s: s.remaining,
                       default=None)
            if best is None:
                return None
            if peek:
                return best
            with best.lock:
                mid = best.pos + best.remaining // 2
                new = Segment(mid, best.end)
                best.end = mid - 1
            new.active = True
            self.segments = sorted(self.segments + [new], key=lambda s: s.start)
            return new

    def _worker(self, seg: Segment) -> None:
        info = split_url(self.url)
        tries = 0
        try:
            while seg.remaining > 0 and not self._stop.is_set():
                try:
                    ftp = self._login()
                except _TooMany:
                    # The server's limit: one connection fewer from now on; the piece waits.
                    with self._lock:
                        self.server_limit = max(1, self._active - 1)
                        self.connection_note = f"servidor aceita {self.server_limit}"
                    return
                except (OSError, EOFError, ftplib.error_temp, ftplib.error_reply, ftplib.error_proto):
                    tries += 1  # (a network hiccup while connecting: again, a moment later)
                    if tries > MAX_TRIES:
                        raise
                    self._stop.wait(min(2 ** tries, 30))
                    continue
                sock = None
                try:
                    sock = ftp.transfercmd("RETR " + info["path"], rest=seg.pos)
                    with self._lock:
                        self._socks.append(sock)
                    try:
                        while not self._stop.is_set():
                            with seg.lock:
                                want = seg.end - seg.pos + 1
                            if want <= 0:
                                break  # piece done (or taken over in part): drop the rest
                            try:
                                data = sock.recv(min(CHUNK, want))
                            except OSError:
                                data = b""  # (dropped: continued below from where it stopped)
                            if not data:
                                break
                            with seg.lock:
                                n = min(len(data), seg.end - seg.pos + 1)
                                with self._fh_lock:
                                    if self._fh.closed:
                                        return
                                    self._fh.seek(seg.pos)
                                    self._fh.write(data[:n])
                                seg.pos += n
                            self._count(n)
                            tries = 0
                    finally:
                        with self._lock:
                            if sock in self._socks:
                                self._socks.remove(sock)
                        sock.close()
                except (OSError, EOFError, ftplib.error_temp, ftplib.error_reply, ftplib.error_proto):
                    pass  # (a dropped connection, "425 can't open data connection"...: again, below)
                finally:
                    try:
                        if sock is not None:  # (a transfer was going: end it properly)
                            ftp.abort() if seg.remaining > 0 or self._stop.is_set() else ftp.voidresp()
                    except (OSError, EOFError, ftplib.Error):
                        pass
                    ftp.close()
                if seg.remaining > 0 and not self._stop.is_set():
                    tries += 1  # cut before the end of the piece: again, from where it stopped
                    if tries > MAX_TRIES:
                        raise RuntimeError("A ligação FTP caiu demasiadas vezes")
                    self._stop.wait(min(2 ** tries, 30))
        except (ftplib.Error, OSError, EOFError, RuntimeError) as exc:
            if not self._stop.is_set():
                self._errors.append(exc)
        finally:
            seg.active = False

    def _finish(self) -> None:
        final = self.path
        part = self._part_path
        if os.path.exists(final):
            final = unique_path(final)
        _retry_in_use(lambda: os.replace(part, final))
        try:
            os.remove(self._state_path)
        except OSError:
            pass
        self.path, self.filename = final, os.path.basename(final)
        self.downloaded = self.size or self.downloaded
        if self.mark_internet:
            from .system import mark_from_internet
            mark_from_internet(final, clean_url(self.url))
        self.status = Status.COMPLETED


def _message(exc: BaseException) -> str:
    text = str(exc)
    if isinstance(exc, ftplib.error_perm):
        if text.startswith("530"):
            return "O servidor FTP recusou o utilizador ou a senha"
        if text.startswith("550"):
            return "O ficheiro não existe no servidor FTP (ou não há permissão)"
    if isinstance(exc, socket.timeout):
        return "O servidor FTP não respondeu"
    return f"FTP: {text or type(exc).__name__}"
