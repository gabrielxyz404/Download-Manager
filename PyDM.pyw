# Clique duas vezes neste arquivo (Windows) para abrir o PyDM sem janela de terminal.
import sys

from pydm.service import run

run(background="--background" in sys.argv[1:])
