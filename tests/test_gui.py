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
        with mock.patch.object(self.gui.simpledialog, "askinteger", return_value=0):
            app._global_limit()
        self.assertEqual(GLOBAL_LIMITER.rate, 0)

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

    def test_dark_theme_and_columns(self):
        app = self.open_app()
        app.dark_var.set(True)
        app._toggle_theme()
        style = self.gui.ttk.Style(app)
        self.assertEqual(style.lookup("Treeview", "background"), self.gui.THEMES["dark"]["field"])
        self.assertTrue(self.svc.settings.dark)
        app.col_vars["speed"].set(False)
        app.col_vars["url"].set(True)
        app._apply_columns()
        shown = list(app.tree["displaycolumns"])
        self.assertNotIn("speed", shown)
        self.assertIn("url", shown)
        self.assertEqual(self.svc.settings.columns, shown)

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
        pump(app, 2)
        self.assertFalse(app.winfo_exists() if self._alive(app) else False)  # app closed itself
        task = self.svc.manager.tasks[0]
        task.wait(30)
        self.assertEqual(task.status, "Concluído", task.error)
        self.assertEqual(task.headers, {"Referer": "https://site/", "Cookie": "s=1"})

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

    def test_shutdown_after_all_downloads_finish(self):
        self.svc.scheduler.on_complete = "shutdown"
        app = self.open_app()
        with mock.patch.object(self.gui, "shutdown_computer") as off, \
                mock.patch.object(self.gui.ShutdownCountdown, "__init__",
                                  lambda s, a, seconds=30: (tk.Toplevel.__init__(s, a),
                                                            setattr(s, "app", a), setattr(s, "left", 1),
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
