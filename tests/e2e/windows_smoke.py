"""Smoke test of the packaged program (Flow Downloads.exe) or of the source launcher.

    python tests/e2e/windows_smoke.py "dist/Flow Downloads/Flow Downloads.exe"
    python tests/e2e/windows_smoke.py "Flow Downloads.pyw"      (runs it with this Python)

Checks: starts in the background, receives a browser download and downloads it, the
browser-list stream gives identical bytes, single instance, restart to free memory,
memory use while idle.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RESULTS: list[bool] = []
PORT = 17890


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(bool(ok))
    print(f"{'OK  ' if ok else 'FAIL'} {name}{' - ' + detail if detail else ''}", flush=True)


def wait_for(fn, secs=20.0, step=0.25):
    end = time.time() + secs
    while time.time() < end:
        try:
            if fn():
                return True
        except OSError:
            pass
        time.sleep(step)
    return False


def call(method: str, path: str, payload: dict | None = None, timeout=25) -> tuple[int, bytes]:
    body = json.dumps(payload or {}).encode() if method == "POST" else b""
    with socket.create_connection(("127.0.0.1", PORT), timeout=timeout) as s:
        s.sendall(f"{method} {path} HTTP/1.1\r\nHost: x\r\nX-FlowDL: 1\r\nContent-Type: application/json\r\n"
                  f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        data = b""
        while True:
            chunk = s.recv(1 << 20)
            if not chunk:
                break
            data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), rest


def ping() -> bool:
    return call("GET", "/ping", timeout=2)[0] == 200


def app_procs(name: str) -> list[tuple[int, float]]:
    """(pid, start time) of the app's processes. On Windows a restart gives a new pid; on
    Linux the process is replaced in place (same pid, new start time and command line)."""
    try:
        import psutil
    except ImportError:
        return []
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            text = " ".join([p.info["name"] or ""] + (p.info["cmdline"] or []))
        except Exception:  # noqa: BLE001
            continue
        if (name in text or "flowdl.service import run" in text) and "windows_smoke" not in text:
            out.append((p.info["pid"], p.info["create_time"], text))
    return [(pid, t) for pid, t, _ in out]


def app_pids(name: str) -> list[int]:
    return [pid for pid, _ in app_procs(name)]


def memory_mb(pid: int) -> float:
    import psutil
    return psutil.Process(pid).memory_info().rss / 1e6


def main() -> int:
    target = Path(sys.argv[1]).resolve()
    cmd = [str(target)] if target.suffix.lower() == ".exe" else [sys.executable, str(target)]
    name = target.name
    work = Path(tempfile.mkdtemp(prefix="flowdl-smoke-"))
    site = work / "site"
    site.mkdir()
    data = os.urandom(20 * 1024 * 1024)
    (site / "instalador.bin").write_bytes(data)
    (site / "segundo.bin").write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def copyfile(self, src, dst):
            try:
                super().copyfile(src, dst)
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(site)))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    cfg = work / "cfg"
    (cfg / "FlowDownloads").mkdir(parents=True)
    dl = work / "dl"
    (cfg / "FlowDownloads" / "settings.json").write_text(json.dumps({
        "dest_dir": str(dl), "background_hint_shown": True, "confirm_browser": False,
        "show_progress_window": False}))
    env = {**os.environ, "APPDATA": str(cfg), "XDG_CONFIG_HOME": str(cfg)}

    env["FLOWDL_DEBUG"] = "1"
    proc = subprocess.Popen(cmd + ["--background"], env=env)
    t0 = time.time()
    started = False
    while time.time() - t0 < 120:
        if wait_for(ping, 10):
            started = True
            break
        if proc.poll() is not None:  # it stopped: crash.log below says why
            break
        log = cfg / "FlowDownloads" / "debug.log"
        print(f"  ... {time.time() - t0:.0f}s, ainda não responde; debug.log:",
              log.read_text(encoding="utf-8", errors="replace").strip()[-300:] if log.exists() else "(ainda não)",
              flush=True)
    check("Arranca em segundo plano", started, f"respondeu ao fim de {time.time() - t0:.0f}s")
    if not started:
        for log in ("crash.log", "debug.log", "stacks.log"):
            path = cfg / "FlowDownloads" / log
            print(f"--- {log}:", path.read_text(encoding="utf-8", errors="replace") if path.exists() else "(não existe)")
        print("--- processo ainda vivo:", proc.poll() is None)
        proc.kill()
        return 1
    pids = app_pids(name)
    procs = app_procs(name)

    # A download sent by the browser extension (no confirmation, no window).
    call("POST", "/add", {"url": base + "/instalador.bin"})
    target_file = dl / "instalador.bin"
    ok = wait_for(lambda: target_file.exists() and hashlib.sha256(target_file.read_bytes()).hexdigest() == sha, 60)
    check("Descarrega um ficheiro de 20 MB intacto", ok)

    # Browser-list mode: the bytes the browser would receive are identical.
    code, body = call("POST", "/add", {"url": base + "/segundo.bin", "mirror": True})
    rid = json.loads(body)["rid"]
    code, body = call("GET", f"/request/{rid}")
    stream = json.loads(body).get("stream")
    code, streamed = call("GET", stream, timeout=60)
    check("Lista do navegador: o navegador recebe o ficheiro igual", code == 200 and
          hashlib.sha256(streamed).hexdigest() == sha, f"{len(streamed)} bytes")
    call("POST", "/mirror-done", {"rid": rid, "path": str(work / "browser-copy.bin")})
    check("…e a cópia temporária da app é apagada", wait_for(lambda: not list(dl.glob("segundo.bin*")), 10))

    # Opening the program again must not start a second copy.
    second = subprocess.Popen(cmd + ["--background"], env=env)
    try:
        second.wait(20)
        single = True
    except subprocess.TimeoutExpired:
        single = False
        second.kill()
    check("Abrir de novo não cria uma segunda cópia", single)

    # After downloading, it restarts in its slim form (frees the download engine).
    if pids:
        def changed():
            import psutil
            now = app_procs(name)
            same_cmd = all(" ".join(psutil.Process(pid).cmdline()).endswith("--background") and
                           "flowdl.service import run" not in " ".join(psutil.Process(pid).cmdline())
                           for pid, _ in now) if sys.platform != "win32" else True
            return now and (now != procs or not same_cmd) and ping()
        restarted = wait_for(changed, 40)
        check("Reinicia-se em modo leve depois dos downloads", restarted,
              f"antes {procs} depois {app_procs(name)}")
        time.sleep(3)
        now = app_pids(name)
        if now:
            mem = memory_mb(now[0])
            check("Memória em segundo plano", mem < 40, f"{mem:.1f} MB")
    else:
        print("(psutil em falta: verificações de processo ignoradas)")
    check("Continua a responder ao navegador", wait_for(ping, 10))

    for pid in app_pids(name):
        try:
            import psutil
            psutil.Process(pid).kill()
        except Exception:  # noqa: BLE001
            pass
    proc.kill()
    server.shutdown()
    passed = sum(RESULTS)
    if passed != len(RESULTS):
        for log in ("crash.log", "debug.log"):
            path = cfg / "FlowDownloads" / log
            print(f"--- {log}:", path.read_text(encoding="utf-8", errors="replace")[-3000:] if path.exists() else "(não existe)")
    print(f"\n{passed}/{len(RESULTS)} verificações passaram")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
