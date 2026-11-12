"""Entry point of the Windows program (Flow Downloads.exe)."""

import os
import sys
import time

if os.environ.get("FLOWDL_DEBUG"):  # very first sign of life, before loading the app
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    try:
        os.makedirs(os.path.join(base, "FlowDownloads"), exist_ok=True)
        with open(os.path.join(base, "FlowDownloads", "debug.log"), "a", encoding="utf-8") as f:
            f.write(f"[launcher {os.getpid()} {time.strftime('%H:%M:%S')}] started {sys.argv} "
                    f"stderr={sys.stderr!r}\n")
        # If it is still stuck after 20 s, write where every thread is (diagnostics only).
        import faulthandler
        _dump = open(os.path.join(base, "FlowDownloads", "stacks.log"), "w", encoding="utf-8")
        faulthandler.dump_traceback_later(20, repeat=False, file=_dump)
    except OSError:
        pass

from flowdl.service import run  # noqa: E402

if os.environ.get("FLOWDL_DEBUG"):
    from flowdl.service import log
    log("launcher: app imported")

args = [a for a in sys.argv[1:] if a != "--background"]
if args:  # command line use: "Flow Downloads.exe URL -o folder"
    from flowdl.cli import main

    sys.exit(main(args))
run(background="--background" in sys.argv[1:])
