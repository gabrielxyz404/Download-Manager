"""Entry point of the Windows program (Flow Downloads.exe)."""

import sys

from flowdl.service import run

args = [a for a in sys.argv[1:] if a != "--background"]
if args:  # command line use: "Flow Downloads.exe URL -o folder"
    from flowdl.cli import main

    sys.exit(main(args))
run(background="--background" in sys.argv[1:])
