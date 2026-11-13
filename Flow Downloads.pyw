# Clique duas vezes neste arquivo (Windows) para abrir o Flow Downloads sem janela de terminal.
import sys

from flowdl import nativehost

if nativehost.called_by_browser(sys.argv):  # the browser asks us to start (app was closed)
    sys.exit(nativehost.serve())

from flowdl.service import run

run(background="--background" in sys.argv[1:])
