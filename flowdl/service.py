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
        if sys.stderr is not None:
            print(line, file=sys.stderr, flush=True)
        else:
            with open(config_dir() / "debug.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass
REQUEST_WAIT_S = 20        # the extension asks "has it started?" and we answer within 20 s
NO_BROWSER_AFTER_S = 30    # finished but the browser never came for the file: keep it here
BROWSER_GONE_AFTER_S = 120  # the browser stopped receiving and did not come back
SLIM_AFTER_IDLE_S = 5  # restart to free memory only after this many quiet seconds
ROOT = Path(__file__).resolve().parent.parent


class Service:
    def __init__(self, settings: Settings | None = None, manager=None, port: int = DEFAULT_PORT):
        self.settings = settings or Settings()
        self.events: queue.Queue = queue.Queue()
        self._manager = None
        if manager is not None:
            self._attach(manager)
        self.scheduler = Scheduler(self.settings.scheduler)
        GLOBAL_LIMITER.rate = self.settings.global_limit_kb * 1024
        self.port = port
        self.bridge: BrowserBridge | None = None
        self.pending: list[dict] = []  # browser requests waiting for a window
        self.exit_requested = False
        self.slim_enabled = False  # set by run(): allow restarting to free memory
        # Downloads shown in the browser too (see mirror.py)
        self.requests: dict[str, dict] = {}  # request id from the extension -> state
        self._req_cond = threading.Condition()
        self.active_streams = 0
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

    def has_saved_active_downloads(self) -> bool:
        """Cheap check (no engine import) for downloads to resume at start-up."""
        try:
            data = json.loads((config_dir() / "downloads.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return any(t.get("status") in ACTIVE for t in data.get("tasks", []))

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
        if self.active_streams or any(r["state"] == "waiting" for r in list(self.requests.values())):
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
        self.events.put(("item", item))

    # ------------------------------------------------- browser download list
    def _set_request(self, rid: str | None, **state) -> None:
        if not rid:
            return
        log("request", rid, state.get("state"))
        with self._req_cond:
            self.requests[rid] = state
            self._req_cond.notify_all()

    def bind_request(self, rid: str | None, task) -> None:
        """The download asked by the browser has started: tell the extension where to get it."""
        if not rid:
            return
        task.mirror = True
        task.rid = rid
        task.stream_token = task.stream_token or secrets.token_urlsafe(16)
        self._set_request(rid, state="started", task=task.id, stream=f"/stream/{task.stream_token}")
        self.manager.save()

    def cancel_request(self, rid: str | None, state: str = "cancelled") -> None:
        self._set_request(rid, state=state)

    def _restore_request(self, rid: str) -> None:
        """After a restart the request ids are known again from the saved downloads."""
        if not rid or rid in self.requests:
            return
        for t in self.manager.tasks:
            if getattr(t, "rid", None) == rid and t.stream_token:
                with self._req_cond:
                    self.requests.setdefault(rid, {"state": "started", "task": t.id,
                                                   "stream": f"/stream/{t.stream_token}"})
                return

    def _task_for(self, rid: str):
        tid = self.requests.get(rid, {}).get("task")
        return next((t for t in self.manager.tasks if t.id == tid), None) if tid else None

    def api(self, method: str, path: str, data: dict):
        """Called from the bridge threads for the extension's questions."""
        if method == "GET" and path.startswith("/request/"):
            rid = path.split("/")[2]
            self._restore_request(rid)
            with self._req_cond:
                if rid not in self.requests:
                    log("unknown request", rid)
                    return 404, {"ok": False}
                self._req_cond.wait_for(lambda: self.requests[rid]["state"] != "waiting",
                                        timeout=REQUEST_WAIT_S)
                return 200, {"ok": True, **{k: v for k, v in self.requests[rid].items() if k != "task"}}
        rid = str(data.get("rid", ""))
        self._restore_request(rid)
        task = self._task_for(rid) if rid in self.requests else None
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
                self.events.put(("tick", None))
            return 200, {"ok": True}
        if path == "/mirror-done":  # the browser saved the file: drop our temporary copy
            task.release_to_browser(str(data.get("path") or "") or None)
            self.manager.save()
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
                self.manager.save()
            elif task.status == Status.COMPLETED and task.awaiting_browser:
                self._later(BROWSER_GONE_AFTER_S, self._keep_if_unclaimed, task)
            self.events.put(("tick", None))

    def _keep_if_unclaimed(self, task) -> None:
        if task.awaiting_browser and not getattr(task, "stream_attached", False):
            task.finalize_local()
            self.manager.save()
            self.events.put(("tick", None))

    @staticmethod
    def _later(seconds: float, fn, *args) -> None:
        t = threading.Timer(seconds, fn, args)
        t.daemon = True
        t.start()

    def _scheduler_enabled(self) -> bool:
        s = self.scheduler
        return s.start_enabled or s.stop_enabled

    def check_scheduler(self) -> None:
        import time
        now = time.monotonic()
        if not self._scheduler_enabled() or now < self._next_schedule_check:
            return
        self._next_schedule_check = now + SCHEDULER_CHECK_S
        for event in self.scheduler.due():
            if event == "start":
                self.manager.resume_all()
            elif event == "stop" and self._manager is not None:
                for t in self._manager.tasks:
                    t.pause()

    def on_tick(self, task=None) -> str | None:
        """A download stopped: start the next ones; if it was the last one to finish,
        return the 'when all downloads finish' action ("countdown" / "quit")."""
        self.manager.tick()
        if (task is not None and task.status == Status.COMPLETED and getattr(task, "awaiting_browser", False)
                and not getattr(task, "stream_attached", False)):
            # Browser mode, but the browser has not started receiving: give it some time.
            self._later(NO_BROWSER_AFTER_S, self._keep_if_unclaimed, task)
        if task is not None and task.status == Status.COMPLETED and not self.busy():
            return {"shutdown": "countdown", "close": "quit"}.get(self.scheduler.on_complete)
        return None

    def accept_item(self, item: dict) -> bool:
        """Handle a browser request. Returns True if it needs a window (dialog)."""
        if item.get("kind") == "show":
            return True
        if item.get("kind") == "video" or self.settings.confirm_browser:
            self.pending.append(item)
            return True
        headers = {}
        if item.get("referrer"):
            headers["Referer"] = item["referrer"]
        if item.get("cookies"):
            headers["Cookie"] = item["cookies"]
        task = self.manager.add(item["url"], self.settings.dest_dir, self.settings.connections,
                                filename=item.get("filename"), headers=headers)
        self.bind_request(item.get("rid"), task)
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
            elif kind == "quit":
                return None
        return None

    def shutdown(self) -> None:
        self.settings.scheduler = self.scheduler.to_dict()
        self.settings.save()
        self.stop_bridge()
        if self._manager is not None:
            self._manager.shutdown()

    def restart_slim(self) -> bool:
        """Replace this process with a fresh background one (frees all window memory).
        Returns False (and keeps running) if something arrived at the last moment."""
        had_bridge = self.bridge is not None
        self.stop_bridge()  # no new requests; requests already received finish first
        if not self.events.empty() or not self.can_slim_down():
            log("restart cancelled: something arrived")
            if had_bridge:
                self.start_bridge(retries=10)
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
        sys.stdout.flush()
        sys.stderr.flush()
        if sys.platform.startswith("win"):
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(args, creationflags=flags, close_fds=True)
            os._exit(0)
        os.execv(sys.executable, args)


# --------------------------------------------------------------- entry point
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
    if _existing_instance(show=not background):
        log("already running")
        return
    svc = Service()
    svc.slim_enabled = True
    if svc.settings.browser_integration:
        log("bridge", "listening" if svc.start_bridge(retries=20) else "FAILED to listen")
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
