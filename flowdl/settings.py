"""User settings (kept free of GUI imports so the background service stays small)."""

from __future__ import annotations

import json
import os
import threading

from .paths import config_dir, default_download_dir

_SAVE_LOCK = threading.Lock()
DEFAULT_COLUMNS = ["name", "size", "progress", "speed", "eta"]  # the details panel shows the rest
OLD_DEFAULT_COLUMNS = ["name", "size", "progress", "speed", "eta", "conns", "status"]  # up to 0.8.0


NULLABLE = {"theme"}  # None is a real value here (older versions)


def _same_kind(value, default, key: str) -> bool:
    if value is None:
        return default is None or key in NULLABLE
    if default is None:
        return True
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(value, bool) and isinstance(default, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and value == value and abs(value) != float("inf")
    return isinstance(value, type(default))


def _valid_category(c) -> bool:
    return (isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"].strip() != ""
            and isinstance(c.get("exts", []), list) and all(isinstance(e, str) for e in c.get("exts", []))
            and all(isinstance(c.get(k, []), list) and all(isinstance(x, str) for x in c.get(k, []))
                    for k in ("url_patterns", "name_patterns"))
            and (c.get("folder") is None or isinstance(c.get("folder"), str)))


def keep_damaged(path) -> None:
    """Keep a copy of a file that could not be read, so nothing is lost for good."""
    try:
        import shutil
        shutil.copyfile(path, path.with_name(path.name + ".bad"))
    except OSError:
        pass


def clean_queues(queues) -> list:
    """Only well-formed queues (a hand-edited file must not break the app), names unique."""
    out, ids = [], {"main"}
    for q in queues if isinstance(queues, list) else []:
        if not (isinstance(q, dict) and isinstance(q.get("id"), str) and q["id"] and q["id"] not in ids
                and isinstance(q.get("name"), str) and q["name"].strip()):
            continue
        n = q.get("max_active")
        n = n if isinstance(n, int) and not isinstance(n, bool) else 2
        ids.add(q["id"])
        out.append({"id": q["id"], "name": q["name"].strip()[:40], "max_active": max(1, min(20, n)),
                    "scheduler": q["scheduler"] if isinstance(q.get("scheduler"), dict) else {}})
    return out


class Settings:
    def __init__(self) -> None:
        self.path = config_dir() / "settings.json"
        self.dest_dir = default_download_dir()
        self.connections = 8
        self.max_active = 3
        self.watch_clipboard = True
        self.dark = False
        self.theme = "auto"  # "auto" (like Windows), "claro", "escuro" (None, from older versions: "dark")
        self.filter_style = "side"   # filters: "side" (bar on the left) or "pills" (above the list)
        self.show_details = True     # details of the chosen download on the right
        self.show_parts_map = True   # ... with the map of the parts
        self.columns = list(DEFAULT_COLUMNS)
        self.global_limit_kb = 0
        self.limit_on = True         # the global limit can be switched off/on from the tray icon
        self.show_tray = True        # icon next to the Windows clock
        self.check_updates = True    # once a day, look for a new version (installed app only)
        self.update_checked = 0.0    # when it last looked
        self.tray_notify = True      # Windows notification when a download finishes
        self.browser_integration = True
        self.confirm_browser = False  # browser downloads start at once, with a progress window
        self.show_progress_window = True
        # Browser downloads the app takes also appear in the browser's own list, saved in the
        # browser's folder (off: the app keeps them, like other download managers - one copy
        # written to disk, and no second "Save as" when the browser asks where to save).
        self.browser_list = True
        self.auto_resume = True
        self.run_in_background = True
        self.background_hint_shown = False
        self.scheduler: dict = {}  # (the main queue's schedule)
        # Other queues, each with its own limit and schedule:
        # [{"id": "q1", "name": "Noturna", "max_active": 2, "scheduler": {...}}]
        self.queues: list = []
        # Proxy (see proxy.py): "system" (Windows settings), "none", "http" or "socks5"
        self.proxy_mode = "system"
        self.proxy_host = ""
        self.proxy_port = 0
        self.proxy_user = ""
        self.proxy_pass = ""        # encrypted (vault), never in plain text
        self.proxy_remote_dns = True  # SOCKS5: the proxy looks names up (Tor)
        self.proxy_bypass = ["localhost", "127.0.0.1", "::1"]
        # Browser downloads the app takes over: these types (like IDM), or all of them; never
        # from these sites.
        from .capture import DEFAULT_TYPES
        self.capture_types = list(DEFAULT_TYPES)
        self.capture_all_types = False
        self.capture_skip_sites: list = []
        # When a download finishes (all off by default; a checksum given for a download is
        # always verified)
        self.after_open = False      # open the file
        self.after_extract = False   # extract .zip files next to them
        self.after_scan = False      # scan with the Windows antivirus
        self.after_command = ""      # run a command: {ficheiro} {pasta} {nome} {url}
        self.host_limits: dict = {}  # max connections per site, e.g. {"lento.com": 2}
        self.keep_awake = True       # no sleep while downloading (Windows)
        self.mark_internet = True    # Windows warns before running a downloaded program, as with the browser
        self.category_folders = True   # save each category in its own folder (Downloads/Vídeos...)
        self.categories: list = []     # rules + folders; empty = the defaults (categories.default_rules)
        self._load()

    def _load(self) -> None:
        """Read the saved settings. A damaged file (hand edits, a disk problem) never stops the
        app: each value of the wrong kind keeps its default, and a file that cannot be read at
        all is kept aside as settings.json.bad (before: the app closed at every start, or the
        next save silently reset every setting)."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = ""  # not text at all: damaged (kept aside below)
        except OSError:
            return
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            try:  # a complete list followed by stray bytes (a disk hiccup): keep the list
                data = json.JSONDecoder().raw_decode(text.lstrip())[0]
            except (ValueError, RecursionError):
                data = None
            keep_damaged(self.path)
        if not isinstance(data, dict):
            keep_damaged(self.path)
            return
        for k, v in data.items():
            if k == "path" or k.startswith("_"):
                continue
            if k in self.__dict__ and not _same_kind(v, self.__dict__[k], k):
                continue  # wrong kind of value: keep the default
            self.__dict__[k] = v
        self.columns = [c for c in self.columns if isinstance(c, str)] or list(DEFAULT_COLUMNS)
        self.categories = [c for c in self.categories if _valid_category(c)]
        self.capture_types = [t for t in self.capture_types if isinstance(t, str)]
        self.capture_skip_sites = [h for h in self.capture_skip_sites if isinstance(h, str)]
        self.host_limits = {h: n for h, n in self.host_limits.items()
                            if isinstance(h, str) and isinstance(n, int) and not isinstance(n, bool) and n >= 1}
        self.connections = max(1, min(32, int(self.connections)))
        self.max_active = max(1, min(20, int(self.max_active)))
        self.global_limit_kb = max(0, int(self.global_limit_kb))
        self.queues = clean_queues(self.queues)
        if self.proxy_mode not in ("system", "none", "http", "socks5"):
            self.proxy_mode = "system"
        self.proxy_port = self.proxy_port if 0 < self.proxy_port < 65536 else 0
        self.proxy_bypass = [b for b in self.proxy_bypass if isinstance(b, str)]

    def category_rules(self) -> list:
        from .categories import default_rules
        return self.categories or default_rules()

    def save(self) -> None:
        """Written to a temporary file and then put in place: a crash or two saves at the
        same time can never leave a broken file (which reset every setting at the next start)."""
        with _SAVE_LOCK:
            try:
                data = {k: v for k, v in dict(self.__dict__).items() if k != "path"}
                text = json.dumps(data, indent=1)
            except (RuntimeError, TypeError, ValueError):
                return
            tmp = self.path.with_name(self.path.name + ".tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                pass
