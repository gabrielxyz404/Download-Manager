"""Download queue: limits simultaneous downloads and persists the list to disk."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .engine import DownloadTask, Status


def default_download_dir() -> str:
    return str(Path.home() / "Downloads")


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    path = Path(base) / "pydm"
    path.mkdir(parents=True, exist_ok=True)
    return path


class DownloadManager:
    def __init__(self, max_active: int = 3, db_path: str | Path | None = None):
        self.max_active = max_active
        self.db_path = Path(db_path) if db_path else config_dir() / "downloads.json"
        self.tasks: list[DownloadTask] = []
        self._lock = threading.RLock()
        self._last_saved: str | None = None
        self.load()

    # ------------------------------------------------------------- persistence
    def load(self) -> None:
        try:
            data = json.loads(self.db_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.tasks = [DownloadTask.from_dict(d) for d in data.get("tasks", [])]

    def save(self) -> None:
        with self._lock:
            payload = json.dumps({"tasks": [t.to_dict() for t in self.tasks]}, indent=1)
        if payload == self._last_saved:
            return
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.db_path.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.db_path)
            self._last_saved = payload
        except OSError:
            pass

    # -------------------------------------------------------------- operations
    def add(self, url: str, dest_dir: str | None = None, connections: int = 8,
            start: bool = True) -> DownloadTask:
        task = DownloadTask(url.strip(), dest_dir or default_download_dir(), connections)
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

    def pause_all(self) -> None:
        for t in list(self.tasks):
            t.pause()
        for t in list(self.tasks):
            t.wait(5)
        self.save()

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
        self.save()
