"""User settings (kept free of GUI imports so the background service stays small)."""

from __future__ import annotations

import json

from .paths import config_dir, default_download_dir

DEFAULT_COLUMNS = ["name", "size", "progress", "speed", "eta", "conns", "status"]


class Settings:
    def __init__(self) -> None:
        self.path = config_dir() / "settings.json"
        self.dest_dir = default_download_dir()
        self.connections = 8
        self.max_active = 3
        self.watch_clipboard = True
        self.dark = False
        self.columns = list(DEFAULT_COLUMNS)
        self.global_limit_kb = 0
        self.browser_integration = True
        self.confirm_browser = False  # browser downloads start at once, with a progress window
        self.show_progress_window = True
        self.auto_resume = True
        self.run_in_background = True
        self.background_hint_shown = False
        self.scheduler: dict = {}
        self.category_folders = True   # save each category in its own folder (Downloads/Vídeos...)
        self.categories: list = []     # rules + folders; empty = the defaults (categories.default_rules)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.__dict__.update({k: v for k, v in data.items() if k != "path"})
        except (OSError, ValueError):
            pass

    def category_rules(self) -> list:
        from .categories import default_rules
        return self.categories or default_rules()

    def save(self) -> None:
        data = {k: v for k, v in self.__dict__.items() if k != "path"}
        try:
            self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError:
            pass
