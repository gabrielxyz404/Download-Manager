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
import socket
import subprocess
import sys
from pathlib import Path

from .bridge import DEFAULT_PORT, BrowserBridge
from .limiter import GLOBAL_LIMITER
from .paths import config_dir
from .scheduler import Scheduler
from .settings import Settings
from .status import ACTIVE, Status

SCHEDULER_CHECK_S = 30
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
                self.bridge = BrowserBridge(lambda item: self.events.put(("item", item)), self.port).start()
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
        if self._manager is None:
            return False
        return any(t.is_running() or t.status == Status.QUEUED for t in self._manager.tasks)

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
        self.manager.add(item["url"], self.settings.dest_dir, self.settings.connections,
                         filename=item.get("filename"), headers=headers)
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

    def restart_slim(self) -> None:
        """Replace this process with a fresh background one (frees all window memory)."""
        self.shutdown()
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
def _existing_instance(port: int = DEFAULT_PORT) -> bool:
    """If Flow Downloads is already running, ask it to show its window and return True."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
            s.settimeout(3)
            s.sendall(b"POST /show HTTP/1.1\r\nHost: 127.0.0.1\r\nX-FlowDL: 1\r\n"
                      b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
            reply = s.recv(4096)
        return reply.startswith(b"HTTP/1.1 200")
    except OSError:
        return False


def run(background: bool = False) -> None:
    if _existing_instance():
        return
    svc = Service()
    svc.slim_enabled = True
    if svc.settings.browser_integration:
        svc.start_bridge(retries=20)
    if svc.settings.auto_resume and svc.has_saved_active_downloads():
        svc.manager.tick()  # interrupted downloads continue right away
    mode = None if background else "main"
    while not svc.exit_requested:
        if mode == "slim":
            svc.restart_slim()
            return
        if mode:
            from .gui import App  # Tkinter is only loaded when a window is needed
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
