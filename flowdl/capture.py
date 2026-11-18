"""Which browser downloads the app takes over (like IDM's file type list)."""

from __future__ import annotations

import fnmatch
import re
import mimetypes
import urllib.parse

from .categories import extension_of, name_from_url

DEFAULT_TYPES = (
    "3gp 7z aac apk appimage avi bin bz2 cab deb dmg docx epub exe flac flv gz img iso jar "
    "m4a m4v mkv mov mp3 mp4 mpeg mpg msi msu ogg ogv opus pdf pkg pptx rar rpm tar tgz "
    "wav webm wma wmv xlsx xz z zip zst"
).split()

# Small everyday files the browser handles well, even when a server sends them as a plain
# "file" (application/octet-stream): never taken just because of that.
KEEP_IN_BROWSER = set(
    "txt csv json xml html htm css js ics vcf eml torrent srt vtt sub ass log ini cfg md "
    "jpg jpeg png gif webp svg ico bmp avif heic".split())

# Types that mean "a file to save" when the name says nothing.
# Servers' "just a file" types: they map to ".bin", which says nothing about the file - so
# removing "bin" from the list must not stop these downloads from being taken.
GENERIC_MIMES = ("application/octet-stream", "binary/octet-stream", "application/force-download",
                 "application/download", "application/x-download", "application/binary")
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


def skipped_site(settings, *urls: str) -> str | None:
    """The entry of the "never from these sites" list that covers one of these addresses.
    An entry starting with "!" is an exception ("!drive.google.com" next to "google.com")."""
    hosts = {(urllib.parse.urlsplit(u).hostname or "").lower() for u in urls if u}
    entries = list(getattr(settings, "capture_skip_sites", []))
    allowed = [p[1:] for p in entries if p.startswith("!")]
    for pattern in entries:
        if pattern.startswith("!"):
            continue
        covered = [h for h in hosts if h and host_matches(pattern, h)]
        if any(not any(host_matches(a, h) for a in allowed) for h in covered):
            return pattern
    return None


def set_site_skipped(settings, host: str, skipped: bool) -> bool:
    """Add / remove a site from the "never from these sites" list (www. is ignored, so the
    whole site counts). Returns True if the list changed."""
    host = (host or "").strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    # Only a real site name from the popup (before: "*" or any text was stored, and "*"
    # stopped the app taking any download at all).
    if not re.fullmatch(r"[a-z0-9_-]+(\.[a-z0-9_-]+)*|\[[0-9a-f:.]+\]", host) or len(host) > 253:
        return False
    sites = list(getattr(settings, "capture_skip_sites", []))
    before = list(sites)
    if skipped:
        sites = [p for p in sites if p != "!" + host]  # an exception for it goes away
        settings.capture_skip_sites = sites
        if not skipped_site(settings, "https://" + host + "/"):
            sites.append(host)
        settings.capture_skip_sites = sites
        return sites != before
    else:
        # Only this site's own entries go; a rule for a bigger site (google.com when this is
        # drive.google.com) stays, with an exception for this one. (Before: turning one
        # sub-site back on removed the whole google.com rule.)
        kept = [p for p in sites if p.lstrip("!") not in (host, "www." + host)]
        settings.capture_skip_sites = kept
        if skipped_site(settings, "https://" + host + "/"):
            kept.append("!" + host)
        if kept == sites:
            return False
        sites = kept
    settings.capture_skip_sites = sites
    return True


def should_capture(settings, url: str, final_url: str = "", filename: str = "", mime: str = "",
                   page: str = "") -> bool:
    if skipped_site(settings, url, final_url, page):
        return False
    if getattr(settings, "capture_all_types", False):
        return True
    types = {t.lower().lstrip(".") for t in getattr(settings, "capture_types", DEFAULT_TYPES)}
    mime = (mime or "").split(";")[0].strip().lower()
    for name in (filename, name_from_url(final_url) if final_url else "", name_from_url(url)):
        ext = extension_of(name)
        if ext and ext not in NOT_A_TYPE:
            if ext in types:
                return True
            # A type nobody chose or removed (".dat", ".vhdx", ".pak"...) that the server sends as
            # "just a file to save": a download, so the app takes it. Seen: a 5 GB ".dat" test
            # file stayed with the browser. Types removed from the list still stay with it.
            return ext not in DEFAULT_TYPES and ext not in KEEP_IN_BROWSER and mime in GENERIC_MIMES
    # The browser often does not know the name yet (links like /download?id=7): use the type of
    # content the server announced, checked against the same list when it maps to a file type.
    if mime in GENERIC_MIMES:
        return True  # "some file" with no name to judge by: a download (the browser saves it too)
    ext = MIME_TYPES.get(mime) or (mimetypes.guess_extension(mime) or "").lstrip(".")
    if ext:
        return ext in types
    return mime.startswith(("video/", "audio/")) or mime in DOWNLOAD_MIMES
