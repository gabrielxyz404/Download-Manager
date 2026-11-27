"""Site grabber: list downloadable files linked from a web page."""

from __future__ import annotations

import fnmatch
import re
import time
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser

from .categories import OTHER, category_for
from .engine import USER_AGENT

MAX_PAGE_BYTES = 5 * 1024 * 1024
LINK_ATTRS = {"a": "href", "img": "src", "video": "src", "audio": "src", "source": "src",
              "embed": "src", "link": "href"}


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr = LINK_ATTRS.get(tag)
        if not attr:
            return
        d = dict(attrs)
        if d.get(attr):
            self.links.append(d[attr])
        if tag in ("img", "source") and d.get("srcset"):
            for part in d["srcset"].split(","):
                if part.strip():
                    self.links.append(part.strip().split()[0])


def _fetch(url: str) -> tuple[str, str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        ctype = resp.headers.get_content_type()
        charset = resp.headers.get_content_charset() or "utf-8"
        body = resp.read(MAX_PAGE_BYTES).decode(charset, "replace") if ctype in (
            "text/html", "application/xhtml+xml") else ""
        return resp.geturl(), ctype, body


def extract_links(html: str, base_url: str) -> list[str]:
    parser = _LinkParser()
    parser.feed(html)
    out, seen = [], set()
    for link in parser.links:
        absolute = urllib.parse.urljoin(base_url, link.strip())
        absolute = urllib.parse.urldefrag(absolute)[0]
        if absolute.startswith(("http://", "https://", "ftp://")) and absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def _patterns(text) -> list[str]:
    return [p.strip().lower() for p in re.split(r"[,;\s]+", text or "") if p.strip()]


def _matches(link: str, name: str, patterns: list[str]) -> bool:
    """A pattern with * / ? (like "*.pdf" or "*/thumbs/*") against the name or the address."""
    low_link, low_name = link.lower(), name.lower()
    return any(fnmatch.fnmatchcase(low_name, p) or fnmatch.fnmatchcase(low_link, p) for p in patterns)


def _in_scope(link: str, start: urllib.parse.SplitResult, scope: str) -> bool:
    u = urllib.parse.urlsplit(link)
    if scope == "any":
        return True
    if u.netloc.lower() != start.netloc.lower():
        return False
    if scope == "folder":
        folder = start.path.rsplit("/", 1)[0] + "/"
        return u.path.startswith(folder)
    return True


class _Robots:
    """robots.txt of each site, read once (pages it forbids to robots are not visited)."""

    def __init__(self):
        self.cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        u = urllib.parse.urlsplit(url)
        key = f"{u.scheme}://{u.netloc}"
        if key not in self.cache:
            rp = urllib.robotparser.RobotFileParser()
            try:
                req = urllib.request.Request(key + "/robots.txt", headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    rp.parse(resp.read(512 * 1024).decode("utf-8", "replace").splitlines())
            except Exception:  # noqa: BLE001 - none (or unreachable): everything allowed
                rp = None
            self.cache[key] = rp
        rp = self.cache[key]
        return rp is None or rp.can_fetch("FlowDownloads", url)


def grab(start_url: str, categories: set[str] | None = None, depth: int = 0,
         max_pages: int = 30, include: str = "", exclude: str = "", scope: str = "site",
         robots: bool = True, pause: float = 0.3, time_limit: float = 120.0,
         on_progress=None) -> list[dict]:
    """Return [{url, name, category}] for files found on ``start_url``.

    ``depth`` (0-3): how many levels of linked pages are visited too, within ``scope``:
    "site" (the same site), "folder" (this folder and below) or "any" site.
    ``categories`` filters by category name (None = all except plain pages); ``include`` /
    ``exclude``: patterns like "*.pdf" or "*/thumbs/*" (name or address).
    Polite with the sites: one page at a time with a short pause, robots.txt respected
    (unless ``robots`` is off), at most ``max_pages`` pages and ``time_limit`` seconds.
    """
    depth = max(0, min(3, int(depth)))
    inc, exc = _patterns(include), _patterns(exclude)
    rules = _Robots() if robots else None
    deadline = time.monotonic() + time_limit
    final, _, html = _fetch(start_url)
    start = urllib.parse.urlsplit(final)
    pages = [(final, html)]
    found: dict[str, dict] = {}
    visited = {final}
    level = 0
    while pages:
        next_pages = []
        for page_url, body in pages:
            for link in extract_links(body, page_url):
                name = urllib.parse.unquote(urllib.parse.urlparse(link).path.rsplit("/", 1)[-1])
                cat = category_for(name)
                if cat != OTHER:
                    if ((categories is None or cat in categories) and (not inc or _matches(link, name, inc))
                            and not (exc and _matches(link, name, exc))):
                        found.setdefault(link, {"url": link, "name": name, "category": cat})
                elif (level < depth and link not in visited and len(visited) < max_pages
                      and link.startswith(("http://", "https://")) and _in_scope(link, start, scope)
                      and not (exc and _matches(link, name, exc))):
                    visited.add(link)
                    next_pages.append(link)
        pages = []
        for link in next_pages:
            if time.monotonic() > deadline:
                break
            if rules is not None and not rules.allowed(link):
                continue
            if on_progress:
                on_progress(len(visited), len(found))
            time.sleep(pause)  # (one page at a time, a moment apart: polite with the site)
            try:
                u, _, body = _fetch(link)
            except Exception:  # noqa: BLE001 - skip unreachable pages
                continue
            if body:
                pages.append((u, body))
        level += 1
    return list(found.values())


def sizes(items: list[dict], limit: int = 200, workers: int = 4) -> None:
    """Fill item["size"] (when the server says it) for the first ``limit`` files."""
    from concurrent.futures import ThreadPoolExecutor
    from .engine import probe

    def one(item):
        try:
            item["size"] = probe(item["url"])["size"]
        except Exception:  # noqa: BLE001 - unknown size
            item["size"] = None
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(one, [i for i in items[:limit] if i["url"].startswith(("http://", "https://"))]))
