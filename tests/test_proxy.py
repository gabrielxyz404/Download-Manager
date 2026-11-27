"""Proxies chosen in the app (SOCKS5, HTTP) and Basic / Digest authentication, against small
local proxies and sites (no real network)."""
import hashlib
import re
import select
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flowdl import net, proxy
from flowdl.engine import DownloadTask, Status

DATA = bytes(range(256)) * 8192  # 2 MB


class FileSite(BaseHTTPRequestHandler):
    """A file with ranges. With ``auth`` set, it asks for a user / password (Basic or Digest,
    MD5 or SHA-256), and says "stale" once to see the client renew its answer."""
    protocol_version = "HTTP/1.1"
    auth = None  # None | ("basic"|"digest", algorithm)
    stale_once = False
    nonce_n = 0
    seen_auth: list = []

    def log_message(self, *a):
        pass

    def _challenge(self, stale=False):
        kind, algo = FileSite.auth
        FileSite.nonce_n += 1
        if kind == "basic":
            value = 'Basic realm="arquivos"'
        else:
            value = (f'Digest realm="arquivos", qop="auth", nonce="n{FileSite.nonce_n}", '
                     f'opaque="op", algorithm={algo}' + (", stale=true" if stale else ""))
        self.send_response(401)
        self.send_header("WWW-Authenticate", value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authorized(self) -> bool:
        if FileSite.auth is None:
            return True
        header = self.headers.get("Authorization", "")
        FileSite.seen_auth.append(header.split(" ")[0])
        kind, algo = FileSite.auth
        if kind == "basic":
            import base64
            return header == "Basic " + base64.b64encode(b"ana:s3gredo").decode()
        scheme, p = proxy.parse_challenge(header)
        if scheme != "digest" or p.get("username") != "ana":
            return False
        if FileSite.stale_once and p.get("nonce") == "n1":
            FileSite.stale_once = False
            self._challenge(stale=True)
            return None
        name = "sha256" if algo.startswith("SHA") else "md5"
        h = lambda s: hashlib.new(name, s.encode()).hexdigest()
        ha1 = h("ana:arquivos:s3gredo")
        ha2 = h(f"GET:{p.get('uri')}")
        want = h(f"{ha1}:{p['nonce']}:{p['nc']}:{p['cnonce']}:auth:{ha2}")
        return p.get("response") == want and p.get("opaque") == "op" and p.get("uri") == self.path

    def do_GET(self):
        ok = self._authorized()
        if ok is None:
            return
        if not ok:
            return self._challenge()
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        a = int(m.group(1)) if m else 0
        b = min(int(m.group(2)) if m and m.group(2) else len(DATA) - 1, len(DATA) - 1)
        self.send_response(206 if m else 200)
        if m:
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(DATA)}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(b - a + 1))
        self.end_headers()
        try:
            self.wfile.write(DATA[a:b + 1])
        except OSError:
            pass


def pipe(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 30)
            if not r:
                return
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        pass
    finally:
        a.close()
        b.close()


class Socks5(socketserver.BaseRequestHandler):
    """A minimal SOCKS5 proxy (no auth or user/password), forwarding to the real address."""
    user = None  # (user, password) to require
    seen: list = []  # (address type, host) asked for

    def recv(self, n):
        data = b""
        while len(data) < n:
            chunk = self.request.recv(n - len(data))
            if not chunk:
                raise OSError("closed")
            data += chunk
        return data

    def handle(self):
        s = self.request
        _ver, n = self.recv(2)
        methods = self.recv(n)
        if Socks5.user:
            if 2 not in methods:
                return s.sendall(b"\x05\xff")
            s.sendall(b"\x05\x02")
            _v, ul = self.recv(2)
            user = self.recv(ul)
            pl = self.recv(1)[0]
            pw = self.recv(pl)
            if (user.decode(), pw.decode()) != Socks5.user:
                return s.sendall(b"\x01\x01")
            s.sendall(b"\x01\x00")
        else:
            s.sendall(b"\x05\x00")
        _v, cmd, _r, atyp = self.recv(4)
        if atyp == 1:
            host = socket.inet_ntoa(self.recv(4))
        elif atyp == 3:
            host = self.recv(self.recv(1)[0]).decode()
        else:
            host = socket.inet_ntop(socket.AF_INET6, self.recv(16))
        port = int.from_bytes(self.recv(2), "big")
        Socks5.seen.append((atyp, host))
        try:
            out = socket.create_connection(("127.0.0.1" if host == "localhost" else host, port), 5)
        except OSError:
            return s.sendall(b"\x05\x05\x00\x01" + b"\0" * 6)
        s.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + (0).to_bytes(2, "big"))
        pipe(s, out)


