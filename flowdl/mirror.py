"""Show app downloads in the browser's own download list.

The browser downloads from the app (http://127.0.0.1:17890/stream/<token>) while the
app downloads from the internet with many connections. Bytes are handed to the browser
as soon as they are available in order, so the browser shows the transfer, its progress
and, at the end, a normal finished download saved in the browser's folder.
"""

from __future__ import annotations

import os
import re
import socket
import time
import urllib.parse

from .status import Status

BLOCK = 1024 * 1024
WAIT_FOR_START_S = 600  # the download may wait in the queue before it starts


def content_disposition(name: str) -> str:
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "").replace("?", "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(name)}"


def _send_error(conn: socket.socket, code: int, reason: str) -> None:
    try:
        conn.sendall(f"HTTP/1.1 {code} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
    except OSError:
        pass


def serve(conn: socket.socket, task, headers: dict, is_alive) -> bool:
    """Send ``task``'s file to the browser as it downloads. Returns True if the browser
    received the whole file. ``is_alive()`` says whether the task still exists."""
    conn.settimeout(120)
    f = None
    try:
        deadline = time.monotonic() + WAIT_FOR_START_S
        while task.path is None:  # the first answer gives the name and size
            if task.status == Status.ERROR or not is_alive() or time.monotonic() > deadline:
                _send_error(conn, 404, "Not Found")
                return False
            time.sleep(0.05)
        size = task.size
        if task.status == Status.COMPLETED:
            done_path = task.data_path()
            if not done_path or not os.path.isfile(done_path):
                # Asked again after the browser already has the file (the app's copy is gone):
                # say so, instead of promising the whole file and sending nothing.
                _send_error(conn, 404, "Not Found")
                return False
        m = re.match(r"bytes=(\d+)-", headers.get("range", ""))
        if size == 0:
            m = None  # an empty file: nothing to resume, always the whole (empty) answer
        if size is None:
            # No size from the server (one stream from the start): sent as it arrives, ended by
            # closing. Before: held until the whole download finished, and given up after 10 min.
            m = None
        pos = int(m.group(1)) if m else 0  # the browser resumes with a Range request
        if m and size is not None and pos >= size and size > 0:
            _send_error(conn, 416, "Range Not Satisfiable")  # nothing left from there
            return False
        lines = [
            "HTTP/1.1 206 Partial Content" if m else "HTTP/1.1 200 OK",
            "Content-Type: application/octet-stream",
            "Content-Disposition: " + content_disposition(task.filename or "download"),
            "Accept-Ranges: bytes",
            f'ETag: "{task.stream_token}"',  # lets the browser resume after a pause
            "Last-Modified: " + time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(task.added_at)),
            "Cache-Control: no-store",
            "Connection: close",
        ]
        if size is not None:
            lines.append(f"Content-Length: {size - pos}")
            if m:
                lines.append(f"Content-Range: bytes {pos}-{size - 1}/{size}")
        conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        while True:
            available = task.available_prefix()
            if pos < available:
                if f is None:
                    path = task.data_path()
                    if not path:
                        time.sleep(0.2)
                        continue
                    # Unbuffered: a buffered reader keeps read-ahead bytes, which may still be
                    # the zeros of a part not downloaded yet; it would hand those out later.
                    f = open(path, "rb", buffering=0)
                f.seek(pos)
                data = f.read(min(BLOCK, available - pos))  # never beyond what is downloaded
                if not data:
                    time.sleep(0.05)
                    continue
                conn.sendall(data)
                pos += len(data)
                continue
            if task.status == Status.COMPLETED and pos >= (size if size is not None else task.available_prefix()):
                return True
            if task.status == Status.ERROR or not is_alive():
                return False  # closing the connection makes the browser show a failed download
            time.sleep(0.05)  # caught up: the browser is at most this far behind the app
    except OSError:
        return False  # the browser paused / cancelled / closed
    finally:
        if f is not None:
            f.close()
        try:
            conn.close()
        except OSError:
            pass
