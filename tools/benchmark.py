"""Compare downloading a file with and without Flow Downloads.

    python tools/benchmark.py URL [--rounds 2]

"Without": one plain HTTP connection, the way a browser downloads.
"With": Flow Downloads (speed test + up to 8 connections), and with up to 16.
Every file is checked to be byte-for-byte identical (SHA-256).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flowdl.engine import USER_AGENT, DownloadTask, format_size  # noqa: E402


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def without_app(url: str, folder: str) -> tuple[float, str, int]:
    path = os.path.join(folder, "plain.bin")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=60) as resp, open(path, "wb") as f:
        shutil.copyfileobj(resp, f, 256 * 1024)
    return time.monotonic() - t0, path, 1


def with_app(url: str, folder: str, connections: int) -> tuple[float, str, int]:
    task = DownloadTask(url, folder, connections)
    peak = 0
    t0 = time.monotonic()
    task.start()
    while task.is_running():
        peak = max(peak, task.active_connections)
        time.sleep(0.1)
    elapsed = time.monotonic() - t0
    if task.status != "Concluído":
        raise RuntimeError(task.error or task.status)
    return elapsed, task.path, peak


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url")
    ap.add_argument("--rounds", type=int, default=2)
    args = ap.parse_args()

    methods = [
        ("Sem a app (1 ligação, como o navegador)", lambda d: without_app(args.url, d)),
        ("Flow Downloads (até 8 ligações)", lambda d: with_app(args.url, d, 8)),
        ("Flow Downloads (até 16 ligações)", lambda d: with_app(args.url, d, 16)),
    ]
    results: dict[str, list[tuple[float, int]]] = {name: [] for name, _ in methods}
    hashes, size = set(), 0
    for r in range(1, args.rounds + 1):
        for name, fn in methods:  # alternate methods every round, so network changes affect all
            folder = tempfile.mkdtemp(prefix="flowdl-bench-")
            try:
                elapsed, path, conns = fn(folder)
                size = os.path.getsize(path)
                hashes.add(sha256(path))
                results[name].append((elapsed, conns))
                print(f"  ronda {r}: {name:42s} {elapsed:6.1f} s  {format_size(size / elapsed)}/s"
                      f"  ({conns} lig.)", flush=True)
            finally:
                shutil.rmtree(folder, ignore_errors=True)

    print(f"\nFicheiro: {format_size(size)}")
    base = min(t for t, _ in results[methods[0][0]])
    for name, runs in results.items():
        best = min(t for t, _ in runs)
        print(f"{name:42s} melhor {best:6.1f} s  {format_size(size / best):>10s}/s  "
              f"{base / best:4.1f}x")
    print("Ficheiros idênticos em todos os métodos:" , "sim" if len(hashes) == 1 else "NÃO")
    print("SHA-256:", next(iter(hashes)))
    return 0 if len(hashes) == 1 else 1


if __name__ == "__main__":
    sys.exit(main())
