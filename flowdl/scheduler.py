"""Built-in scheduler: start/stop the queue at set times, act when everything finishes."""

from __future__ import annotations

import datetime as dt
import subprocess
import sys

ACTIONS = {
    "nothing": "Não fazer nada",
    "close": "Fechar o Flow Downloads",
    "sleep": "Suspender o computador",
    "shutdown": "Desligar o computador",
}


class Scheduler:
    def __init__(self, config: dict | None = None):
        c = config or {}
        self.start_enabled: bool = c.get("start_enabled", False)
        self.start_time: str = c.get("start_time", "02:00")
        self.stop_enabled: bool = c.get("stop_enabled", False)
        self.stop_time: str = c.get("stop_time", "07:00")
        self.days: list[int] = c.get("days", list(range(7)))  # 0 = Monday
        self.on_complete: str = c.get("on_complete", "nothing")
        self._fired: set[str] = set()

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    def due(self, now: dt.datetime | None = None) -> list[str]:
        """Return the events ("start"/"stop") that should fire now (once per day)."""
        now = now or dt.datetime.now()
        if now.weekday() not in self.days:
            return []
        events = []
        hm = now.strftime("%H:%M")
        for name, enabled, at in (("start", self.start_enabled, self.start_time),
                                  ("stop", self.stop_enabled, self.stop_time)):
            key = f"{name}-{now.date()}-{at}"
            if enabled and hm == at and key not in self._fired:
                self._fired.add(key)
                events.append(name)
        return events


def shutdown_computer() -> None:
    if sys.platform.startswith("win"):
        subprocess.Popen(["shutdown", "/s", "/t", "0"])
    elif sys.platform == "darwin":
        subprocess.Popen(["osascript", "-e", 'tell app "System Events" to shut down'])
    else:
        subprocess.Popen(["systemctl", "poweroff"])


def sleep_computer() -> None:
    """Suspend (sleep): the downloads are done, the computer can rest."""
    if sys.platform.startswith("win"):
        import ctypes
        ctypes.windll.powrprof.SetSuspendState(False, True, False)  # sleep, not hibernate
    elif sys.platform == "darwin":
        subprocess.Popen(["pmset", "sleepnow"])
    else:
        subprocess.Popen(["systemctl", "suspend"])
