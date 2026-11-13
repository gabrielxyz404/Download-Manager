"""Which browser downloads the app takes over (like IDM's file type list)."""

from __future__ import annotations

import fnmatch
import urllib.parse

from .categories import extension_of, name_from_url

DEFAULT_TYPES = (
    "3gp 7z aac apk appimage avi bin bz2 cab deb dmg docx epub exe flac flv gz img iso jar "
    "m4a m4v mkv mov mp3 mp4 mpeg mpg msi msu ogg ogv opus pdf pkg pptx rar rpm tar tgz "
    "wav webm wma wmv xlsx xz z zip zst"
).split()

# Types that mean "a file to save" when the name says nothing.
DOWNLOAD_MIMES = ("application/octet-stream", "application/zip", "application/x-msdownload",
                  "application/x-msdos-program", "application/vnd.android.package-archive",
                  "application/x-7z-compressed", "application/x-rar-compressed", "application/x-iso9660-image",
                  "application/pdf")


# Script / page addresses (download.php?file=x.zip): their "extension" says nothing about the file.
NOT_A_TYPE = {"php", "asp", "aspx", "jsp", "cgi", "pl", "py", "do", "action", "html", "htm", "shtml", "cfm"}


def host_matches(pattern: str, host: str) -> bool:
    """'exemplo.com' also covers its sub-domains; '*' and '?' work as wildcards."""
    pattern, host = pattern.strip().lower(), host.lower()
    if not pattern:
        return False
    if "*" in pattern or "?" in pattern:
        return fnmatch.fnmatch(host, pattern)
    return host == pattern or host.endswith("." + pattern)


def should_capture(settings, url: str, final_url: str = "", filename: str = "", mime: str = "",
                   page: str = "") -> bool:
    hosts = {(urllib.parse.urlsplit(u).hostname or "").lower() for u in (url, final_url, page) if u}
    for pattern in getattr(settings, "capture_skip_sites", []):
        if any(host_matches(pattern, h) for h in hosts if h):
            return False
    if getattr(settings, "capture_all_types", False):
        return True
    types = {t.lower().lstrip(".") for t in getattr(settings, "capture_types", DEFAULT_TYPES)}
    for name in (filename, name_from_url(final_url) if final_url else "", name_from_url(url)):
        ext = extension_of(name)
        if ext and ext not in NOT_A_TYPE:
            return ext in types
    mime = (mime or "").split(";")[0].strip().lower()
    return mime.startswith(("video/", "audio/")) or mime in DOWNLOAD_MIMES
