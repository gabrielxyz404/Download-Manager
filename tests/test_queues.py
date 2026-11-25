"""Several queues, each with its own limit and schedule (like other download managers)."""
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flowdl.manager import MAIN_QUEUE, DownloadManager
from flowdl.settings import Settings, clean_queues
from flowdl.status import Status


class FakeTask:
    def __init__(self, i, queue=MAIN_QUEUE):
        self.id, self.queue_id, self.status, self.path = f"t{i}", queue, Status.QUEUED, None
        self.running = False

    def is_running(self):
        return self.running

    def start(self):
        self.running, self.status = True, Status.DOWNLOADING

    def pause(self):
        self.running, self.status = False, Status.PAUSED

    def to_dict(self):
        return {"id": self.id, "url": "http://a.com/x", "dest_dir": ".", "status": self.status}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = DownloadManager(max_active=2, db_path=Path(self.tmp.name) / "db.json")
        self.m.keep_awake = False
        self.m.save = lambda: None

    def running(self, queue):
        return sum(1 for t in self.m.tasks if t.running and self.m.queue_of(t) == queue)

    def test_each_queue_up_to_its_own_limit(self):
        self.m.queue_limits = {"night": 1}
        self.m.tasks = [FakeTask(i) for i in range(4)] + [FakeTask(10 + i, "night") for i in range(3)]
        self.m.tick()
        self.assertEqual((self.running(MAIN_QUEUE), self.running("night")), (2, 1))

    def test_a_queue_that_no_longer_exists_counts_as_the_main_one(self):
        self.m.queue_limits = {}
        self.m.tasks = [FakeTask(1, "gone"), FakeTask(2), FakeTask(3)]
        self.m.tick()
        self.assertEqual(self.running(MAIN_QUEUE), 2)

    def test_resume_and_pause_one_queue_only(self):
        self.m.queue_limits = {"night": 3}
        self.m.tasks = [FakeTask(1), FakeTask(2, "night"), FakeTask(3, "night")]
        for t in self.m.tasks:
            t.status = Status.PAUSED
        self.m.resume_all("night")
        self.assertEqual([t.running for t in self.m.tasks], [False, True, True])
        self.m.pause_queue("night")
        self.assertFalse(any(t.running for t in self.m.tasks))

    def test_queue_is_kept_in_the_list(self):
        m = DownloadManager(max_active=2, db_path=Path(self.tmp.name) / "db2.json")
        m.queue_limits = {"night": 1}
        t = m.add("http://127.0.0.1:1/f.bin", self.tmp.name, start=False)
        m.move_to_queue(t, "night")
        m.save()
        saved = json.loads((Path(self.tmp.name) / "db2.json").read_text())["tasks"][0]
        self.assertEqual(saved["queue"], "night")
        again = DownloadManager(db_path=Path(self.tmp.name) / "db2.json")
        self.assertEqual(again.tasks[0].queue_id, "night")
        m.move_to_queue(t, "nope")  # (an unknown queue: the main one)
        self.assertEqual(t.queue_id, MAIN_QUEUE)


class QueueSettingsTests(unittest.TestCase):
    def test_damaged_queues_are_dropped(self):
        got = clean_queues([{"id": "a", "name": " Noite ", "max_active": 99},
                            {"id": "a", "name": "dup"}, {"id": "main", "name": "x"}, {"name": "sem id"},
                            "x", {"id": "b", "name": "B", "max_active": True, "scheduler": 5}])
        self.assertEqual(got, [{"id": "a", "name": "Noite", "max_active": 20, "scheduler": {}},
                               {"id": "b", "name": "B", "max_active": 2, "scheduler": {}}])
        self.assertEqual(clean_queues("x"), [])


class QueueScheduleTests(unittest.TestCase):
    """Each queue starts / stops at its own times; the others are not touched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"APPDATA": self.tmp.name, "XDG_CONFIG_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_night_queue_starts_at_its_time_only(self):
        from flowdl.service import Service
        st = Settings()
        st.queues = [{"id": "night", "name": "Noturna", "max_active": 2,
                      "scheduler": {"start_enabled": True, "start_time": "02:00", "days": list(range(7))}}]
        m = DownloadManager(max_active=3, db_path=Path(self.tmp.name) / "db.json")
        m.save = lambda: None
        svc = Service(st, m, port=0)
        self.assertEqual(m.queue_limits, {"night": 2})
        m.tasks = [FakeTask(1), FakeTask(2, "night")]
        for t in m.tasks:
            t.status = Status.PAUSED
        at = dt.datetime(2026, 11, 25, 2, 1)
        with mock.patch("flowdl.scheduler.dt.datetime") as d:
            d.now.return_value = at
            svc.check_scheduler()
        self.assertEqual([t.status for t in m.tasks], [Status.PAUSED, Status.DOWNLOADING])
        svc.save_queue_schedules()
        self.assertTrue(st.queues[0]["scheduler"]["start_enabled"])


if __name__ == "__main__":
    unittest.main()