class HttpProxy(socketserver.BaseRequestHandler):
    """A minimal HTTP proxy: CONNECT (for HTTPS) and whole-address requests (plain HTTP),
    asking for a user / password with Digest (or Basic)."""
    auth = None  # None | "basic" | "digest"
    seen: list = []

    def check(self, method, uri, header) -> bool:
        if HttpProxy.auth is None:
            return True
        if HttpProxy.auth == "basic":
            import base64  # noqa: F811
            return header == "Basic " + base64.b64encode(b"rui:pr0xy").decode()
        scheme, p = proxy.parse_challenge(header)
        if scheme != "digest":
            return False
        h = lambda s: hashlib.md5(s.encode()).hexdigest()
        want = h(f"{h('rui:proxy:pr0xy')}:{p.get('nonce')}:{p.get('nc')}:{p.get('cnonce')}:auth:{h(f'{method}:{uri}')}")
        return p.get("response") == want and p.get("uri") == uri

    def head(self, f):
        line = f.readline().decode()
        hdrs = {}
        while True:
            h = f.readline().decode()
            if h in ("\r\n", "\n", ""):
                break
            k, _, v = h.partition(":")
            hdrs[k.strip().lower()] = v.strip()
        return line, hdrs

    def handle(self):
        f = self.request.makefile("rb")
        while True:
            line, hdrs = self.head(f)
            if not line:
                return
            method, uri, _ = line.split(" ", 2)
            HttpProxy.seen.append((method, uri, hdrs.get("proxy-authorization", "").split(" ")[0]))
            if not self.check(method, uri, hdrs.get("proxy-authorization", "")):
                value = ('Basic realm="proxy"' if HttpProxy.auth == "basic" else
                         'Digest realm="proxy", qop="auth", nonce="pn1"')
                self.request.sendall(f"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: "
                                     f"{value}\r\nContent-Length: 0\r\n\r\n".encode())
                continue
            if method == "CONNECT":
                host, _, port = uri.rpartition(":")
                out = socket.create_connection((host, int(port)), 5)
                self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                return pipe(self.request, out)
            # whole address: forward one request
            from urllib.parse import urlsplit
            u = urlsplit(uri)
            out = socket.create_connection((u.hostname, u.port), 5)
            req = f"{method} {u.path or '/'} HTTP/1.1\r\n" + "".join(
                f"{k}: {v}\r\n" for k, v in hdrs.items() if k not in ("proxy-authorization", "connection")
            ) + "Connection: close\r\n\r\n"
            out.sendall(req.encode())
            while True:
                data = out.recv(65536)
                if not data:
                    break
                self.request.sendall(data)
            out.close()
            return


