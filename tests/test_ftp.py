"""FTP downloads against a small local FTP server (no real network)."""
import os
import socket
import socketserver
import tempfile
import threading
import time
import unittest

from flowdl import ftp as ftpmod
from flowdl.engine import Status
from flowdl.ftp import FtpTask, clean_url, split_url

DATA = os.urandom(6 * 1024 * 1024)


class FtpHandler(socketserver.StreamRequestHandler):
    """USER/PASS, TYPE, SIZE, MDTM, PASV/EPSV, REST, RETR, ABOR, QUIT. Settings on the class:
    the file, whether REST/SIZE exist, a connection limit (421), a pace."""
    data = DATA
    mdtm = "20261126120000"
    rest = True
    size = True
    limit = 0
    pace = 0.0  # seconds per 64 KB
    drop_once = False  # the first transfer is cut after 1 MB (a dropped connection)
    users = {"anonymous": None, "ana": "s3gredo"}
    open_now = 0
    peak = 0
    retr_offsets: list = []
    lock = threading.Lock()

    def reply(self, text):
        self.wfile.write((text + "\r\n").encode())

    def handle(self):
        cls = FtpHandler
        with cls.lock:
            cls.open_now += 1
            cls.peak = max(cls.peak, cls.open_now)
            over = cls.limit and cls.open_now > cls.limit
        try:
            if over:
                return self.reply("421 Too many connections from your address")
            self.reply("220 teste")
            user, offset, listener, authed = None, 0, None, False
            while True:
                line = self.rfile.readline()
                if not line:
                    return
                cmd, _, arg = line.decode("latin-1").strip().lstrip("\xff\xf4\xf2").partition(" ")
                cmd = cmd.upper()
                if cmd == "USER":
                    user = arg
                    self.reply("331 senha")
                elif cmd == "PASS":
                    want = cls.users.get(user, "x")
                    authed = user in cls.users and (want is None or want == arg)
                    self.reply("230 ok" if authed else "530 Login incorrect")
                elif not authed:
                    self.reply("530 Please login")
                elif cmd == "TYPE":
                    self.reply("200 binary")
                elif cmd == "SIZE":
                    self.reply(f"213 {len(cls.data)}" if cls.size else "502 no SIZE")
                elif cmd == "MDTM":
                    self.reply(f"213 {cls.mdtm}")
                elif cmd == "REST":
                    if not cls.rest:
                        self.reply("502 no REST")
                    else:
                        offset = int(arg)
                        self.reply(f"350 restarting at {offset}")
                elif cmd in ("PASV", "EPSV"):
                    listener = socket.socket()
                    listener.bind(("127.0.0.1", 0))
                    listener.listen(1)
                    port = listener.getsockname()[1]
                    if cmd == "EPSV":
                        self.reply(f"229 Entering Extended Passive Mode (|||{port}|)")
                    else:
                        self.reply(f"227 Entering Passive Mode (127,0,0,1,{port >> 8},{port & 255})")
                elif cmd == "RETR":
                    cls.retr_offsets.append(offset)
                    self.reply("150 sending")
                    conn, _ = listener.accept()
                    listener.close()
                    complete = True
                    try:
                        for i in range(offset, len(cls.data), 65536):
                            if cls.drop_once and i - offset >= 1024 * 1024:
                                cls.drop_once = False
                                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
                                raise OSError("dropped")
                            conn.sendall(cls.data[i:i + 65536])
                            if cls.pace:
                                time.sleep(cls.pace)
                    except OSError:
                        complete = False
                    conn.close()
                    offset = 0
                    self.reply("226 done" if complete else "426 aborted")
                elif cmd == "ABOR":
                    self.reply("226 abort ok")
                elif cmd == "QUIT":
                    self.reply("221 bye")
                    return
                else:
                    self.reply("502 not implemented")
        except OSError:
            pass
        finally:
            with cls.lock:
                cls.open_now -= 1


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass


class FtpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server(("127.0.0.1", 0), FtpHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"ftp://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        h = FtpHandler
        h.data, h.mdtm, h.rest, h.size, h.limit, h.pace = DATA, "20261126120000", True, True, 0, 0.0
        h.peak, h.retr_offsets, h.drop_once = 0, [], False
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_task(self, url, conns=4, **kw):
        task = FtpTask(url, self.tmp.name, conns, **kw)
        task.start()
        task.wait(60)
        return task

    def assertWhole(self, task, data=DATA):
        self.assertEqual(task.status, Status.COMPLETED, task.error)
        with open(task.path, "rb") as f:
            self.assertEqual(f.read(), data)

    def test_several_connections(self):
        task = self.run_task(self.base + "/pasta/ficheiro.zip")
        self.assertWhole(task)
        self.assertEqual(task.filename, "ficheiro.zip")
        self.assertGreater(FtpHandler.peak, 2)  # (the size test + several pieces)
        self.assertFalse(os.path.exists(task.path + ".part"))

    def test_user_and_password(self):
        self.assertWhole(self.run_task(f"ftp://ana:s3gredo@127.0.0.1:{self.server.server_address[1]}/f.bin"))
        task = self.run_task(f"ftp://ana:errada@127.0.0.1:{self.server.server_address[1]}/g.bin")
        self.assertEqual(task.status, Status.ERROR)
        self.assertIn("senha", task.error)

    def test_pause_and_resume_in_a_new_task(self):
        FtpHandler.pace = 0.15
        url = self.base + "/grande.bin"
        task = FtpTask(url, self.tmp.name, 3)
        task.start()
        time.sleep(1.0)
        task.pause()
        task.wait(10)
        self.assertEqual(task.status, Status.PAUSED)
        self.assertGreater(task.downloaded, 0)
        saved = task.to_dict()
        FtpHandler.pace = 0.0
        again = FtpTask.from_dict(saved)
        again.start()
        again.wait(60)
        self.assertWhole(again)
        self.assertTrue(any(o > 0 for o in FtpHandler.retr_offsets))  # (it continued: REST)

    def test_file_changed_while_paused_starts_over(self):
        FtpHandler.pace = 0.15
        task = FtpTask(self.base + "/muda.bin", self.tmp.name, 3)
        task.start()
        time.sleep(1.0)
        task.pause()
        task.wait(10)
        new = os.urandom(len(DATA))
        FtpHandler.data, FtpHandler.mdtm, FtpHandler.pace = new, "20261127090000", 0.0
        again = FtpTask.from_dict(task.to_dict())
        again.start()
        again.wait(60)
        self.assertWhole(again, new)
        self.assertTrue(again.restarted_changed)

    def test_server_without_rest_or_size(self):
        FtpHandler.rest, FtpHandler.size = False, False
        task = self.run_task(self.base + "/simples.bin")
        self.assertWhole(task)
        self.assertEqual(FtpHandler.retr_offsets, [0])

    def test_server_connection_limit_is_respected(self):
        """421 above 2 connections: fewer connections, and the file still completes."""
        FtpHandler.limit, FtpHandler.pace = 2, 0.005
        task = self.run_task(self.base + "/limite.bin")
        self.assertWhole(task)
        self.assertLessEqual(task.server_limit or 99, 2)

    def test_dropped_connection_continues(self):
        """Found in a review: one dropped data connection failed the whole download."""
        FtpHandler.drop_once = True
        task = self.run_task(self.base + "/cai.bin", conns=2)
        self.assertWhole(task)

    def test_too_many_connections_at_the_first_login_is_not_stuck(self):
        """Found in a review: a 421 at the first login left the download "Conectando" for ever."""
        task = FtpTask(self.base + "/x.bin", self.tmp.name)
        with unittest.mock.patch.object(FtpTask, "_login", side_effect=ftpmod._TooMany("421 Too many")):
            task.start()
            task._stop.wait = lambda timeout=None: False  # (no 5 s waits in the test)
            task.wait(20)
        self.assertFalse(task.is_running())
        self.assertEqual(task.status, Status.ERROR)

    def test_marked_as_from_the_internet_and_after_actions_run(self):
        with unittest.mock.patch("flowdl.system.mark_from_internet") as mark:
            task = self.run_task(self.base + "/marca.exe")
        self.assertWhole(task)
        mark.assert_called_once()
        self.assertFalse(task.after_ran)  # (the service runs the actions, then sets it)
        self.assertTrue(FtpTask.from_dict(task.to_dict()).after_ran)

    def test_password_not_kept_in_plain_text(self):
        task = FtpTask("ftp://ana:s3gredo@a.com/x.bin", self.tmp.name)
        self.assertNotIn("s3gredo", task.url)
        import json
        saved = json.dumps(task.to_dict())
        self.assertNotIn("s3gredo", saved)
        self.assertEqual(FtpTask.from_dict(json.loads(saved))._password, "s3gredo")
        self.assertEqual(clean_url("ftp://ana:s3gredo@a.com:2121/x"), "ftp://ana@a.com:2121/x")
        self.assertEqual(split_url("ftp://a.com/d/x.bin")["user"], "anonymous")
        with self.assertRaises(ValueError):
            split_url("http://a.com/x")

    def test_missing_file(self):
        task = FtpTask(self.base + "/x.bin", self.tmp.name)
        orig = FtpHandler.size
        FtpHandler.size = True
        with unittest.mock.patch.object(ftpmod.ftplib.FTP, "transfercmd",
                                        side_effect=ftpmod.ftplib.error_perm("550 No such file")):
            task.start()
            task.wait(30)
        FtpHandler.size = orig
        self.assertEqual(task.status, Status.ERROR)
        self.assertIn("não existe", task.error)


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
