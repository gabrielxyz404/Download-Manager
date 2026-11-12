"""Where PyDM keeps its files. Kept dependency-free so the background service stays tiny."""

from __future__ import annotations

import os
from pathlib import Path


def default_download_dir() -> str:
    return str(Path.home() / "Downloads")


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    path = Path(base) / "pydm"
    path.mkdir(parents=True, exist_ok=True)
    return path
