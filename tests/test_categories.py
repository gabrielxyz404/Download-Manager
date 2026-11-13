"""K: categories with their own folders and rules."""

import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flowdl.categories import OTHER, default_rules, folder_for, pick_category
from flowdl.engine import Status
from flowdl.manager import DownloadManager

DATA = os.urandom(300_000)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        if self.path.startswith("/download"):  # the link says nothing; the real name comes in the answer
            self.send_header("Content-Disposition", 'attachment; filename="relatorio.zip"')
        self.send_header("Content-Length", str(len(DATA)))
        self.end_headers()
        self.wfile.write(DATA)


class RuleTests(unittest.TestCase):
    def test_file_types(self):
        self.assertEqual(pick_category("filme.MKV"), "Vídeos")
        self.assertEqual(pick_category("setup.exe"), "Programas")
        self.assertEqual(pick_category("sem_extensao"), OTHER)

    def test_address_and_name_patterns(self):
        rules = default_rules() + [
            {"name": "Trabalho", "exts": [], "folder": None,
             "url_patterns": ["intranet.empresa.pt/"], "name_patterns": ["fatura_*"]}]
        rules = [rules[-1]] + rules[:-1]  # first matching category wins
        self.assertEqual(pick_category("x.pdf", "https://intranet.empresa.pt/docs/x.pdf", rules=rules), "Trabalho")
        self.assertEqual(pick_category("fatura_2026_03.pdf", "https://banco.pt/f", rules=rules), "Trabalho")
        self.assertEqual(pick_category("outra.pdf", "https://banco.pt/f", rules=rules), "Documentos")
        self.assertEqual(pick_category("y.zip", "https://a.com", page="https://intranet.empresa.pt/", rules=rules),
                         "Trabalho")  # the page address counts too

    def test_folders(self):
        base = os.path.join("C:", "Downloads") if os.name == "nt" else "/home/x/Downloads"
        self.assertEqual(folder_for(base, "a.mp4"), os.path.join(base, "Vídeos"))
        self.assertEqual(folder_for(base, "a.unknown"), base)
        rules = default_rules()
        rules[0]["folder"] = "/discos/arquivo"
        self.assertEqual(folder_for(base, "a.zip", rules=rules), "/discos/arquivo")


class AutoFolderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mgr = DownloadManager(db_path=os.path.join(self.tmp, "db.json"))
        self.mgr.folder_rules = default_rules()

    def finish(self, task):
        task.wait(30)
        self.assertEqual(task.status, Status.COMPLETED, task.error)
        return task.path

    def test_folder_chosen_from_the_real_file_name(self):
        path = self.finish(self.mgr.add(self.base + "/download?id=7", self.tmp, 2, auto_folder=True))
        self.assertEqual(path, os.path.join(self.tmp, "Compactados", "relatorio.zip"))

    def test_by_type_in_the_link(self):
        path = self.finish(self.mgr.add(self.base + "/musica.mp3", self.tmp, 2, auto_folder=True))
        self.assertEqual(os.path.dirname(path), os.path.join(self.tmp, "Músicas"))

    def test_off_or_chosen_by_hand(self):
        path = self.finish(self.mgr.add(self.base + "/filme.mp4", self.tmp, 2, auto_folder=False))
        self.assertEqual(os.path.dirname(path), self.tmp)
        self.mgr.folder_rules = None  # option switched off
        path = self.finish(self.mgr.add(self.base + "/filme2.mp4", self.tmp, 2, auto_folder=True))
        self.assertEqual(os.path.dirname(path), self.tmp)


if __name__ == "__main__":
    unittest.main()
