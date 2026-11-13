"""Download queue: limits simultaneous downloads and persists the list to disk."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .engine import DownloadTask, Status, sanitize_filename
from .paths import config_dir, default_download_dir  # noqa: F401 (re-exported)


def task_from_dict(d: dict):
    if d.get("kind") == "video":
        from .video import VideoTask
        return VideoTask.from_dict(d)
    return DownloadTask.from_dict(d)


class DownloadManager:
    def __init__(self, max_active: int = 3, db_path: str | Path | None = None,
                 auto_resume: bool = False):
        self.max_active = max_active
        self.db_path = Path(db_path) if db_path else config_dir() / "downloads.json"
        self.tasks: list[DownloadTask] = []
        self._lock = threading.RLock()
        self._last_saved: str | None = None
        self._resume_ids: set[str] = set()
        self._signature: tuple = ()
        self.folder_rules = None  # (base folder, category rules) -> category folders, or None
        self.on_change = None  # set by the service: called when a download stops/finishes
        self.load(auto_resume)

    # ------------------------------------------------------------- persistence
    def load(self, auto_resume: bool = False) -> None:
        """Load the saved list. With ``auto_resume``, downloads that were running when the
        app closed (or the computer crashed / lost power) are queued again."""
        try:
            data = json.loads(self.db_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        active = (Status.QUEUED, Status.CONNECTING, Status.DOWNLOADING)
        self.tasks = []
        for d in data.get("tasks", []):
            try:
                task = task_from_dict(d)
            except (KeyError, TypeError, ValueError):
                continue
            if auto_resume and d.get("status") in active:
                task.status = Status.QUEUED
            task.on_done = self._task_done
            task.on_change = self._task_changed
            self.tasks.append(task)

    def save(self) -> None:
        with self._lock:
            items = [t.to_dict() for t in self.tasks]
            for d in items:
                if d["id"] in self._resume_ids:
                    d["status"] = Status.QUEUED  # was running when the app was closed
            payload = json.dumps({"tasks": items}, indent=1)
        if payload == self._last_saved:
            return
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.db_path.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            if os.name != "nt":
                os.chmod(tmp, 0o600)  # holds login cookies: readable by this user only
            os.replace(tmp, self.db_path)
            self._last_saved = payload
        except OSError:
            pass

    # -------------------------------------------------------------- operations
    def add(self, url: str, dest_dir: str | None = None, connections: int = 8,
            start: bool = True, filename: str | None = None,
            headers: dict | None = None, cookies: dict | None = None,
            final_url: str | None = None, auto_folder: bool = False,
            page: str = "", expect: dict | None = None) -> DownloadTask:
        """``auto_folder``: ``dest_dir`` is only the base; the category folder inside it is
        chosen once the real file name is known."""
        task = DownloadTask(url.strip(), dest_dir or default_download_dir(), connections,
                            filename=sanitize_filename(filename) if filename else None,
                            headers=headers, cookies=cookies, final_url=final_url)
        task.expect = expect  # what the browser received: the app must get the same file
        if auto_folder and self.folder_rules is not None:
            from .categories import folder_for
            base, rules = task.dest_dir, self.folder_rules
            task.choose_folder = lambda name, url: folder_for(base, name, url, page, rules)
        return self.add_task(task, start)

    def _task_changed(self, task) -> None:
        """The file name / place became known: save the list (not a 'finished' event)."""
        self.save()

    def _task_done(self, task) -> None:
        if self.on_change:
            self.on_change(task)

    def find_duplicate(self, url: str, final_url: str | None = None):
        """A download of the same link already in the list (the most recent one), or None.
        The original link and the final address both count: signed addresses change every time."""
        def norm(u):
            return (u or "").strip().split("#", 1)[0]
        keys = {norm(url)} | ({norm(final_url)} if final_url else set())
        keys.discard("")
        with self._lock:
            for t in reversed(self.tasks):
                if norm(t.url) in keys or (getattr(t, "_final_url", None) and norm(t._final_url) in keys):
                    return t
        return None

    def add_task(self, task, start: bool = True):
        """Add any task object (DownloadTask or video.VideoTask)."""
        task.on_done = self._task_done
        task.on_change = self._task_changed
        if not start:
            task.status = Status.PAUSED
        with self._lock:
            self.tasks.append(task)
        self.tick()
        return task

    def resume(self, task: DownloadTask) -> None:
        if task.status in (Status.PAUSED, Status.ERROR):
            task.status = Status.QUEUED
        self.tick()

    def pause(self, task: DownloadTask) -> None:
        task.pause()
        self.save()

    def remove(self, task: DownloadTask, delete_files: bool = False) -> None:
        task.pause()
        task.wait(5)
        with self._lock:
            if task in self.tasks:
                self.tasks.remove(task)
        if delete_files and task.path:
            for p in (task.path + ".part", task.path + ".part.json"):
                try:
                    os.remove(p)
                except OSError:
                    pass
            if task.status == Status.COMPLETED:
                try:
                    os.remove(task.path)
                except OSError:
                    pass
        self.save()

    def resume_all(self) -> None:
        for t in list(self.tasks):
            if t.status in (Status.PAUSED, Status.ERROR):
                t.status = Status.QUEUED
        self.tick()

    def all_finished(self) -> bool:
        return bool(self.tasks) and not any(
            t.is_running() or t.status == Status.QUEUED for t in self.tasks)

    def pause_all(self) -> None:
        for t in list(self.tasks):
            t.pause()
        for t in list(self.tasks):
            t.wait(5)
        self.save()

    def shutdown(self) -> None:
        """Pause everything for app exit, remembering what was active for next start."""
        self._resume_ids = {t.id for t in self.tasks
                            if t.is_running() or t.status in (Status.QUEUED, Status.CONNECTING)}
        self.pause_all()

    def tick(self) -> None:
        """Start queued downloads while there are free slots. Call periodically."""
        with self._lock:
            running = sum(1 for t in self.tasks if t.is_running())
            for t in self.tasks:
                if running >= self.max_active:
                    break
                if t.status == Status.QUEUED and not t.is_running():
                    t.start()
                    running += 1
            signature = tuple((t.id, t.status, t.path) for t in self.tasks)
        if signature != self._signature:  # only touch the disk when something changed
            self._signature = signature
            self.save()
