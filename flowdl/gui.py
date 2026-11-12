"""Tkinter desktop interface for Flow Downloads."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import importlib
import urllib.parse

from . import grabber, video
from . import autostart
from .categories import CATEGORIES, OTHER
from .engine import Status, format_size, format_time
from .limiter import GLOBAL_LIMITER
from .manager import config_dir
from .scheduler import ACTIONS, shutdown_computer
from .settings import DEFAULT_COLUMNS, Settings  # noqa: F401 (re-exported)
from .video import VideoTask

REFRESH_MS = 500
VIDEO_SITES = ("youtube.com", "youtu.be", "vimeo.com", "tiktok.com", "instagram.com", "facebook.com",
               "fb.watch", "x.com", "twitter.com", "twitch.tv", "dailymotion.com", "reddit.com",
               "soundcloud.com", "bilibili.com", "kwai.com")


def is_video_site(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    return any(host == d or host.endswith("." + d) for d in VIDEO_SITES)
COLUMNS = {
    "name": ("Arquivo", 220),
    "size": ("Tamanho", 85),
    "progress": ("Progresso", 175),
    "speed": ("Velocidade", 95),
    "eta": ("Restante", 75),
    "conns": ("Conexões", 70),
    "status": ("Status", 150),
    "category": ("Categoria", 95),
    "added": ("Adicionado", 115),
    "url": ("Endereço", 200),
}

THEMES = {
    "light": dict(bg="#f3f3f3", fg="#1b1b1b", field="#ffffff", head="#e6e6e6", border="#c8c8c8",
                  sel="#cce4f7", selfg="#000000", hover="#e0e0e0", muted="#666666",
                  done="#1a7f37", error="#c62828", paused="#8a6d00"),
    "dark": dict(bg="#1e1f22", fg="#e6e6e6", field="#2b2d31", head="#313338", border="#3f4147",
                 sel="#2f5d8a", selfg="#ffffff", hover="#3a3c42", muted="#a0a0a0",
                 done="#5fd27a", error="#ff7b72", paused="#e3b341"),
}


def open_path(path: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def progress_bar(fraction: float, width: int = 10) -> str:
    filled = int(round(fraction * width))
    return "█" * filled + "░" * (width - filled) + f" {fraction * 100:5.1f}%"


# ------------------------------------------------------------------- dialogs
class AddDialog(tk.Toplevel):
    """'Download file info' dialog: confirm name / folder before downloading."""

    def __init__(self, app: "App", url: str = "", filename: str | None = None,
                 headers: dict | None = None, title: str = "Adicionar download"):
        super().__init__(app)
        self.app, self.headers = app, headers
        self.title(title)
        app.attach_dialog(self)
        self.resizable(True, False)
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        self.url = tk.StringVar(value=url)
        self.name = tk.StringVar(value=filename or "")
        self.folder = tk.StringVar(value=app.settings.dest_dir)
        self.conns = tk.IntVar(value=app.settings.connections)

        ttk.Label(frm, text="Endereço:").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.url, width=70).grid(row=0, column=1, columnspan=2, sticky="ew")
        ttk.Label(frm, text="Nome do arquivo:").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.name).grid(row=1, column=1, columnspan=2, sticky="ew")
        ttk.Label(frm, text="(deixe vazio para detectar automaticamente)", foreground=app.colors["muted"]
                  ).grid(row=2, column=1, sticky="w")
        ttk.Label(frm, text="Salvar em:").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.folder).grid(row=3, column=1, sticky="ew")
        ttk.Button(frm, text="...", width=3, command=self._browse).grid(row=3, column=2, padx=(4, 0))
        ttk.Label(frm, text="Conexões:").grid(row=4, column=0, sticky="w", pady=3)
        ttk.Spinbox(frm, from_=1, to=32, width=5, textvariable=self.conns).grid(row=4, column=1, sticky="w")

        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Baixar agora", command=lambda: self._ok(True)).pack(side="left", padx=4)
        ttk.Button(btns, text="Baixar depois", command=lambda: self._ok(False)).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancelar", command=self.destroy).pack(side="left", padx=4)
        self.bind("<Return>", lambda _e: self._ok(True))
        self.bind("<Escape>", lambda _e: self.destroy())
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))
        self.focus_force()

    def _browse(self) -> None:
        path = filedialog.askdirectory(parent=self, initialdir=self.folder.get())
        if path:
            self.folder.set(path)

    def _ok(self, start: bool) -> None:
        url = self.url.get().strip()
        if not url.lower().startswith(("http://", "https://", "ftp://")):
            messagebox.showwarning("URL inválida", "O link deve começar com http://, https:// ou ftp://",
                                   parent=self)
            return
        try:
            conns = max(1, min(32, int(self.conns.get())))
        except (tk.TclError, ValueError):
            conns = 8
        self.app.add_download(url, self.folder.get(), conns, start,
                              self.name.get().strip() or None, self.headers)
        self.destroy()


class SchedulerDialog(tk.Toplevel):
    DAYS = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]

    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        s = app.scheduler
        self.title("Agendador")
        app.attach_dialog(self)
        self.resizable(False, False)
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)

        self.start_on = tk.BooleanVar(value=s.start_enabled)
        self.start_at = tk.StringVar(value=s.start_time)
        self.stop_on = tk.BooleanVar(value=s.stop_enabled)
        self.stop_at = tk.StringVar(value=s.stop_time)
        self.days = [tk.BooleanVar(value=i in s.days) for i in range(7)]
        self.action = tk.StringVar(value=ACTIONS.get(s.on_complete, ACTIONS["nothing"]))

        ttk.Checkbutton(frm, text="Iniciar downloads às", variable=self.start_on).grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.start_at, width=7).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Checkbutton(frm, text="Parar downloads às", variable=self.stop_on).grid(row=1, column=0, sticky="w",
                                                                                     pady=6)
        ttk.Entry(frm, textvariable=self.stop_at, width=7).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(frm, text="(formato HH:MM, ex.: 02:30)", foreground=app.colors["muted"]
                  ).grid(row=0, column=2, sticky="w")

        days = ttk.Frame(frm)
        days.grid(row=2, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Label(days, text="Dias:").pack(side="left")
        for i, name in enumerate(self.DAYS):
            ttk.Checkbutton(days, text=name, variable=self.days[i]).pack(side="left", padx=2)

        ttk.Label(frm, text="Quando todos os downloads terminarem:").grid(row=3, column=0, columnspan=2,
                                                                          sticky="w", pady=(8, 2))
        ttk.Combobox(frm, textvariable=self.action, values=list(ACTIONS.values()), state="readonly",
                     width=26).grid(row=4, column=0, columnspan=2, sticky="w")

        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="Salvar", command=self._save).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancelar", command=self.destroy).pack(side="left")

    @staticmethod
    def _valid(hm: str) -> bool:
        try:
            time.strptime(hm, "%H:%M")
            return len(hm) == 5
        except ValueError:
            return False

    def _save(self) -> None:
        for var in (self.start_at, self.stop_at):
            if not self._valid(var.get().strip()):
                messagebox.showwarning("Horário inválido", "Use o formato HH:MM (ex.: 02:30).", parent=self)
                return
        s = self.app.scheduler
        s.start_enabled, s.start_time = self.start_on.get(), self.start_at.get().strip()
        s.stop_enabled, s.stop_time = self.stop_on.get(), self.stop_at.get().strip()
        s.days = [i for i, v in enumerate(self.days) if v.get()]
        s.on_complete = next((k for k, v in ACTIONS.items() if v == self.action.get()), "nothing")
        self.app.settings.scheduler = s.to_dict()
        self.app.settings.save()
        self.app.status_var.set("Agendamento salvo")
        self.destroy()


class GrabberDialog(tk.Toplevel):
    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        self.title("Capturar arquivos de um site")
        app.attach_dialog(self)
        self.geometry("820x480")
        self.results: queue.Queue = queue.Queue()
        self.items: dict[str, dict] = {}

        top = ttk.Frame(self, padding=(12, 12, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Página:").pack(side="left")
        self.url = tk.StringVar(value=app.url_var.get())
        ttk.Entry(top, textvariable=self.url).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Label(top, text="Profundidade:").pack(side="left")
        self.depth = tk.IntVar(value=0)
        ttk.Spinbox(top, from_=0, to=2, width=3, textvariable=self.depth).pack(side="left", padx=4)
        self.go_btn = ttk.Button(top, text="Procurar", command=self._search)
        self.go_btn.pack(side="left", padx=(6, 0))

        cats = ttk.Frame(self, padding=(12, 0, 12, 4))
        cats.pack(fill="x")
        ttk.Label(cats, text="Tipos:").pack(side="left")
        self.cat_vars = {c: tk.BooleanVar(value=c in ("Vídeos", "Músicas", "Compactados", "Documentos",
                                                       "Programas")) for c in CATEGORIES}
        for c, v in self.cat_vars.items():
            ttk.Checkbutton(cats, text=c, variable=v).pack(side="left", padx=3)

        body = ttk.Frame(self, padding=(12, 0, 12, 0))
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=("name", "category", "url"), show="headings")
        for key, title, w in (("name", "Arquivo", 220), ("category", "Tipo", 90), ("url", "Endereço", 420)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=w, stretch=key == "url")
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        bottom = ttk.Frame(self, padding=12)
        bottom.pack(fill="x")
        self.info = tk.StringVar(value="Digite o endereço de uma página e clique em Procurar.")
        ttk.Label(bottom, textvariable=self.info).pack(side="left")
        ttk.Button(bottom, text="Fechar", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="Baixar selecionados", command=self._download).pack(side="right", padx=6)
        ttk.Button(bottom, text="Selecionar tudo",
                   command=lambda: self.tree.selection_set(self.tree.get_children())).pack(side="right")

    def _search(self) -> None:
        url = self.url.get().strip()
        if not url.lower().startswith(("http://", "https://")):
            messagebox.showwarning("URL inválida", "Informe o endereço de uma página web.", parent=self)
            return
        cats = {c for c, v in self.cat_vars.items() if v.get()}
        depth = max(0, min(2, int(self.depth.get() or 0)))
        self.go_btn.state(["disabled"])
        self.info.set("Procurando...")

        def work():
            try:
                self.results.put(("ok", grabber.grab(url, cats, depth)))
            except Exception as exc:  # noqa: BLE001
                self.results.put(("err", str(exc)))

        threading.Thread(target=work, daemon=True).start()
        self.after(200, self._poll)

    def _poll(self) -> None:
        try:
            kind, data = self.results.get_nowait()
        except queue.Empty:
            self.after(200, self._poll)
            return
        self.go_btn.state(["!disabled"])
        if kind == "err":
            self.info.set(f"Erro: {data}")
            return
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        for i, item in enumerate(data):
            iid = str(i)
            self.items[iid] = item
            self.tree.insert("", "end", iid=iid, values=(item["name"], item["category"], item["url"]))
        self.tree.selection_set(self.tree.get_children())
        self.info.set(f"{len(data)} arquivo(s) encontrado(s)")

    def _download(self) -> None:
        sel = self.tree.selection()
        for iid in sel:
            item = self.items[iid]
            self.app.add_download(item["url"], self.app.settings.dest_dir, self.app.settings.connections, True,
                                  None, {"Referer": self.url.get().strip()})
        if sel:
            self.app.status_var.set(f"{len(sel)} download(s) adicionados pelo capturador")
            self.destroy()


class VideoDialog(tk.Toplevel):
    """Choose quality for a video page (yt-dlp) or a media file captured by the browser."""

    def __init__(self, app: "App", page: str, title: str | None = None, media: list | None = None,
                 cookie_jar: list | None = None):
        super().__init__(app)
        self.app, self.page, self.media = app, page, media or []
        self.browser_title = (title or "").strip()
        self.title("Baixar vídeo")
        app.attach_dialog(self)
        self.geometry("620x400")
        self.results: queue.Queue = queue.Queue()
        self.choices: dict[str, dict] = {}
        host = urllib.parse.urlparse(page).netloc or "site"
        self.cookiefile = video.write_cookiefile(
            cookie_jar or [], str(config_dir() / "cookies" / f"{re_safe(host)}.txt"))
        self.headers = {"Referer": page}

        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        self.name_var = tk.StringVar(value=title or page)
        ttk.Label(frm, textvariable=self.name_var, font=("", 11, "bold"), wraplength=580).pack(anchor="w")
        ttk.Label(frm, text=page, foreground=app.colors["muted"], wraplength=580).pack(anchor="w", pady=(2, 8))

        self.tree = ttk.Treeview(frm, columns=("q", "info"), show="headings", selectmode="browse", height=8)
        self.tree.heading("q", text="Qualidade")
        self.tree.heading("info", text="Detalhes")
        self.tree.column("q", width=200)
        self.tree.column("info", width=360)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _e: self._download())

        self.info = tk.StringVar()
        ttk.Label(frm, textvariable=self.info, foreground=app.colors["muted"], wraplength=580
                  ).pack(anchor="w", pady=6)
        btns = ttk.Frame(frm)
        btns.pack(fill="x")
        self.install_btn = ttk.Button(btns, text="Instalar suporte a vídeos", command=self._install)
        ttk.Button(btns, text="Cancelar", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="⬇ Baixar", command=self._download).pack(side="right", padx=6)

        for m in self.media:
            self._add_media_row(m)
        self._lookup()
        self.lift()
        self.attributes("-topmost", True)  # appear in front of the browser
        self.after(400, lambda: self.winfo_exists() and self.attributes("-topmost", False))
        self.focus_force()

    def _add_media_row(self, m: dict) -> None:
        iid = f"m{len(self.choices)}"
        name = urllib.parse.unquote(urllib.parse.urlparse(m["url"]).path.rsplit("/", 1)[-1]) or "arquivo"
        if m.get("hls"):
            self.choices[iid] = {"type": "hls", "url": m["url"]}
            self.tree.insert("", "end", iid=iid, values=("Transmissão (HLS)", name))
        else:
            self.choices[iid] = {"type": "file", "url": m["url"]}
            size = format_size(m["size"]) if m.get("size") else "tamanho ?"
            self.tree.insert("", "end", iid=iid, values=("Arquivo direto", f"{name} — {size}"))

    def _lookup(self) -> None:
        if not video.available():
            self.info.set("Para vídeos do YouTube, Instagram, TikTok etc. é preciso o yt-dlp (gratuito). "
                          "Clique em 'Instalar suporte a vídeos'.")
            self.install_btn.pack(side="left")
            self._select_first()
            return
        self.info.set("Procurando qualidades disponíveis...")

        def work():
            try:
                self.results.put(("ok", video.get_options(self.page, self.cookiefile, self.headers)))
            except Exception as exc:  # noqa: BLE001
                self.results.put(("err", str(exc).replace("ERROR: ", "")))

        threading.Thread(target=work, daemon=True).start()
        self.after(200, self._poll)

    def _poll(self) -> None:
        if not self.winfo_exists():
            return
        try:
            kind, data = self.results.get_nowait()
        except queue.Empty:
            self.after(200, self._poll)
            return
        if kind == "err":
            self.info.set("Não encontrei vídeos nesta página pelo yt-dlp." +
                          (" Use um dos arquivos capturados acima." if self.media else f"\n{data[:200]}"))
            self._select_first()
            return
        if not self.browser_title:
            self.name_var.set(data["title"])
        for i, opt in enumerate(data["options"]):
            iid = f"q{i}"
            self.choices[iid] = {"type": "ytdlp", "url": data["url"], **opt}
            detail = "convertido para MP3" if opt["audio_only"] and video.has_ffmpeg() else \
                "melhor vídeo + áudio" if not opt["audio_only"] else "faixa de áudio"
            self.tree.insert("", i, iid=iid, values=(opt["label"], detail))
        notes = []
        if data.get("limited"):
            notes.append("Instale o ffmpeg para ter 1080p ou mais e conversão para MP3 "
                         "(Windows: winget install ffmpeg).")
        self.info.set(" ".join(notes) or "Escolha a qualidade e clique em Baixar.")
        self._select_first()

    def _select_first(self) -> None:
        kids = self.tree.get_children()
        if kids and not self.tree.selection():
            self.tree.selection_set(kids[0])

    def _download(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        c = self.choices[sel[0]]
        s = self.app.settings
        if c["type"] == "file":
            self.app.add_download(c["url"], s.dest_dir, s.connections, True, None, self.headers)
        else:
            if not video.available():
                messagebox.showinfo("yt-dlp necessário", "Instale o suporte a vídeos primeiro.", parent=self)
                return
            fmt, audio = (c.get("format", "b"), c.get("audio_only", False)) if c["type"] == "ytdlp" \
                else ("b", False)
            task = VideoTask(c["url"], s.dest_dir, fmt, audio, self.name_var.get(), s.connections,
                             headers=self.headers, cookiefile=self.cookiefile,
                             quality=c.get("label", "HLS"))
            self.app.manager.add_task(task)
            self.app.status_var.set(f"Vídeo adicionado: {self.name_var.get()}")
        self.destroy()

    def _install(self) -> None:
        self.install_btn.state(["disabled"])
        self.info.set("Instalando o yt-dlp... (pode levar um minuto)")

        def done(ok: bool) -> None:
            if not self.winfo_exists():
                return
            if ok:
                self.install_btn.pack_forget()
                self._lookup()
            else:
                self.install_btn.state(["!disabled"])
                self.info.set("A instalação falhou. Veja a mensagem de erro.")

        VideoDialog.install(self.app, on_done=done)

    @staticmethod
    def install(app: "App", on_done=None) -> None:
        q: queue.Queue = queue.Queue()
        app.status_var.set("Instalando/atualizando o yt-dlp...")
        threading.Thread(target=lambda: q.put(video.install_yt_dlp()), daemon=True).start()

        def poll():
            try:
                ok, out = q.get_nowait()
            except queue.Empty:
                app.after(300, poll)
                return
            if ok:
                importlib.invalidate_caches()
                try:
                    video.yt_dlp = importlib.import_module("yt_dlp")
                except ImportError:
                    ok = False
            if ok:
                app.status_var.set("Suporte a vídeos instalado ✔")
            else:
                messagebox.showerror("Erro ao instalar", "Não consegui instalar o yt-dlp:\n\n" + out[-800:])
            if on_done:
                on_done(ok)

        app.after(300, poll)


def re_safe(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in text)[:80]


class ShutdownCountdown(tk.Toplevel):
    def __init__(self, app: "App", seconds: int = 30):
        super().__init__(app)
        self.app, self.left = app, seconds
        self.title("Desligar o computador")
        self.attributes("-topmost", True)
        frm = ttk.Frame(self, padding=20)
        frm.pack()
        self.msg = tk.StringVar()
        ttk.Label(frm, textvariable=self.msg, font=("", 11)).pack(pady=(0, 12))
        ttk.Button(frm, text="Cancelar", command=self.destroy).pack()
        self._tick()

    def _tick(self) -> None:
        if self.left <= 0:
            self.app.quit_all()
            shutdown_computer()
            return
        self.msg.set(f"Downloads concluídos. O computador será desligado em {self.left} s.")
        self.left -= 1
        self.after(1000, self._tick)


# ------------------------------------------------------------------ main app
class App(tk.Tk):
    SIDEBAR = [("all", "Todos os downloads"), ("unfinished", "Incompletos"), ("finished", "Concluídos")]

    def __init__(self, service, mode: str = "main") -> None:
        """``mode``: "main" shows the window; "dialog" only shows the browser's dialog;
        "countdown" only shows the shutdown countdown."""
        super().__init__()
        self.withdraw()
        self.title("Flow Downloads")
        self.geometry("1100x580")
        self.minsize(820, 400)

        self.service = service
        self.settings = service.settings
        self.manager = service.manager
        self.scheduler = service.scheduler
        self.colors = THEMES["dark" if self.settings.dark else "light"]
        self._last_clip = ""
        self._statuses: dict[str, str] = {t.id: t.status for t in self.manager.tasks}
        self._filter = "all"
        self._last_save = time.monotonic()

        here = os.path.dirname(__file__)
        try:
            if sys.platform.startswith("win"):
                self.iconbitmap(default=os.path.join(here, "icon.ico"))  # crisp taskbar/title icon
            self._icons = [tk.PhotoImage(file=os.path.join(here, f"icon{s}.png")) for s in (256, 48, 32, 16)]
            self.iconphoto(True, *self._icons)
        except tk.TclError:
            pass

        self._build_menu()
        self._build_ui()
        self.apply_theme()
        self._apply_columns()
        self.protocol("WM_DELETE_WINDOW", self.close_window)
        self.bind("<FocusIn>", self._on_focus)  # check copied links only when the user comes back
        if service.bridge is None and self.settings.browser_integration:
            self.status_var.set("Integração com navegador indisponível: a porta já está em uso")
        if mode == "main":
            self.show_window()
        elif mode == "countdown":
            ShutdownCountdown(self)
        self._process_events()
        self.after(REFRESH_MS, self._refresh)

    # --------------------------------------------------------- window life
    def show_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()
        self._refresh(schedule=False)

    def attach_dialog(self, dialog: tk.Toplevel) -> None:
        # A dialog tied to a hidden main window would be hidden too, so only attach when visible.
        if self.state() != "withdrawn":
            dialog.transient(self)

    def _dialogs_open(self) -> bool:
        return any(isinstance(w, tk.Toplevel) for w in self.winfo_children())

    def close_window(self) -> None:
        """Window X: keep running in the background (unless disabled), freeing the GUI."""
        if not self.settings.run_in_background:
            self.quit_all()
            return
        if not self.settings.background_hint_shown:
            self.settings.background_hint_shown = True
            messagebox.showinfo(
                "Continua em segundo plano",
                "O Flow Downloads continua a funcionar em segundo plano (quase sem gastar memória ou CPU), "
                "pronto para receber downloads do navegador.\n\n"
                "Para fechar de vez: menu Tarefas → Sair.\n"
                "Para voltar a abrir: abra o Flow Downloads de novo ou use o botão da extensão.", parent=self)
        self._save_ui_settings()
        self.destroy()

    def quit_all(self) -> None:
        self._save_ui_settings()
        self.service.exit_requested = True
        self.destroy()

    def _save_ui_settings(self) -> None:
        self.settings.watch_clipboard = bool(self.clip_var.get())
        self.settings.confirm_browser = bool(self.confirm_var.get())
        self.settings.auto_resume = bool(self.resume_var.get())
        self.settings.scheduler = self.scheduler.to_dict()
        self.settings.save()
        self.manager.save()

    # ------------------------------------------------------------------- menu
    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        self.menus = [menubar]

        tasks = tk.Menu(menubar, tearoff=False)
        tasks.add_command(label="Adicionar URL...", accelerator="Ctrl+N", command=self.open_add_dialog)
        tasks.add_command(label="Baixar vídeo (YouTube, Instagram...)...", command=self.open_video_dialog)
        tasks.add_command(label="Capturar arquivos de um site...", command=lambda: GrabberDialog(self))
        tasks.add_separator()
        tasks.add_command(label="Fechar janela (continua em segundo plano)", accelerator="Ctrl+W",
                          command=self.close_window)
        tasks.add_command(label="Sair", accelerator="Ctrl+Q", command=self.quit_all)
        menubar.add_cascade(label="Tarefas", menu=tasks)

        dl = tk.Menu(menubar, tearoff=False)
        dl.add_command(label="Retomar", command=self._resume)
        dl.add_command(label="Pausar", command=self._pause)
        dl.add_command(label="Retomar tudo", command=self.manager.resume_all)
        dl.add_command(label="Pausar tudo", command=self._pause_all)
        dl.add_separator()
        dl.add_command(label="Limitador de velocidade...", command=self._global_limit)
        dl.add_command(label="Agendador...", command=lambda: SchedulerDialog(self))
        dl.add_separator()
        dl.add_command(label="Remover concluídos da lista", command=self._clear_finished)
        menubar.add_cascade(label="Downloads", menu=dl)

        view = tk.Menu(menubar, tearoff=False)
        self.dark_var = tk.BooleanVar(value=self.settings.dark)
        view.add_checkbutton(label="Tema escuro", variable=self.dark_var, command=self._toggle_theme)
        cols = tk.Menu(view, tearoff=False)
        self.col_vars = {}
        for key, (title, _w) in COLUMNS.items():
            var = tk.BooleanVar(value=key in self.settings.columns)
            self.col_vars[key] = var
            cols.add_checkbutton(label=title, variable=var, command=self._apply_columns,
                                 state="disabled" if key == "name" else "normal")
        view.add_cascade(label="Colunas", menu=cols)
        menubar.add_cascade(label="Exibir", menu=view)

        opts = tk.Menu(menubar, tearoff=False)
        self.bridge_var = tk.BooleanVar(value=self.settings.browser_integration)
        self.confirm_var = tk.BooleanVar(value=self.settings.confirm_browser)
        self.clip_var = tk.BooleanVar(value=self.settings.watch_clipboard)
        opts.add_checkbutton(label="Integração com o navegador", variable=self.bridge_var,
                             command=self._toggle_bridge)
        opts.add_checkbutton(label="Confirmar downloads vindos do navegador", variable=self.confirm_var)
        opts.add_checkbutton(label="Capturar links copiados", variable=self.clip_var)
        self.resume_var = tk.BooleanVar(value=self.settings.auto_resume)
        opts.add_checkbutton(label="Retomar downloads ao abrir o Flow Downloads", variable=self.resume_var)
        self.bg_var = tk.BooleanVar(value=self.settings.run_in_background)
        opts.add_checkbutton(label="Continuar em segundo plano ao fechar a janela", variable=self.bg_var,
                             command=lambda: setattr(self.settings, "run_in_background", self.bg_var.get()))
        self.autostart_var = tk.BooleanVar(value=autostart.supported() and autostart.is_enabled())
        opts.add_checkbutton(label="Iniciar com o computador (em segundo plano)", variable=self.autostart_var,
                             command=self._toggle_autostart,
                             state="normal" if autostart.supported() else "disabled")
        opts.add_command(label="Instalar/atualizar suporte a vídeos (yt-dlp)",
                         command=lambda: VideoDialog.install(self))
        opts.add_command(label="Downloads simultâneos...", command=self._max_active)
        opts.add_command(label="Pasta padrão...", command=self._choose_dir)
        menubar.add_cascade(label="Opções", menu=opts)

        helpm = tk.Menu(menubar, tearoff=False)
        helpm.add_command(label="Como instalar a extensão do navegador", command=self._extension_help)
        helpm.add_command(label="Sobre", command=lambda: messagebox.showinfo(
            "Sobre", "Flow Downloads — gerenciador de downloads gratuito\n"
                     "Downloads com várias conexões, retomada, agendador e integração com o navegador."))
        menubar.add_cascade(label="Ajuda", menu=helpm)

        self.menus += [tasks, dl, view, cols, opts, helpm]
        self.config(menu=menubar)
        self.bind_all("<Control-n>", lambda _e: self.open_add_dialog())
        self.bind("<Control-w>", lambda _e: self.close_window())
        self.bind("<Control-q>", lambda _e: self.quit_all())

    # --------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=(8, 6), style="Toolbar.TFrame")
        toolbar.pack(fill="x")
        for text, cmd in (
            ("➕ Adicionar", self.open_add_dialog),
            ("▶ Retomar", self._resume),
            ("⏸ Pausar", self._pause),
            ("⏹ Pausar tudo", self._pause_all),
            ("🗑 Excluir", self._remove),
            ("🚀 Limitador", self._global_limit),
            ("⏰ Agendador", lambda: SchedulerDialog(self)),
            ("🕸 Capturar site", lambda: GrabberDialog(self)),
        ):
            ttk.Button(toolbar, text=text, command=cmd, style="Tool.TButton").pack(side="left", padx=2)

        urlbar = ttk.Frame(self, padding=(10, 4, 10, 6))
        urlbar.pack(fill="x")
        ttk.Label(urlbar, text="URL:").pack(side="left")
        self.url_var = tk.StringVar()
        entry = ttk.Entry(urlbar, textvariable=self.url_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda _e: self._quick_add())
        entry.focus_set()
        ttk.Button(urlbar, text="Colar", command=self._paste).pack(side="left")
        ttk.Button(urlbar, text="⬇ Baixar", command=self._quick_add).pack(side="left", padx=(6, 0))

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10)

        side = ttk.Frame(paned)
        self.side = ttk.Treeview(side, show="tree", selectmode="browse")
        self.side.pack(fill="both", expand=True)
        self.side.column("#0", width=210)
        for key, label in self.SIDEBAR:
            self.side.insert("", "end", iid=key, text=label, open=True)
        for cat in list(CATEGORIES) + [OTHER]:
            self.side.insert("all", "end", iid=f"cat:{cat}", text=cat)
        self.side.selection_set("all")
        self.side.bind("<<TreeviewSelect>>", self._on_filter)
        paned.add(side, weight=0)

        table = ttk.Frame(paned)
        self.tree = ttk.Treeview(table, columns=list(COLUMNS), show="headings", selectmode="extended")
        for key, (title, width) in COLUMNS.items():
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w" if key in ("name", "url") else "center",
                             stretch=key == "name")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        paned.add(table, weight=1)

        self.tree.bind("<Double-1>", lambda _e: self._open_file())
        self.tree.bind("<Delete>", lambda _e: self._remove())
        self.tree.bind("<Button-3>", self._context_menu)
        self.tree.bind("<Button-2>", self._context_menu)

        self.ctx = tk.Menu(self, tearoff=False)
        self.ctx.add_command(label="Abrir", command=self._open_file)
        self.ctx.add_command(label="Abrir pasta", command=self._open_folder)
        self.ctx.add_separator()
        self.ctx.add_command(label="Retomar", command=self._resume)
        self.ctx.add_command(label="Pausar", command=self._pause)
        self.ctx.add_command(label="Limitar velocidade...", command=self._task_limit)
        self.ctx.add_command(label="Copiar endereço", command=self._copy_url)
        self.ctx.add_separator()
        self.ctx.add_command(label="Excluir", command=self._remove)
        self.menus.append(self.ctx)

        self.status_var = tk.StringVar(value="Pronto")
        ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(10, 4),
                  style="Status.TLabel").pack(fill="x", side="bottom")

    def apply_theme(self) -> None:
        c = self.colors = THEMES["dark" if self.settings.dark else "light"]
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"],
                        bordercolor=c["border"], lightcolor=c["bg"], darkcolor=c["bg"],
                        troughcolor=c["field"], selectbackground=c["sel"], selectforeground=c["selfg"],
                        insertcolor=c["fg"], arrowcolor=c["fg"])
        style.map(".", background=[("active", c["hover"]), ("disabled", c["bg"])],
                  foreground=[("disabled", c["muted"])])
        style.configure("TButton", padding=(10, 4), background=c["head"])
        style.configure("Tool.TButton", padding=(8, 6), background=c["bg"], borderwidth=0)
        style.configure("Toolbar.TFrame", background=c["bg"])
        style.configure("TEntry", fieldbackground=c["field"], foreground=c["fg"])
        style.configure("TSpinbox", fieldbackground=c["field"], foreground=c["fg"])
        style.configure("TCombobox", fieldbackground=c["field"], foreground=c["fg"])
        style.map("TCombobox", fieldbackground=[("readonly", c["field"])], foreground=[("readonly", c["fg"])])
        style.configure("Treeview", background=c["field"], fieldbackground=c["field"], foreground=c["fg"],
                        rowheight=24, borderwidth=0)
        style.map("Treeview", background=[("selected", c["sel"])], foreground=[("selected", c["selfg"])])
        style.configure("Treeview.Heading", background=c["head"], foreground=c["fg"], relief="flat")
        style.map("Treeview.Heading", background=[("active", c["hover"])])
        style.configure("Status.TLabel", background=c["head"], foreground=c["fg"])
        self.configure(background=c["bg"])
        self.option_add("*TCombobox*Listbox.background", c["field"])
        self.option_add("*TCombobox*Listbox.foreground", c["fg"])
        for m in self.menus:
            try:
                m.configure(background=c["field"], foreground=c["fg"], activebackground=c["sel"],
                            activeforeground=c["selfg"], selectcolor=c["fg"])
            except tk.TclError:
                pass
        self.tree.tag_configure(Status.COMPLETED, foreground=c["done"])
        self.tree.tag_configure(Status.ERROR, foreground=c["error"])
        self.tree.tag_configure(Status.PAUSED, foreground=c["paused"])

    # ---------------------------------------------------------------- actions
    def add_download(self, url, folder, conns, start=True, filename=None, headers=None) -> None:
        self.settings.dest_dir = folder or self.settings.dest_dir
        self.settings.connections = conns
        self.manager.add(url, folder, conns, start=start, filename=filename, headers=headers)
        self._last_clip = url
        self.status_var.set(f"Adicionado: {filename or url}")
        self._refresh(schedule=False)

    def open_add_dialog(self, url: str = "") -> None:
        AddDialog(self, url or self.url_var.get().strip())

    def _quick_add(self) -> None:
        url = self.url_var.get().strip()
        if not url.lower().startswith(("http://", "https://", "ftp://")):
            messagebox.showwarning("URL inválida", "Informe um link que comece com http://, https:// ou ftp://")
            return
        self.url_var.set("")
        if is_video_site(url):
            self.open_video_dialog(url)
        else:
            self.add_download(url, self.settings.dest_dir, self.settings.connections)

    def open_video_dialog(self, url: str = "") -> None:
        url = url or self.url_var.get().strip()
        if not url:
            url = simpledialog.askstring("Baixar vídeo", "Endereço da página do vídeo:", parent=self) or ""
        if url.strip():
            VideoDialog(self, url.strip())

    def _selected(self):
        ids = set(self.tree.selection())
        return [t for t in self.manager.tasks if t.id in ids]

    def _paste(self) -> None:
        try:
            self.url_var.set(self.clipboard_get().strip())
        except tk.TclError:
            pass

    def _choose_dir(self) -> None:
        path = filedialog.askdirectory(initialdir=self.settings.dest_dir)
        if path:
            self.settings.dest_dir = path
            self.status_var.set(f"Pasta padrão: {path}")

    def _max_active(self) -> None:
        n = simpledialog.askinteger("Downloads simultâneos", "Quantos downloads ao mesmo tempo?",
                                    initialvalue=self.manager.max_active, minvalue=1, maxvalue=20, parent=self)
        if n:
            self.manager.max_active = self.settings.max_active = n

    def _global_limit(self) -> None:
        kb = simpledialog.askinteger(
            "Limitador de velocidade", "Velocidade máxima total em KB/s (0 = sem limite):",
            initialvalue=self.settings.global_limit_kb, minvalue=0, parent=self)
        if kb is not None:
            self.settings.global_limit_kb = kb
            GLOBAL_LIMITER.rate = kb * 1024
            self.status_var.set("Limite removido" if kb == 0 else f"Limite total: {kb} KB/s")

    def _task_limit(self) -> None:
        tasks = self._selected()
        if not tasks:
            return
        kb = simpledialog.askinteger(
            "Limitar velocidade", "Velocidade máxima deste download em KB/s (0 = sem limite):",
            initialvalue=tasks[0].speed_limit // 1024, minvalue=0, parent=self)
        if kb is not None:
            for t in tasks:
                t.speed_limit = kb * 1024
            self.manager.save()

    def _resume(self) -> None:
        for t in self._selected():
            self.manager.resume(t)

    def _pause(self) -> None:
        for t in self._selected():
            self.manager.pause(t)

    def _pause_all(self) -> None:
        for t in self.manager.tasks:
            t.pause()

    def _clear_finished(self) -> None:
        for t in [t for t in self.manager.tasks if t.status == Status.COMPLETED]:
            self.manager.remove(t)
        self._refresh(schedule=False)

    def _remove(self) -> None:
        tasks = self._selected()
        if not tasks:
            return
        answer = messagebox.askyesnocancel(
            "Excluir", f"Excluir {len(tasks)} download(s) da lista?\n\n"
            "Sim = excluir e apagar arquivos incompletos\nNão = só tirar da lista")
        if answer is None:
            return
        for t in tasks:
            self.manager.remove(t, delete_files=answer and t.status != Status.COMPLETED)
        self._refresh(schedule=False)

    def _open_file(self) -> None:
        for t in self._selected():
            if t.status == Status.COMPLETED and t.path and os.path.exists(t.path):
                open_path(t.path)
                return
            if t.status in (Status.PAUSED, Status.ERROR):
                self.manager.resume(t)

    def _open_folder(self) -> None:
        tasks = self._selected()
        folder = tasks[0].dest_dir if tasks else self.settings.dest_dir
        if os.path.isdir(folder):
            open_path(folder)

    def _copy_url(self) -> None:
        tasks = self._selected()
        if tasks:
            self.clipboard_clear()
            self.clipboard_append(tasks[0].url)
            self._last_clip = tasks[0].url

    def _context_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.ctx.tk_popup(event.x_root, event.y_root)

    def _on_filter(self, _event=None) -> None:
        sel = self.side.selection()
        self._filter = sel[0] if sel else "all"
        self._refresh(schedule=False)

    def _toggle_theme(self) -> None:
        self.settings.dark = self.dark_var.get()
        self.apply_theme()

    def _apply_columns(self) -> None:
        cols = [k for k in COLUMNS if self.col_vars[k].get() or k == "name"]
        self.settings.columns = cols
        self.tree.configure(displaycolumns=cols)

    # ------------------------------------------------------- browser bridge
    def _toggle_bridge(self) -> None:
        self.settings.browser_integration = self.bridge_var.get()
        if self.bridge_var.get():
            if not self.service.start_bridge():
                self.status_var.set("Integração com navegador indisponível: a porta já está em uso")
        else:
            self.service.stop_bridge()

    def _toggle_autostart(self) -> None:
        try:
            autostart.set_enabled(self.autostart_var.get())
        except OSError as exc:
            messagebox.showerror("Iniciar com o computador", str(exc), parent=self)
            self.autostart_var.set(autostart.is_enabled())

    def _process_events(self) -> None:
        """Handle what the background service received while the window is open."""
        svc = self.service
        while True:
            try:
                kind, data = svc.events.get_nowait()
            except queue.Empty:
                break
            if kind == "tick":
                action = svc.on_tick(data)
                if action == "countdown":
                    ShutdownCountdown(self)
                elif action == "quit":
                    self.after(2000, self.quit_all)
            elif kind == "item":
                if svc.accept_item(data) and data.get("kind") == "show":
                    self.show_window()
            elif kind == "quit":
                self.quit_all()
                return
        svc.check_scheduler()
        while svc.pending:
            item = svc.pending.pop(0)
            if item.get("kind") == "video":
                VideoDialog(self, item["page"], item.get("title"), item.get("media"), item.get("cookie_jar"))
                continue
            headers = {}
            if item.get("referrer"):
                headers["Referer"] = item["referrer"]
            if item.get("cookies"):
                headers["Cookie"] = item["cookies"]
            AddDialog(self, item["url"], item.get("filename"), headers, title="Download do navegador")

    def _extension_help(self) -> None:
        folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "browser-extension")
        messagebox.showinfo(
            "Extensão do navegador",
            "Brave (ou Chrome / Edge / Opera):\n"
            "1. Abra brave://extensions\n"
            "2. Ative o 'Modo de desenvolvedor' (canto superior direito)\n"
            "3. Clique em 'Carregar sem compactação' e escolha a pasta:\n"
            f"   {folder}\n\n"
            "Com o Flow Downloads aberto, os downloads do navegador passam a ser feitos por ele.\n\n"
            "Para baixar um vídeo: copie o link, clique no ícone do Flow Downloads na barra do navegador, "
            "cole o link em 'Baixar vídeo' e escolha a qualidade.")

    # ---------------------------------------------------------------- refresh
    def _on_focus(self, event) -> None:
        if event.widget is self:
            self._check_clipboard()

    def _check_clipboard(self) -> None:
        if not self.clip_var.get():
            return
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        if text != self._last_clip and text.lower().startswith(("http://", "https://", "ftp://")) \
                and " " not in text and len(text) < 4000:
            self._last_clip = text
            if not self.url_var.get():
                self.url_var.set(text)
                self.status_var.set("Link copiado detectado — clique em Baixar para iniciar")

    def _visible(self, t) -> bool:
        f = self._filter
        if f == "unfinished":
            return t.status != Status.COMPLETED
        if f == "finished":
            return t.status == Status.COMPLETED
        if f.startswith("cat:"):
            return t.category == f[4:]
        return True

    def _row(self, t) -> tuple:
        status = t.status
        if status == Status.ERROR and t.error:
            status = f"Erro: {t.error}"
        elif status == Status.DOWNLOADING and t.speed_limit:
            status = f"Baixando (limite {format_size(t.speed_limit)}/s)"
        running = t.is_running()
        return (
            t.filename or t.url,
            format_size(t.size),
            progress_bar(t.progress),
            f"{format_size(t.speed)}/s" if running else "",
            format_time(t.eta) if running else "",
            t.active_connections or "",
            status,
            t.category,
            time.strftime("%d/%m/%Y %H:%M", time.localtime(t.added_at)),
            t.url,
        )

    def _refresh(self, schedule: bool = True) -> None:
        if schedule:
            if not self.winfo_exists():
                return
            self._process_events()
            state = self.state()
            if state == "withdrawn":
                # Dialog-only mode: go back to the background once the dialogs are closed.
                if not self._dialogs_open() and not self.service.pending:
                    self.destroy()
                    return
                self.after(300, self._refresh)
                return
            busy = any(t.is_running() for t in self.manager.tasks)
            if state == "iconic":  # minimized: don't redraw anything
                self.after(1000, self._refresh)
                return
            if busy and time.monotonic() - self._last_save > 5:
                self._last_save = time.monotonic()
                self.manager.save()  # keep progress in the list file
        total_speed = 0.0
        active = 0
        visible = [t for t in self.manager.tasks if self._visible(t)]
        visible_ids = {t.id for t in visible}
        for iid in self.tree.get_children():
            if iid not in visible_ids:
                self.tree.delete(iid)
        for idx, t in enumerate(visible):
            values = self._row(t)
            if self.tree.exists(t.id):
                self.tree.item(t.id, values=values, tags=(t.status,))
                self.tree.move(t.id, "", idx)
            else:
                self.tree.insert("", idx, iid=t.id, values=values, tags=(t.status,))
        for t in self.manager.tasks:
            total_speed += t.speed
            active += t.is_running()
            prev = self._statuses.get(t.id)
            if prev != t.status and t.status == Status.COMPLETED and prev is not None:
                self.status_var.set(f"✔ Concluído: {t.filename}")
                self.bell()
            self._statuses[t.id] = t.status

        counts = {"all": len(self.manager.tasks),
                  "unfinished": sum(t.status != Status.COMPLETED for t in self.manager.tasks),
                  "finished": sum(t.status == Status.COMPLETED for t in self.manager.tasks)}
        for key, label in self.SIDEBAR:
            self.side.item(key, text=f"{label} ({counts[key]})")

        if schedule:
            if active:
                limit = f" (limite {self.settings.global_limit_kb} KB/s)" if self.settings.global_limit_kb else ""
                self.status_var.set(f"{active} download(s) ativo(s) — {format_size(total_speed)}/s{limit}")
            # Fast refresh only while something is downloading.
            self.after(REFRESH_MS if active else 1500, self._refresh)


def main() -> None:
    from .service import run
    run()


if __name__ == "__main__":
    main()
