"""File categories (like IDM's sidebar: Compressed, Documents, Music...).

Each category has rules (file types, and optionally address / name patterns) and a folder:
new downloads that match are saved there (by default a sub-folder of the download folder).
"""

from __future__ import annotations

import fnmatch
import os
import urllib.parse

CATEGORIES: dict[str, set[str]] = {
    "Compactados": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "tgz", "iso", "cab", "zst"},
    "Documentos": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "txt",
                   "rtf", "epub", "csv"},
    "Músicas": {"mp3", "wav", "flac", "aac", "ogg", "m4a", "wma", "opus"},
    "Programas": {"exe", "msi", "apk", "dmg", "pkg", "deb", "rpm", "appimage", "bat", "jar"},
    "Vídeos": {"mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "3gp", "ts", "ogv"},
    "Imagens": {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "tiff", "ico", "heic"},
}
OTHER = "Outros"


def default_rules() -> list[dict]:
    """The editable category list kept in the settings."""
    return [{"name": name, "exts": sorted(exts), "folder": None, "url_patterns": [], "name_patterns": []}
            for name, exts in CATEGORIES.items()]


def extension_of(name: str | None) -> str:
    if not name:
        return ""
    return os.path.splitext(name.split("?")[0])[1].lower().lstrip(".")


def _matches(pattern: str, text: str) -> bool:
    """A pattern without * or ? matches anywhere in the text; with them it is a wildcard."""
    pattern, text = pattern.strip().lower(), text.lower()
    if not pattern:
        return False
    if "*" in pattern or "?" in pattern:
        return fnmatch.fnmatch(text, pattern) or fnmatch.fnmatch(text, "*" + pattern + "*")
    return pattern in text


def rule_matches(rule: dict, name: str | None, url: str = "", page: str = "") -> bool:
    """A category matches when any of its rules does: the file type, an address pattern (checked
    against the download and the page address) or a file name pattern."""
    if extension_of(name) and extension_of(name) in {e.lower().lstrip(".") for e in rule.get("exts", [])}:
        return True
    for pattern in rule.get("url_patterns", []):
        if any(_matches(pattern, u) for u in (url, page) if u):
            return True
    for pattern in rule.get("name_patterns", []):
        if name and _matches(pattern, name):
            return True
    return False


def pick_category(name: str | None, url: str = "", page: str = "", rules: list | None = None) -> str:
    for rule in rules if rules is not None else default_rules():
        if rule_matches(rule, name, url, page):
            return rule["name"]
    return OTHER


def category_for(name: str | None, rules: list | None = None) -> str:
    return pick_category(name, rules=rules)


def folder_for(base: str, name: str | None, url: str = "", page: str = "", rules: list | None = None) -> str:
    """Where a new download of this file goes: its category's folder, or the download folder."""
    rules = rules if rules is not None else default_rules()
    category = pick_category(name, url, page, rules)
    if category == OTHER:
        return base
    rule = next(r for r in rules if r["name"] == category)
    return rule.get("folder") or os.path.join(base, category)


def name_from_url(url: str) -> str:
    return urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path))
