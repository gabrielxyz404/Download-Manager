"""File categories (like IDM's sidebar: Compressed, Documents, Music...)."""

from __future__ import annotations

import os

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


def category_for(name: str | None) -> str:
    if not name:
        return OTHER
    ext = os.path.splitext(name.split("?")[0])[1].lower().lstrip(".")
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return OTHER
