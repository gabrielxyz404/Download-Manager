"""What each server taught us: how many connections paid off, and whether it limits them.

Like a person who remembers that a site is fast with 8 connections, or only accepts 2: the
next download from the same server starts there instead of testing again from scratch.
A tiny JSON file; entries older than 30 days are forgotten (servers change).
"""

from __future__ import annotations

import json
import threading
import time

MAX_AGE = 30 * 24 * 3600
MAX_HOSTS = 500


class HostMemory:
    def __init__(self, path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict | None = None

    def _load(self) -> dict:
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, AttributeError):
                self._data = {}
        return self._data

    def get(self, host: str) -> dict | None:
        with self._lock:
            entry = self._load().get((host or "").lower())
        if not entry or time.time() - entry.get("time", 0) > MAX_AGE:
            return None
        return entry

    def put(self, host: str, connections: int, limit: int | None) -> None:
        host = (host or "").lower()
        if not host or connections < 1:
            return
        with self._lock:
            data = self._load()
            data[host] = {"conns": int(connections), "limit": int(limit) if limit else None, "time": time.time()}
            if len(data) > MAX_HOSTS:  # keep the most recent
                for old in sorted(data, key=lambda h: data[h].get("time", 0))[:len(data) - MAX_HOSTS]:
                    del data[old]
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data), encoding="utf-8")
                tmp.replace(self.path)
            except OSError:
                pass


def parse_limits(text: str) -> dict:
    """User's per-site limits, one per line: "site = connections" (e.g. "lento.com = 2").
    The whole site counts (www. and sub-domains). Bad lines are ignored."""
    out = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        host, _, n = line.partition("=")
        host = host.strip().lower().removeprefix("www.")
        try:
            n = int(n.strip())
        except ValueError:
            continue
        if host and " " not in host and "/" not in host and 1 <= n <= 32:
            out[host] = n
    return out


def limit_for(limits: dict, url: str) -> int | None:
    """The user's connection limit for this address's site, if any."""
    import urllib.parse
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    for site, n in (limits or {}).items():
        if host == site or host.endswith("." + site):
            return n
    return None
