"""Download states (separate tiny module so the background service can use them cheaply)."""


class Status:
    QUEUED = "Na fila"
    CONNECTING = "Conectando"
    DOWNLOADING = "Baixando"
    PAUSED = "Pausado"
    COMPLETED = "Concluído"
    ERROR = "Erro"


ACTIVE = (Status.QUEUED, Status.CONNECTING, Status.DOWNLOADING)
