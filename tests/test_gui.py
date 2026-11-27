"""Drives the real Tkinter interface (needs a display; skipped otherwise).

Run on a headless machine with:  xvfb-run python -m unittest tests.test_gui
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

try:
    import tkinter as tk
    HAVE_TK = bool(os.environ.get("DISPLAY")) or os.name == "nt"
except ImportError:  # pragma: no cover
    HAVE_TK = False

from tests.test_engine import DATA, Handler

TMP_CONFIG = tempfile.mkdtemp(prefix="flowdl-gui-")
os.environ["XDG_CONFIG_HOME"] = TMP_CONFIG
os.environ["APPDATA"] = TMP_CONFIG


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def pump(app, secs=1.0, until=None):
    """Run the Tk event loop for a while (or until a condition is true)."""
    end = time.time() + secs
    while time.time() < end:
        try:
            app.update()
        except tk.TclError:  # window destroyed
            break
        if until and until():
            return True
        time.sleep(0.02)
    return bool(until and until())


def dialogs(app, cls):
    try:
        return [w for w in app.winfo_children() if isinstance(w, cls)]
    except tk.TclError:
        return []


@unittest.skipUnless(HAVE_TK, "precisa de um ecrã (DISPLAY)")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from flowdl import gui
        cls.gui = gui
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        # a small web site for the grabber / video tests
        cls.site = tempfile.mkdtemp()
        with open(os.path.join(cls.site, "index.html"), "w", encoding="utf-8") as f:
            f.write('<html><head><meta charset="utf-8"><title>Página</title></head><body>'
                    '<a href="a.zip">a</a> <a href="b.pdf">b</a> <a href="c.mp3">c</a> <a href="/x.html">x</a>'
                    '<video src="clip.mp4"></video></body></html>')
        for name in ("a.zip", "b.pdf", "c.mp3"):
            with open(os.path.join(cls.site, name), "wb") as f:
                f.write(os.urandom(50_000))
        cls.have_ffmpeg = shutil.which("ffmpeg") is not None
        if cls.have_ffmpeg:
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            "testsrc=size=320x240:rate=25", "-f", "lavfi", "-i", "sine", "-t", "2",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                            os.path.join(cls.site, "clip.mp4")], check=True)

        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def copyfile(self, src, dst):
                try:
                    super().copyfile(src, dst)
                except OSError:
                    pass

        cls.site_server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=cls.site))
        cls.site_server.daemon_threads = True
        threading.Thread(target=cls.site_server.serve_forever, daemon=True).start()
        cls.site_url = f"http://127.0.0.1:{cls.site_server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.site_server.shutdown()
        shutil.rmtree(cls.site, ignore_errors=True)

    def setUp(self):
        from flowdl.manager import DownloadManager
        from flowdl.service import Service
        from flowdl.settings import Settings
        Handler.delay = 0.0
        self.tmp = tempfile.mkdtemp()
        self.dl = os.path.join(self.tmp, "dl")
        settings = Settings()
        settings.path = type(settings.path)(os.path.join(self.tmp, "settings.json"))
        settings.dest_dir = self.dl
        settings.background_hint_shown = True
        settings.scheduler = {}
        self.svc = Service(settings, DownloadManager(db_path=os.path.join(self.tmp, "db.json")), port=0)
        self.app = None

    def tearDown(self):
        Handler.delay = 0.0
        for t in self.svc.manager.tasks:
            t.pause()
        for t in self.svc.manager.tasks:
            t.wait(5)
        self.svc.stop_bridge()
        if self.app is not None:
            try:
                self.app.close_gui()
            except tk.TclError:
                pass
            self.app = None
        import gc
        gc.collect()  # same as the real app does after closing a window
        gc.enable()
        self.assertFalse(gc.garbage)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def open_app(self, mode="main"):
        if self.app is not None:
            import gc
            self.app = None
            gc.collect()
        self.app = self.gui.App(self.svc, mode)
        pump(self.app, 0.3)
        return self.app

    def wait_status(self, task, status, secs=30):
        ok = pump(self.app, secs, lambda: task.status == status)
        self.assertTrue(ok, f"{task.filename}: {task.status} {task.error}")

    def select(self, task):
        self.app.tree.selection_set(task.id)
        pump(self.app, 0.1)

    # ------------------------------------------------------------ main list
    def test_quick_add_downloads_and_lists_file(self):
        app = self.open_app()
        app.url_var.set(self.base + "/Programa.exe")
        app._quick_add()
        task = self.svc.manager.tasks[0]
        self.wait_status(task, "Concluído")
        pump(app, 2)
        with open(task.path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).digest(), hashlib.sha256(DATA).digest())
        values = app.tree.item(task.id)["values"]
        self.assertEqual(values[0], "Programa.exe")
        self.assertIn("100.0%", values[2])
        self.assertIn("Concluídos (1)", app.side.item("finished")["text"])

    def test_invalid_url_warns(self):
        app = self.open_app()
        app.url_var.set("isto não é um link")
        with mock.patch.object(self.gui.messagebox, "showwarning") as warn:
            app._quick_add()
        warn.assert_called_once()
        self.assertEqual(self.svc.manager.tasks, [])

    def test_video_site_link_opens_quality_window(self):
        app = self.open_app()
        app.url_var.set("https://www.youtube.com/watch?v=abc")
        with mock.patch.object(self.gui.VideoDialog, "_lookup"):
            app._quick_add()
            pump(app, 0.3)
            self.assertEqual(len(dialogs(app, self.gui.VideoDialog)), 1)
        self.assertEqual(self.svc.manager.tasks, [])

    def test_pause_and_resume_buttons(self):
        Handler.delay = 0.2
        app = self.open_app()
        app.add_download(self.base + "/lento.bin", self.dl, 4)
        task = self.svc.manager.tasks[0]
        self.wait_status(task, "Baixando", 10)
        pump(app, 1)
        self.select(task)
        app._pause()
        self.wait_status(task, "Pausado", 10)
        done_before = task.downloaded
        self.assertGreater(done_before, 0)
        Handler.delay = 0.0
        app._resume()
        self.wait_status(task, "Concluído")
        with open(task.path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).digest(), hashlib.sha256(DATA).digest())

    def test_pause_all_and_resume_all(self):
        Handler.delay = 0.05
        app = self.open_app()
        app.add_download(self.base + "/um.bin", self.dl, 2)
        app.add_download(self.base + "/dois.bin", self.dl, 2)
        pump(app, 1.5)
        app._pause_all()
        tasks = self.svc.manager.tasks
        self.assertTrue(pump(app, 10, lambda: all(t.status == "Pausado" for t in tasks)))
        Handler.delay = 0.0
        self.svc.manager.resume_all()
        self.assertTrue(pump(app, 30, lambda: all(t.status == "Concluído" for t in tasks)))

    def test_speed_limits(self):
        from flowdl.limiter import GLOBAL_LIMITER
        app = self.open_app()
        app.add_download(self.base + "/x.bin", self.dl, 2, start=False)
        task = self.svc.manager.tasks[0]
        self.select(task)
        with mock.patch.object(self.gui.simpledialog, "askinteger", return_value=100):
            app._task_limit()
        self.assertEqual(task.speed_limit, 100 * 1024)
        with mock.patch.object(self.gui.simpledialog, "askinteger", return_value=500):
            app._global_limit()
        self.assertEqual(GLOBAL_LIMITER.rate, 500 * 1024)
        # saved at once, not only when the window closes (a crash or log-off lost it)
        self.assertIn('"global_limit_kb": 500', read_text(self.svc.settings.path))
        with mock.patch.object(self.gui.simpledialog, "askinteger", return_value=0):
            app._global_limit()
        self.assertEqual(GLOBAL_LIMITER.rate, 0)

    def test_queues_window_and_moving_downloads(self):
        """Filas: a new queue with its own limit and schedule; downloads moved to it with the
        right-click menu; deleting it sends them back to Principal."""
        app = self.open_app()
        app.add_download(self.base + "/q.bin", self.dl, 2, start=False)
        task = self.svc.manager.tasks[0]
        try:
            dlg = self.gui.QueuesDialog(app)
            with mock.patch.object(self.gui.simpledialog, "askstring", return_value="Noturna"):
                dlg._new()
            q = self.svc.settings.queues[0]
            self.assertEqual(q["name"], "Noturna")
            self.assertEqual(self.svc.manager.queue_limits, {q["id"]: 2})
            with mock.patch.object(self.gui.simpledialog, "askinteger", return_value=1):
                dlg._limit()
            self.assertEqual(self.svc.manager.queue_limits[q["id"]], 1)
            dlg._schedule()
            pump(app, 0.3)
            sched = next(w for w in app.winfo_children() if isinstance(w, self.gui.SchedulerDialog))
            sched.start_on.set(True)
            sched.start_at.set("03:15")
            sched._save()
            pump(app, 0.3)
            self.assertTrue(self.svc.queue_schedulers[q["id"]].start_enabled)
            self.assertEqual(self.svc.settings.queues[0]["scheduler"]["start_time"], "03:15")
            self.assertFalse(self.svc.scheduler.start_enabled)  # (Principal's schedule untouched)
            self.select(task)
            app._fill_queue_menu()
            labels = [app.queue_menu.entrycget(i, "label") for i in range(app.queue_menu.index("end") + 1)
                      if app.queue_menu.type(i) == "command"]
            self.assertEqual(labels, ["Principal", "Noturna", "Gerir filas..."])
            app._move_to_queue(q["id"])
            self.assertEqual(self.svc.manager.queue_of(task), q["id"])
            self.assertEqual(app._queue_name(task), "Noturna")
            dlg.tree.selection_set(q["id"])
            with mock.patch.object(self.gui.messagebox, "askyesno", return_value=True):
                dlg._delete()
            self.assertEqual(self.svc.settings.queues, [])
            self.assertEqual(app._queue_name(task), "Principal")
            dlg.destroy()
        finally:
            self.svc.settings.queues = []
            self.svc.apply_queues()

    def test_proxy_settings_page(self):
        """Configurações -> Rede: a SOCKS5 proxy with a password (kept encrypted), used at once."""
        from flowdl import proxy
        app = self.open_app()
        win = app.open_settings()
        try:
            win.show("Rede")
            pump(app, 0.2)
            win.proxy_mode.set("socks5")
            for key, value in (("host", "10.0.0.5"), ("port", "1080"), ("user", "ana"), ("pass", "s3gredo"),
                               ("bypass", "localhost, *.lan")):
                win.proxy_vars[key].set(value)
            win._save_proxy()
            c = proxy.current()
            self.assertEqual((c["mode"], c["host"], c["port"], c["user"], c["password"], c["bypass"]),
                             ("socks5", "10.0.0.5", 1080, "ana", "s3gredo", ["localhost", "*.lan"]))
            self.assertNotIn("s3gredo", read_text(self.svc.settings.path))
            with mock.patch.object(self.gui.messagebox, "showwarning") as warn:
                win.proxy_vars["port"].set("99999")
                win._save_proxy()
            warn.assert_called_once()
            win._use_tor()
            self.assertEqual((proxy.current()["host"], proxy.current()["port"]), ("127.0.0.1", 9150))
            win.destroy()
        finally:
            st = self.svc.settings
            st.proxy_mode, st.proxy_host, st.proxy_port, st.proxy_user, st.proxy_pass = "system", "", 0, "", ""
            st.save()
            self.svc.apply_proxy()

    def test_ftp_link_downloads_in_the_window(self):
        """An ftp:// link pasted in the app: downloaded (several connections), shown in the
        list and the details like any other, the diagnostic works."""
        import tests.test_ftp as tf
        server = tf.Server(("127.0.0.1", 0), tf.FtpHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            tf.FtpHandler.data, tf.FtpHandler.pace, tf.FtpHandler.limit = tf.DATA, 0.0, 0
            app = self.open_app()
            app.url_var.set(f"ftp://127.0.0.1:{server.server_address[1]}/pasta/arquivo.zip")
            app._quick_add()
            task = self.svc.manager.tasks[-1]
            self.assertEqual(task.kind, "ftp")
            self.wait_status(task, "Concluído", 30)
            with open(task.path, "rb") as f:
                self.assertEqual(f.read(), tf.DATA)
            app._refresh(schedule=False)
            self.select(task)
            pump(app, 0.5)
            app._copy_diagnostic()
            self.assertIn("FTP", app.clipboard_get())
            self.svc.manager.save()
            self.assertIn('"kind": "ftp"', read_text(self.svc.manager.db_path))
        finally:
            server.shutdown()

    def test_connections_per_download_setting(self):
        """Configurações -> Downloads: the maximum for new downloads (before, only the "+"
        window had it), and the "+" window starts from it."""
        app = self.open_app()
        try:
            with mock.patch.object(self.gui.simpledialog, "askinteger", return_value=12):
                app._connections()
            self.assertEqual(self.svc.settings.connections, 12)
            self.assertIn('"connections": 12', read_text(self.svc.settings.path))
            dlg = self.gui.AddDialog(app)
            self.assertEqual(dlg.conns.get(), 12)
            dlg.destroy()
        finally:
            self.svc.settings.connections = 8
            self.svc.settings.save()

    def test_delete_removes_unfinished_files(self):
        Handler.delay = 0.05
        app = self.open_app()
        app.add_download(self.base + "/apagar.bin", self.dl, 2)
        task = self.svc.manager.tasks[0]
        self.wait_status(task, "Baixando", 10)
        pump(app, 1)
        self.select(task)
        app._pause()
        self.wait_status(task, "Pausado", 10)
        self.assertTrue(os.path.exists(task.path + ".part"))
        self.select(task)
        with mock.patch.object(self.gui.messagebox, "askyesnocancel", return_value=True):
            app._remove()
        pump(app, 0.3)
        self.assertEqual(self.svc.manager.tasks, [])
        self.assertFalse(os.path.exists(task.path + ".part"))
        self.assertFalse(app.tree.exists(task.id))

    def test_categories_filter(self):
        app = self.open_app()
        for name in ("filme.mp4", "contrato.pdf", "jogo.zip"):
            app.add_download(f"{self.base}/{name}", self.dl, 2)
        tasks = self.svc.manager.tasks
        self.assertTrue(pump(app, 30, lambda: all(t.status == "Concluído" for t in tasks)))
        for item, expected in (("cat:Vídeos", ["filme.mp4"]), ("cat:Documentos", ["contrato.pdf"]),
                               ("cat:Compactados", ["jogo.zip"]), ("unfinished", []),
                               ("finished", ["filme.mp4", "contrato.pdf", "jogo.zip"])):
            app.side.selection_set(item)
            pump(app, 0.3)
            shown = [app.tree.item(i)["values"][0] for i in app.tree.get_children()]
            self.assertEqual(shown, expected, item)

    def test_looks_and_columns(self):
        app = self.open_app()
        style = self.gui.ttk.Style(app)
        for key in ("claro", "escuro"):
            app.theme_var.set(key)
            app._choose_theme()
            look = self.gui.THEMES[key]
            self.assertEqual(style.lookup("Treeview", "background"), look["field"], key)
            self.assertEqual(style.lookup("Accent.TButton", "background"), look["accent"], key)
            self.assertEqual(style.lookup("Side.Treeview", "background"), look["sidebar"], key)
            self.assertEqual(self.svc.settings.theme, key)
            self.assertEqual(self.svc.settings.dark, key in self.gui.DARK_THEMES)
        self.assertIn('"theme": "escuro"', read_text(self.svc.settings.path))
        # black & white only: no colour in any look
        for look in (self.gui.THEMES["claro"], self.gui.THEMES["escuro"]):
            for value in look.values():
                if value.startswith("#"):
                    self.assertTrue(value[1:3] == value[3:5] == value[5:7], value)
        # ◐ switches light <-> dark
        app._toggle_dark()
        self.assertEqual(self.svc.settings.theme, "claro")
        app._toggle_dark()
        self.assertEqual(self.svc.settings.theme, "escuro")
        with mock.patch.object(self.gui, "windows_prefers_dark", return_value=True):
            app.theme_var.set("auto")
            app._choose_theme()
            self.assertEqual(style.lookup("Treeview", "background"), self.gui.THEMES["escuro"]["field"])
        self.svc.settings.theme = None  # settings from an older version: "dark" decides
        self.svc.settings.dark = True
        self.assertEqual(self.gui.theme_name(self.svc.settings), "escuro")
        for old, new in (("meia-noite", "escuro"), ("brasa", "escuro"), ("menta", "claro")):  # looks of 0.8.0
            self.svc.settings.theme = old
            self.assertEqual(self.gui.theme_name(self.svc.settings), new)
        self.svc.settings.theme = "auto"
        app.col_vars["speed"].set(False)
        app.col_vars["url"].set(True)
        app._apply_columns()
        shown = list(app.tree["displaycolumns"])
        self.assertNotIn("speed", shown)
        self.assertIn("url", shown)
        self.assertEqual(self.svc.settings.columns, shown)

    def test_gear_menu_and_settings_window(self):
        app = self.open_app()
        labels = [app.gear_menu.entrycget(i, "label") for i in range(app.gear_menu.index("end") + 1)
                  if app.gear_menu.type(i) not in ("separator", "tearoff")]
        for wanted in ("Configurações...", "Tema", "Adicionar URL...", "Agendador...", "Sair"):
            self.assertIn(wanted, labels)
        self.assertEqual(str(app["menu"]), "")  # no menu bar any more
        app.event_generate("<Control-comma>")
        pump(app, 0.3)
        win = dialogs(app, self.gui.SettingsDialog)
        self.assertEqual(len(win), 1)
        win = win[0]
        self.assertIs(app.open_settings("Sistema"), win)  # one window only
        for section in win.SECTIONS:  # every page builds
            win.show(section)
            pump(app, 0.1)
            self.assertTrue(win.page.winfo_children(), section)
        win.show("Aparência")
        pump(app, 0.1)
        win._theme("escuro")
        self.assertEqual(self.svc.settings.theme, "escuro")
        self.assertEqual(str(win.cards["escuro"][0]["highlightthickness"]), "2")
        win._theme("claro")
        win.filter_var.set("pills")
        win._filters()
        pump(app, 0.2)
        self.assertEqual(self.svc.settings.filter_style, "pills")
        self.assertFalse(app.side_frame.winfo_ismapped())
        self.assertIn("all", app.pill_buttons)
        self.assertNotIn("cat:Vídeos", app.pill_buttons)  # only categories in use
        win.filter_var.set("side")
        win._filters()
        pump(app, 0.2)
        self.assertTrue(app.side_frame.winfo_ismapped())
        win.destroy()

    def test_pills_filter(self):
        self.svc.settings.filter_style = "pills"
        try:
            app = self.open_app()
            for name in ("filme.mp4", "contrato.pdf"):
                app.add_download(f"{self.base}/{name}", self.dl, 2)
            tasks = self.svc.manager.tasks
            self.assertTrue(pump(app, 30, lambda: all(t.status == "Concluído" for t in tasks)))
            app.pill_buttons["cat:Vídeos"].invoke()
            pump(app, 0.3)
            shown = [app.tree.item(i)["values"][0] for i in app.tree.get_children()]
            self.assertEqual(shown, ["filme.mp4"])
            self.assertEqual(str(app.pill_buttons["cat:Vídeos"]["style"]), "PillOn.TButton")
            app.pill_buttons["all"].invoke()
            pump(app, 0.6)
            self.assertEqual(len(app.tree.get_children()), 2)
            self.assertIn("2", app.pill_buttons["all"]["text"])
        finally:
            self.svc.settings.filter_style = "side"

    def test_browser_requests_are_handled_at_once(self):
        """With the window open, a download from the browser waited for the next redraw of the
        list (up to 1.5 s when nothing was downloading) - the browser's own download stays
        paused meanwhile."""
        app = self.open_app()
        pump(app, 2)  # idle: the list redraws only every 1.5 s
        seen = []
        real = app._process_events
        with mock.patch.object(app, "_process_events", lambda: (seen.append(time.monotonic()), real())):
            t0 = time.monotonic()
            self.svc.events.put(("tick", None))
            pump(app, 1.0, lambda: bool(seen))
        self.assertTrue(seen)
        self.assertLess(seen[0] - t0, 0.4)

    def test_details_panel(self):
        Handler.delay = 0.2
        app = self.open_app()
        app.add_download(self.base + "/detalhe.bin", self.dl, 4)
        task = self.svc.manager.tasks[0]
        self.wait_status(task, "Baixando", 10)
        pump(app, 1)
        self.select(task)
        pump(app, 0.6)
        panel = app.details
        self.assertIs(panel.task, task)
        self.assertEqual(panel.name["text"], "detalhe.bin")
        self.assertEqual(str(panel.primary["text"]), "॥ Pausar")
        self.assertTrue(panel.map_box.winfo_ismapped())
        fills = [panel.canvas.itemcget(i, "fill") for i in panel.cells]
        self.assertIn(app.colors["accent"], fills + [app.colors["partial"]])
        panel.primary.invoke()  # pause from the panel
        self.wait_status(task, "Pausado", 10)
        pump(app, 0.6)
        self.assertEqual(str(panel.primary["text"]), "▶ Retomar")
        self.assertIn("continuar", panel.rows[2][1]["text"])
        Handler.delay = 0.0
        panel.primary.invoke()
        self.wait_status(task, "Concluído")
        pump(app, 3, lambda: str(panel.primary["text"]) == "Abrir")
        self.assertEqual(str(panel.primary["text"]), "Abrir")
        self.assertFalse(panel.map_box.winfo_ismapped())
        # measured, to compare with the browser: how long it took, average / best speed
        labels = {str(k["text"]): str(v["text"]) for k, v in panel.rows if k.winfo_ismapped()}
        self.assertIn("Demorou", labels)
        self.assertIn("1.º byte", labels["Demorou"])
        self.assertIn("média", labels["Velocidade"])
        self.assertGreater(task.active_seconds, 0.5)
        again = type(task).from_dict(task.to_dict())  # kept in the list
        self.assertAlmostEqual(again.active_seconds, task.active_seconds, places=1)
        self.assertEqual(again.first_byte_after, task.first_byte_after)
        task.status, task.error = "Erro", "HTTP 403: Forbidden"  # how an expired link looks
        pump(app, 3, lambda: str(panel.second["text"]) == "Atualizar link")
        self.assertEqual(str(panel.second["text"]), "Atualizar link")
        self.assertEqual(str(panel.third["text"]), "Baixar no navegador")
        self.assertIn("expirado", panel.rows[2][1]["text"])
        task.status, task.error = "Concluído", None
        # a narrow window: the list keeps its room, the panel steps aside (and comes back)
        app.geometry("880x600")
        pump(app, 0.5, lambda: not panel.winfo_ismapped())
        self.assertFalse(panel.winfo_ismapped())
        app.geometry("1240x640")
        pump(app, 0.5, lambda: panel.winfo_ismapped())
        self.assertTrue(panel.winfo_ismapped())
        # the panel can be hidden
        self.svc.settings.show_details = False
        app._layout()
        pump(app, 0.2)
        self.assertFalse(panel.winfo_ismapped())
        self.svc.settings.show_details = True
        app._layout()

    def test_update_link(self):
        app = self.open_app()
        app.add_download(self.base + "/expirou.bin", self.dl, 2, start=False)
        task = self.svc.manager.tasks[0]
        self.select(task)
        with mock.patch.object(self.gui.simpledialog, "askstring", return_value=self.base + "/novo.bin"):
            app._update_link()
        self.assertEqual(task.url, self.base + "/novo.bin")
        self.wait_status(task, "Concluído")
        # while it is downloading (found in an audit: it was left paused)
        Handler.delay = 0.2
        app.add_download(self.base + "/correndo.bin", self.dl, 2)
        running = self.svc.manager.tasks[-1]
        self.wait_status(running, "Baixando", 10)
        self.select(running)
        Handler.delay = 0.0
        with mock.patch.object(self.gui.simpledialog, "askstring", return_value=self.base + "/outro.bin"):
            app._update_link()
        self.assertEqual(running.url, self.base + "/outro.bin")
        self.wait_status(running, "Concluído")

    def test_category_with_a_repeated_name_is_refused(self):
        """Found by clicking through every window: a second "Vídeos" was saved, and the main
        window could never be opened again (the list of filters failed)."""
        app = self.open_app()
        dlg = self.gui.CategoriesDialog(app)
        ed = self.gui.CategoryEditor(dlg, {"name": "", "exts": []}, None)
        ed.name.set("vídeos")
        with mock.patch.object(self.gui.messagebox, "showwarning") as warn:
            ed._ok()
        warn.assert_called_once()
        self.assertEqual(sum(r["name"].casefold() == "vídeos" for r in dlg.rules), 1)
        ed.destroy()
        dlg.destroy()
        # settings already holding a repeated name (older version) still open the window
        self.svc.settings.categories = [dict(r) for r in self.svc.settings.category_rules()] + [
            {"name": "Vídeos", "exts": ["xyz"]}]
        try:
            app = self.open_app()
            self.assertTrue(app.side.exists("cat:Outros"))
        finally:
            self.svc.settings.categories = []

    def test_finished_download_is_never_deleted_by_hand_back_or_new_link(self):
        """'Baixar no navegador' deleted a finished file; 'Atualizar link' downloaded it again."""
        app = self.open_app()
        app.add_download(self.base + "/feito.bin", self.dl, 2)
        task = self.svc.manager.tasks[0]
        self.wait_status(task, "Concluído")
        self.select(task)
        with mock.patch("webbrowser.open") as browser, \
                mock.patch.object(self.gui.simpledialog, "askstring", return_value=self.base + "/x.bin") as ask:
            app._to_browser()
            app._update_link()
        browser.assert_not_called()
        ask.assert_not_called()
        self.assertTrue(os.path.exists(task.path))
        self.assertIn(task, self.svc.manager.tasks)

    def test_closed_window_is_freed(self):
        """Each open/close of the window kept the whole window in memory (~2 MB each time)."""
        import gc
        import weakref
        app = self.open_app()
        ref = weakref.ref(app)
        app.close_gui()
        self.app = app = None
        for _ in range(3):
            gc.collect()
        self.assertIsNone(ref(), "the closed window is still in memory")

    def test_queued_download_can_start_now(self):
        """A download waiting for a free slot: its button did nothing."""
        Handler.delay = 0.05
        self.svc.manager.max_active = 1
        try:
            app = self.open_app()
            app.add_download(self.base + "/primeiro.bin", self.dl, 2)
            app.add_download(self.base + "/segundo.bin", self.dl, 2)
            first, second = self.svc.manager.tasks
            self.wait_status(first, "Baixando", 10)
            self.select(second)
            pump(app, 0.6)
            self.assertEqual(second.status, "Na fila")
            self.assertEqual(str(app.details.primary["text"]), "▶ Começar já")
            app.details.primary.invoke()
            self.assertTrue(pump(app, 5, second.is_running))
        finally:
            Handler.delay = 0.0
            self.svc.manager.max_active = 3

    def test_copy_url_and_open_folder(self):
        app = self.open_app()
        app.add_download(self.base + "/f.bin", self.dl, 2, start=False)
        task = self.svc.manager.tasks[0]
        self.select(task)
        app._copy_url()
        self.assertEqual(app.clipboard_get(), task.url)
        os.makedirs(self.dl, exist_ok=True)
        with mock.patch.object(self.gui, "open_path") as op:
            app._open_folder()
        op.assert_called_once_with(self.dl)

    def test_copy_diagnostic(self):
        """What happened, second by second, to paste in a message: never the link itself
        (it may carry a key) nor cookies."""
        app = self.open_app()
        app.add_download(self.base + "/f.bin?token=SECRET", self.dl, 2)
        task = self.svc.manager.tasks[0]
        self.assertTrue(pump(app, 15, lambda: task.status == "Concluído"))
        self.select(task)
        app._copy_diagnostic()
        text = app.clipboard_get()
        self.assertIn("diagnóstico", text)
        self.assertIn("início:", text)
        self.assertIn("fim: Concluído", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("f.bin", text)
        self.assertIn("Diagnóstico copiado", app.status_var.get())

    def test_clipboard_link_detected_when_window_gets_focus(self):
        app = self.open_app()
        app.clipboard_clear()
        app.clipboard_append("https://exemplo.com/arquivo.zip")
        pump(app, 0.2)
        app._on_focus(type("E", (), {"widget": app})())
        self.assertEqual(app.url_var.get(), "https://exemplo.com/arquivo.zip")

    # ----------------------------------------------------- browser dialogs
    def test_browser_download_shows_only_small_window(self):
        self.svc.pending.append({"url": self.base + "/doNavegador.zip", "filename": None,
                                 "referrer": "https://site/", "cookies": "s=1"})
        app = self.open_app("dialog")
        self.assertEqual(app.state(), "withdrawn")  # main window stays hidden
        dlg = dialogs(app, self.gui.AddDialog)
        self.assertEqual(len(dlg), 1)
        self.assertEqual(dlg[0].url.get(), self.base + "/doNavegador.zip")
        dlg[0]._ok(True)
        pump(app, 0.3)
        # after "Baixar agora": only the small progress window, which closes when it is done,
        # and then the hidden app window closes itself too
        self.assertEqual(len(dialogs(app, self.gui.ProgressWindow)), 1)
        self.assertTrue(pump(app, 60, lambda: not self._alive(app)), "the app should close itself")
        task = self.svc.manager.tasks[0]
        task.wait(30)
        self.assertEqual(task.status, "Concluído", task.error)
        self.assertEqual(task.headers, {"Referer": "https://site/"})
        self.assertEqual(task.cookies, {"127.0.0.1": "s=1"})  # login cookies are kept per site

    @staticmethod
    def _alive(app):
        try:
            return bool(app.winfo_exists())
        except tk.TclError:
            return False

    def test_add_dialog_custom_name_download_later(self):
        app = self.open_app()
        dlg = self.gui.AddDialog(app, self.base + "/qualquer")
        dlg.name.set("meu arquivo.bin")
        dlg._ok(False)
        task = self.svc.manager.tasks[0]
        self.assertEqual(task.status, "Pausado")
        self.assertEqual(task.filename, "meu arquivo.bin")
        self.select(task)
        app._resume()
        self.wait_status(task, "Concluído")
        self.assertEqual(os.path.basename(task.path), "meu arquivo.bin")

    def test_confirmed_browser_download_respects_the_browser_list_option(self):
        """Found in a review: with "ask before downloading" on, the confirm dialog always
        showed the file in the browser's list too (a second copy), whatever the option."""
        app = self.open_app()
        self.svc.settings.browser_list = False
        for version, kept in (("0.9.11", True), ("0.9.9", False)):
            rid = "rq" + version
            self.svc.requests[rid] = {"state": "waiting"}
            dlg = self.gui.AddDialog(app, self.base + f"/confirmado{version}.bin", rid=rid,
                                     item={"rid": rid, "extension_version": version})
            dlg._ok(True)
            self.assertEqual(self.svc.requests[rid]["stream"] is None, kept, version)
            task = self.svc._task_for(rid)
            self.assertEqual(task.mirror, not kept)
            self.wait_status(task, "Concluído")

    def test_no_settings_text_is_cut_off(self):
        """Seen in the audit: a long option text ran past the window's edge (half of it was
        never visible). Every section, light and dark: nothing wider than the window.
        (With the Windows-only rows shown: ffmpeg installed by the app.)"""
        from flowdl import video
        for p in (mock.patch.object(video, "WINDOWS", True),
                  mock.patch.object(video, "ffmpeg_state", lambda: "instalado pela app")):
            p.start()
            self.addCleanup(p.stop)
        app = self.open_app()
        win = app.open_settings()
        pump(app, 0.3)

        def walk(w):
            for c in w.winfo_children():
                yield c
                yield from walk(c)
        cut = []
        for section in self.gui.SettingsDialog.SECTIONS:
            win.show(section)
            pump(app, 0.2)
            right = win.winfo_rootx() + win.winfo_width()
            for c in walk(win):
                text = str(c.cget("text")) if "text" in c.keys() else ""
                if text and c.winfo_ismapped() and c.winfo_rootx() + c.winfo_reqwidth() > right + 2:
                    cut.append((section, text[:60]))
        win.destroy()
        self.assertEqual(cut, [])

    def test_checksum_given_when_adding_is_verified_at_the_end(self):
        app = self.open_app()
        for name, given, expect in (("bom.bin", hashlib.sha256(DATA).hexdigest(), "SHA-256 confere"),
                                    ("mau.bin", "0" * 64, "NÃO confere")):
            dlg = self.gui.AddDialog(app, self.base + "/" + name)
            dlg.checksum.set(given)
            dlg._ok(True)
            task = next(t for t in self.svc.manager.tasks if t.url.endswith(name))
            self.wait_status(task, "Concluído")
            for _ in range(100):
                pump(app, 0.1)
                if task.check:
                    break
            self.assertIn(expect, task.check)
            app._refresh(schedule=False)
            status_col = list(app.tree["columns"]).index("status")
            self.assertIn(expect, str(app.tree.item(task.id)["values"][status_col]))  # shown in the list

    def test_after_download_window_and_actions(self):
        app = self.open_app()
        dlg = self.gui.AfterDialog(app)
        dlg.open.set(True)
        dlg.command.set("echo {ficheiro}")
        dlg._save()
        s = self.svc.settings
        self.assertTrue(s.after_open)
        self.assertEqual(s.after_command, "echo {ficheiro}")
        with mock.patch("flowdl.after.open_file") as opened, mock.patch("flowdl.after.run_command", return_value="") as ran:
            app.url_var.set(self.base + "/acabou.bin")
            app._quick_add()
            task = self.svc.manager.tasks[-1]
            self.wait_status(task, "Concluído")
            for _ in range(100):
                pump(app, 0.1)
                if opened.called:
                    break
            opened.assert_called_once_with(task.path)
            ran.assert_called_once()

    def test_check_for_updates(self):
        from flowdl import updater
        app = self.open_app()
        with mock.patch.object(updater, "check", return_value=(None, "Está na versão mais recente (0.8.0).")), \
                mock.patch.object(self.gui.messagebox, "showinfo") as info:
            app._check_updates()
            pump(app, 2, lambda: info.called)
        self.assertIn("mais recente", info.call_args[0][1])
        newer = updater.Update("9.9.9", "https://x/FlowDownloads-Setup.exe", "https://x/SHA256SUMS.txt", "Novidades", "https://x/page")
        with mock.patch.object(updater, "check", return_value=(newer, "Há uma versão nova: 9.9.9")), \
                mock.patch.object(updater, "can_install", return_value=True), \
                mock.patch.object(self.gui.messagebox, "askyesno", return_value=False) as ask, \
                mock.patch.object(updater, "download") as dl:
            app._check_updates()
            pump(app, 2, lambda: ask.called)
        self.assertIn("9.9.9", ask.call_args[0][1])
        dl.assert_not_called()  # the user said no: nothing downloaded

    def test_video_window_without_ytdlp_offers_install(self):
        app = self.open_app()
        with mock.patch.object(self.gui.video, "available", return_value=False):
            dlg = self.gui.VideoDialog(app, "https://www.youtube.com/watch?v=abc")
            pump(app, 0.3)
            self.assertTrue(dlg.install_btn.winfo_ismapped())
            self.assertIn("yt-dlp", dlg.info.get())

    def test_video_window_install_button_runs_pip(self):
        app = self.open_app()
        with mock.patch.object(self.gui.video, "available", return_value=False), \
                mock.patch.object(self.gui.video, "install_yt_dlp", return_value=(False, "sem internet")), \
                mock.patch.object(self.gui.messagebox, "showerror") as err:
            dlg = self.gui.VideoDialog(app, "https://www.youtube.com/watch?v=abc")
            dlg._install()
            pump(app, 2, lambda: err.called)
        err.assert_called_once()
        self.assertIn("sem internet", err.call_args[0][1])

    def test_video_window_installs_ffmpeg_and_shows_the_better_qualities(self):
        """Without ffmpeg YouTube stops at 360p: the window offers "Instalar ffmpeg" (Windows)
        and, once installed, lists the qualities again."""
        app = self.open_app()
        low = {"title": "T", "url": "u", "limited": True,
               "options": [{"label": "360p", "format": "18", "audio_only": False}]}
        full = {"title": "T", "url": "u", "limited": False,
                "options": [{"label": "1080p", "format": "137+140", "audio_only": False},
                            {"label": "360p", "format": "18", "audio_only": False}]}
        answers = [low, full]
        with mock.patch.object(self.gui.video, "WINDOWS", True), \
                mock.patch.object(self.gui.video, "available", return_value=True), \
                mock.patch.object(self.gui.video, "get_options", side_effect=lambda *a, **k: answers.pop(0)), \
                mock.patch.object(self.gui.video, "install_ffmpeg",
                                  side_effect=lambda progress: (progress(50, 100), (True, "ffmpeg instalado"))[1]):
            dlg = self.gui.VideoDialog(app, "https://www.youtube.com/watch?v=abc")
            pump(app, 3, lambda: dlg.tree.get_children())
            pump(app, 0.3)
            self.assertTrue(dlg.ffmpeg_btn.winfo_ismapped())
            self.assertIn("ffmpeg", dlg.info.get())
            dlg._install_ffmpeg()
            pump(app, 4, lambda: "q1" in dlg.tree.get_children())
            labels = [dlg.tree.item(i)["values"][0] for i in dlg.tree.get_children()]
            self.assertEqual(labels, ["1080p", "360p"])
            self.assertFalse(dlg.ffmpeg_btn.winfo_ismapped())
            dlg.destroy()

    def test_video_window_lists_qualities_and_downloads_mp3(self):
        from flowdl import video
        if not (video.available() and self.have_ffmpeg):
            self.skipTest("yt-dlp/ffmpeg não instalados")
        app = self.open_app()
        dlg = self.gui.VideoDialog(app, self.site_url + "/index.html", "Meu vídeo")
        self.assertTrue(pump(app, 30, lambda: dlg.tree.exists("q0")), dlg.info.get())
        rows = {dlg.tree.item(i)["values"][0]: i for i in dlg.tree.get_children()}
        self.assertIn("Só áudio (MP3)", rows)
        dlg.tree.selection_set(rows["Só áudio (MP3)"])
        dlg._download()
        task = self.svc.manager.tasks[0]
        self.wait_status(task, "Concluído", 60)
        self.assertEqual(os.path.basename(task.path), "Meu vídeo.mp3")

    # ---------------------------------------------------- progress window
    def test_progress_window_follows_and_closes_when_done(self):
        Handler.delay = 0.03
        app = self.open_app()
        task = app.add_download(self.base + "/progresso.bin", self.dl, 4)
        win = self.gui.ProgressWindow(app, task)
        self.assertTrue(pump(app, 10, lambda: win.bar["value"] > 0))
        self.assertIn("progresso.bin", win.title())
        Handler.delay = 0.0
        self.assertTrue(pump(app, 30, lambda: not self._alive(win)), "the window should close by itself")
        self.assertEqual(task.status, "Concluído")

    def test_progress_window_pause_resume_cancel(self):
        Handler.delay = 0.2
        app = self.open_app()
        task = app.add_download(self.base + "/controlo.bin", self.dl, 2)
        win = self.gui.ProgressWindow(app, task)
        self.wait_status(task, "Baixando", 10)
        win._toggle()
        self.wait_status(task, "Pausado", 10)
        pump(app, 0.6)
        self.assertEqual(str(win.pause_btn["text"]), "Retomar")
        win._toggle()
        self.wait_status(task, "Baixando", 10)
        with mock.patch.object(self.gui.messagebox, "askyesno", return_value=True):
            win._cancel()
        pump(app, 0.5)
        self.assertNotIn(task, self.svc.manager.tasks)
        self.assertFalse(self._alive(win))

    def test_window_objects_are_only_freed_in_the_window_thread(self):
        """Regression: a download thread saving the list ran the automatic memory clean-up,
        which freed a Tk variable of a closed window outside the Tk thread: both froze."""
        import gc
        app = self.open_app()
        self.assertFalse(gc.isenabled())  # while windows exist: only the Tk thread cleans up
        for _ in range(5):
            dlg = self.gui.HostLimitsDialog(app)
            dlg.destroy()  # leaves Tk objects in reference cycles
        made_in_other_threads = []

        def churn():  # what download threads do: lots of allocations
            for _ in range(20000):
                made_in_other_threads.append({"a": [1, 2, 3]})
        threads = [threading.Thread(target=churn) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive(), "a thread froze")
        pump(app, 3.5)  # the periodic clean-up in the Tk thread runs
        self.assertFalse(gc.isenabled())

    def test_quit_while_progress_window_is_open(self):
        """Regression: closing the app with a progress window open raised a Tk error
        ("can't delete Tcl command") and the app did not close."""
        Handler.delay = 0.2
        app = self.open_app()
        task = app.add_download(self.base + "/sair.bin", self.dl, 2)
        self.gui.ProgressWindow(app, task)
        pump(app, 1)
        app.quit_all()
        self.assertFalse(self._alive(app))
        self.assertTrue(self.svc.exit_requested)

    # ------------------------------------------------------------ categories
    def test_categories_window_creates_category_with_own_folder(self):
        app = self.open_app()
        dlg = self.gui.CategoriesDialog(app)
        own = os.path.join(self.tmp, "Trabalho")
        dlg.store({"name": "Trabalho", "exts": ["xyz"], "folder": own, "url_patterns": [], "name_patterns": []}, None)
        dlg.on.set(True)
        dlg._save()
        self.assertTrue(app.side.exists("cat:Trabalho"))
        self.assertIn("Trabalho", [r["name"] for r in self.svc.settings.categories])
        self.assertIn('"Trabalho"', read_text(self.svc.settings.path))
        task = app.add_download(self.base + "/relatorio.xyz", self.dl, 2, auto_folder=True)
        self.wait_status(task, "Concluído")
        self.assertEqual(os.path.dirname(task.path), own)
        app.side.selection_set("cat:Trabalho")
        pump(app, 0.4)
        self.assertEqual([app.tree.item(i)["values"][0] for i in app.tree.get_children()], ["relatorio.xyz"])

    def test_category_editor_validates_and_reset(self):
        app = self.open_app()
        dlg = self.gui.CategoriesDialog(app)
        ed = self.gui.CategoryEditor(dlg, {"name": "", "exts": []}, None)
        with mock.patch.object(self.gui.messagebox, "showwarning") as warn:
            ed._ok()  # no name
        warn.assert_called_once()
        ed.name.set("Livros")
        ed.exts.set(".EPUB, mobi")
        ed._ok()
        self.assertEqual(dlg.rules[-1]["exts"], ["epub", "mobi"])
        dlg._reset()
        self.assertNotIn("Livros", [r["name"] for r in dlg.rules])

    def test_quick_add_uses_category_folder(self):
        app = self.open_app()
        self.svc.settings.category_folders = True
        self.svc.apply_category_settings()
        app.url_var.set(self.base + "/jogo.zip")
        app._quick_add()
        task = self.svc.manager.tasks[0]
        self.wait_status(task, "Concluído")
        self.assertEqual(os.path.dirname(task.path), os.path.join(self.dl, "Compactados"))

    def test_capture_types_window(self):
        app = self.open_app()
        dlg = self.gui.CaptureDialog(app)
        dlg.types.delete("1.0", "end")
        dlg.types.insert("1.0", "zip, .MKV  pdf")
        dlg.sites.insert("1.0", "banco.pt\n")
        dlg._save()
        s = self.svc.settings
        self.assertEqual(s.capture_types, ["zip", "mkv", "pdf"])
        self.assertEqual(s.capture_skip_sites, ["banco.pt"])
        self.assertIn('"banco.pt"', read_text(s.path))
        dlg = self.gui.CaptureDialog(app)
        dlg.all.set(True)
        dlg._toggle()
        self.assertEqual(str(dlg.types["state"]), "disabled")
        dlg._save()
        self.assertTrue(s.capture_all_types)

    def test_host_limits_window(self):
        app = self.open_app()
        dlg = self.gui.HostLimitsDialog(app)
        dlg.text.insert("1.0", "www.lento.com = 2\nisto não é válido\n")
        # A line that is not understood is pointed out, not silently dropped (found in a test of
        # every window): nothing is saved until it is fixed.
        with mock.patch.object(self.gui.messagebox, "showwarning") as warn:
            dlg._save()
        warn.assert_called_once()
        self.assertIn("isto não é válido", warn.call_args[0][1])
        self.assertTrue(dlg.winfo_exists())
        dlg.text.delete("1.0", "end")
        dlg.text.insert("1.0", "www.lento.com = 2\n")
        dlg._save()
        self.assertEqual(self.svc.settings.host_limits, {"lento.com": 2})
        self.assertEqual(self.svc.manager.host_limits, {"lento.com": 2})
        task = self.svc.manager.add("https://cdn.lento.com/a.zip", self.tmp, 8, start=False)
        self.assertEqual(task.connections, 2)

    # ------------------------------------------------------------ duplicates
    def test_same_link_twice_finished_open_or_again(self):
        app = self.open_app()
        app.url_var.set(self.base + "/repetido.bin")
        app._quick_add()
        first = self.svc.manager.tasks[0]
        self.wait_status(first, "Concluído")
        app.url_var.set(self.base + "/repetido.bin")
        app._quick_add()
        pump(app, 0.3)
        dlg = dialogs(app, self.gui.DuplicateDialog)
        self.assertEqual(len(dlg), 1)
        self.assertEqual(len(self.svc.manager.tasks), 1)  # nothing added before the user decides
        with mock.patch.object(self.gui, "open_path") as op:
            dlg[0]._open()
        op.assert_called_once_with(first.path)
        app.url_var.set(self.base + "/repetido.bin")
        app._quick_add()
        pump(app, 0.3)
        dialogs(app, self.gui.DuplicateDialog)[0]._new()
        self.assertEqual(len(self.svc.manager.tasks), 2)  # "Baixar de novo"

    def test_same_link_twice_unfinished_resume(self):
        app = self.open_app()
        app.add_download(self.base + "/ameio.bin", self.dl, 2, start=False)
        existing = self.svc.manager.tasks[0]
        dlg = self.gui.AddDialog(app, self.base + "/ameio.bin")
        dlg._ok(True)
        pump(app, 0.3)
        dup = dialogs(app, self.gui.DuplicateDialog)
        self.assertEqual(len(dup), 1)
        dup[0]._resume()
        self.wait_status(existing, "Concluído")
        self.assertEqual(len(self.svc.manager.tasks), 1)

    def test_browser_sends_a_link_already_in_the_list(self):
        self.svc.settings.confirm_browser = False
        app = self.open_app()
        app.add_download(self.base + "/donavegador.bin", self.dl, 2, start=False)
        existing = self.svc.manager.tasks[0]
        item = {"url": self.base + "/donavegador.bin", "rid": "r1", "referrer": None, "cookies": None}
        self.svc._set_request("r1", state="waiting")
        self.assertTrue(self.svc.accept_item(item))
        app._process_events()
        pump(app, 0.3)
        dup = dialogs(app, self.gui.DuplicateDialog)
        self.assertEqual(len(dup), 1)
        dup[0]._resume()
        self.assertEqual(self.svc.requests["r1"]["state"], "started")  # the browser shows the existing one
        self.assertEqual(self.svc.requests["r1"]["task"], existing.id)
        self.wait_status(existing, "Concluído")
        # cancelling the question: the extension stops waiting, nothing is added
        self.svc._set_request("r2", state="waiting")
        self.svc.accept_item({**item, "rid": "r2"})
        app._process_events()
        pump(app, 0.3)
        dialogs(app, self.gui.DuplicateDialog)[0].destroy()
        pump(app, 0.2)
        self.assertEqual(self.svc.requests["r2"]["state"], "cancelled")
        self.assertEqual(len(self.svc.manager.tasks), 1)

    # -------------------------------------------------------- other windows
    def test_site_grabber(self):
        app = self.open_app()
        dlg = self.gui.GrabberDialog(app)
        dlg.url.set(self.site_url + "/index.html")
        for cat, var in dlg.cat_vars.items():
            var.set(cat in ("Compactados", "Documentos"))
        dlg._search()
        self.assertTrue(pump(app, 15, lambda: len(dlg.tree.get_children()) > 0))
        names = sorted(dlg.tree.item(i)["values"][0] for i in dlg.tree.get_children())
        self.assertEqual(names, ["a.zip", "b.pdf"])
        dlg._download()
        tasks = self.svc.manager.tasks
        self.assertEqual(len(tasks), 2)
        self.assertTrue(pump(app, 30, lambda: all(t.status == "Concluído" for t in tasks)))

    def test_scheduler_window(self):
        app = self.open_app()
        dlg = self.gui.SchedulerDialog(app)
        dlg.start_on.set(True)
        dlg.start_at.set("25:99")
        with mock.patch.object(self.gui.messagebox, "showwarning") as warn:
            dlg._save()
        warn.assert_called_once()
        dlg.start_at.set("03:15")
        dlg.stop_on.set(True)
        dlg.stop_at.set("07:45")
        for i, var in enumerate(dlg.days):
            var.set(i < 5)
        dlg.action.set(self.gui.ACTIONS["shutdown"])
        dlg._save()
        s = self.svc.scheduler
        self.assertEqual((s.start_enabled, s.start_time, s.stop_time, s.days, s.on_complete),
                         (True, "03:15", "07:45", [0, 1, 2, 3, 4], "shutdown"))
        self.assertIn('"start_time": "03:15"', read_text(self.svc.settings.path))

    def test_shutdown_countdown_can_be_cancelled(self):
        app = self.open_app()
        with mock.patch.object(self.gui, "shutdown_computer") as off:
            cd = self.gui.ShutdownCountdown(app, seconds=2)
            pump(app, 0.5)
            cd.destroy()  # "Cancelar"
            pump(app, 3)
        off.assert_not_called()
        self.assertFalse(self.svc.exit_requested)

    def test_sleep_when_downloads_finish_keeps_the_app_running(self):
        app = self.open_app()
        with mock.patch.object(self.gui, "sleep_computer") as zz, \
                mock.patch.object(self.gui, "shutdown_computer") as off:
            cd = self.gui.ShutdownCountdown(app, seconds=1, action="sleep")
            self.assertIn("suspenso", cd.msg.get())
            pump(app, 2.5)
        zz.assert_called_once()
        off.assert_not_called()
        self.assertFalse(self.svc.exit_requested)  # still there when the computer wakes up
        self.assertEqual(self.svc.on_tick.__self__, self.svc)
        self.svc.scheduler.on_complete = "sleep"
        task = self.svc.manager.add(self.base + "/fim.bin", self.tmp, 2)
        self.wait_status(task, "Concluído")
        self.assertEqual(self.svc.on_tick(task), "countdown")

    def test_shutdown_after_all_downloads_finish(self):
        self.svc.scheduler.on_complete = "shutdown"
        app = self.open_app()
        with mock.patch.object(self.gui, "shutdown_computer") as off, \
                mock.patch.object(self.gui.ShutdownCountdown, "__init__",
                                  lambda s, a, seconds=30, action=None: (tk.Toplevel.__init__(s, a),
                                                            setattr(s, "app", a), setattr(s, "left", 1),
                                                            setattr(s, "sleeping", False),
                                                            setattr(s, "msg", tk.StringVar()), s._tick())[0]):
            app.add_download(self.base + "/ultimo.bin", self.dl, 2)
            pump(app, 20, lambda: off.called)
        off.assert_called_once()
        self.assertTrue(self.svc.exit_requested)

    def test_close_when_all_finished(self):
        self.svc.scheduler.on_complete = "close"
        app = self.open_app()
        app.add_download(self.base + "/fim.bin", self.dl, 2)
        pump(app, 20, lambda: self.svc.exit_requested)
        self.assertTrue(self.svc.exit_requested)

    # ------------------------------------------------------ window life
    def test_close_window_keeps_running_and_quit_exits(self):
        app = self.open_app()
        app.close_window()
        self.assertFalse(self._alive(app))
        self.assertFalse(self.svc.exit_requested)
        app2 = self.open_app()
        app2.quit_all()
        self.assertTrue(self.svc.exit_requested)

    def test_first_close_explains_background(self):
        self.svc.settings.background_hint_shown = False
        app = self.open_app()
        with mock.patch.object(self.gui.messagebox, "showinfo") as info:
            app.close_window()
        info.assert_called_once()
        self.assertTrue(self.svc.settings.background_hint_shown)

    def test_close_exits_when_background_disabled(self):
        app = self.open_app()
        app.bg_var.set(False)
        app.bg_var.set(False)
        self.svc.settings.run_in_background = False
        app.close_window()
        self.assertTrue(self.svc.exit_requested)

    def test_options_are_saved(self):
        app = self.open_app()
        app.resume_var.set(False)
        app.confirm_var.set(False)
        app.clip_var.set(False)
        app.close_window()
        text = read_text(self.svc.settings.path)
        for key in ('"auto_resume": false', '"confirm_browser": false', '"watch_clipboard": false'):
            self.assertIn(key, text)

    def test_browser_integration_toggle(self):
        app = self.open_app()
        app.bridge_var.set(True)
        app._toggle_bridge()
        self.assertIsNotNone(self.svc.bridge)
        app.bridge_var.set(False)
        app._toggle_bridge()
        self.assertIsNone(self.svc.bridge)

    def test_start_with_computer_option(self):
        from flowdl import autostart
        if not autostart.supported() or os.name == "nt":
            self.skipTest("teste do arranque automático feito em Linux")
        app = self.open_app()
        app.autostart_var.set(True)
        app._toggle_autostart()
        desktop = os.path.join(TMP_CONFIG, "autostart", "flow-downloads.desktop")
        self.assertTrue(os.path.exists(desktop))
        self.assertIn("--background", read_text(desktop))
        app.autostart_var.set(False)
        app._toggle_autostart()
        self.assertFalse(os.path.exists(desktop))

    def test_help_and_about(self):
        app = self.open_app()
        with mock.patch.object(self.gui.messagebox, "showinfo") as info:
            app._extension_help()
        self.assertIn("brave://extensions", info.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
