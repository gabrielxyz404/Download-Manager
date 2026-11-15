"""Command line usage: python -m flowdl.cli URL [URL ...] [-o DIR] [-c CONNECTIONS]"""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from .engine import DownloadTask, Status, format_size, format_time
from .manager import default_download_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Flow Downloads - baixa arquivos usando várias conexões")
    parser.add_argument("urls", nargs="+", help="link(s) para baixar")
    parser.add_argument("-o", "--output", default=default_download_dir(), help="pasta de destino")
    parser.add_argument("-c", "--connections", type=int, default=8, help="conexões por arquivo (padrão 8)")
    args = parser.parse_args(argv)

    # Ctrl+C only asks to pause. (Interrupting the wait for the download thread itself left
    # Python believing the thread had ended: the program then quit before the download had
    # saved where it was, and the same command started over from zero.)
    interrupted = threading.Event()
    previous = signal.signal(signal.SIGINT, lambda *_a: interrupted.set())
    failed = 0
    try:
        for url in args.urls:
            task = DownloadTask(url, args.output, args.connections)
            done = threading.Event()
            task.on_done = lambda _t, done=done: done.set()  # after its progress is saved
            task.start()
            while not done.is_set() and not interrupted.is_set():
                done.wait(0.5)
                name = task.filename or url
                sys.stdout.write(
                    f"\r{name[:40]:40} {task.progress * 100:5.1f}% "
                    f"{format_size(task.downloaded)}/{format_size(task.size)} "
                    f"{format_size(task.speed)}/s ETA {format_time(task.eta)} "
                    f"[{task.active_connections} con]   ")
                sys.stdout.flush()
            if interrupted.is_set():
                task.pause()
                done.wait(60)
                print("\nPausado. Rode o mesmo comando de novo para continuar de onde parou.")
                return 130
            print()
            if task.status == Status.COMPLETED:
                print(f"✔ Salvo em {task.path}")
            else:
                failed += 1
                print(f"✖ {task.status}: {task.error or ''}")
    finally:
        signal.signal(signal.SIGINT, previous)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
