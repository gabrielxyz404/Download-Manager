"""Which browser downloads the app takes over (like IDM's file type list)."""

from __future__ import annotations

import fnmatch
import mimetypes
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
                  "application/pdf", "application/x-zip-compressed", "application/x-zip",
                  "application/x-rar", "application/vnd.rar", "application/gzip", "application/x-gzip",
                  "application/x-tar", "application/x-bzip2", "application/x-xz", "application/zstd",
                  "application/x-msi", "application/x-ms-installer", "application/x-apple-diskimage",
                  "application/java-archive", "application/x-debian-package", "binary/octet-stream",
                  "application/force-download", "application/download", "application/x-download")


# Content types whose file type the system tables may not know (e.g. Windows servers' zip type).
MIME_TYPES = {
    "application/zip": "zip", "application/x-zip-compressed": "zip", "application/x-zip": "zip",
    "application/x-rar-compressed": "rar", "application/x-rar": "rar", "application/vnd.rar": "rar",
    "application/x-7z-compressed": "7z", "application/gzip": "gz", "application/x-gzip": "gz",
    "application/x-tar": "tar", "application/x-bzip2": "bz2", "application/x-xz": "xz",
    "application/zstd": "zst", "application/x-msdownload": "exe", "application/x-msdos-program": "exe",
    "application/x-msi": "msi", "application/x-ms-installer": "msi",
    "application/vnd.android.package-archive": "apk", "application/x-apple-diskimage": "dmg",
    "application/x-iso9660-image": "iso", "application/x-debian-package": "deb",
    "application/java-archive": "jar", "application/pdf": "pdf", "application/epub+zip": "epub",
}

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
    # The browser often does not know the name yet (links like /download?id=7): use the type of
    # content the server announced, checked against the same list when it maps to a file type.
    mime = (mime or "").split(";")[0].strip().lower()
    ext = MIME_TYPES.get(mime) or (mimetypes.guess_extension(mime) or "").lstrip(".")
    if ext:
        return ext in types
    return mime.startswith(("video/", "audio/")) or mime in DOWNLOAD_MIMES
