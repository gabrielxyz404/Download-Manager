"""Damaged files and odd requests must never take the app down (found by fuzzing the inputs:
the extension's requests, the saved files, the update server's answers)."""
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from flowdl import bridge, updater
from flowdl.capture import set_site_skipped, skipped_site
from flowdl.engine import DownloadTask, clean_record, host_of
from flowdl.hostmemory import HostMemory, limit_for
from flowdl.manager import DownloadManager
from flowdl.scheduler import Scheduler
from flowdl.service import Service
from flowdl.settings import Settings

EXT = {"x-flowdl": "1"}


class TmpConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"APPDATA": self.tmp.name, "XDG_CONFIG_HOME": self.tmp.name})
        self.env.start()
        self.dir = Path(self.tmp.name) / "FlowDownloads"
        self.dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write(self, name, content):
        p = self.dir / name
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
        return p


class BridgeRequestTests(unittest.TestCase):
    def call(self, path, body):
        added = []
        code, payload = bridge.handle_request("POST", path, EXT, body.encode(), added.append,
                                              api=lambda m, p, d: (200, {"ok": True, "kind": type(d).__name__}))
        return code, payload, added

    def test_api_questions_must_be_json_objects(self):
        for path in ("/check", "/site", "/control", "/mirror-done", "/request/x"):
            for body in ("[]", "null", "5", '"x"', "[" * 5000):
                self.assertEqual(self.call(path, body)[0], 400, (path, body[:10]))
            self.assertEqual(self.call(path, "{}")[1]["kind"], "dict")

    def test_add_with_wrong_kinds_of_values(self):
        for body in ("[]", "null", '{"url": "http://[::1/x"}', '{"url": "http://a.com:99999/x"}',
                     '{"url": "http://"}', "[" * 5000):
            code, _payload, added = self.call("/add", body)
            self.assertEqual(code, 400, body[:30])
            self.assertEqual(added, [])
        code, _p, added = self.call("/add", json.dumps({"url": "http://a.com/f.zip", "filename": 5,
                                                         "referrer": ["x"], "cookies": {"a": 1}}))
        self.assertEqual(code, 200)
        self.assertEqual((added[0]["filename"], added[0]["referrer"], added[0]["cookies"]), (None, None, None))
        code, _p, added = self.call("/add", '{"url": "http://a.com/f.zip", "browser_size": 1e999}')
        self.assertEqual(code, 200)  # (an impossible size is ignored)

    def test_forms_from_the_extension(self):
        """A file made by a form: the app repeats the POST. Only GET/POST, a body of 1 MB at
        most, valid base64; the body is not kept in plain text anywhere (see the engine)."""
        import base64
        ok = {"url": "https://a.com/export", "method": "POST", "body_b64": base64.b64encode(b"a=1&b=2").decode(),
              "content_type": "application/x-www-form-urlencoded", "origin": "https://a.com"}
        code, _p, added = self.call("/add", json.dumps(ok))
        self.assertEqual(code, 200)
        self.assertEqual(added[0]["post"], {"body": b"a=1&b=2", "type": "application/x-www-form-urlencoded",
                                            "origin": "https://a.com"})
        for bad in ({"method": "PUT"}, {"body_b64": "não é base64!"},
                    {"body_b64": base64.b64encode(b"x" * 1_000_001).decode()}):
            code, _p, added = self.call("/add", json.dumps({**ok, **bad}))
            self.assertEqual(code, 400, list(bad))
            self.assertEqual(added, [])
        code, _p, added = self.call("/add", json.dumps({**ok, "origin": "javascript:x", "content_type": "a\r\nX: y"}))
        self.assertEqual(code, 200)
        self.assertEqual(added[0]["post"]["origin"], "")
        self.assertNotIn("\n", added[0]["post"]["type"])
        code, _p, added = self.call("/add", json.dumps({"url": "https://a.com/f.zip"}))
        self.assertNotIn("post", added[0])  # (an ordinary link: GET)

    def test_an_unexpected_failure_still_gets_an_answer(self):
        """Before: the connection stayed open with no answer until the browser gave up."""
        def broken(item):
            raise KeyError("boom")
        b = bridge.BrowserBridge(broken, port=0)
        b.start()
        try:
            s = socket.create_connection(("127.0.0.1", b.port), 5)
            body = b'{"url": "http://a.com/f.zip"}'
            s.sendall(b"POST /add HTTP/1.1\r\nX-FlowDL: 1\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            s.settimeout(5)
            self.assertTrue(s.recv(100).startswith(b"HTTP/1.1 500"))
            s.close()
        finally:
            b.stop()


class ServiceRequestTests(TmpConfig):
    def test_a_request_that_fails_inside_the_app_does_not_close_it(self):
        settings = Settings()
        settings.dest_dir = self.tmp.name
        svc = Service(settings, DownloadManager(db_path=self.dir / "db.json"), port=0)
        with mock.patch.object(svc.manager, "add", side_effect=AttributeError("boom")):
            self.assertFalse(svc.accept_item({"url": "http://a.com/f.zip", "rid": "r1"}))
        self.assertEqual(svc.requests["r1"]["state"], "failed")  # the browser keeps its download


class DamagedSettingsTests(TmpConfig):
    def test_not_an_object_or_not_text(self):
        for content in ("[]", "null", "5", "{bad", b"\xff\xfe{\x00}\x00"):
            self.write("settings.json", content)
            s = Settings()
            self.assertEqual(s.connections, 8)
            self.assertTrue((self.dir / "settings.json.bad").exists(), content)
            (self.dir / "settings.json.bad").unlink()

    def test_values_of_the_wrong_kind_keep_their_defaults(self):
        self.write("settings.json", json.dumps({
            "columns": 5, "max_active": "x", "connections": None, "global_limit_kb": None, "dest_dir": 5,
            "categories": [5, {}, {"name": "A", "exts": 5}, {"name": "Ok", "exts": ["zip"]}],
            "host_limits": {"a.com": "x", "b.com": 2}, "scheduler": "x", "update_checked": "x",
            "capture_skip_sites": [5, "c.com"], "show_tray": "no", "theme": None, "dark": True}))
        s = Settings()
        self.assertEqual((s.max_active, s.connections, s.global_limit_kb, s.show_tray), (3, 8, 0, True))
        self.assertIsInstance(s.columns, list)
        self.assertIsInstance(s.dest_dir, str)
        self.assertEqual([c["name"] for c in s.categories], ["Ok"])
        self.assertEqual(s.host_limits, {"b.com": 2})
        self.assertEqual(s.scheduler, {})
        self.assertEqual(s.capture_skip_sites, ["c.com"])
        self.assertIsNone(s.theme)  # (older versions: follow "dark")
        self.assertTrue(updater.due(s))
        for n, want in ((0, 1), (-1, 1), (10 ** 9, 20)):
            self.write("settings.json", json.dumps({"max_active": n}))
            self.assertEqual(Settings().max_active, want)

    def test_scheduler_with_impossible_values(self):
        sch = Scheduler({"start_enabled": True, "start_time": "25:99", "stop_enabled": "yes",
                         "days": "x", "on_complete": 5})
        self.assertEqual((sch.start_time, sch.stop_enabled, sch.days, sch.on_complete),
                         ("02:00", False, list(range(7)), "nothing"))
        sch.start_time = 5  # (set by hand afterwards)
        self.assertEqual(sch.due(), [])
        self.assertEqual(Scheduler([1]).days, list(range(7)))


class DamagedListTests(TmpConfig):
    def task(self, i, **kw):
        return {"id": f"t{i}", "url": f"http://a.com/{i}.zip", "dest_dir": self.tmp.name,
                "status": "Pausado", "filename": f"{i}.zip", **kw}

    def test_damaged_list_is_kept_aside_not_emptied(self):
        db = self.dir / "downloads.json"
        for content in ("[]", "null", '{"tasks": "x"}', "{bad"):
            db.write_text(content)
            m = DownloadManager(db_path=db)
            self.assertEqual(m.tasks, [])
            self.assertEqual((self.dir / "downloads.json.bad").read_text(), content)

    def test_stray_bytes_after_the_list_lose_nothing(self):
        db = self.dir / "downloads.json"
        db.write_text(json.dumps({"tasks": [self.task(i) for i in range(5)]}) + "\x00")
        self.assertEqual(len(DownloadManager(db_path=db).tasks), 5)

    def test_one_bad_entry_does_not_cost_the_others(self):
        db = self.dir / "downloads.json"
        db.write_text(json.dumps({"tasks": [None, 5, {"id": "x"}, self.task(1, url=5),
                                            self.task(2, size="x", downloaded="y", added_at="x",
                                                      active_seconds=None, filename=5, connections="x",
                                                      headers="x"),
                                            self.task(3)]}))
        m = DownloadManager(db_path=db)
        self.assertEqual([t.id for t in m.tasks], ["t2", "t3"])
        t2 = m.tasks[0]
        self.assertEqual((t2.size, t2.downloaded, t2.active_seconds, t2.connections), (None, 0, 0.0, 8))
        self.assertIsInstance(t2.added_at, float)
        m.save()  # (before: every save failed)
        self.assertEqual(len(json.loads(db.read_text())["tasks"]), 2)
        self.assertTrue((self.dir / "downloads.json.bad").exists())

    def test_clean_record(self):
        self.assertEqual(clean_record({"url": "u", "dest_dir": "d", "size": True, "downloaded": -5,
                                       "added_at": float("nan"), "other": 1}),
                         {"url": "u", "dest_dir": "d", "other": 1})
        with self.assertRaises(ValueError):
            clean_record({"url": 5, "dest_dir": "d"})


class OtherDamagedInputTests(TmpConfig):
    def test_damaged_server_memory_is_ignored(self):
        for content in ("[]", '{"a.com": 5}', '{"a.com": {"time": "x", "conns": 2}}', "{bad"):
            p = self.write("hosts.json", content)
            self.assertIsNone(HostMemory(p).get("a.com"))
        p = self.write("hosts.json", json.dumps({"a.com": {"time": time.time(), "conns": 4, "limit": None}}))
        self.assertEqual(HostMemory(p).get("a.com")["conns"], 4)

    def test_broken_addresses_have_no_site(self):
        self.assertEqual(host_of("http://[::1/x"), "")
        self.assertIsNone(limit_for({"a.com": 2}, "http://[::1/x"))
        self.assertIsNone(limit_for("x", "http://a.com/"))
        self.assertEqual(limit_for({"a.com": 2}, "http://www.a.com/f"), 2)

    def test_only_real_site_names_are_skipped(self):
        st = Settings()
        st.capture_skip_sites = []
        for bad in ("*", "?", "{'a': 1}", "a b", "a/b", ""):
            self.assertFalse(set_site_skipped(st, bad, True), bad)
        self.assertFalse(skipped_site(st, "https://any.com/f.zip"))
        self.assertTrue(set_site_skipped(st, "www.Sub-1.example.com", True))
        self.assertEqual(st.capture_skip_sites, ["sub-1.example.com"])

    def test_update_check_always_answers(self):
        for answer in (b"[]", b"null", b'{"tag_name": "v99", "assets": [1, null]}',
                       b'{"tag_name": "' + b"9" * 5000 + b'"}', b'{"tag_name": 5}'):
            with mock.patch.object(updater, "_get", return_value=answer):
                update, message = updater.check("0.9.0")
            self.assertIsNone(update)
            self.assertTrue(message)
        import http.client
        with mock.patch.object(updater.urllib.request, "urlopen", side_effect=http.client.BadStatusLine("x")):
            self.assertIsNone(updater.check("0.9.0")[0])


class StreamEdgeTests(unittest.TestCase):
    def serve(self, task, headers):
        from flowdl import mirror
        a, b = socket.socketpair()
        try:
            mirror.serve(a, task, headers, lambda: True)
            a.close()
            out = b""
            while chunk := b.recv(65536):
                out += chunk
            return out
        finally:
            b.close()

    def test_empty_file_asked_with_a_range(self):
        from flowdl.status import Status
        with tempfile.TemporaryDirectory() as d:
            t = DownloadTask("http://a.com/e.bin", d)
            t.path = os.path.join(d, "e.bin")
            open(t.path, "wb").close()
            t.size, t.status, t.stream_token = 0, Status.COMPLETED, "tok"
            out = self.serve(t, {"range": "bytes=5-"})
            self.assertTrue(out.startswith(b"HTTP/1.1 200"), out[:40])
            self.assertIn(b"Content-Length: 0\r\n", out)
            self.assertNotIn(b"-5", out)

    def test_asked_again_after_the_browser_took_the_file(self):
        from flowdl.status import Status
        with tempfile.TemporaryDirectory() as d:
            t = DownloadTask("http://a.com/e.bin", d)
            t.path = os.path.join(d, "gone.bin")  # (the app's copy is gone)
            t.size, t.status, t.stream_token = 100, Status.COMPLETED, "tok"
            self.assertTrue(self.serve(t, {}).startswith(b"HTTP/1.1 404"))


if __name__ == "__main__":
    unittest.main()
