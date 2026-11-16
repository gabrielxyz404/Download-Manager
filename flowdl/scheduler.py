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


def _time_or(value, default: str) -> str:
    """ "HH:MM" with a real hour and minute, or the default."""
    try:
        h, m = (int(x) for x in value.split(":"))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    except (AttributeError, ValueError, TypeError):
        pass
    return default


class Scheduler:
    def __init__(self, config: dict | None = None):
        # (A hand-edited settings file may hold anything: wrong values keep the default.)
        c = config if isinstance(config, dict) else {}
        self.start_enabled: bool = c.get("start_enabled") is True
        self.start_time: str = _time_or(c.get("start_time"), "02:00")
        self.stop_enabled: bool = c.get("stop_enabled") is True
        self.stop_time: str = _time_or(c.get("stop_time"), "07:00")
        days = c.get("days", list(range(7)))  # 0 = Monday
        self.days: list[int] = sorted({d for d in days if isinstance(d, int) and 0 <= d <= 6}) \
            if isinstance(days, list) else list(range(7))
        on_complete = c.get("on_complete", "nothing")
        self.on_complete: str = on_complete if on_complete in ACTIONS else "nothing"
        self._fired: set[str] = set()

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    def due(self, now: dt.datetime | None = None) -> list[str]:
        """Return the events ("start"/"stop") that should fire now (once per day)."""
        now = now or dt.datetime.now()
        if now.weekday() not in self.days:
            return []
        events = []
        for name, enabled, at in (("start", self.start_enabled, self.start_time),
                                  ("stop", self.stop_enabled, self.stop_time)):
            key = f"{name}-{now.date()}-{at}"
            if not enabled or key in self._fired:
                continue
            try:
                h, m = (int(x) for x in at.split(":"))
                late = (now - now.replace(hour=h, minute=m, second=0, microsecond=0)).total_seconds()
            except (AttributeError, TypeError, ValueError):
                continue
            # Not only that exact minute: the computer may have been asleep or busy then.
            # Up to CATCH_UP late it still happens (once); later than that it waits for tomorrow.
            if 0 <= late <= CATCH_UP_S:
                self._fired.add(key)
                events.append(name)
        return events


CATCH_UP_S = 15 * 60


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
