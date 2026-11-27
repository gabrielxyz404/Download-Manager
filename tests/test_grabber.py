"""Site grabber: depth, filters, staying on the site / folder, robots.txt (local sites only)."""
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flowdl import grabber


class Site(BaseHTTPRequestHandler):
    other = ""  # address of another site (set by the test)
    asked: list = []
    pages = {
        "/robots.txt": "User-agent: *\nDisallow: /secreto/\n",
        "/": '<a href="/docs/">docs</a> <a href="/a.zip">a</a> <a href="/thumbs/t1.jpg">t</a>'
             '<a href="/secreto/">s</a> <a href="OTHER/fora.html">fora</a> <a href="OTHER/x.zip">x</a>',
        "/docs/": '<a href="/docs/n1/">n1</a> <a href="manual.pdf">m</a> <a href="/raiz.html">r</a>',
        "/docs/n1/": '<a href="/docs/n1/n2/">n2</a> <a href="nivel2.pdf">p</a>',
        "/docs/n1/n2/": '<a href="/docs/n1/n2/n3/">n3</a> <a href="nivel3.zip">z</a>',
        "/docs/n1/n2/n3/": '<a href="nivel4.zip">z</a>',
        "/secreto/": '<a href="segredo.zip">z</a>',
        "/raiz.html": '<a href="/raiz.zip">z</a>',
        "/fora.html": '<a href="/fora.zip">z</a>',
    }

    def log_message(self, *a):
        pass

    def do_GET(self):
        Site.asked.append((self.server.server_address[1], self.path))
        body = Site.pages.get(self.path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = body.replace("OTHER", Site.other).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain" if self.path.endswith(".txt") else "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class GrabberTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servers = []
        for _ in range(2):
            s = ThreadingHTTPServer(("127.0.0.1", 0), Site)
            s.daemon_threads = True
            threading.Thread(target=s.serve_forever, daemon=True).start()
            cls.servers.append(s)
        cls.base = f"http://127.0.0.1:{cls.servers[0].server_address[1]}"
        Site.other = f"http://localhost:{cls.servers[1].server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        for s in cls.servers:
            s.shutdown()

    def setUp(self):
        Site.asked = []

    def names(self, start="/", **kw):
        kw.setdefault("pause", 0)
        return sorted(i["name"] for i in grabber.grab(self.base + start, **kw))

    def test_depth(self):
        self.assertEqual(self.names(depth=0), ["a.zip", "t1.jpg", "x.zip"])
        self.assertIn("manual.pdf", self.names(depth=1))
        self.assertIn("nivel3.zip", self.names(depth=3))
        self.assertNotIn("nivel4.zip", self.names(depth=3))

    def test_stays_on_the_site_and_respects_robots(self):
        got = self.names(depth=2)
        self.assertNotIn("fora.zip", got)  # (another site: not visited)
        self.assertNotIn("segredo.zip", got)  # (robots.txt forbids /secreto/)
        other = self.servers[1].server_address[1]
        self.assertFalse([p for port, p in Site.asked if port == other and p != "/robots.txt"])
        self.assertIn("segredo.zip", self.names(depth=1, robots=False))
        self.assertIn("fora.zip", self.names(depth=1, scope="any", robots=False))

    def test_folder_scope(self):
        got = self.names("/docs/", depth=2, scope="folder")
        self.assertIn("nivel2.pdf", got)
        self.assertNotIn("raiz.zip", got)  # (outside /docs/)
        self.assertIn("raiz.zip", self.names("/docs/", depth=1, scope="site"))

    def test_include_and_exclude(self):
        self.assertEqual(self.names(depth=1, include="*.pdf"), ["manual.pdf"])
        self.assertNotIn("t1.jpg", self.names(exclude="*/thumbs/*"))
        self.assertEqual(self.names(include="*.zip, *.pdf", exclude="x.*"), ["a.zip"])


if __name__ == "__main__":
    unittest.main()