class Threaded(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass  # (a client that closes early)


class QuietHTTP(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


class _Servers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = QuietHTTP(("127.0.0.1", 0), FileSite)
        threading.Thread(target=cls.site.serve_forever, daemon=True).start()
        cls.port = cls.site.server_address[1]
        cls.socks = Threaded(("127.0.0.1", 0), Socks5)
        threading.Thread(target=cls.socks.serve_forever, daemon=True).start()
        cls.hproxy = Threaded(("127.0.0.1", 0), HttpProxy)
        threading.Thread(target=cls.hproxy.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for s in (cls.site, cls.socks, cls.hproxy):
            s.shutdown()
        proxy.configure("system")

    def setUp(self):
        FileSite.auth, FileSite.stale_once, FileSite.seen_auth = None, False, []
        Socks5.user, Socks5.seen = None, []
        HttpProxy.auth, HttpProxy.seen = None, []
        net._PROXY_CACHE.clear()
        net._SITE_CREDS.clear()
        proxy._PROXY_CREDS.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(proxy.configure, "system")

    def download(self, url, conns=4):
        task = DownloadTask(url, self.tmp.name, conns)
        task.start()
        task.wait(60)
        return task

    def assertWhole(self, task):
        self.assertEqual(task.status, Status.COMPLETED, task.error)
        with open(task.path, "rb") as f:
            self.assertEqual(f.read(), DATA)


class ProxyTests(_Servers):
    def test_socks5_with_the_name_looked_up_by_the_proxy(self):
        """Tor-style: the site's name goes to the proxy, never to the local DNS."""
        proxy.configure("socks5", "127.0.0.1", self.socks.server_address[1], bypass=[])
        self.assertWhole(self.download(f"http://localhost:{self.port}/f.bin"))
        self.assertTrue(Socks5.seen)
        self.assertTrue(all(s == (3, "localhost") for s in Socks5.seen), Socks5.seen)

    def test_socks5_with_local_lookup_and_password(self):
        Socks5.user = ("ana", "p4ss")
        proxy.configure("socks5", "127.0.0.1", self.socks.server_address[1], "ana", "p4ss",
                        remote_dns=False, bypass=[])
        self.assertWhole(self.download(f"http://localhost:{self.port}/f.bin"))
        self.assertTrue(all(atyp in (1, 4) for atyp, _h in Socks5.seen), Socks5.seen)

    def test_socks5_wrong_password_says_so_and_does_not_hammer(self):
        Socks5.user = ("ana", "p4ss")
        proxy.configure("socks5", "127.0.0.1", self.socks.server_address[1], "ana", "errada", bypass=[])
        t0 = time.monotonic()
        task = self.download(f"http://localhost:{self.port}/f.bin")
        self.assertEqual(task.status, Status.ERROR)
        self.assertIn("utilizador ou a senha", task.error)
        self.assertLess(time.monotonic() - t0, 30)
        self.assertLess(len(Socks5.seen) + 0, 5)

    def test_http_proxy_with_digest_for_plain_http(self):
        HttpProxy.auth = "digest"
        proxy.configure("http", "127.0.0.1", self.hproxy.server_address[1], "rui", "pr0xy", bypass=[])
        self.assertWhole(self.download(f"http://localhost:{self.port}/f.bin", conns=1))
        self.assertIn(("GET", f"http://localhost:{self.port}/f.bin", "Digest"), HttpProxy.seen)

    def test_https_tunnel_through_http_proxy_with_digest(self):
        HttpProxy.auth = "digest"
        proxy.configure("http", "127.0.0.1", self.hproxy.server_address[1], "rui", "pr0xy", bypass=[])
        r = proxy.route("https", "localhost")
        sock = proxy.http_tunnel(r, "localhost", self.port, 5)  # (then TLS would go on top)
        sock.sendall(b"GET /f.bin HTTP/1.1\r\nHost: x\r\nRange: bytes=0-9\r\n\r\n")
        self.assertTrue(sock.recv(100).startswith(b"HTTP/1.1 206"))
        sock.close()
        self.assertEqual([s[2] for s in HttpProxy.seen if s[0] == "CONNECT"], ["Basic", "Digest"])

    def test_http_proxy_wrong_password(self):
        HttpProxy.auth = "basic"
        proxy.configure("http", "127.0.0.1", self.hproxy.server_address[1], "rui", "errada", bypass=[])
        with self.assertRaises(proxy.ProxyError) as cm:
            proxy.http_tunnel(proxy.route("https", "localhost"), "localhost", self.port, 5)
        self.assertIn("senha", str(cm.exception))

    def test_bypass_and_none(self):
        proxy.configure("socks5", "127.0.0.1", 9, bypass=["*.interno.lan", "localhost"])
        self.assertIsNone(proxy.route("http", "localhost"))
        self.assertIsNone(proxy.route("https", "a.interno.lan"))
        self.assertIsNotNone(proxy.route("https", "exemplo.com"))
        proxy.configure("none")
        self.assertIsNone(proxy.route("https", "exemplo.com"))
        proxy.configure("http", "", 0)  # (no host: no proxy)
        self.assertIsNone(proxy.route("https", "exemplo.com"))

    def test_address_for_video_tools(self):
        proxy.configure("socks5", "127.0.0.1", 9150, "a b", "p@ss")
        self.assertEqual(proxy.url_for_tools(), "socks5h://a%20b:p%40ss@127.0.0.1:9150")
        proxy.configure("system")
        self.assertIsNone(proxy.url_for_tools())


class VideoProxyTests(unittest.TestCase):
    def test_videos_use_the_chosen_proxy(self):
        from flowdl import video
        self.addCleanup(proxy.configure, "system")
        proxy.configure("socks5", "127.0.0.1", 9150)
        self.assertEqual(video._base_opts()["proxy"], "socks5h://127.0.0.1:9150")
        proxy.configure("none")
        self.assertEqual(video._base_opts()["proxy"], "")
        proxy.configure("system")
        self.assertNotIn("proxy", video._base_opts())


class SiteAuthTests(_Servers):
    """A user / password in the link: answered with Basic or Digest (MD5, SHA-256), renewed
    when the server says the answer is stale, and only to the link's own site."""

    def run_auth(self, kind, algo="MD5", stale=False):
        FileSite.auth, FileSite.stale_once = (kind, algo), stale
        task = self.download(f"http://ana:s3gredo@127.0.0.1:{self.port}/f.bin")
        self.assertWhole(task)
        return task

    def test_basic(self):
        self.run_auth("basic")

    def test_digest_md5(self):
        self.run_auth("digest")
        self.assertIn("Digest", FileSite.seen_auth)

    def test_digest_sha256_with_stale_nonce(self):
        self.run_auth("digest", "SHA-256", stale=True)

    def test_wrong_password_is_an_error_not_a_loop(self):
        FileSite.auth = ("digest", "MD5")
        task = self.download(f"http://ana:errada@127.0.0.1:{self.port}/f.bin")
        self.assertEqual(task.status, Status.ERROR)
        self.assertIn("401", task.error)
        self.assertLess(len(FileSite.seen_auth), 20)

    def test_digest_unit(self):
        """RFC 2617's example (MD5, qop=auth)."""
        p = {"realm": "testrealm@host.com", "nonce": "dcd98b7102dd2f0e8b11d0f600bfb0c093", "qop": "auth",
             "opaque": "5ccc069c403ebaf9f0171e9517f40e41"}
        out = proxy.digest_header("Mufasa", "Circle Of Life", "GET", "/dir/index.html", p, 1, "0a4f113b")
        self.assertIn('response="6629fae49393a05397450978507c4ef1"', out)



if __name__ == "__main__":
    unittest.main()
