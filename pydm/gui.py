"""Tkinter desktop interface for PyDM."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .engine import Status, format_size, format_time
from .manager import DownloadManager, config_dir, default_download_dir

REFRESH_MS = 500
COLUMNS = (
    ("name", "Arquivo", 260),
    ("size", "Tamanho", 90),
    ("progress", "Progresso", 150),
    ("speed", "Velocidade", 100),
    ("eta", "Restante", 80),
    ("conns", "Conexões", 70),
    ("status", "Status", 160),
)


def open_path(path: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def progress_bar(fraction: float, width: int = 12) -> str:
    filled = int(round(fraction * width))
    return "█" * filled + "░" * (width - filled) + f" {fraction * 100:5.1f}%"


class Settings:
    def __init__(self) -> None:
        self.path = config_dir() / "settings.json"
        self.dest_dir = default_download_dir()
        self.connections = 8
        self.max_active = 3
        self.watch_clipboard = True
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.__dict__.update({k: v for k, v in data.items() if k != "path"})
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        data = {k: v for k, v in self.__dict__.items() if k != "path"}
        try:
            self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError:
            pass


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("PyDM - Gerenciador de Downloads")
        self.geometry("1000x520")
        self.minsize(760, 360)

        self.settings = Settings()
        self.manager = DownloadManager(max_active=self.settings.max_active)
        self._last_clip = ""

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(REFRESH_MS, self._refresh)

    # --------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")

        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.pack(fill="x")
        ttk.Label(top, text="URL:").pack(side="left")
        self.url_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.url_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda _e: self._add())
        entry.focus_set()
        ttk.Button(top, text="Colar", command=self._paste).pack(side="left")
        ttk.Label(top, text="  Conexões:").pack(side="left")
        self.conn_var = tk.IntVar(value=self.settings.connections)
        ttk.Spinbox(top, from_=1, to=32, width=4, textvariable=self.conn_var).pack(side="left", padx=4)
        ttk.Button(top, text="⬇ Baixar", command=self._add).pack(side="left", padx=(6, 0))

        folder = ttk.Frame(self, padding=(10, 0, 10, 6))
        folder.pack(fill="x")
        ttk.Label(folder, text="Salvar em:").pack(side="left")
        self.dir_var = tk.StringVar(value=self.settings.dest_dir)
        ttk.Label(folder, textvariable=self.dir_var, foreground="#555").pack(side="left", padx=6)
        ttk.Button(folder, text="Alterar...", command=self._choose_dir).pack(side="left")
        ttk.Label(folder, text="   Downloads simultâneos:").pack(side="left")
        self.max_var = tk.IntVar(value=self.settings.max_active)
        ttk.Spinbox(folder, from_=1, to=10, width=4, textvariable=self.max_var,
                    command=self._apply_max).pack(side="left", padx=4)
        self.clip_var = tk.BooleanVar(value=self.settings.watch_clipboard)
        ttk.Checkbutton(folder, text="Capturar links copiados", variable=self.clip_var).pack(side="right")

        table = ttk.Frame(self, padding=(10, 0, 10, 0))
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=[c[0] for c in COLUMNS], show="headings",
                                 selectmode="extended")
        for key, title, width in COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w" if key == "name" else "center",
                             stretch=key == "name")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self._open_file())
        self.tree.bind("<Delete>", lambda _e: self._remove())
        self.tree.tag_configure(Status.COMPLETED, foreground="#1a7f37")
        self.tree.tag_configure(Status.ERROR, foreground="#c62828")
        self.tree.tag_configure(Status.PAUSED, foreground="#8a6d00")

        bar = ttk.Frame(self, padding=10)
        bar.pack(fill="x")
        for text, cmd in (
            ("▶ Retomar", self._resume),
            ("⏸ Pausar", self._pause),
            ("✖ Remover", self._remove),
            ("Abrir arquivo", self._open_file),
            ("Abrir pasta", self._open_folder),
        ):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Pausar tudo", command=self._pause_all).pack(side="right")
        ttk.Button(bar, text="Retomar tudo", command=self._resume_all).pack(side="right", padx=6)

        self.status_var = tk.StringVar(value="Pronto")
        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w",
                  padding=(8, 2)).pack(fill="x", side="bottom")

    # ---------------------------------------------------------------- actions
    def _selected(self):
        ids = set(self.tree.selection())
        return [t for t in self.manager.tasks if t.id in ids]

    def _add(self) -> None:
        url = self.url_var.get().strip()
        if not url.lower().startswith(("http://", "https://")):
            messagebox.showwarning("URL inválida", "Informe um link que comece com http:// ou https://")
            return
        try:
            conns = max(1, min(32, int(self.conn_var.get())))
        except (tk.TclError, ValueError):
            conns = 8
        self.settings.connections = conns
        self.manager.add(url, self.dir_var.get(), conns)
        self.url_var.set("")
        self._last_clip = url
        self.status_var.set(f"Adicionado: {url}")
        self._refresh(schedule=False)

    def _paste(self) -> None:
        try:
            self.url_var.set(self.clipboard_get().strip())
        except tk.TclError:
            pass

    def _choose_dir(self) -> None:
        path = filedialog.askdirectory(initialdir=self.dir_var.get())
        if path:
            self.dir_var.set(path)
            self.settings.dest_dir = path

    def _apply_max(self) -> None:
        try:
            self.manager.max_active = self.settings.max_active = int(self.max_var.get())
        except (tk.TclError, ValueError):
            pass

    def _resume(self) -> None:
        for t in self._selected():
            self.manager.resume(t)

    def _pause(self) -> None:
        for t in self._selected():
            self.manager.pause(t)

    def _resume_all(self) -> None:
        for t in self.manager.tasks:
            self.manager.resume(t)

    def _pause_all(self) -> None:
        for t in self.manager.tasks:
            t.pause()

    def _remove(self) -> None:
        tasks = self._selected()
        if not tasks:
            return
        answer = messagebox.askyesnocancel(
            "Remover", f"Remover {len(tasks)} download(s) da lista?\n\n"
            "Sim = remover e apagar arquivos incompletos\nNão = só remover da lista")
        if answer is None:
            return
        for t in tasks:
            delete = answer and t.status != Status.COMPLETED
            self.manager.remove(t, delete_files=delete)
            if self.tree.exists(t.id):
                self.tree.delete(t.id)

    def _open_file(self) -> None:
        for t in self._selected():
            if t.status == Status.COMPLETED and t.path and os.path.exists(t.path):
                open_path(t.path)
                return

    def _open_folder(self) -> None:
        tasks = self._selected()
        folder = tasks[0].dest_dir if tasks else self.dir_var.get()
        if os.path.isdir(folder):
            open_path(folder)

    # ---------------------------------------------------------------- refresh
    def _check_clipboard(self) -> None:
        if not self.clip_var.get():
            return
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        if text != self._last_clip and text.lower().startswith(("http://", "https://")) \
                and " " not in text and len(text) < 4000:
            self._last_clip = text
            if not self.url_var.get():
                self.url_var.set(text)
                self.status_var.set("Link copiado detectado — clique em Baixar para iniciar")

    def _refresh(self, schedule: bool = True) -> None:
        self.manager.tick()
        total_speed = 0.0
        active = 0
        for t in self.manager.tasks:
            total_speed += t.speed
            active += t.is_running()
            status = t.status
            if status == Status.ERROR and t.error:
                status = f"Erro: {t.error}"
            elif status == Status.DOWNLOADING and t.size and not t.resumable:
                status = "Baixando (sem retomar)"
            values = (
                t.filename or t.url,
                format_size(t.size),
                progress_bar(t.progress),
                f"{format_size(t.speed)}/s" if t.is_running() else "",
                format_time(t.eta) if t.is_running() else "",
                t.active_connections or "",
                status,
            )
            if self.tree.exists(t.id):
                self.tree.item(t.id, values=values, tags=(t.status,))
            else:
                self.tree.insert("", "end", iid=t.id, values=values, tags=(t.status,))
        if schedule:
            self._check_clipboard()
            if active:
                self.status_var.set(f"{active} download(s) ativo(s) — {format_size(total_speed)}/s")
            self.after(REFRESH_MS, self._refresh)

    def _on_close(self) -> None:
        self.settings.dest_dir = self.dir_var.get()
        self.settings.watch_clipboard = bool(self.clip_var.get())
        self._apply_max()
        self.settings.save()
        self.manager.pause_all()
        self.destroy()


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
