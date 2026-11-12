"""Entry point of the Windows program (Flow Downloads.exe)."""

import os
import sys
import time
import traceback


def _settings_folder() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "FlowDownloads")
    os.makedirs(folder, exist_ok=True)
    return folder


def _write(name: str, text: str, mode: str = "a") -> None:
    try:
        with open(os.path.join(_settings_folder(), name), mode, encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


if os.environ.get("FLOWDL_DEBUG"):  # very first sign of life, before loading the app
    _write("debug.log", f"[launcher {os.getpid()} {time.strftime('%H:%M:%S')}] started {sys.argv} "
                        f"stderr={sys.stderr!r}\n")
    try:  # if it is still stuck after 20 s, write where every thread is
        import faulthandler
        _dump = open(os.path.join(_settings_folder(), "stacks.log"), "w", encoding="utf-8")
        faulthandler.dump_traceback_later(20, repeat=False, file=_dump)
    except OSError:
        pass


def main() -> int:
    from flowdl.service import run

    args = [a for a in sys.argv[1:] if a != "--background"]
    if args:  # command line use: "Flow Downloads.exe URL -o folder"
        from flowdl.cli import main as cli_main

        return cli_main(args)
    run(background="--background" in sys.argv[1:])
    return 0


try:
    code = main()
except SystemExit:
    raise
except BaseException:
    # Without a console an unhandled error would show a dialog and keep the program waiting
    # for a click forever, even when it was started in the background. Record it and stop.
    _write("crash.log", f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{traceback.format_exc()}\n")
    code = 1
sys.exit(code)
