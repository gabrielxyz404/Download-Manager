"""Site grabber: list downloadable files linked from a web page."""

from __future__ import annotations

import urllib.parse
import urllib.request
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


def grab(start_url: str, categories: set[str] | None = None, depth: int = 0,
         max_pages: int = 30) -> list[dict]:
    """Return [{url, name, category}] for files found on ``start_url``.

    ``depth=1`` also visits pages on the same site linked from the start page.
    ``categories`` filters by category name (None = all except plain pages).
    """
    final, _, html = _fetch(start_url)
    host = urllib.parse.urlparse(final).netloc
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
                    if categories is None or cat in categories:
                        found.setdefault(link, {"url": link, "name": name, "category": cat})
                elif level < depth and urllib.parse.urlparse(link).netloc == host \
                        and link not in visited and len(visited) < max_pages:
                    visited.add(link)
                    next_pages.append(link)
        pages = []
        for link in next_pages:
            try:
                u, _, body = _fetch(link)
            except Exception:  # noqa: BLE001 - skip unreachable pages
                continue
            if body:
                pages.append((u, body))
        level += 1
    return list(found.values())
