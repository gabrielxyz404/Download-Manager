# Clique duas vezes neste arquivo (Windows) para abrir o Flow Downloads sem janela de terminal.
import sys

from flowdl.service import run

run(background="--background" in sys.argv[1:])
