import sys

args = [a for a in sys.argv[1:] if a != "--background"]
if args:
    from .cli import main as cli_main

    sys.exit(cli_main(args))
else:
    from .service import run

    run(background="--background" in sys.argv[1:])
