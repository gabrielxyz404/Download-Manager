"""System scenarios with the real app process: power loss, quit and reopen, single instance,
command line + Ctrl+C, unstable internet, start with the computer, resource use.

Linux only (uses /proc, xdotool and kill -9).  Run with:
    APP_PY=python3 xvfb-run -a python3 tests/e2e/system_e2e.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_PY = os.environ.get("APP_PY", sys.executable)
LAUNCHER = str(ROOT / "Flow Downloads.pyw")
DATA = os.urandom(8 * 1024 * 1024)
SHA = hashlib.sha256(DATA).hexdigest()
RESULTS: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(bool(ok))
    print(f"{'✔' if ok else '✘'} {name}{' — ' + detail if detail else ''}", flush=True)


class Site(BaseHTTPRequestHandler):
    rate = 0            # bytes/s per connection (0 = unlimited)
    drop_first = 0      # cut this many responses half-way (unstable internet)
    starts: list[int] = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        cls = type(self)
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        start, end = (int(m.group(1)), int(m.group(2)) if m.group(2) else len(DATA) - 1) if m else (0, len(DATA) - 1)
        if end > start:  # ignore the 1-byte size probe (bytes=0-0)
            cls.starts.append(start)
        body = DATA[start:end + 1]
        self.send_response(206 if m else 200)
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(DATA)}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        drop = False
        if cls.drop_first > 0 and len(body) > 200_000:
            cls.drop_first -= 1
            drop = True
        try:
            for i in range(0, len(body), 32 * 1024):
                if drop and i > len(body) // 2:
                    self.connection.shutdown(socket.SHUT_RDWR)  # connection lost mid-way
                    return
                self.wfile.write(body[i:i + 32 * 1024])
                if cls.rate:
                    time.sleep(32 * 1024 / cls.rate)
        except OSError:
            pass


def file_ok(path: Path) -> bool:
    return path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == SHA


def wait_for(fn, secs=20.0, step=0.2) -> bool:
    end = time.time() + secs
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return False


def ping() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 17890), timeout=1) as s:
            s.sendall(b"GET /ping HTTP/1.1\r\nX-FlowDL: 1\r\n\r\n")
            return s.recv(100).startswith(b"HTTP/1.1 200")
    except OSError:
        return False


def post(path: str, payload: dict) -> bool:
    body = json.dumps(payload).encode()
    try:
        with socket.create_connection(("127.0.0.1", 17890), timeout=3) as s:
            s.sendall(f"POST {path} HTTP/1.1\r\nX-FlowDL: 1\r\nContent-Type: application/json\r\n"
                      f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            return s.recv(100).startswith(b"HTTP/1.1 200")
    except OSError:
        return False


def app_pids() -> list[int]:
    out = subprocess.run(["pgrep", "-f", "Flow Downloads.pyw|flowdl.service import run"],
                         capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]


def rss_mb(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) // 1024
    return 0


class App:
    def __init__(self, cfg: Path, dl: Path, **settings):
        self.cfg, self.dl = cfg, dl
        (cfg / "FlowDownloads").mkdir(parents=True, exist_ok=True)
        path = cfg / "FlowDownloads" / "settings.json"
        current = json.loads(path.read_text()) if path.exists() else {}
        current.update({"dest_dir": str(dl), "background_hint_shown": True, **settings})
        path.write_text(json.dumps(current))
        self.env = {**os.environ, "XDG_CONFIG_HOME": str(cfg), "APPDATA": str(cfg)}

    def start(self, *args: str) -> subprocess.Popen:
        proc = subprocess.Popen([APP_PY, LAUNCHER, *args], env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        wait_for(ping, 10)
        return proc

    def tasks(self) -> list[dict]:
        try:
            return json.loads((self.cfg / "FlowDownloads" / "downloads.json").read_text())["tasks"]
        except (OSError, ValueError, KeyError):
            return []


def kill_all() -> None:
    for pid in app_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    wait_for(lambda: not app_pids() and not ping(), 5)


def windows(name: str) -> list[str]:
    out = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", name], capture_output=True, text=True)
    return out.stdout.split()


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="flowdl-sys-"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    site = f"http://127.0.0.1:{server.server_address[1]}"
    kill_all()

    # 1. Power loss in the middle of a download (kill -9), then the PC is turned on again.
    app = App(work / "c1", work / "dl1", confirm_browser=False, auto_resume=True)
    Site.rate, Site.starts = 100_000, []
    app.start("--background")
    post("/add", {"url": site + "/filme.mkv"})
    target = app.dl / "filme.mkv"
    part_json = Path(str(target) + ".part.json")
    wait_for(part_json.exists, 10)
    time.sleep(3)
    kill_all()  # sudden power loss: no chance to save anything
    done_before = 0
    if part_json.exists():
        segs = json.loads(part_json.read_text())["segments"]
        done_before = sum(pos - start for start, _end, pos in segs)
    check("Falta de luz: progresso guardado no disco", done_before > 0, f"{done_before // 1024} KB guardados")
    Site.rate, Site.starts = 0, []
    app.start("--background")  # computer turned on again
    check("Ao reabrir, o download recomeça sozinho e termina", wait_for(lambda: file_ok(target), 30))
    check("Continuou de onde parou (não recomeçou do zero)", Site.starts and min(Site.starts) > 0,
          f"primeiro byte pedido: {min(Site.starts) if Site.starts else '-'}")
    kill_all()

    # 2. Quit normally (Tarefas -> Sair) during a download, open again later.
    app = App(work / "c2", work / "dl2", confirm_browser=False, auto_resume=True)
    Site.rate = 100_000
    app.start("--background")
    post("/add", {"url": site + "/jogo.iso"})
    time.sleep(3)
    post("/show", {})
    wait_for(lambda: bool(windows("^Flow Downloads$")), 10)
    w = windows("^Flow Downloads$")
    if w:
        subprocess.run(["xdotool", "windowfocus", "--sync", w[0]])
        subprocess.run(["xdotool", "key", "ctrl+q"])
    exited = wait_for(lambda: not app_pids(), 10)
    statuses = [t.get("status") for t in app.tasks()]
    check("Ctrl+Q fecha a app de vez", exited)
    check("Download marcado para continuar", statuses == ["Na fila"], str(statuses))
    Site.rate = 0
    app.start("--background")
    check("Ao abrir de novo, continua e termina", wait_for(lambda: file_ok(app.dl / "jogo.iso"), 30))

    # 3. Single instance: opening the app again shows the running one.
    pids_before = app_pids()
    second = subprocess.Popen([APP_PY, LAUNCHER], env=app.env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    try:
        second.wait(10)
        quick_exit = True
    except subprocess.TimeoutExpired:
        quick_exit = False
    check("Abrir de novo não cria uma segunda cópia", quick_exit and app_pids() == pids_before)
    check("…e mostra a janela da que já está aberta", wait_for(lambda: bool(windows("^Flow Downloads$")), 10))
    kill_all()

    # 4. Unstable internet: connections drop half-way, the download still finishes intact.
    app = App(work / "c4", work / "dl4", confirm_browser=False)
    Site.drop_first, Site.rate = 6, 0
    app.start("--background")
    post("/add", {"url": site + "/instavel.zip"})
    check("Internet instável: 6 ligações cortadas a meio, ficheiro completo e intacto",
          wait_for(lambda: file_ok(app.dl / "instavel.zip"), 60), f"cortes por fazer: {Site.drop_first}")
    kill_all()

    # 5. Command line, interrupted with Ctrl+C, then the same command again.
    out = work / "dl5"
    Site.rate, Site.drop_first = 100_000, 0
    env = {**os.environ, "XDG_CONFIG_HOME": str(work / "c5")}
    cli = subprocess.Popen([APP_PY, "-m", "flowdl", site + "/cli.bin", "-o", str(out), "-c", "4"],
                           cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(3)
    cli.send_signal(signal.SIGINT)
    text = cli.communicate(timeout=15)[0]
    check("Linha de comando: Ctrl+C pausa", "Pausado" in text and cli.returncode == 130)
    Site.rate, Site.starts = 0, []
    again = subprocess.run([APP_PY, "-m", "flowdl", site + "/cli.bin", "-o", str(out), "-c", "4"],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    check("Linha de comando: o mesmo comando continua de onde parou e termina",
          again.returncode == 0 and file_ok(out / "cli.bin") and bool(Site.starts) and min(Site.starts) > 0,
          f"saída={again.returncode}, primeiro byte pedido={min(Site.starts) if Site.starts else '-'}")

    # 6. Start with the computer: the saved command really starts the app in the background.
    app = App(work / "c6", work / "dl6")
    subprocess.run([APP_PY, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                    "from flowdl import autostart; autostart.set_enabled(True)", str(ROOT)], env=app.env)
    desktop = work / "c6" / "autostart" / "flow-downloads.desktop"
    exec_line = next((l[5:] for l in desktop.read_text().splitlines() if l.startswith("Exec=")), "") \
        if desktop.exists() else ""
    check("Iniciar com o computador: atalho criado", "--background" in exec_line)
    if exec_line:
        subprocess.Popen(shlex.split(exec_line), env=app.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        check("…e esse atalho abre a app em segundo plano, sem janela",
              wait_for(ping, 10) and not windows("^Flow Downloads$"))

    # 7. Resource use in the background.
    time.sleep(2)
    pid = app_pids()[0]
    rss = rss_mb(pid)

    def cpu_ticks():
        return sum(int(x) for x in Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[11:13])
    t0 = cpu_ticks()
    time.sleep(10)
    idle_ms = (cpu_ticks() - t0) * 10
    check("Em segundo plano: pouca memória", rss <= 20, f"{rss} MB")
    check("Em segundo plano: CPU zero", idle_ms == 0, f"{idle_ms} ms em 10 s")
    post("/show", {})
    wait_for(lambda: bool(windows("^Flow Downloads$")), 10)
    time.sleep(1)
    with_window = rss_mb(app_pids()[0])
    w = windows("^Flow Downloads$")
    subprocess.run(["xdotool", "windowfocus", "--sync", w[0]])
    subprocess.run(["xdotool", "key", "ctrl+w"])
    freed = wait_for(lambda: app_pids() and rss_mb(app_pids()[0]) <= 20 and ping(), 20)
    check("Ao fechar a janela a memória volta ao mínimo", freed,
          f"com janela {with_window} MB → {rss_mb(app_pids()[0]) if app_pids() else '?'} MB")
    kill_all()
    server.shutdown()

    passed = sum(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} verificações passaram")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
