"""Background service: the part of Flow Downloads that stays running when no window is open.

Design rules (the app must be very light in the background):
  * No CPU while idle: the browser bridge sleeps in accept() and the main thread
    sleeps on an event queue. Nothing polls.
  * Small memory: only tiny modules are loaded at start. The download engine is
    imported when there is something to download, Tkinter when a window is needed.
  * When the window is closed and nothing is downloading, the process restarts
    itself in its slim form, so the memory used by the window goes back to the system.
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from .bridge import DEFAULT_PORT, BrowserBridge
from .limiter import GLOBAL_LIMITER
from .paths import config_dir
from .scheduler import Scheduler
from .settings import Settings
from .status import ACTIVE, Status

SCHEDULER_CHECK_S = 30
DEBUG = bool(os.environ.get("FLOWDL_DEBUG"))


def log(*args) -> None:
    """Diagnostics, only when the FLOWDL_DEBUG environment variable is set. Goes to the
    console, or to debug.log in the settings folder when there is no console (the .exe)."""
    if not DEBUG:
        return
    import time
    line = f"[flowdl {os.getpid()} {time.strftime('%H:%M:%S')}] " + " ".join(str(a) for a in args)
    try:
        from .paths import FROZEN
        if sys.stderr is not None and not FROZEN:
            print(line, file=sys.stderr, flush=True)
        else:
            with open(config_dir() / "debug.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass
REQUEST_WAIT_S = 8         # the extension asks "has it started?" and we answer within 8 s (it asks
                           # again; short answers keep few of the browser's 6 connections to us busy)
NO_BROWSER_AFTER_S = 30    # finished but the browser never came for the file: keep it here
BROWSER_GONE_AFTER_S = 120  # the browser stopped receiving and did not come back
SLIM_AFTER_IDLE_S = 5  # restart to free memory only after this many quiet seconds
ROOT = Path(__file__).resolve().parent.parent


def browser_request(item: dict) -> tuple[dict, dict]:
    """Headers and per-site cookies for a download handed over by the browser."""
    headers = {"Referer": item["referrer"]} if item.get("referrer") else {}
    if item.get("user_agent"):  # exactly the browser's, so the site sees the same visitor
        headers["User-Agent"] = item["user_agent"]
    if item.get("accept_language"):
        headers["Accept-Language"] = item["accept_language"]
    headers.update(item.get("browser_headers") or {})
    cookies = dict(item.get("cookies_by_host") or {})
    if item.get("cookies") and not cookies:  # older extension: cookies of the link's site
        from .engine import host_of
        cookies[host_of(item["url"])] = item["cookies"]
    return headers, cookies


NOTIFY_LEFT_MIN = 20 * 1024 * 1024  # files left to the browser that deserve a word


class Service:
    def __init__(self, settings: Settings | None = None, manager=None, port: int = DEFAULT_PORT):
        self.settings = settings or Settings()
        self.events: queue.Queue = queue.Queue()
        self._manager = None
        if manager is not None:
            self._attach(manager)
        self.scheduler = Scheduler(self.settings.scheduler)  # (the main queue's)
        self.apply_proxy()
        self.apply_queues()  # the other queues' (limits and schedules)
        GLOBAL_LIMITER.rate = self.settings.global_limit_kb * 1024 if getattr(self.settings, "limit_on", True) else 0
        self.tray = None  # icon next to the Windows clock (see start_tray)
        self.port = port
        self.bridge: BrowserBridge | None = None
        self.pending: list[dict] = []  # browser requests waiting for a window
        self.exit_requested = False
        self.slim_enabled = False  # set by run(): allow restarting to free memory
        # Downloads shown in the browser too (see mirror.py)
        self.requests: dict[str, dict] = {}  # request id from the extension -> state
        self._req_cond = threading.Condition()
        self.active_streams = 0
        self.extension_outdated = False  # an old extension talked to us: suggest updating it
        self._to_browser: dict = {}  # link -> until when the browser keeps it (hand_to_browser)
        self._next_schedule_check = 0.0

    # ------------------------------------------------------- lazy manager
    @property
    def manager(self):
        """The download list. Loading it imports the download engine, so only on demand."""
        if self._manager is None:
            from .manager import DownloadManager
            self._attach(DownloadManager(max_active=self.settings.max_active,
                                         auto_resume=self.settings.auto_resume))
        return self._manager

    def _attach(self, manager) -> None:
        self._manager = manager
        manager.on_change = lambda task: self.events.put(("tick", task))
        manager.keep_awake = getattr(self.settings, "keep_awake", True)
        manager.host_limits = dict(getattr(self.settings, "host_limits", {}) or {})
        manager.mark_internet = getattr(self.settings, "mark_internet", True)
        self.apply_category_settings()
        self.apply_queues()

    def apply_proxy(self) -> None:
        """The proxy chosen in the settings (or the system's), for every new connection."""
        from . import net, proxy, vault
        st = self.settings
        proxy.configure(getattr(st, "proxy_mode", "system"), getattr(st, "proxy_host", ""),
                        getattr(st, "proxy_port", 0), getattr(st, "proxy_user", ""),
                        vault.unprotect(getattr(st, "proxy_pass", "") or None, "") or "",
                        getattr(st, "proxy_remote_dns", True), getattr(st, "proxy_bypass", None))
        net._PROXY_CACHE.clear()

    def apply_queues(self) -> None:
        """The other queues, from the settings: their limits (manager) and schedules."""
        queues = getattr(self.settings, "queues", []) or []
        if self._manager is not None:
            self._manager.queue_limits = {q["id"]: q["max_active"] for q in queues}
            self._manager.tick()
        old = getattr(self, "queue_schedulers", {})
        self.queue_schedulers = {}
        for q in queues:
            sch = Scheduler(q.get("scheduler"))
            if q["id"] in old:
                sch._fired = old[q["id"]]._fired  # (an event already fired today stays fired)
            self.queue_schedulers[q["id"]] = sch

    def apply_category_settings(self) -> None:
        """Category folders on/off and their rules, from the settings."""
        if self._manager is not None:
            s = self.settings
            self._manager.folder_rules = s.category_rules() if s.category_folders else None

    def has_saved_active_downloads(self) -> bool:
        """Cheap check (no engine import) for downloads to resume at start-up."""
        try:
            data = json.loads((config_dir() / "downloads.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return False
        tasks = data.get("tasks") if isinstance(data, dict) else None
        return isinstance(tasks, list) and any(isinstance(t, dict) and t.get("status") in ACTIVE for t in tasks)

    # -------------------------------------------------------------- bridge
    def start_bridge(self, retries: int = 0) -> bool:
        """Listen for the browser. ``retries``: extra attempts, 0.5 s apart (used at start-up,
        e.g. right after a restart when the previous process is still releasing the port)."""
        import time
        if self.bridge:
            return True
        for attempt in range(retries + 1):
            try:
                self.bridge = BrowserBridge(self._from_browser, self.port,
                                            api=self.api, on_stream=self.stream).start()
                return True
            except OSError:
                self.bridge = None
                if attempt < retries:
                    time.sleep(0.5)
        return False

    def stop_bridge(self) -> None:
        if self.bridge:
            self.bridge.stop()
            self.bridge = None

    # ------------------------------------------------------------- helpers
    def busy(self) -> bool:
        import time
        now = time.monotonic()
        # A request nobody has asked about for 40 min is forgotten (the extension gives up
        # after 30): it must not keep "when all downloads finish" from ever happening.
        if self.active_streams or any(r["state"] == "waiting" and now - r.get("at", now) < 40 * 60
                                      for r in list(self.requests.values())):
            return True
        if self._manager is None:
            return False
        return any(t.is_running() or t.status == Status.QUEUED or getattr(t, "awaiting_browser", False)
                   for t in self._manager.tasks)

    def _from_browser(self, item: dict) -> None:
        """Called in the bridge thread, before the extension gets its answer. The request id
        is registered right here, so the extension's next question ("has it started?")
        always finds it, even if the main thread is still busy."""
        if item.get("rid"):
            self._set_request(item["rid"], state="waiting")
        if item.get("extension_ok") is False:
            self.extension_outdated = True
        self.events.put(("item", item))

    # ------------------------------------------------- browser download list
    def _set_request(self, rid: str | None, **state) -> None:
        if not rid:
            return
        log("request", rid, state.get("state"))
        import time
        with self._req_cond:
            self.requests[rid] = {**state, "at": time.monotonic()}
            self._req_cond.notify_all()

    def keeps_browser_downloads(self, item: dict) -> bool:
        """Browser downloads the app keeps in its own folder (not shown in the browser's
        list): the setting is off, and the extension understands that answer (0.9.11+)."""
        from .bridge import version_tuple
        return (not getattr(self.settings, "browser_list", True)
                and version_tuple(item.get("extension_version")) >= version_tuple("0.9.11"))

    def bind_request(self, rid: str | None, task, keep: bool = False) -> None:
        """The download asked by the browser has started: tell the extension where to get it."""
        if not rid:
            return
        task.rid = rid
        if keep:
            # The app keeps the file: the extension only drops the browser's own copy once
            # the site really sends data (no stream, nothing shown in the browser's list).
            self._set_request(rid, state="started", task=task.id, stream=None)
            self.manager.save()
            return
        task.mirror = True
        task.stream_token = task.stream_token or secrets.token_urlsafe(16)
        self._set_request(rid, state="started", task=task.id, stream=f"/stream/{task.stream_token}")
        self.manager.save()

    def cancel_request(self, rid: str | None, state: str = "cancelled") -> None:
        self._set_request(rid, state=state)

    def hand_to_browser(self, *urls: str) -> None:
        """The next browser download of these links (within 2 min) stays with the browser:
        "Baixar no navegador" for a site that refuses the app."""
        import time
        until = time.monotonic() + 120
        for u in urls:
            if u:
                self._to_browser[u] = until

    def _handed_to_browser(self, *urls: str) -> bool:
        import time
        now = time.monotonic()
        for u in urls:
            if u and self._to_browser.get(u, 0) > now:
                self._to_browser.pop(u, None)
                return True
        return False

    def request_state(self, rid: str | None) -> str | None:
        with self._req_cond:
            return (self.requests.get(rid) or {}).get("state") if rid else None

    def _restore_request(self, rid: str) -> None:
        """After a restart the request ids are known again from the saved downloads."""
        if not rid or rid in self.requests:
            return
        for t in self.manager.tasks:
            if getattr(t, "rid", None) == rid:
                # (A download the app keeps has no stream: the extension must still learn it
                # started, or it hands the file back to the browser - two copies.)
                stream = f"/stream/{t.stream_token}" if t.stream_token and t.mirror else None
                with self._req_cond:
                    self.requests.setdefault(rid, {"state": "started", "task": t.id, "stream": stream})
                return

    def _verdict(self, task) -> str | None:
        """Has the app really got this download going? "ok", None (not known yet) or the
        reason it could not (the site refused it before sending anything)."""
        if task is None:
            return "ok"
        if task.downloaded > 0 or task.status in (Status.COMPLETED, Status.PAUSED):
            return "ok"
        # (Browser downloads start at once, whatever the limit of simultaneous downloads:
        # "ok" only once the site has really sent data, so the browser never drops its own
        # download for one the site might refuse later.)
        if task.status == Status.QUEUED and not task.is_running():
            return "ok"  # not one of ours (e.g. added paused and resumed later)
        if task.status == Status.ERROR:
            return task.error or "erro"
        return None

    def _give_back(self, rid: str, reason: str) -> None:
        """The site refused the app (single-use link, login, protection page...): drop our
        copy and tell the extension, which lets the browser carry on with its own download."""
        task = self._task_for(rid)
        log("give back", rid, reason)
        if task is not None:
            self.manager.remove(task, delete_files=True)
            self.events.put(("tick", None))
        self._set_request(rid, state="failed", error=reason)
        # Say why the app did not take it (before: the browser simply carried on, and the
        # user could not tell what had happened).
        if self.tray is not None and getattr(self.settings, "tray_notify", True):
            name = getattr(task, "filename", None) or "Download"
            try:
                self.tray.notify("Este download ficou com o navegador",
                                 f"{name}\n{reason}", warning=True)
            except Exception:
                pass

    def _tell_left_to_browser(self, url: str, final: str, filename: str, size, reason: str) -> None:
        """A big file the app leaves to the browser because of its type or site: say so, with
        the way to change it (seen: the user could not tell why the app "did nothing")."""
        try:
            size = int(size or 0)
        except (TypeError, ValueError, OverflowError):
            size = 0
        if size < NOTIFY_LEFT_MIN or self.tray is None or not getattr(self.settings, "tray_notify", True):
            return
        from .categories import extension_of, name_from_url
        name = filename or name_from_url(final or url) or "Download"
        ext = extension_of(name)
        how = (f"Para a app ficar com ficheiros .{ext}: Configurações → Navegador → tipos de ficheiro."
               if reason.startswith("tipo") and ext else
               "Para mudar: botão do Flow Downloads no navegador → \"Capturar neste site\".")
        try:
            self.tray.notify("Este download ficou com o navegador", f"{name} ({reason})\n{how}")
        except Exception:
            pass

    def _task_for(self, rid: str):
        tid = self.requests.get(rid, {}).get("task")
        return next((t for t in self.manager.tasks if t.id == tid), None) if tid else None

    def api(self, method: str, path: str, data: dict):
        """Called from the bridge threads for the extension's questions."""
        if method == "GET" and path.startswith("/request/"):
            rid = path.split("/")[2]
            self._restore_request(rid)
            # One limit for the whole answer: the browser stops an extension whose request takes
            # more than 30 s (and then forgets the paused original). Before: 20 s + 20 s.
            import time
            deadline = time.monotonic() + REQUEST_WAIT_S
            with self._req_cond:
                if rid not in self.requests:
                    log("unknown request", rid)
                    return 404, {"ok": False}
                self._req_cond.wait_for(lambda: self.requests[rid]["state"] != "waiting",
                                        timeout=REQUEST_WAIT_S)
            # "started" only once the site has really sent us data: until then the browser
            # keeps its own download paused, and gets it back if the site refuses us.
            while True:
                with self._req_cond:
                    req = dict(self.requests[rid])
                if req.get("state") != "started":
                    break
                verdict = self._verdict(self._task_for(rid))
                if verdict == "ok":
                    break
                if verdict is not None:  # the site refused the app
                    self._give_back(rid, verdict)
                    with self._req_cond:
                        req = dict(self.requests[rid])
                    break
                if time.monotonic() > deadline:
                    req = {**req, "state": "waiting"}  # ask again
                    break
                time.sleep(0.2)
            return 200, {"ok": True, **{k: v for k, v in req.items() if k not in ("task", "at")}}
        if path == "/check":  # should the app take this browser download? (file types, sites)
            from .capture import should_capture, skipped_site
            url, final, ref = str(data.get("url", "")), str(data.get("final_url", "")), str(data.get("referrer", ""))
            if self._handed_to_browser(url, final):
                return 200, {"ok": True, "capture": False, "reason": "devolvido ao navegador pela app"}
            ok = should_capture(self.settings, url, final, str(data.get("filename", "")),
                                str(data.get("mime", "")), ref)
            reason = "" if ok else ("site ignorado" if skipped_site(self.settings, url, final, ref)
                                    else "tipo fora da lista da app")
            if not ok:
                self._tell_left_to_browser(url, final, str(data.get("filename", "")), data.get("size"), reason)
            return 200, {"ok": True, "capture": ok, "reason": reason}
        if path == "/site":  # the popup's "capture on this site" switch
            from .capture import set_site_skipped, skipped_site
            host = data.get("host") if isinstance(data.get("host"), str) else ""
            if "skip" in data and set_site_skipped(self.settings, host, bool(data["skip"])):
                self.settings.save()
            return 200, {"ok": True, "host": host,
                         "skipped": bool(skipped_site(self.settings, "https://" + host + "/"))}
        rid = str(data.get("rid", ""))
        self._restore_request(rid)
        task = self._task_for(rid) if rid in self.requests else None
        if task is None and path == "/control" and data.get("action") == "cancel" and rid in self.requests:
            # The extension gave up waiting (or the user cancelled) while the app was still
            # asking the user: the browser keeps its own download, the app must not start too.
            self.cancel_request(rid)
            return 200, {"ok": True}
        if task is None:
            return 404, {"ok": False}
        if path == "/control":
            action = data.get("action")
            if action == "pause":
                self.manager.pause(task)
            elif action == "resume":
                self.manager.resume(task)
            elif action == "cancel":  # cancelled in the browser's list
                self.manager.remove(task, delete_files=True)
                self.cancel_request(rid)  # a pending "has it started?" answers "cancelled"
                self.events.put(("tick", None))
            return 200, {"ok": True}
        if path == "/mirror-done":  # the browser saved the file: drop our temporary copy
            saved = str(data.get("path") or "")
            # Only a real file of the right size is taken as "where the browser saved it":
            # the end-of-download actions (checksum, extract, open, command) use this path.
            try:
                ok_path = bool(saved) and os.path.isfile(saved) and (
                    not task.size or os.path.getsize(saved) == task.size)
            except OSError:
                ok_path = False
            task.release_to_browser(saved if ok_path else None)
            if ok_path and getattr(task, "mark_internet", True):
                # The browser saved it from the app (127.0.0.1): Windows' "came from the
                # internet" mark must name the real site, as when the browser downloads alone.
                from .system import mark_from_internet
                from .engine import _headers
                mark_from_internet(saved, task.url, _headers(task.headers).get("Referer"))
            task.stream_token = None  # its address to the browser is not valid any more
            self.manager.save()
            self.after_download(task)  # now the file is where the browser saved it
            self.tray_notify_done(task)
            self.events.put(("tick", None))
            return 200, {"ok": True}
        return 404, {"ok": False}

    def stream(self, conn, path: str, headers: dict) -> None:
        """The browser downloads a task's bytes from us (see mirror.py)."""
        from . import mirror
        token = path.split("/")[2] if path.count("/") >= 2 else ""
        task = next((t for t in self.manager.tasks
                     if token and getattr(t, "stream_token", None) == token), None)
        if task is None:
            mirror._send_error(conn, 404, "Not Found")
            conn.close()
            return
        log("stream start", task.filename, headers.get("range"))
        with self._req_cond:
            self.active_streams += 1
            task.stream_attached = True
        complete = False
        try:
            complete = mirror.serve(conn, task, headers, lambda: task in self.manager.tasks)
        finally:
            with self._req_cond:
                self.active_streams -= 1
                task.stream_attached = False
            log("stream end", task.filename, "complete" if complete else "interrupted")
            if complete:
                task.release_to_browser()  # the extension tells us the final path soon after
                self.tray_notify_done(task)  # the browser has every byte now
                self.manager.save()
            elif task.status == Status.COMPLETED and task.awaiting_browser:
                self._later(BROWSER_GONE_AFTER_S, self._keep_if_unclaimed, task)
            self.events.put(("tick", None))

    def _keep_if_unclaimed(self, task) -> None:
        if task.awaiting_browser and not getattr(task, "stream_attached", False):
            try:
                task.finalize_local()
            except Exception as exc:  # noqa: BLE001 - e.g. the folder was removed meanwhile
                log("keep local failed", task.id, exc)
                task.error = str(exc)
                return
            self.after_download(task)
            self.tray_notify_done(task)
            self.manager.save()
            self.events.put(("tick", None))

    @staticmethod
    def _later(seconds: float, fn, *args) -> None:
        t = threading.Timer(seconds, fn, args)
        t.daemon = True
        t.start()

    def _scheduler_enabled(self) -> bool:
        return any(s.start_enabled or s.stop_enabled
                   for s in [self.scheduler, *getattr(self, "queue_schedulers", {}).values()])

    def check_scheduler(self) -> None:
        import time
        now = time.monotonic()
        if not self._scheduler_enabled() or now < self._next_schedule_check:
            return
        self._next_schedule_check = now + SCHEDULER_CHECK_S
        from .manager import MAIN_QUEUE
        # Each queue follows its own schedule (only its own downloads start / stop).
        for qid, sch in [(MAIN_QUEUE, self.scheduler), *self.queue_schedulers.items()]:
            for event in sch.due():
                if event == "start":
                    self.manager.resume_all(qid)
                elif event == "stop" and self._manager is not None:
                    self._manager.pause_queue(qid)

    # ------------------------------------------------------------- updates
    def check_updates_later(self, delay: float = 90) -> None:
        """Installed app: at most once a day, look for a new version in the background and tell
        the user next to the clock (installing is their choice: ⚙ → Procurar atualizações)."""
        from .paths import FROZEN
        if not (FROZEN and getattr(self.settings, "check_updates", True)):
            return

        def work():
            from . import updater
            if not updater.due(self.settings):
                return
            update, _message = updater.check()
            self.settings.update_checked = time.time()
            self.settings.save()
            if update is not None and self.tray is not None:
                self.tray.notify(f"Flow Downloads {update.version} disponível",
                                 "Abra o Flow Downloads → ⚙ → Procurar atualizações.")
        timer = threading.Timer(delay, work)
        timer.daemon = True
        timer.start()

    # ------------------------------------------------------------- tray icon
    def start_tray(self) -> bool:
        """Icon next to the Windows clock (Windows only; can be turned off in the settings)."""
        if self.tray is not None or not getattr(self.settings, "show_tray", True):
            return False
        from . import tray
        if not tray.supported():
            return False
        from .paths import install_dir
        icon = install_dir() / "flowdl" / "icon.ico"
        if not icon.exists():
            icon = Path(__file__).with_name("icon.ico")
        t = tray.Tray(lambda action: self.events.put(("tray", action)), self.tray_status,
                      self.tray_menu_state, str(icon))
        if t.start():
            self.tray = t
            return True
        return False

    def stop_tray(self) -> None:
        if self.tray is not None:
            self.tray.stop()
            self.tray = None

    def tray_status(self) -> str:
        if self._manager is None:
            return "Flow Downloads — à espera de downloads"
        from .engine import format_size
        running = [t for t in self._manager.tasks if t.is_running()]
        if not running:
            return "Flow Downloads — à espera de downloads"
        speed = sum(t.speed for t in running)
        n = len(running)
        return f"Flow Downloads — {n} a baixar · {format_size(speed)}/s"

    def tray_menu_state(self) -> dict:
        tasks = self._manager.tasks if self._manager is not None else []
        return {"running": any(t.is_running() or t.status == Status.QUEUED for t in tasks),
                "paused": any(t.status in (Status.PAUSED, Status.ERROR) for t in tasks),
                "limit_kb": self.settings.global_limit_kb, "limit_on": GLOBAL_LIMITER.rate > 0}

    def tray_action(self, action: str) -> str | None:
        """What the tray menu asked. Returns "show" / "quit" for the caller (window or not)."""
        if action in ("show", "quit"):
            return action
        if action == "pause_all":
            self.manager.pause_all()
        elif action == "resume_all":
            self.manager.resume_all()
        elif action == "limit" and self.settings.global_limit_kb:
            self.settings.limit_on = GLOBAL_LIMITER.rate == 0
            GLOBAL_LIMITER.rate = self.settings.global_limit_kb * 1024 if self.settings.limit_on else 0
            self.settings.save()
        return None

    def tray_notify_done(self, task) -> None:
        """A Windows notification when a download finishes (or fails)."""
        if self.tray is None or not getattr(self.settings, "tray_notify", True) or task is None:
            return
        name = getattr(task, "filename", None) or "download"
        if task.status == Status.COMPLETED and getattr(task, "awaiting_browser", False):
            return  # the browser is still saving it: "done" once it has it (/mirror-done)
        if task.status == Status.COMPLETED and not getattr(task, "notified", False):
            task.notified = True
            check = getattr(task, "check", "")
            self.tray.notify("Download concluído", name + (f"\n{check}" if check else ""))
        elif task.status == Status.ERROR and not getattr(task, "notified", False):
            task.notified = True
            self.tray.notify("O download falhou", f"{name}\n{task.error or ''}", warning=True)

    def after_download(self, task) -> None:
        """A download's file is in its final place: checksum, antivirus, extract, command, open
        (whatever was chosen), in a background thread. Once per download."""
        if task is None or getattr(task, "after_ran", True) or task.status != Status.COMPLETED:
            return
        if getattr(task, "awaiting_browser", False):
            return  # the browser is still saving it: done when it tells us where it is
        s = self.settings
        if not (getattr(task, "expected_hash", None) or s.after_scan or s.after_extract
                or s.after_command or s.after_open):
            return
        task.after_ran = True

        def work():
            from . import after
            try:
                notes = after.run(task, s)
            except Exception as exc:  # noqa: BLE001 - never take the app down for this
                notes = [f"ações no fim falharam ({exc})"]
            task.check = " · ".join(n for n in notes if n)
            self.manager.save()
            self.events.put(("tick", None))
        threading.Thread(target=work, name="after", daemon=True).start()

    def on_tick(self, task=None) -> str | None:
        """A download stopped: start the next ones; if it was the last one to finish,
        return the 'when all downloads finish' action ("countdown" / "quit")."""
        self.manager.tick()
        self.after_download(task)
        self.tray_notify_done(task)
        if (task is not None and task.status == Status.COMPLETED and getattr(task, "awaiting_browser", False)
                and not getattr(task, "stream_attached", False)):
            # Browser mode, but the browser has not started receiving: give it some time.
            self._later(NO_BROWSER_AFTER_S, self._keep_if_unclaimed, task)
        if task is not None and task.status == Status.COMPLETED and not self.busy():
            return {"shutdown": "countdown", "sleep": "countdown", "close": "quit"}.get(self.scheduler.on_complete)
        return None

    def accept_item(self, item: dict) -> bool:
        """Handle a browser request. Returns True if it needs a window (dialog).
        Never lets one bad request take the whole app down (before: a malformed request
        from this computer could close the app running in the background)."""
        try:
            return self._accept_item(item)
        except Exception as exc:
            import traceback
            log("request not accepted:", repr(exc))
            traceback.print_exc()
            rid = item.get("rid") if isinstance(item, dict) else None
            if rid:
                try:  # the browser carries on with its own download
                    self._set_request(rid, state="failed", error="a app não conseguiu aceitar este download")
                except Exception:
                    pass
            return False

    def _accept_item(self, item: dict) -> bool:
        if item.get("kind") == "show":
            return True
        if item.get("kind") == "video" or self.settings.confirm_browser:
            self.pending.append(item)
            return True
        # (a form asked again - an export, a report - is a new file, not a duplicate)
        existing = None if item.get("post") else self.manager.find_duplicate(item["url"], item.get("final_url"))
        if existing is not None:  # ask: resume / open the existing one, or download again
            self.pending.append({"kind": "duplicate", "item": item, "task": existing.id})
            return True
        return self.add_from_browser(item)

    def add_from_browser(self, item: dict) -> bool:
        """Start a download handed over by the browser. True if a window is needed."""
        headers, cookies = browser_request(item)
        task = self.manager.add(item["url"], self.settings.dest_dir, self.settings.connections,
                                filename=item.get("filename"), headers=headers, cookies=cookies,
                                # Shown in the browser's list: the file ends up in the browser's
                                # folder, never in a category folder (before: an empty
                                # "Compactados" / "Vídeos" folder was left behind).
                                final_url=item.get("final_url"),
                                auto_folder=not item.get("rid") or self.keeps_browser_downloads(item),
                                page=item.get("referrer") or "", expect=item.get("expect"),
                                now=bool(item.get("rid")), post=item.get("post"))
        self.bind_request(item.get("rid"), task, self.keeps_browser_downloads(item))
        if self.settings.show_progress_window:
            self.pending.append({"kind": "progress", "task": task.id})
            return True
        return False

    def can_slim_down(self) -> bool:
        heavy = "tkinter" in sys.modules or "urllib.request" in sys.modules
        return self.slim_enabled and heavy and not self.busy() and not self.pending

    # ----------------------------------------------------------- main loop
    def wait(self) -> str | None:
        """Block (using no CPU) until something needs attention.

        Returns "main" (show the main window), "dialog" (only the browser's dialog),
        "countdown" (shutdown countdown), "slim" (restart to free memory) or None to exit.
        """
        import time
        slim_at = None
        while not self.exit_requested:
            timeout = SCHEDULER_CHECK_S if self._scheduler_enabled() else None
            if slim_at is not None:
                left = max(0.0, slim_at - time.monotonic())
                timeout = left if timeout is None else min(timeout, left)
            self.check_scheduler()  # also when events keep arriving (it checks at most every 30 s)
            try:
                kind, data = self.events.get(timeout=timeout)
            except queue.Empty:
                self.check_scheduler()
                if slim_at is not None and time.monotonic() >= slim_at:
                    if self.can_slim_down():
                        return "slim"
                    slim_at = None
                continue
            slim_at = None  # any activity postpones the restart
            if kind == "tick":
                action = self.on_tick(data)
                if action == "quit":
                    return None
                if action == "countdown":
                    return "countdown"
                if self.can_slim_down():
                    slim_at = time.monotonic() + SLIM_AFTER_IDLE_S
            elif kind == "item":
                log("item", data.get("kind") or data.get("url"))
                if self.accept_item(data):
                    return "main" if data.get("kind") == "show" else "dialog"
            elif kind == "tray":
                what = self.tray_action(data)
                if what == "show":
                    return "main"
                if what == "quit":
                    self.exit_requested = True
                    return None
            elif kind == "quit":
                return None
        return None

    def shutdown(self) -> None:
        self.stop_tray()
        self.settings.scheduler = self.scheduler.to_dict()
        self.save_queue_schedules()
        self.settings.save()
        self.stop_bridge()
        if self._manager is not None:
            self._manager.shutdown()

    def save_queue_schedules(self) -> None:
        for q in getattr(self.settings, "queues", []) or []:
            if q["id"] in self.queue_schedulers:
                q["scheduler"] = self.queue_schedulers[q["id"]].to_dict()

    def restart_slim(self) -> bool:
        """Replace this process with a fresh background one (frees all window memory).
        Returns False (and keeps running) if something arrived at the last moment."""
        had_bridge = self.bridge is not None
        self.stop_bridge()  # no new requests; requests already received finish first
        self.stop_tray()  # the new process shows its own
        if not self.events.empty() or not self.can_slim_down():
            log("restart cancelled: something arrived")
            if had_bridge:
                self.start_bridge(retries=10)
            self.start_tray()
            return False
        log("restart to free memory")
        self.shutdown()
        from .paths import FROZEN
        if FROZEN:
            args = [sys.executable, "--background"]  # the installed .exe
        else:
            code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
                    "from flowdl.service import run; run(background=True)")
            args = [sys.executable, "-c", code]
        for stream in (sys.stdout, sys.stderr):  # None in a program without a console
            if stream is not None:
                stream.flush()
        if sys.platform.startswith("win"):
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            env = dict(os.environ)
            # A packaged program started from itself would otherwise inherit the first one's
            # internal settings and behave like its helper process (PyInstaller): start fresh.
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            log("restart: starting", args)
            subprocess.Popen(args, creationflags=flags, close_fds=True, env=env)
            os._exit(0)
        os.execv(sys.executable, args)


# --------------------------------------------------------------- entry point
_INSTANCE_LOCK = None


def _instance_lock(wait: float = 0.0, path=None):
    """Only one copy of the app at a time: an exclusive lock on a file, held while the app
    runs and released by the system when it ends (even if it crashes). None if another copy
    holds it after ``wait`` seconds."""
    import time
    path = path or (config_dir() / "instance.lock")
    import tempfile
    from pathlib import Path
    # If the usual file cannot be opened (a folder in its place, a read-only profile), one
    # in the temporary folder still keeps a second copy out (before: no protection at all).
    tag = "".join(c for c in os.path.expanduser("~") if c.isalnum())[-40:]
    f = None
    for candidate in (path, Path(tempfile.gettempdir()) / f"flowdl-{tag}.lock"):
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            f = open(candidate, "a+b")
            break
        except OSError:
            continue
    if f is None:
        return True  # cannot create either: do not block the app
    deadline = time.monotonic() + wait
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if time.monotonic() >= deadline:
                f.close()
                return None
            time.sleep(0.2)


def _existing_instance(port: int = DEFAULT_PORT, show: bool = True) -> bool:
    """If Flow Downloads is already running, return True (and ask it to show its window
    when the user opened the program; not when it was started in the background)."""
    request = (b"POST /show HTTP/1.1\r\nHost: 127.0.0.1\r\nX-FlowDL: 1\r\n"
               b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}") if show else \
        b"GET /ping HTTP/1.1\r\nHost: 127.0.0.1\r\nX-FlowDL: 1\r\n\r\n"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
            s.settimeout(3)
            s.sendall(request)
            reply = s.recv(4096)
        return reply.startswith(b"HTTP/1.1 200")
    except OSError:
        return False


def run(background: bool = False) -> None:
    try:
        _run(background)
    except BaseException as exc:  # record why the program stopped (there may be no console)
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        import time
        import traceback
        try:
            with open(config_dir() / "crash.log", "a", encoding="utf-8") as f:
                f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{traceback.format_exc()}\n")
        except OSError:
            pass
        raise


def _run(background: bool) -> None:
    log("start", sys.executable, sys.argv)
    if os.environ.get("FLOWDL_DEBUG") and hasattr(__import__("signal"), "SIGUSR1"):
        # Diagnosis: "kill -USR1 <pid>" writes where every thread is (a stuck app tells why).
        import faulthandler
        import signal
        faulthandler.register(signal.SIGUSR1, file=open(config_dir() / "stacks.log", "a"), all_threads=True)
    if _existing_instance(show=not background):
        log("already running")
        return
    global _INSTANCE_LOCK
    _INSTANCE_LOCK = _instance_lock(wait=6.0)  # a restarting copy may take a moment to leave
    if _INSTANCE_LOCK is None:
        # Another copy is running but did not answer (its browser link is off or it is just
        # starting): never run two - both would write the same files and the same list.
        log("already running (lock held)")
        _existing_instance(show=not background)
        return
    svc = Service()
    svc.slim_enabled = True
    try:  # let the browser start us when we are closed (Windows; quick, only writes changes)
        from . import nativehost
        nativehost.register()
    except Exception as exc:  # noqa: BLE001 - never stop the app for this
        log("native host registration failed:", exc)
    if svc.settings.browser_integration:
        log("bridge", "listening" if svc.start_bridge(retries=20) else "FAILED to listen")
    try:
        svc.start_tray()  # icon next to the Windows clock
    except Exception as exc:  # noqa: BLE001 - the app works without it
        log("tray failed:", exc)
    svc.check_updates_later()
    if svc.settings.auto_resume and svc.has_saved_active_downloads():
        svc.manager.tick()  # interrupted downloads continue right away
    mode = None if background else "main"
    while not svc.exit_requested:
        if mode == "slim":
            svc.restart_slim()  # only returns if it was cancelled
            mode = svc.wait()
            if mode is None:
                break
            continue
        if mode:
            try:
                from .gui import App  # Tkinter is only loaded when a window is needed
            except ImportError as exc:  # no window support: keep working in the background
                log("no window support:", exc)
                svc.pending.clear()
                mode = svc.wait()
                if mode is None:
                    break
                continue
            App(svc, mode).mainloop()
            # Free the window objects now, in this (the Tk) thread. Otherwise the garbage
            # collector may do it later from a download thread, which Tkinter does not allow.
            import gc
            gc.collect()
            gc.enable()  # the windows are gone: automatic clean-up is safe again (see gui.App)
            if svc.exit_requested or not svc.settings.run_in_background:
                break
            if svc.can_slim_down():
                svc.events.put(("tick", None))  # start the quiet-period countdown in wait()
        mode = svc.wait()
        if mode is None:
            break
    svc.shutdown()


def main() -> None:
    run(background="--background" in sys.argv[1:])
