"""Command line usage: python -m flowdl.cli URL [URL ...] [-o DIR] [-c CONNECTIONS]"""

from __future__ import annotations

import argparse
import sys
import time

from .engine import DownloadTask, Status, format_size, format_time
from .manager import default_download_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Flow Downloads - baixa arquivos usando várias conexões")
    parser.add_argument("urls", nargs="+", help="link(s) para baixar")
    parser.add_argument("-o", "--output", default=default_download_dir(), help="pasta de destino")
    parser.add_argument("-c", "--connections", type=int, default=8, help="conexões por arquivo (padrão 8)")
    args = parser.parse_args(argv)

    failed = 0
    for url in args.urls:
        task = DownloadTask(url, args.output, args.connections)
        task.start()
        try:
            while task.is_running():
                task.wait(0.5)  # returns as soon as the download ends
                name = task.filename or url
                sys.stdout.write(
                    f"\r{name[:40]:40} {task.progress * 100:5.1f}% "
                    f"{format_size(task.downloaded)}/{format_size(task.size)} "
                    f"{format_size(task.speed)}/s ETA {format_time(task.eta)} "
                    f"[{task.active_connections} con]   ")
                sys.stdout.flush()
        except KeyboardInterrupt:
            task.pause()
            task.wait()
            print("\nPausado. Rode o mesmo comando de novo para continuar de onde parou.")
            return 130
        print()
        if task.status == Status.COMPLETED:
            print(f"✔ Salvo em {task.path}")
        else:
            failed += 1
            print(f"✖ {task.status}: {task.error or ''}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
