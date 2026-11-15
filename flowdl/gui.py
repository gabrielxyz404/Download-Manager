"""Tkinter desktop interface for Flow Downloads."""

from __future__ import annotations

import gc
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
from .categories import CATEGORIES, OTHER, default_rules, folder_for, name_from_url, pick_category
from .engine import Status, format_size, format_time
from .limiter import GLOBAL_LIMITER
from .manager import config_dir
from .paths import FROZEN, install_dir
from .scheduler import ACTIONS, shutdown_computer, sleep_computer
from .settings import DEFAULT_COLUMNS, OLD_DEFAULT_COLUMNS, Settings  # noqa: F401 (re-exported)
from .video import VideoTask

REFRESH_MS = 500
VIDEO_SITES = ("youtube.com", "youtu.be", "vimeo.com", "tiktok.com", "instagram.com", "facebook.com",
               "fb.watch", "x.com", "twitter.com", "twitch.tv", "dailymotion.com", "reddit.com",
               "soundcloud.com", "bilibili.com", "kwai.com")


def is_video_site(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    return any(host == d or host.endswith("." + d) for d in VIDEO_SITES)
COLUMNS = {
    "name": ("Arquivo", 200),
    "size": ("Tamanho", 80),
    "progress": ("Progresso", 140),
    "speed": ("Velocidade", 92),
    "eta": ("Restante", 78),
    "conns": ("Conexões", 82),
    "status": ("Status", 200),
    "category": ("Categoria", 95),
    "added": ("Adicionado", 115),
    "url": ("Endereço", 200),
}

# Looks of the app: black & white only (light / dark). Colours only, no images or extra library,
# so a look costs nothing. Keys keep their meaning: "accent" is the main button / the bars,
# "muted" secondary text, "trough" the empty part of a bar.
THEMES = {
    "claro": dict(label="Claro", bg="#ffffff", fg="#111111", field="#ffffff", head="#f5f5f5",
                  border="#e6e6e6", sel="#ececec", selfg="#111111", hover="#f0f0f0", muted="#6e6e6e",
                  sidebar="#f5f5f5", stripe="#fafafa", accent="#111111", accentfg="#ffffff",
                  trough="#e8e8e8", partial="#9a9a9a",
                  done="#6e6e6e", error="#111111", paused="#111111"),
    "escuro": dict(label="Escuro", bg="#111111", fg="#eeeeee", field="#111111", head="#1a1a1a",
                   border="#2a2a2a", sel="#262626", selfg="#ffffff", hover="#222222", muted="#9a9a9a",
                   sidebar="#1a1a1a", stripe="#151515", accent="#f2f2f2", accentfg="#111111",
                   trough="#2c2c2c", partial="#6e6e6e",
                   done="#9a9a9a", error="#eeeeee", paused="#eeeeee"),
}
THEMES["light"], THEMES["dark"] = THEMES["claro"], THEMES["escuro"]  # older settings / dialogs
DARK_THEMES = ("escuro",)
OLD_LOOKS = {"meia-noite": "escuro", "brasa": "escuro", "menta": "claro"}  # looks of 0.8.0


def windows_prefers_dark() -> bool:
    """Windows' own "dark mode for apps" setting (False elsewhere / if unknown)."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def theme_name(settings) -> str:
    """The chosen look; "auto" follows Windows (light / dark)."""
    name = getattr(settings, "theme", None) or ("escuro" if getattr(settings, "dark", False) else "claro")
    if name == "auto":
        return "escuro" if windows_prefers_dark() else "claro"
    name = OLD_LOOKS.get(name, name)
    return name if name in ("claro", "escuro") else "claro"


def open_path(path: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def progress_bar(fraction: float, width: int = 9) -> str:
    filled = int(round(fraction * width))
    return "━" * filled + "╌" * (width - filled) + f"  {fraction * 100:5.1f}%"


# ------------------------------------------------------------------- dialogs
class AddDialog(tk.Toplevel):
    """'Download file info' dialog: confirm name / folder before downloading."""

    def __init__(self, app: "App", url: str = "", filename: str | None = None,
                 headers: dict | None = None, title: str = "Adicionar download", rid: str | None = None,
                 cookies: dict | None = None, final_url: str | None = None):
        super().__init__(app)
        self.app, self.headers = app, headers
        self.cookies, self.final_url = cookies, final_url
        self.rid = rid  # set when the download came from the browser
        self._decided = False
        self.bind("<Destroy>", self._on_destroy)
        self.title(title)
        app.attach_dialog(self)
        self.resizable(True, False)
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        self.url = tk.StringVar(value=url)
        self.url_original = url
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
        ttk.Label(frm, text="Conexões (máx.):").grid(row=4, column=0, sticky="w", pady=3)
        ttk.Spinbox(frm, from_=1, to=32, width=5, textvariable=self.conns).grid(row=4, column=1, sticky="w")
        self.auto = tk.BooleanVar(value=app.settings.category_folders)
        ttk.Checkbutton(frm, text="Guardar na pasta da categoria (Vídeos, Compactados...) dentro desta pasta",
                        variable=self.auto).grid(row=5, column=1, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(frm, text="Checksum (opcional):").grid(row=6, column=0, sticky="w", pady=3)
        self.checksum = tk.StringVar()
        ttk.Entry(frm, textvariable=self.checksum).grid(row=6, column=1, columnspan=2, sticky="ew")
        ttk.Label(frm, text="(MD5, SHA-1 ou SHA-256 do site: confirma que o ficheiro chegou perfeito)",
                  foreground=app.colors["muted"]).grid(row=7, column=1, sticky="w")

        btns = ttk.Frame(frm)
        btns.grid(row=8, column=0, columnspan=3, sticky="e", pady=(12, 0))
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
        if self.rid and self.app.service.request_state(self.rid) == "cancelled":
            # The browser stopped waiting meanwhile and carries on with its own download:
            # starting here too would download the file twice.
            self._decided = True
            messagebox.showinfo("Download do navegador", "O navegador já continuou este download "
                                "sozinho (a app esperou demasiado pela confirmação).", parent=self)
            self.destroy()
            return
        url = self.url.get().strip()
        if not url.lower().startswith(("http://", "https://", "ftp://")):
            messagebox.showwarning("URL inválida", "O link deve começar com http://, https:// ou ftp://",
                                   parent=self)
            return
        try:
            conns = max(1, min(32, int(self.conns.get())))
        except (tk.TclError, ValueError):
            conns = 8
        same_link = url == self.url_original
        if not getattr(self, "_dup_checked", False):
            existing = self.app.manager.find_duplicate(url, self.final_url if same_link else None)
            if existing is not None:
                self._dup_checked = True
                self.withdraw()

                def again():
                    self._ok(start)

                def resume():
                    self._decided = True
                    self.app.service.bind_request(self.rid, existing)
                    self.app.manager.resume(existing)
                    self.destroy()

                DuplicateDialog(self.app, existing, on_new=again, on_resume=resume,
                                on_close=lambda: self.winfo_exists() and self.destroy())
                return
        task = self.app.add_download(url, self.folder.get(), conns, start,
                                     self.name.get().strip() or None, self.headers,
                                     cookies=self.cookies, final_url=self.final_url if same_link else None,
                                     auto_folder=bool(self.auto.get()),
                                     page=(self.headers or {}).get("Referer", ""), now=bool(self.rid))
        from .after import algo_for, clean_hash
        wanted = clean_hash(self.checksum.get())
        if wanted and algo_for(wanted):
            task.expected_hash = wanted
        self._decided = True
        svc = self.app.service
        if start:
            svc.bind_request(self.rid, task)  # the browser shows it too
            if self.app.settings.show_progress_window:
                ProgressWindow(self.app, task)
        else:
            svc.cancel_request(self.rid, "later")  # starts later, only in the app
        self.destroy()

    def _on_destroy(self, event) -> None:
        if event.widget is self and not self._decided:
            self.app.service.cancel_request(self.rid)  # "Cancelar": the extension stops waiting


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
        self.geometry("860x560")
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
                                  None, {"Referer": self.url.get().strip()},
                                  auto_folder=self.app.settings.category_folders, page=self.url.get().strip())
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
        self.minsize(640, 0)  # tall enough for its content (rows are taller in the modern look)
        self.results: queue.Queue = queue.Queue()
        self.choices: dict[str, dict] = {}
        self.cookie_jar = list(cookie_jar or [])  # kept in memory; written only temporarily
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
        self.bind("<Return>", lambda _e: self._download())
        self.bind("<Escape>", lambda _e: self.destroy())

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
                self.results.put(("ok", video.get_options(self.page, None, self.headers, self.cookie_jar)))
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
            folder = s.dest_dir
            if s.category_folders:  # videos in the Vídeos folder, audio in Músicas
                folder = folder_for(s.dest_dir, "x.mp3" if audio else "x.mp4", rules=s.category_rules())
            task = VideoTask(c["url"], folder, fmt, audio, self.name_var.get(), s.connections,
                             headers=self.headers, cookie_jar=self.cookie_jar,
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
        if FROZEN or video.available():
            # The newest yt-dlp from its official releases (checksum checked), used from now on.
            app.status_var.set("A procurar a versão mais recente do suporte a vídeos...")
            work = video.update_yt_dlp
        else:
            app.status_var.set("Instalando o yt-dlp...")
            work = video.install_yt_dlp
        threading.Thread(target=lambda: q.put(work()), daemon=True).start()

        def poll():
            try:
                ok, out = q.get_nowait()
            except queue.Empty:
                app.after(300, poll)
                return
            if ok and work is video.install_yt_dlp:
                importlib.invalidate_caches()
                try:
                    video.yt_dlp = importlib.import_module("yt_dlp")
                except ImportError:
                    ok = False
            if ok:
                app.status_var.set(("✔ " + out) if work is video.update_yt_dlp else "Suporte a vídeos instalado ✔")
            else:
                messagebox.showerror("Suporte a vídeos", "Não consegui atualizar o suporte a vídeos:\n\n" + out[-800:])
            if on_done:
                on_done(ok)

        app.after(300, poll)


def re_safe(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in text)[:80]


class ProgressWindow(tk.Toplevel):
    """Small window with a download's progress (like IDM's). Closes itself when done."""

    def __init__(self, app: "App", task):
        super().__init__(app)
        self.app, self.task = app, task
        app.attach_dialog(self)
        self.resizable(False, False)
        self.title("Flow Downloads")
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        self.vars = {}
        rows = [("url", "Endereço"), ("state", "Estado"), ("size", "Tamanho"), ("done", "Baixado"),
                ("speed", "Velocidade"), ("eta", "Tempo restante"), ("conns", "Conexões")]
        for i, (key, label) in enumerate(rows):
            ttk.Label(frm, text=label + ":").grid(row=i, column=0, sticky="w", pady=1)
            self.vars[key] = tk.StringVar()
            ttk.Label(frm, textvariable=self.vars[key], width=52).grid(row=i, column=1, sticky="w", padx=(8, 0))
        self.vars["url"].set(task.url if len(task.url) <= 60 else task.url[:57] + "...")
        self.bar = ttk.Progressbar(frm, length=440, mode="determinate", maximum=1000)
        self.bar.grid(row=len(rows), column=0, columnspan=2, sticky="ew", pady=(10, 8))
        btns = ttk.Frame(frm)
        btns.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="e")
        self.pause_btn = ttk.Button(btns, text="Pausar", command=self._toggle)
        self.pause_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="Cancelar", command=self._cancel).pack(side="left", padx=4)
        ttk.Button(btns, text="Esconder", command=self.destroy).pack(side="left", padx=4)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.lift()
        self.attributes("-topmost", True)
        self.after(400, lambda: self.winfo_exists() and self.attributes("-topmost", False))
        self._closing = False
        self._update()

    def _toggle(self) -> None:
        if self.task.is_running():
            self.app.manager.pause(self.task)
        else:
            self.app.manager.resume(self.task)

    def _cancel(self) -> None:
        if messagebox.askyesno("Cancelar download", "Cancelar este download?", parent=self):
            self.app.manager.remove(self.task, delete_files=True)
            self.destroy()

    def _update(self) -> None:
        if not self.winfo_exists():
            return
        t = self.task
        running = t.is_running()
        status = t.status
        if status == Status.ERROR and t.error:
            status = f"Erro: {t.error}"
        elif status == Status.PAUSED and t.error:
            status = f"Pausado: {t.error}"  # e.g. the disk is full: says what to do
        if status == Status.COMPLETED and getattr(t, "check", ""):
            status = f"Concluído · {t.check}"
        elif status == Status.DOWNLOADING:
            status = "Recebendo dados..." + (f" ({t.connection_note})" if getattr(t, "connection_note", "") else "")
        self.vars["state"].set(status)
        self.vars["size"].set(format_size(t.size))
        self.vars["done"].set(f"{format_size(t.downloaded)} ({t.progress * 100:.1f}%)")
        self.vars["speed"].set(f"{format_size(t.speed)}/s" if running else "-")
        self.vars["eta"].set(format_time(t.eta) if running else "-")
        self.vars["conns"].set(str(t.active_connections) if running else "-")
        self.bar["value"] = int(t.progress * 1000)
        self.title(f"{t.progress * 100:.0f}% {t.filename or ''}".strip())
        self.pause_btn.configure(text="Pausar" if running or t.status == Status.QUEUED else "Retomar")
        if t.status == Status.COMPLETED and not self._closing:
            self._closing = True
            self.after(1200, self.destroy)  # the browser's list now shows it finished
            return
        if t not in self.app.manager.tasks:  # removed elsewhere
            self.destroy()
            return
        self.after(500, self._update)


class CategoriesDialog(tk.Toplevel):
    """Categories: their rules and the folder new downloads of each one go to."""

    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        app.attach_dialog(self)
        self.title("Categorias e pastas")
        self.geometry("760x520")
        self.rules = [dict(r) for r in app.settings.category_rules()]
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        self.on = tk.BooleanVar(value=app.settings.category_folders)
        ttk.Checkbutton(frm, text="Guardar cada categoria na sua pasta (ex.: Downloads\\Vídeos)",
                        variable=self.on).pack(anchor="w")
        ttk.Label(frm, text="Os downloads vindos do navegador ficam na pasta do navegador.",
                  foreground=app.colors["muted"]).pack(anchor="w", pady=(0, 8))
        self.tree = ttk.Treeview(frm, columns=("name", "folder", "types"), show="headings", height=9)
        for key, title, w in (("name", "Categoria", 120), ("folder", "Pasta", 260), ("types", "Tipos", 300)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=w, stretch=key == "types")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _e: self._edit())
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Adicionar", command=self._add).pack(side="left")
        ttk.Button(btns, text="Editar", command=self._edit).pack(side="left", padx=4)
        ttk.Button(btns, text="Apagar", command=self._delete).pack(side="left")
        ttk.Button(btns, text="Repor as de origem", command=self._reset).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancelar", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Guardar", command=self._save).pack(side="right", padx=4)
        self._fill()

    def _folder_text(self, rule: dict) -> str:
        return rule.get("folder") or os.path.join(self.app.settings.dest_dir, rule["name"])

    def _fill(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.rules):
            extra = len(r.get("url_patterns", [])) + len(r.get("name_patterns", []))
            types = ", ".join(r.get("exts", [])) + (f"  (+{extra} regras)" if extra else "")
            self.tree.insert("", "end", iid=str(i), values=(r["name"], self._folder_text(r), types))

    def _selected(self) -> int | None:
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _add(self) -> None:
        CategoryEditor(self, {"name": "", "exts": [], "folder": None, "url_patterns": [], "name_patterns": []},
                       None)

    def _edit(self) -> None:
        i = self._selected()
        if i is not None:
            CategoryEditor(self, self.rules[i], i)

    def _delete(self) -> None:
        i = self._selected()
        if i is not None:
            del self.rules[i]
            self._fill()

    def _reset(self) -> None:
        self.rules = default_rules()
        self._fill()

    def store(self, rule: dict, index: int | None) -> None:
        if index is None:
            self.rules.append(rule)
        else:
            self.rules[index] = rule
        self._fill()

    def _save(self) -> None:
        s = self.app.settings
        s.category_folders = bool(self.on.get())
        s.categories = self.rules
        s.save()
        self.app.service.apply_category_settings()
        self.app._build_category_sidebar()
        self.app._refresh(schedule=False)
        self.destroy()


class CategoryEditor(tk.Toplevel):
    def __init__(self, parent: CategoriesDialog, rule: dict, index: int | None):
        super().__init__(parent)
        self.parent, self.index = parent, index
        self.transient(parent)
        self.title("Categoria")
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        self.name = tk.StringVar(value=rule.get("name", ""))
        self.exts = tk.StringVar(value=", ".join(rule.get("exts", [])))
        self.folder = tk.StringVar(value=rule.get("folder") or "")
        ttk.Label(frm, text="Nome:").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=self.name, width=50).grid(row=0, column=1, columnspan=2, sticky="ew")
        ttk.Label(frm, text="Tipos de ficheiro:").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=self.exts).grid(row=1, column=1, columnspan=2, sticky="ew")
        ttk.Label(frm, text="separados por vírgulas, ex.: mp4, mkv, webm", foreground=parent.app.colors["muted"]
                  ).grid(row=2, column=1, sticky="w")
        ttk.Label(frm, text="Pasta:").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=self.folder).grid(row=3, column=1, sticky="ew")
        ttk.Button(frm, text="...", width=3, command=self._browse).grid(row=3, column=2, padx=(4, 0))
        ttk.Label(frm, text="vazio = subpasta com o nome da categoria", foreground=parent.app.colors["muted"]
                  ).grid(row=4, column=1, sticky="w")
        ttk.Label(frm, text="Endereços:").grid(row=5, column=0, sticky="nw", pady=2)
        self.urls = tk.Text(frm, height=3, width=40)
        self.urls.grid(row=5, column=1, columnspan=2, sticky="ew")
        self.urls.insert("1.0", "\n".join(rule.get("url_patterns", [])))
        ttk.Label(frm, text="um por linha, ex.: github.com/ ou *.exemplo.com/*",
                  foreground=parent.app.colors["muted"]).grid(row=6, column=1, sticky="w")
        ttk.Label(frm, text="Nomes:").grid(row=7, column=0, sticky="nw", pady=2)
        self.names = tk.Text(frm, height=3, width=40)
        self.names.grid(row=7, column=1, columnspan=2, sticky="ew")
        self.names.insert("1.0", "\n".join(rule.get("name_patterns", [])))
        ttk.Label(frm, text="um por linha, ex.: fatura_* (sem * procura em qualquer parte do nome)",
                  foreground=parent.app.colors["muted"]).grid(row=8, column=1, sticky="w")
        btns = ttk.Frame(frm)
        btns.grid(row=9, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="OK", command=self._ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancelar", command=self.destroy).pack(side="left")

    def _browse(self) -> None:
        path = filedialog.askdirectory(parent=self, initialdir=self.folder.get() or self.parent.app.settings.dest_dir)
        if path:
            self.folder.set(path)

    def _ok(self) -> None:
        name = self.name.get().strip()
        if not name or name == OTHER:
            messagebox.showwarning("Categoria", "Escolha um nome para a categoria.", parent=self)
            return
        lines = lambda w: [x.strip() for x in w.get("1.0", "end").splitlines() if x.strip()]  # noqa: E731
        rule = {"name": name,
                "exts": [e.strip().lower().lstrip(".") for e in self.exts.get().split(",") if e.strip()],
                "folder": self.folder.get().strip() or None,
                "url_patterns": lines(self.urls), "name_patterns": lines(self.names)}
        self.parent.store(rule, self.index)
        self.destroy()


class AfterDialog(tk.Toplevel):
    """What to do with each file when its download finishes."""

    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        app.attach_dialog(self)
        self.title("Ao terminar um download")
        s = app.settings
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        self.scan = tk.BooleanVar(value=s.after_scan)
        self.extract = tk.BooleanVar(value=s.after_extract)
        self.open = tk.BooleanVar(value=s.after_open)
        ttk.Checkbutton(frm, text="Analisar com o antivírus do Windows", variable=self.scan).pack(anchor="w")
        ttk.Checkbutton(frm, text="Extrair ficheiros .zip (para uma pasta ao lado)", variable=self.extract).pack(anchor="w")
        ttk.Checkbutton(frm, text="Abrir o ficheiro", variable=self.open).pack(anchor="w")
        ttk.Label(frm, text="Correr um comando (opcional):").pack(anchor="w", pady=(10, 2))
        self.command = tk.StringVar(value=s.after_command)
        ttk.Entry(frm, textvariable=self.command, width=60).pack(fill="x")
        ttk.Label(frm, text="Pode usar {ficheiro}, {pasta}, {nome} e {url}.",
                  foreground=app.colors["muted"]).pack(anchor="w")
        ttk.Label(frm, text="Se um checksum não conferir ou o antivírus encontrar algo, nada é aberto nem extraído.",
                  foreground=app.colors["muted"]).pack(anchor="w", pady=(6, 0))
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Cancelar", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Guardar", command=self._save).pack(side="right", padx=4)

    def _save(self) -> None:
        s = self.app.settings
        s.after_scan, s.after_extract, s.after_open = self.scan.get(), self.extract.get(), self.open.get()
        s.after_command = self.command.get().strip()
        s.save()
        self.destroy()


class HostLimitsDialog(tk.Toplevel):
    """Max connections for chosen sites (some only accept a few, or block who opens many)."""

    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        app.attach_dialog(self)
        self.title("Limites por site")
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Máximo de ligações por site, um por linha (ex.: lento.com = 2).\n"
                            "Os outros sites usam o teste de velocidade automático.").pack(anchor="w")
        self.text = tk.Text(frm, height=8, width=50)
        self.text.pack(fill="both", expand=True, pady=(6, 0))
        self.text.insert("1.0", "\n".join(f"{h} = {n}" for h, n in app.settings.host_limits.items()))
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Cancelar", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Guardar", command=self._save).pack(side="right", padx=4)

    def _save(self) -> None:
        from .hostmemory import parse_limits
        limits = parse_limits(self.text.get("1.0", "end"))
        self.app.settings.host_limits = limits
        self.app.settings.save()
        self.app.manager.host_limits = dict(limits)
        self.destroy()


class CaptureDialog(tk.Toplevel):
    """Which browser downloads the app takes over."""

    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        app.attach_dialog(self)
        self.title("Tipos apanhados do navegador")
        s = app.settings
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        self.all = tk.BooleanVar(value=s.capture_all_types)
        ttk.Checkbutton(frm, text="Apanhar todos os tipos de ficheiro", variable=self.all,
                        command=self._toggle).pack(anchor="w")
        ttk.Label(frm, text="Apanhar só estes tipos (os outros ficam com o navegador):").pack(anchor="w", pady=(8, 2))
        self.types = tk.Text(frm, height=5, width=70, wrap="word")
        self.types.pack(fill="x")
        self.types.insert("1.0", " ".join(s.capture_types))
        ttk.Button(frm, text="Repor a lista de origem", command=self._reset).pack(anchor="w", pady=(4, 0))
        ttk.Label(frm, text="Nunca apanhar destes sites (um por linha, ex.: banco.pt ou *.exemplo.com):"
                  ).pack(anchor="w", pady=(10, 2))
        self.sites = tk.Text(frm, height=4, width=70)
        self.sites.pack(fill="x")
        self.sites.insert("1.0", "\n".join(s.capture_skip_sites))
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Cancelar", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Guardar", command=self._save).pack(side="right", padx=4)
        self._toggle()

    def _toggle(self) -> None:
        self.types.configure(state="disabled" if self.all.get() else "normal")

    def _reset(self) -> None:
        from .capture import DEFAULT_TYPES
        self.types.configure(state="normal")
        self.types.delete("1.0", "end")
        self.types.insert("1.0", " ".join(DEFAULT_TYPES))
        self._toggle()

    def _save(self) -> None:
        import re as _re
        s = self.app.settings
        s.capture_all_types = bool(self.all.get())
        s.capture_types = [t.lower().lstrip(".") for t in _re.split(r"[\s,;]+", self.types.get("1.0", "end")) if t.strip()]
        s.capture_skip_sites = [x.strip() for x in self.sites.get("1.0", "end").splitlines() if x.strip()]
        s.save()
        self.destroy()


class DuplicateDialog(tk.Toplevel):
    """'This download is already in the list': resume / open it, or download again."""

    def __init__(self, app: "App", existing, on_new, on_resume=None, on_close=None):
        super().__init__(app)
        self.app, self.task = app, existing
        self.on_new, self.on_resume, self.on_close = on_new, on_resume, on_close
        self._chosen = False
        app.attach_dialog(self)
        self.title("Download repetido")
        self.resizable(False, False)
        frm = ttk.Frame(self, padding=16)
        frm.pack(fill="both", expand=True)
        done = existing.status == Status.COMPLETED
        has_file = done and existing.path and os.path.exists(existing.path)
        name = existing.filename or existing.url
        state = "já foi descarregado" if done else f"já está na lista ({existing.status.lower()}"
        state += "" if done else f", {existing.progress * 100:.0f}%)"
        ttk.Label(frm, text=f"Este download {state}:", font=("", 10, "bold")).pack(anchor="w")
        ttk.Label(frm, text=name, wraplength=440).pack(anchor="w", pady=(2, 12))
        btns = ttk.Frame(frm)
        btns.pack(anchor="e")
        if has_file:
            first = ttk.Button(btns, text="Abrir o ficheiro", command=self._open)
        elif not done:
            first = ttk.Button(btns, text="Retomar o existente", command=self._resume)
        else:
            first = None  # finished, but the file is gone: only "download again" makes sense
        if first:
            first.pack(side="left", padx=4)
        ttk.Button(btns, text="Baixar de novo", command=self._new).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancelar", command=self.destroy).pack(side="left", padx=4)
        self.bind("<Return>", lambda _e: (first.invoke() if first else self._new()))
        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Destroy>", self._destroyed)
        self.lift()
        self.attributes("-topmost", True)
        self.after(400, lambda: self.winfo_exists() and self.attributes("-topmost", False))
        self.focus_force()

    def _pick(self, action) -> None:
        self._chosen = True
        self.destroy()
        if action:
            action()

    def _open(self) -> None:
        open_path(self.task.path)
        self._pick(None)
        if self.on_close:
            self.on_close()

    def _resume(self) -> None:
        self._pick(self.on_resume)

    def _new(self) -> None:
        self._pick(self.on_new)

    def _destroyed(self, event) -> None:
        if event.widget is self and not self._chosen and self.on_close:
            self.on_close()


class ShutdownCountdown(tk.Toplevel):
    def __init__(self, app: "App", seconds: int = 30, action: str | None = None):
        super().__init__(app)
        self.app, self.left = app, seconds
        self.action = action or app.service.scheduler.on_complete
        self.sleeping = self.action == "sleep"
        self.title("Suspender o computador" if self.sleeping else "Desligar o computador")
        self.attributes("-topmost", True)
        frm = ttk.Frame(self, padding=20)
        frm.pack()
        self.msg = tk.StringVar()
        ttk.Label(frm, textvariable=self.msg, font=("", 11)).pack(pady=(0, 12))
        ttk.Button(frm, text="Cancelar", command=self.destroy).pack()
        self._tick()

    def _tick(self) -> None:
        if self.left <= 0:
            if self.sleeping:  # the app keeps running and is there when the computer wakes up
                self.destroy()
                sleep_computer()
                return
            self.app.quit_all()
            shutdown_computer()
            return
        what = "suspenso" if self.sleeping else "desligado"
        self.msg.set(f"Downloads concluídos. O computador será {what} em {self.left} s.")
        self.left -= 1
        self.after(1000, self._tick)


# ------------------------------------------------------------------ main app
def error_hint(error: str) -> str:
    """What to do about a download error, in plain words."""
    e = (error or "").lower()
    if "403" in e or "401" in e or "410" in e or "expir" in e:
        return "o link pode ter expirado: volte à página, copie o link de novo e use Atualizar link"
    if "404" in e:
        return "o ficheiro já não está neste endereço: procure-o de novo na página"
    if "espaço" in e or "disco" in e or "space" in e:
        return "liberte espaço no disco (ou mude a pasta) e clique em Tentar de novo"
    if "tempo" in e or "timed out" in e or "conex" in e or "connection" in e or "rede" in e:
        return "a ligação falhou: verifique a internet e clique em Tentar de novo"
    return "clique em Tentar de novo; se repetir, atualize o link"


class DetailsPanel(ttk.Frame):
    """Right side of the window: the chosen download, with the map of its parts.
    Built once; refresh() only changes texts and colours (cheap, twice a second)."""

    CELLS, ROWS = 36, 2
    WIDTH = 360

    def __init__(self, app: "App", parent):
        super().__init__(parent, style="Soft.TFrame", padding=(20, 16), width=self.WIDTH + 40)
        self.app = app
        self.task = None
        self._map_key = None
        self.empty = ttk.Label(self, text="Escolha um download na lista para ver os detalhes.",
                               style="SoftMuted.TLabel", wraplength=self.WIDTH)
        self.box = ttk.Frame(self, style="Soft.TFrame")
        self.name = ttk.Label(self.box, style="SoftName.TLabel", wraplength=self.WIDTH)
        self.name.pack(anchor="w")
        self.url = ttk.Label(self.box, style="SoftMuted.TLabel")
        self.url.pack(anchor="w", pady=(2, 12))
        self.bar = ttk.Progressbar(self.box, style="Big.Horizontal.TProgressbar", length=self.WIDTH, maximum=1000)
        self.bar.pack(anchor="w")
        grid = ttk.Frame(self.box, style="Soft.TFrame")
        grid.pack(anchor="w", pady=(12, 8), fill="x")
        self.rows = []
        for i in range(5):
            k = ttk.Label(grid, style="SoftMuted.TLabel")
            v = ttk.Label(grid, style="Soft.TLabel", wraplength=self.WIDTH - 110, justify="left")
            k.grid(row=i, column=0, sticky="nw", pady=3, padx=(0, 18))
            v.grid(row=i, column=1, sticky="nw", pady=3)
            self.rows.append((k, v))
        self.map_box = ttk.Frame(self.box, style="Soft.TFrame")
        ttk.Label(self.map_box, text="MAPA DAS PARTES", style="SoftSmall.TLabel").pack(anchor="w", pady=(6, 4))
        self.canvas = tk.Canvas(self.map_box, width=self.WIDTH, height=self.ROWS * 22 - 4, highlightthickness=0)
        self.canvas.pack(anchor="w")
        step = self.WIDTH / self.CELLS
        self.cells = [self.canvas.create_rectangle(int(c * step), r * 22, int((c + 1) * step) - 2, r * 22 + 18,
                                                   width=0)
                      for r in range(self.ROWS) for c in range(self.CELLS)]
        self.legend = ttk.Label(self.map_box, text="■ baixado   ▪ a meio   □ falta", style="SoftMuted.TLabel")
        self.legend.pack(anchor="w", pady=(4, 0))
        self.buttons = ttk.Frame(self.box, style="Soft.TFrame")
        self.buttons.pack(anchor="w", pady=(16, 0))
        self.primary = ttk.Button(self.buttons, style="Accent.TButton")
        self.second = ttk.Button(self.buttons, style="Soft.TButton")
        self.third = ttk.Button(self.buttons, style="Soft.TButton")
        for b in (self.primary, self.second, self.third):
            b.pack(side="left", padx=(0, 8))
        self.pack_propagate(False)
        self.recolor()
        self.show(None)

    def recolor(self) -> None:
        self.canvas.configure(background=self.app.colors["sidebar"])
        self._map_key = None

    def show(self, task) -> None:
        self.task = task
        self._map_key = None
        if task is None:
            self.box.pack_forget()
            self.empty.pack(anchor="w")
        else:
            self.empty.pack_forget()
            self.box.pack(fill="both", expand=True)
            self.refresh()

    def _set_rows(self, rows) -> None:
        for i, (k, v) in enumerate(self.rows):
            if i < len(rows):
                k.configure(text=rows[i][0])
                v.configure(text=rows[i][1])
                k.grid()
                v.grid()
            else:
                k.grid_remove()
                v.grid_remove()

    def _buttons(self, spec) -> None:
        for b, (text, cmd) in zip((self.primary, self.second, self.third), spec):
            b.configure(text=text, command=cmd)
            b.pack(side="left", padx=(0, 8))
        for b in (self.primary, self.second, self.third)[len(spec):]:
            b.pack_forget()

    def refresh(self) -> None:
        t, app = self.task, self.app
        if t is None or not self.app.settings.show_details:
            return
        if t not in app.manager.tasks:
            self.show(None)
            return
        self.name.configure(text=t.filename or name_from_url(t.url))
        url = t.url if len(t.url) <= 46 else t.url[:44] + "…"
        self.url.configure(text=url)
        self.bar["value"] = t.progress * 1000
        folder = (os.path.dirname(t.path) if t.path else "") or t.dest_dir
        size = format_size(t.size) if t.size else "desconhecido"
        got = f"{format_size(t.downloaded)} de {size}" + (f" ({t.progress * 100:.0f}%)" if t.size else "")
        st = t.status
        if t.is_running() and st == Status.DOWNLOADING:
            conns = f"{t.active_connections} ativas"
            if getattr(t, "server_limit", None):
                conns += f" · servidor aceita {t.server_limit}"
            elif getattr(t, "connection_note", ""):
                conns += f" · {t.connection_note}"
            limit = f" (limite {format_size(t.speed_limit)}/s)" if t.speed_limit else ""
            peak = f" (máx {format_size(t.peak_speed)}/s)" if getattr(t, "peak_speed", 0) and not limit else ""
            rows = [("Baixado", got), ("Velocidade", f"{format_size(t.speed)}/s{limit}{peak}"),
                    ("Falta", format_time(t.eta)), ("Ligações", conns), ("Guardar em", folder)]
            buttons = [("॥ Pausar", app._pause), ("Abrir pasta", app._open_folder), ("✕", app._remove)]
        elif st == Status.COMPLETED:
            took = getattr(t, "active_seconds", 0.0)
            rows = [("Tamanho", size)]
            if took >= 0.5:
                first = getattr(t, "first_byte_after", None)
                rows.append(("Demorou", format_time(took) + (f" (1.º byte aos {first:.1f} s)" if first else "")))
                speeds = f"média {format_size((t.size or t.downloaded) / took)}/s"
                if getattr(t, "peak_speed", 0):
                    speeds += f" · máx {format_size(t.peak_speed)}/s"
                rows.append(("Velocidade", speeds))
            else:
                rows.append(("Adicionado", time.strftime("%d/%m/%Y %H:%M", time.localtime(t.added_at))))
            if getattr(t, "check", ""):
                rows.append(("Verificação", t.check))
            rows.append(("Guardado em", folder))
            buttons = [("Abrir", app._open_file), ("Abrir pasta", app._open_folder), ("✕", app._remove)]
        elif st == Status.ERROR:
            rows = [("Baixado", got), ("Problema", t.error or "erro desconhecido"),
                    ("O que fazer", error_hint(t.error or "")), ("Guardar em", folder)]
            buttons = [("↻ Tentar de novo", app._resume), ("Atualizar link", app._update_link), ("✕", app._remove)]
        elif st == Status.PAUSED:
            again = ("o servidor permite continuar ✓" if t.resumable or not t.downloaded
                     else "o servidor não permite continuar: recomeça do início")
            rows = [("Baixado", got), ("Estado", f"pausado · {t.error}" if t.error else "pausado"),
                    ("Retomar", again), ("Guardar em", folder)]
            buttons = [("▶ Retomar", app._resume), ("Abrir pasta", app._open_folder), ("✕", app._remove)]
        else:  # queued / starting / video steps
            rows = [("Estado", st), ("Baixado", got), ("Guardar em", folder)]
            buttons = [("॥ Pausar", app._pause) if t.is_running() else ("▶ Iniciar", app._resume),
                       ("Abrir pasta", app._open_folder), ("✕", app._remove)]
        self._set_rows(rows)
        self._buttons(buttons)
        show_map = (app.settings.show_parts_map and st != Status.COMPLETED and bool(t.size)
                    and getattr(t, "kind", "") != "video")
        if show_map:
            self.map_box.pack(anchor="w", fill="x", before=self.buttons)
            self._draw_map(t)
        else:
            self.map_box.pack_forget()

    def _draw_map(self, t) -> None:
        """Each cell is a slice of the file: filled when downloaded, grey while it is being
        downloaded, empty when it is still missing."""
        n = len(self.cells)
        size = t.size or 0
        if size <= 0:
            return
        bounds = [i * size // n for i in range(n + 1)]  # cell i = bytes [bounds[i], bounds[i+1])
        done = [0] * n
        segments = list(t.segments)
        ranges = ([(seg.start, min(seg.pos, seg.end + 1)) for seg in segments] if segments
                  else [(0, min(t.downloaded, size))])  # one stream from the start
        for a, b in ranges:
            if b <= a:
                continue
            i = min(n - 1, a * n // size)
            while i < n and bounds[i] < b:
                done[i] += max(0, min(b, bounds[i + 1]) - max(a, bounds[i]))
                i += 1
        c = self.app.colors
        fills = [c["accent"] if d >= bounds[i + 1] - bounds[i] else c["partial"] if d > 0 else c["trough"]
                 for i, d in enumerate(done)]
        if fills == self._map_key:
            return
        self._map_key = fills
        for item, fill in zip(self.cells, fills):
            self.canvas.itemconfigure(item, fill=fill)


class SettingsDialog(tk.Toplevel):
    """⚙ → Configurações: every option in one window, by section."""

    SECTIONS = ("Aparência", "Downloads", "Velocidade", "Navegador", "Sistema", "Atualizações")

    def __init__(self, app: "App", section: str = "Aparência"):
        super().__init__(app)
        self.app = app
        self.title("Configurações")
        self.geometry("900x560")
        self.minsize(760, 480)
        app.attach_dialog(self)
        self.configure(background=app.colors["bg"])
        self.bind("<Escape>", lambda _e: self.destroy())
        side = ttk.Frame(self, style="Side.TFrame", padding=(8, 12, 0, 12))
        side.pack(side="left", fill="y")
        self.nav = ttk.Treeview(side, show="tree", selectmode="browse", style="Side.Treeview")
        self.nav.column("#0", width=190)
        for name in self.SECTIONS:
            self.nav.insert("", "end", iid=name, text=name)
        self.nav.pack(fill="both", expand=True)
        self.nav.bind("<<TreeviewSelect>>", lambda _e: self._fill())
        self.line = tk.Frame(self, width=1)
        self.line.pack(side="left", fill="y")
        self.page = ttk.Frame(self, padding=(28, 18))
        self.page.pack(side="left", fill="both", expand=True)
        self.cards: dict[str, tuple] = {}
        self.recolor()
        self.show(section)

    def recolor(self) -> None:
        c = self.app.colors
        self.configure(background=c["bg"])
        self.line.configure(background=c["border"])
        for value, (box, canvas, label) in self.cards.items():
            chosen = self.app.theme_var.get() == value
            box.configure(background=c["sidebar"], highlightbackground=c["fg"] if chosen else c["border"],
                          highlightthickness=2 if chosen else 1)
            canvas.configure(background=c["sidebar"])
            label.configure(background=c["sidebar"], foreground=c["fg"])
            for w in box.winfo_children():
                if isinstance(w, tk.Label) and w is not label:
                    w.configure(background=c["sidebar"], foreground=c["muted"])

    def show(self, section: str) -> None:
        section = section if section in self.SECTIONS else self.SECTIONS[0]
        self.nav.selection_set(section)
        self._fill()

    # helpers
    def _head(self, text: str, first: bool = False) -> None:
        ttk.Label(self.page, text=text.upper(), style="Muted.TLabel", font=self.app._fonts[2]).pack(
            anchor="w", pady=(0 if first else 16, 6))

    def _check(self, text: str, var: tk.BooleanVar, command=None) -> None:
        ttk.Checkbutton(self.page, text=text, variable=var, command=command).pack(anchor="w", pady=3)

    def _setting_check(self, text: str, key: str, after=None) -> tk.BooleanVar:
        var = tk.BooleanVar(value=bool(getattr(self.app.settings, key, False)))

        def changed():
            setattr(self.app.settings, key, var.get())
            self.app.settings.save()
            if after:
                after()
        self._check(text, var, changed)
        return var

    def _set(self, key: str, value) -> None:
        """Change a setting and save it at once (a crash or log-off must not lose it)."""
        setattr(self.app.settings, key, value)
        self.app.settings.save()

    def _row(self, text: str, button: str, command) -> ttk.Label:
        row = ttk.Frame(self.page)
        row.pack(anchor="w", fill="x", pady=3)
        label = ttk.Label(row, text=text)
        label.pack(side="left")
        ttk.Button(row, text=button, command=lambda: (command(), self.after(300, self._fill))).pack(
            side="right")
        return label

    def _fill(self) -> None:
        sel = self.nav.selection()
        section = sel[0] if sel else self.SECTIONS[0]
        for w in self.page.winfo_children():
            w.destroy()
        self.cards = {}
        app, st = self.app, self.app.settings
        if section == "Aparência":
            self._head("Tema", first=True)
            row = ttk.Frame(self.page)
            row.pack(anchor="w")
            for value, name, sub in (("auto", "Automático", "segue o Windows"), ("claro", "Claro", "fundo branco"),
                                     ("escuro", "Escuro", "fundo preto")):
                self._card(row, value, name, sub)
            self._head("Filtros")
            self.filter_var = tk.StringVar(value=st.filter_style)
            for value, text in (("side", "Barra lateral à esquerda"), ("pills", "Botões por cima da lista")):
                ttk.Radiobutton(self.page, text=text, value=value, variable=self.filter_var,
                                command=self._filters).pack(anchor="w", pady=3)
            self._head("Outros")
            self._setting_check("Mostrar os detalhes do download à direita", "show_details", app._layout)
            self._setting_check("Mostrar o mapa das partes", "show_parts_map", lambda: app.details.refresh())
        elif section == "Downloads":
            self._head("Onde guardar", first=True)
            self._row(f"Pasta padrão: {st.dest_dir}", "Alterar...", app._choose_dir)
            self._setting_check("Guardar cada categoria na sua pasta (Downloads\\Vídeos...)", "category_folders")
            self._row("Categorias, regras e pastas", "Editar...", lambda: CategoriesDialog(app))
            self._head("Fila")
            self._row(f"Downloads ao mesmo tempo: {app.manager.max_active}", "Alterar...", app._max_active)
            self._check("Retomar downloads ao abrir o Flow Downloads", app.resume_var,
                        lambda: self._set("auto_resume", app.resume_var.get()))
            self._head("Quando terminar")
            self._row("Abrir, extrair, analisar com o antivírus, correr um comando", "Escolher...",
                      lambda: AfterDialog(app))
        elif section == "Velocidade":
            limit = f"{st.global_limit_kb} KB/s" if st.global_limit_kb else "sem limite"
            self._head("Limites", first=True)
            self._row(f"Velocidade máxima total: {limit}", "Alterar...", app._global_limit)
            self._row("Máximo de ligações por site", "Editar...", lambda: HostLimitsDialog(app))
            self._head("Horário")
            self._row("Agendador (começar, parar, desligar o PC)", "Abrir...", lambda: SchedulerDialog(app))
        elif section == "Navegador":
            self._head("Extensão", first=True)
            self._check("Receber downloads do navegador", app.bridge_var, app._toggle_bridge)
            self._check("Perguntar antes de baixar (em vez de começar logo)", app.confirm_var,
                        lambda: self._set("confirm_browser", app.confirm_var.get()))
            self._setting_check("Mostrar a janela de progresso", "show_progress_window")
            self._row("Tipos de ficheiro apanhados e sites ignorados", "Editar...", lambda: CaptureDialog(app))
            self._row("Como instalar a extensão no Brave / Chrome / Edge", "Ver...", app._extension_help)
            self._head("Área de transferência")
            self._check("Detetar links copiados", app.clip_var,
                        lambda: self._set("watch_clipboard", app.clip_var.get()))
        elif section == "Sistema":
            self._head("Segundo plano", first=True)
            self._check("Continuar em segundo plano ao fechar a janela", app.bg_var,
                        lambda: self._set("run_in_background", app.bg_var.get()))
            ttk.Checkbutton(self.page, text="Iniciar com o computador (em segundo plano)",
                            variable=app.autostart_var, command=app._toggle_autostart,
                            state="normal" if autostart.supported() else "disabled").pack(anchor="w", pady=3)
            self._setting_check("Ícone ao lado do relógio do Windows", "show_tray", self._tray)
            self._setting_check("Avisar quando um download termina", "tray_notify")
            self._head("Windows")
            self._setting_check("Não deixar o PC adormecer durante downloads", "keep_awake", self._awake)
            self._setting_check("Marcar ficheiros como vindos da internet (aviso do Windows ao abrir programas)",
                                "mark_internet")
        elif section == "Atualizações":
            self._head("Flow Downloads", first=True)
            self._setting_check("Procurar novas versões uma vez por dia", "check_updates")
            self._row("Versão instalada: " + _version(), "Procurar agora...", app._check_updates)
            self._head("Vídeos")
            self._row("Suporte a vídeos (yt-dlp)", "Atualizar...", lambda: VideoDialog.install(app))

    def _card(self, parent, value: str, name: str, sub: str) -> None:
        c = self.app.colors
        box = tk.Frame(parent, padx=14, pady=12, cursor="hand2")
        box.pack(side="left", padx=(0, 12))
        canvas = tk.Canvas(box, width=150, height=84, highlightthickness=0, cursor="hand2")
        canvas.pack()
        halves = {"claro": [THEMES["claro"]], "escuro": [THEMES["escuro"]],
                  "auto": [THEMES["claro"], THEMES["escuro"]]}[value]
        w = 150 // len(halves)
        for i, look in enumerate(halves):
            x0, x1 = i * w, (i + 1) * w
            canvas.create_rectangle(x0, 0, x1, 84, fill=look["bg"], outline=look["border"])
            canvas.create_rectangle(x0 + 8, 8, x1 - 8, 16, fill=look["border"], width=0)
            for k in range(4):
                canvas.create_rectangle(x0 + 8, 26 + k * 13, x0 + 8 + (x1 - x0 - 16) * (0.9 - k * 0.15),
                                        31 + k * 13, fill=look["fg"], width=0)
        label = tk.Label(box, text=name, font=self.app._bold_font, anchor="w")
        label.pack(anchor="w", pady=(10, 0))
        tk.Label(box, text=sub, anchor="w").pack(anchor="w")
        self.cards[value] = (box, canvas, label)
        for widget in (box, canvas, label, *box.winfo_children()):
            widget.bind("<Button-1>", lambda _e, v=value: self._theme(v))
        self.recolor()

    def _theme(self, value: str) -> None:
        self.app.theme_var.set(value)
        self.app._choose_theme()  # recolours this window too (apply_theme -> recolor)

    def _filters(self) -> None:
        self.app.settings.filter_style = self.filter_var.get()
        self.app.settings.save()
        if self.app.settings.filter_style != "pills":
            self.app._filter = (self.app.side.selection() or ("all",))[0]
        self.app._layout()

    def _tray(self) -> None:
        svc = self.app.service
        if self.app.settings.show_tray:
            if hasattr(svc, "start_tray"):
                svc.start_tray()
        elif hasattr(svc, "stop_tray"):
            svc.stop_tray()

    def _awake(self) -> None:
        self.app.manager.keep_awake = self.app.settings.keep_awake


def _version() -> str:
    from . import __version__
    return __version__


class App(tk.Tk):
    SIDEBAR = [("all", "Todos os downloads"), ("unfinished", "Incompletos"), ("finished", "Concluídos")]
    SHORT = [("all", "Todos"), ("unfinished", "Incompletos"), ("finished", "Concluídos")]

    def __init__(self, service, mode: str = "main") -> None:
        """``mode``: "main" shows the window; "dialog" only shows the browser's dialog;
        "countdown" only shows the shutdown countdown."""
        super().__init__()
        # Window objects may only be freed in this (the Tk) thread. Python's automatic memory
        # clean-up runs in whatever thread happens to allocate at that moment - e.g. a download
        # saving its list - and freeing a Tk variable there deadlocks both threads (seen: the
        # app froze after a window was closed). So while windows exist, the clean-up runs only
        # here, every few seconds; it is switched back on when the windows are gone.
        gc.disable()
        self.after(3000, self._collect_garbage)
        self.withdraw()
        self.title("Flow Downloads")
        self.geometry("1240x640")
        self.minsize(820, 400)

        self.service = service
        self.settings = service.settings
        self.manager = service.manager
        self.scheduler = service.scheduler
        self.colors = THEMES[theme_name(self.settings)]
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

        if list(self.settings.columns) == OLD_DEFAULT_COLUMNS:
            self.settings.columns = list(DEFAULT_COLUMNS)  # the details panel shows the rest
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
        if getattr(self.service, "extension_outdated", False):
            self.status_var.set("⚠ A extensão do navegador está desatualizada: abra brave://extensions "
                                "e clique em ↻ (recarregar) no Flow Downloads.")
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
                "Para fechar de vez: botão ⚙ → Sair (ou Ctrl+Q).\n"
                "Para voltar a abrir: abra o Flow Downloads de novo ou use o botão da extensão.", parent=self)
        self._save_ui_settings()
        self.close_gui()

    def quit_all(self) -> None:
        self._save_ui_settings()
        self.service.exit_requested = True
        self.close_gui()

    def close_gui(self) -> None:
        """Destroy the window cleanly: cancel pending timers first, so nothing fires afterwards."""
        try:
            # Cancel in Tcl directly: after_cancel() also deletes the Python callback, which
            # destroy() then tries to delete again ("can't delete Tcl command").
            for after_id in self.tk.splitlist(self.tk.call("after", "info")):
                self.tk.call("after", "cancel", after_id)
        except tk.TclError:
            pass
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
        """The ⚙ button's menu (no menu bar: the window stays clean). Options are in
        Configurações (SettingsDialog); the variables live here, shared by both."""
        self.menus = []
        self.bridge_var = tk.BooleanVar(value=self.settings.browser_integration)
        self.confirm_var = tk.BooleanVar(value=self.settings.confirm_browser)
        self.clip_var = tk.BooleanVar(value=self.settings.watch_clipboard)
        self.resume_var = tk.BooleanVar(value=self.settings.auto_resume)
        self.bg_var = tk.BooleanVar(value=self.settings.run_in_background)
        self.autostart_var = tk.BooleanVar(value=autostart.supported() and autostart.is_enabled())
        self.theme_var = tk.StringVar(value=self._theme_choice())

        gear = self.gear_menu = tk.Menu(self, tearoff=False)
        gear.add_command(label="Configurações...", accelerator="Ctrl+,", command=self.open_settings)
        looks = tk.Menu(gear, tearoff=False)
        for value, label in (("auto", "Automático (como o Windows)"), ("claro", "Claro"), ("escuro", "Escuro")):
            looks.add_radiobutton(label=label, value=value, variable=self.theme_var, command=self._choose_theme)
        gear.add_cascade(label="Tema", menu=looks)
        gear.add_separator()
        gear.add_command(label="Adicionar URL...", accelerator="Ctrl+N", command=self.open_add_dialog)
        gear.add_command(label="Baixar vídeo (YouTube, Instagram...)...", command=self.open_video_dialog)
        gear.add_command(label="Capturar arquivos de um site...", command=lambda: GrabberDialog(self))
        gear.add_separator()
        dl = tk.Menu(gear, tearoff=False)
        dl.add_command(label="Retomar tudo", command=self.manager.resume_all)
        dl.add_command(label="Pausar tudo", command=self._pause_all)
        dl.add_separator()
        dl.add_command(label="Remover concluídos da lista", command=self._clear_finished)
        gear.add_cascade(label="Downloads", menu=dl)
        cols = tk.Menu(gear, tearoff=False)
        self.col_vars = {}
        for key, (title, _w) in COLUMNS.items():
            var = tk.BooleanVar(value=key in self.settings.columns)
            self.col_vars[key] = var
            cols.add_checkbutton(label=title, variable=var, command=self._apply_columns,
                                 state="disabled" if key == "name" else "normal")
        gear.add_cascade(label="Colunas", menu=cols)
        gear.add_command(label="Limitador de velocidade...", command=self._global_limit)
        gear.add_command(label="Agendador...", command=lambda: SchedulerDialog(self))
        gear.add_command(label="Limites por site...", command=lambda: HostLimitsDialog(self))
        gear.add_separator()
        gear.add_command(label="Procurar atualizações...", command=self._check_updates)
        gear.add_command(label="Como instalar a extensão do navegador", command=self._extension_help)
        gear.add_command(label="Sobre", command=lambda: messagebox.showinfo(
            "Sobre", "Flow Downloads — gerenciador de downloads gratuito\n"
                     "Downloads com várias conexões, retomada, agendador e integração com o navegador.",
            parent=self))
        gear.add_separator()
        gear.add_command(label="Fechar janela (continua em segundo plano)", accelerator="Ctrl+W",
                         command=self.close_window)
        gear.add_command(label="Sair", accelerator="Ctrl+Q", command=self.quit_all)
        self.menus += [gear, looks, dl, cols]
        self.bind_all("<Control-n>", lambda _e: self.open_add_dialog())
        self.bind("<Control-comma>", lambda _e: self.open_settings())
        self.bind("<Control-w>", lambda _e: self.close_window())
        self.bind("<Control-q>", lambda _e: self.quit_all())

    def _theme_choice(self) -> str:
        name = getattr(self.settings, "theme", None) or ("escuro" if self.settings.dark else "claro")
        return name if name == "auto" else theme_name(self.settings)

    def _show_gear(self) -> None:
        b = self.gear_button
        self.gear_menu.tk_popup(b.winfo_rootx() + b.winfo_width() - 260, b.winfo_rooty() + b.winfo_height() + 4)

    def open_settings(self, section: str = "Aparência") -> "SettingsDialog":
        for w in self.winfo_children():
            if isinstance(w, SettingsDialog):
                w.show(section)
                w.lift()
                return w
        return SettingsDialog(self, section)

    def _toggle_dark(self) -> None:
        """◐: light <-> dark in one click."""
        self.theme_var.set("claro" if theme_name(self.settings) in DARK_THEMES else "escuro")
        self._choose_theme()

    # --------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=(18, 12, 18, 12))
        top.pack(fill="x")
        ttk.Label(top, text="Flow Downloads", style="Title.TLabel").pack(side="left", padx=(0, 18))
        self.url_var = tk.StringVar()
        entry = self.url_entry = ttk.Entry(top, textvariable=self.url_var)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _e: self._quick_add())
        entry.focus_set()
        self.placeholder = tk.Label(entry, text="Cole um link para baixar…", anchor="w", cursor="xterm")
        self.placeholder.bind("<Button-1>", lambda _e: entry.focus_set())
        self.url_var.trace_add("write", lambda *_a: self._placeholder())
        self._placeholder()
        ttk.Button(top, text="Baixar", command=self._quick_add, style="Accent.TButton").pack(side="left", padx=(8, 8))
        ttk.Button(top, text="+", width=3, command=self.open_add_dialog, style="Tool.TButton").pack(side="left")
        ttk.Button(top, text="◐", width=3, command=self._toggle_dark, style="Tool.TButton").pack(side="left")
        self.gear_button = ttk.Button(top, text="⚙", width=3, command=self._show_gear)
        self.gear_button.pack(side="left", padx=(4, 0))
        self.lines = [tk.Frame(self, height=1)]
        self.lines[-1].pack(fill="x")

        self.status_var = tk.StringVar(value="Pronto")
        ttk.Label(self, textvariable=self.status_var, anchor="w", style="Status.TLabel").pack(fill="x", side="bottom")
        self.lines.append(tk.Frame(self, height=1))
        self.lines[-1].pack(fill="x", side="bottom")

        body = self.body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        # Filters: a bar on the left, or "pills" above the list (Configurações → Aparência).
        self.side_frame = ttk.Frame(body, style="Side.TFrame", padding=(8, 10, 0, 10))
        self.side = ttk.Treeview(self.side_frame, show="tree", selectmode="browse", style="Side.Treeview")
        self.side.pack(fill="both", expand=True)
        self.side.column("#0", width=200)
        for key, label in self.SIDEBAR:
            self.side.insert("", "end", iid=key, text=label, open=True)
        self._build_category_sidebar()
        self.side.selection_set("all")
        self.side.bind("<<TreeviewSelect>>", self._on_filter)
        self.side_line = tk.Frame(body, width=1)
        self.lines.append(self.side_line)

        self.details = DetailsPanel(self, body)
        self.details_line = tk.Frame(body, width=1)
        self.lines.append(self.details_line)

        self.center = ttk.Frame(body, padding=(14, 6, 10, 0))
        self.pills = ttk.Frame(self.center, padding=(0, 4, 0, 8))
        self.pill_buttons: dict[str, ttk.Button] = {}
        table = self.table_frame = ttk.Frame(self.center)
        table.pack(side="bottom", fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=list(COLUMNS), show="headings", selectmode="extended")
        for key, (title, width) in COLUMNS.items():
            self.tree.heading(key, text=title.upper(), anchor="w")
            self.tree.column(key, width=width, minwidth=120 if key == "name" else 50, anchor="w",
                             stretch=key == "name")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.details.show(self._shown_task()))
        self._layout()

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
        self.ctx.add_command(label="Verificar checksum...", command=self._verify_checksum)
        self.ctx.add_command(label="Atualizar link...", command=self._update_link)
        self.ctx.add_separator()
        self.ctx.add_command(label="Excluir", command=self._remove)
        self.menus.append(self.ctx)

    def _placeholder(self) -> None:
        if self.url_var.get():
            self.placeholder.place_forget()
        else:
            self.placeholder.place(x=9, rely=0.5, anchor="w")

    def _layout(self) -> None:
        """Place the filters (bar / pills) and the details panel as the settings say."""
        for w in (self.side_frame, self.side_line, self.details, self.details_line, self.center, self.pills):
            w.pack_forget()
        if self.settings.filter_style != "pills":
            self.side_frame.pack(side="left", fill="y")
            self.side_line.pack(side="left", fill="y")
        if self.settings.show_details:
            self.details.pack(side="right", fill="y")
            self.details_line.pack(side="right", fill="y")
        self.center.pack(side="left", fill="both", expand=True)
        if self.settings.filter_style == "pills":
            self._build_pills()
            self.pills.pack(side="top", fill="x")
        self.details.show(self._shown_task())

    def _build_pills(self) -> None:
        for b in self.pill_buttons.values():
            b.destroy()
        self.pill_buttons = {}
        keys = [k for k, _l in self.SIDEBAR] + self._pill_categories()
        self._pill_keys = keys
        for key in keys:
            b = ttk.Button(self.pills, style="Pill.TButton", width=0, command=lambda k=key: self._pick_filter(k))
            b.pack(side="left", padx=(0, 6))
            self.pill_buttons[key] = b
        self._update_pills()

    def _pill_categories(self) -> list:
        """Categories that have downloads (plus the chosen one): the pills stay on one line."""
        used = {self._category(t) for t in self.manager.tasks}
        names = [r["name"] for r in self.settings.category_rules()] + [OTHER]
        return [f"cat:{n}" for n in names if n in used or self._filter == f"cat:{n}"]

    def _pick_filter(self, key: str) -> None:
        if self.side.exists(key):
            self.side.selection_set(key)  # same path as the bar: _on_filter
        self._filter = key
        self._update_pills()
        self._refresh(schedule=False)

    def _update_pills(self, counts: dict | None = None) -> None:
        labels = dict(self.SHORT)
        for key, b in self.pill_buttons.items():
            text = labels.get(key) or key[4:]
            if counts and key in counts:
                text += f"  {counts[key]}"
            b.configure(text=text, style="PillOn.TButton" if key == self._filter else "Pill.TButton")

    def _shown_task(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return next((t for t in self.manager.tasks if t.id == sel[0]), None)

    def _update_link(self) -> None:
        """A link that expired: give the download a fresh one (it continues where it stopped
        when the file is the same; the engine starts over if the server sends another version)."""
        tasks = [t for t in self._selected() if getattr(t, "kind", "") != "video"]
        if not tasks:
            return
        t = tasks[0]
        url = simpledialog.askstring(
            "Atualizar link", "Volte à página do download, copie o link de novo e cole-o aqui:",
            initialvalue="", parent=self)
        url = (url or "").strip()
        if not url:
            return
        if not url.lower().startswith(("http://", "https://", "ftp://")):
            messagebox.showwarning("URL inválida", "Informe um link que comece com http://, https:// ou ftp://",
                                   parent=self)
            return
        t.pause()
        t.wait(5)  # stopped (pausing is immediate) before the address changes under it
        t.url, t._final_url = url, None
        t.error = None
        if t.status not in (Status.PAUSED, Status.ERROR):
            t.status = Status.PAUSED
        self.manager.save()
        self.manager.resume(t)
        self.status_var.set(f"Link atualizado: {t.filename or url}")

    def apply_theme(self) -> None:
        c = self.colors = THEMES[theme_name(self.settings)]
        style = ttk.Style(self)
        style.theme_use("clam")
        family = "Segoe UI" if sys.platform.startswith("win") else None
        import tkinter.font as tkfont
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                f = tkfont.nametofont(name)
                f.configure(size=10, **({"family": family} if family else {}))
            except tk.TclError:
                pass
        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        self._bold_font = bold
        style.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"],
                        bordercolor=c["border"], lightcolor=c["bg"], darkcolor=c["bg"],
                        troughcolor=c["field"], selectbackground=c["sel"], selectforeground=c["selfg"],
                        insertcolor=c["fg"], arrowcolor=c["muted"], focuscolor=c["accent"])
        style.map(".", background=[("active", c["hover"]), ("disabled", c["bg"])],
                  foreground=[("disabled", c["muted"])])
        # Buttons: flat, rounded feel through padding; the main one in the accent colour.
        style.configure("TButton", padding=(12, 5), background=c["head"], bordercolor=c["border"],
                        lightcolor=c["head"], darkcolor=c["head"], relief="flat")
        style.map("TButton", background=[("pressed", c["sel"]), ("active", c["hover"])],
                  bordercolor=[("focus", c["accent"])])
        style.configure("Accent.TButton", padding=(14, 5), background=c["accent"], foreground=c["accentfg"],
                        bordercolor=c["accent"], lightcolor=c["accent"], darkcolor=c["accent"], font=bold)
        style.map("Accent.TButton", background=[("pressed", c["accent"]), ("active", c["accent"])],
                  foreground=[("active", c["accentfg"])])
        style.configure("Tool.TButton", padding=(8, 5), background=c["bg"], borderwidth=0,
                        lightcolor=c["bg"], darkcolor=c["bg"], bordercolor=c["bg"])
        style.map("Tool.TButton", background=[("active", c["hover"]), ("pressed", c["sel"])])
        style.configure("Toolbar.TFrame", background=c["bg"])
        style.configure("TEntry", fieldbackground=c["field"], foreground=c["fg"], padding=(8, 5),
                        bordercolor=c["border"], lightcolor=c["field"], darkcolor=c["field"])
        style.map("TEntry", bordercolor=[("focus", c["accent"])], lightcolor=[("focus", c["accent"])])
        style.configure("TSpinbox", fieldbackground=c["field"], foreground=c["fg"], padding=(6, 3),
                        bordercolor=c["border"], lightcolor=c["field"], darkcolor=c["field"])
        style.configure("TCombobox", fieldbackground=c["field"], foreground=c["fg"], padding=(6, 3),
                        bordercolor=c["border"], lightcolor=c["field"], darkcolor=c["field"])
        style.map("TCombobox", fieldbackground=[("readonly", c["field"])], foreground=[("readonly", c["fg"])])
        style.configure("TCheckbutton", indicatorbackground=c["field"], indicatorforeground=c["accent"])
        style.configure("TRadiobutton", indicatorbackground=c["field"], indicatorforeground=c["accent"])
        style.configure("Treeview", background=c["field"], fieldbackground=c["field"], foreground=c["fg"],
                        rowheight=30, borderwidth=0, lightcolor=c["field"], darkcolor=c["field"])
        style.map("Treeview", background=[("selected", c["sel"])], foreground=[("selected", c["selfg"])])
        style.configure("Treeview.Heading", background=c["field"], foreground=c["muted"], relief="flat",
                        padding=(8, 6), borderwidth=0, lightcolor=c["field"], darkcolor=c["border"],
                        bordercolor=c["border"], font=bold)
        style.map("Treeview.Heading", background=[("active", c["hover"])])
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])  # no frame around the list
        # Sidebar: its own tone, accent on the chosen filter.
        style.configure("Side.Treeview", background=c["sidebar"], fieldbackground=c["sidebar"],
                        foreground=c["fg"], rowheight=32, borderwidth=0)
        style.map("Side.Treeview", background=[("selected", c["sel"])], foreground=[("selected", c["selfg"])])
        style.configure("Side.TFrame", background=c["sidebar"])
        style.configure("TPanedwindow", background=c["border"])
        style.configure("Status.TLabel", background=c["bg"], foreground=c["muted"], padding=(18, 6))
        style.configure("TScrollbar", background=c["head"], troughcolor=c["field"], bordercolor=c["field"],
                        lightcolor=c["head"], darkcolor=c["head"], arrowcolor=c["muted"], gripcount=0)
        style.configure("TProgressbar", background=c["accent"], troughcolor=c["trough"], bordercolor=c["trough"],
                        lightcolor=c["accent"], darkcolor=c["accent"])
        style.configure("Big.Horizontal.TProgressbar", background=c["accent"], troughcolor=c["trough"],
                        bordercolor=c["trough"], lightcolor=c["accent"], darkcolor=c["accent"], thickness=8)
        title = tkfont.nametofont("TkDefaultFont").copy()
        title.configure(size=15, weight="bold")
        name = tkfont.nametofont("TkDefaultFont").copy()
        name.configure(size=12, weight="bold")
        small = tkfont.nametofont("TkDefaultFont").copy()
        small.configure(size=8, weight="bold")
        self._fonts = (title, name, small)
        style.configure("Title.TLabel", font=title)
        style.configure("Treeview.Heading", font=small)  # small capitals: the titles fit
        style.configure("Muted.TLabel", foreground=c["muted"])
        style.configure("Pill.TButton", padding=(9, 3), background=c["bg"], foreground=c["muted"],
                        bordercolor=c["border"], lightcolor=c["bg"], darkcolor=c["bg"])
        style.map("Pill.TButton", background=[("active", c["hover"])], foreground=[("active", c["fg"])])
        style.configure("PillOn.TButton", padding=(9, 3), background=c["accent"], foreground=c["accentfg"],
                        bordercolor=c["accent"], lightcolor=c["accent"], darkcolor=c["accent"])
        style.map("PillOn.TButton", background=[("active", c["accent"])], foreground=[("active", c["accentfg"])])
        # Details panel: its own tone (like the filter bar)
        style.configure("Soft.TFrame", background=c["sidebar"])
        style.configure("Soft.TLabel", background=c["sidebar"], foreground=c["fg"])
        style.configure("SoftMuted.TLabel", background=c["sidebar"], foreground=c["muted"])
        style.configure("SoftSmall.TLabel", background=c["sidebar"], foreground=c["muted"], font=small)
        style.configure("SoftName.TLabel", background=c["sidebar"], foreground=c["fg"], font=name)
        style.configure("Soft.TButton", padding=(12, 5), background=c["sidebar"], bordercolor=c["border"],
                        lightcolor=c["sidebar"], darkcolor=c["sidebar"])
        style.map("Soft.TButton", background=[("active", c["hover"]), ("pressed", c["sel"])])
        style.configure("Card.TFrame", background=c["sidebar"])
        style.configure("TLabelframe", background=c["bg"], bordercolor=c["border"])
        style.configure("TLabelframe.Label", background=c["bg"], foreground=c["muted"])
        style.configure("TNotebook", background=c["bg"], bordercolor=c["border"])
        style.configure("TNotebook.Tab", background=c["head"], padding=(12, 5))
        style.map("TNotebook.Tab", background=[("selected", c["field"])])
        self.configure(background=c["bg"])
        self.option_add("*TCombobox*Listbox.background", c["field"])
        self.option_add("*TCombobox*Listbox.foreground", c["fg"])
        self.option_add("*Text.background", c["field"])
        self.option_add("*Text.foreground", c["fg"])
        self.option_add("*Text.insertBackground", c["fg"])
        self.option_add("*Text.relief", "flat")
        self.option_add("*Text.highlightThickness", 1)
        self.option_add("*Text.highlightColor", c["accent"])
        self.option_add("*Text.highlightBackground", c["border"])
        for m in self.menus:
            try:
                m.configure(background=c["field"], foreground=c["fg"], activebackground=c["sel"],
                            activeforeground=c["selfg"], selectcolor=c["accent"], relief="flat",
                            borderwidth=1, activeborderwidth=0)
            except tk.TclError:
                pass
        if getattr(self, "side", None) is not None:
            self.side.configure(style="Side.Treeview")
        if getattr(self, "placeholder", None) is not None:
            self.placeholder.configure(background=c["field"], foreground=c["muted"])
        for line in getattr(self, "lines", []):
            line.configure(background=c["border"])
        if getattr(self, "details", None) is not None:
            self.details.recolor()
        self.tree.tag_configure(Status.COMPLETED, foreground=c["done"])
        self.tree.tag_configure(Status.ERROR, foreground=c["error"], font=self._bold_font)
        self.tree.tag_configure(Status.PAUSED, foreground=c["paused"])
        self.tree.tag_configure("stripe", background=c["stripe"])
        for w in self.winfo_children():  # open dialogs follow the new look (plain Tk widgets)
            if isinstance(w, tk.Toplevel):
                try:
                    w.configure(background=c["bg"])
                    if hasattr(w, "recolor"):
                        w.recolor()
                except tk.TclError:
                    pass

    # ---------------------------------------------------------------- actions
    def add_download(self, url, folder, conns, start=True, filename=None, headers=None,
                     cookies=None, final_url=None, auto_folder=False, page="", now=False):
        self.settings.dest_dir = folder or self.settings.dest_dir
        self.settings.connections = conns
        self.settings.save()
        task = self.manager.add(url, folder, conns, start=start, filename=filename, headers=headers,
                                cookies=cookies, final_url=final_url, auto_folder=auto_folder, page=page,
                                now=now)
        self._last_clip = url
        self.status_var.set(f"Adicionado: {filename or url}")
        self._refresh(schedule=False)
        return task

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
            self.ask_if_duplicate(url, None, lambda: self.add_download(
                url, self.settings.dest_dir, self.settings.connections,
                auto_folder=self.settings.category_folders))

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
            self.settings.save()
            self.status_var.set(f"Pasta padrão: {path}")

    def _max_active(self) -> None:
        n = simpledialog.askinteger("Downloads simultâneos", "Quantos downloads ao mesmo tempo?",
                                    initialvalue=self.manager.max_active, minvalue=1, maxvalue=20, parent=self)
        if n:
            self.manager.max_active = self.settings.max_active = n
            self.settings.save()
            self.manager.tick()

    def _global_limit(self) -> None:
        kb = simpledialog.askinteger(
            "Limitador de velocidade", "Velocidade máxima total em KB/s (0 = sem limite):",
            initialvalue=self.settings.global_limit_kb, minvalue=0, parent=self)
        if kb is not None:
            self.settings.global_limit_kb = kb
            self.settings.save()
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
        if tasks and tasks[0].path and os.path.exists(tasks[0].path):
            folder = os.path.dirname(tasks[0].path)  # e.g. the browser's folder
        if os.path.isdir(folder):
            open_path(folder)

    def _check_updates(self) -> None:
        """Ajuda → Procurar atualizações: check, ask, download (SHA-256 checked), install."""
        from . import updater
        self.status_var.set("A procurar atualizações...")
        results: queue.Queue = queue.Queue()
        threading.Thread(target=lambda: results.put(updater.check()), daemon=True).start()

        def checked():
            try:
                update, message = results.get_nowait()
            except queue.Empty:
                self.after(300, checked)
                return
            self.status_var.set(message)
            if update is None:
                messagebox.showinfo("Atualizações", message, parent=self)
                return
            notes = ("\n\n" + update.notes[:600]) if update.notes else ""
            if not updater.can_install():
                if messagebox.askyesno("Atualizações", f"{message}{notes}\n\nAbrir a página da versão nova?", parent=self):
                    import webbrowser
                    webbrowser.open(update.page)
                return
            if not messagebox.askyesno("Atualizações", f"{message}{notes}\n\nAtualizar agora? O Flow Downloads "
                                       "fecha por um momento e volta a abrir sozinho.", parent=self):
                return
            self._install_update(update)
        self.after(300, checked)

    def _install_update(self, update) -> None:
        from . import updater
        from .paths import config_dir
        self.status_var.set(f"A descarregar a versão {update.version}...")
        results: queue.Queue = queue.Queue()

        def work():
            try:
                folder = str(config_dir() / "updates")
                updater.clean_old(folder)  # earlier updates' installers are not needed any more
                results.put(("ok", updater.download(update, folder)))
            except (OSError, ValueError) as exc:
                results.put(("err", str(exc)))
        threading.Thread(target=work, daemon=True).start()

        def done():
            try:
                kind, value = results.get_nowait()
            except queue.Empty:
                self.after(300, done)
                return
            if kind == "err":
                self.status_var.set("A atualização falhou.")
                messagebox.showerror("Atualizações", f"Não foi possível atualizar: {value}", parent=self)
                return
            try:
                updater.run_installer(value)
            except OSError as exc:
                self.status_var.set("A atualização falhou.")
                messagebox.showerror("Atualizações", f"Não foi possível abrir o instalador: {exc}\n\n"
                                     f"Pode corrê-lo à mão: {value}", parent=self)
                return
            self.quit_all()  # the installer replaces the files and starts the app again
        self.after(300, done)

    def _verify_checksum(self) -> None:
        tasks = [t for t in self._selected() if t.status == Status.COMPLETED and t.path and os.path.isfile(t.path)]
        if not tasks:
            messagebox.showinfo("Verificar checksum", "Escolha um download concluído.", parent=self)
            return
        task = tasks[0]
        text = simpledialog.askstring(
            "Verificar checksum", "Cole o checksum indicado no site (MD5, SHA-1 ou SHA-256).\n"
            "Deixe vazio para ver o SHA-256 deste ficheiro.", parent=self)
        if text is None:
            return
        from . import after

        def work():
            try:
                if text.strip():
                    ok, msg = after.verify(task.path, text)
                    task.check = msg
                    self.manager.save()
                    result = (ok, msg)
                else:
                    result = (True, "SHA-256:\n" + after.file_hash(task.path, "sha256"))
            except OSError as exc:
                result = (False, f"Não consegui ler o ficheiro: {exc}")
            self.after(0, lambda: (messagebox.showinfo if result[0] else messagebox.showwarning)(
                "Verificar checksum", f"{task.filename}\n\n{result[1]}", parent=self))
        threading.Thread(target=work, daemon=True).start()

    def _collect_garbage(self) -> None:
        gc.collect()
        try:
            self.after(3000, self._collect_garbage)
        except tk.TclError:
            pass

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
        self._update_pills()
        self._refresh(schedule=False)

    def _choose_theme(self) -> None:
        self.settings.theme = self.theme_var.get()
        self.settings.dark = theme_name(self.settings) in DARK_THEMES
        self.settings.save()
        self.apply_theme()
        self._refresh(schedule=False)

    def _apply_columns(self) -> None:
        cols = [k for k in COLUMNS if self.col_vars[k].get() or k == "name"]
        self.settings.columns = cols
        self.tree.configure(displaycolumns=cols)

    # ------------------------------------------------------- browser bridge
    def _toggle_bridge(self) -> None:
        self.settings.browser_integration = self.bridge_var.get()
        self.settings.save()
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
            elif kind == "tray":  # the icon next to the clock
                what = svc.tray_action(data)
                if what == "show":
                    self.show_window()
                elif what == "quit":
                    self.quit_all()
                    return
            elif kind == "quit":
                self.quit_all()
                return
        svc.check_scheduler()
        while svc.pending:
            item = svc.pending.pop(0)
            if item.get("kind") == "duplicate":
                existing = next((t for t in self.manager.tasks if t.id == item["task"]), None)
                if existing is None:
                    svc.add_from_browser(item["item"])
                else:
                    self._browser_duplicate(item["item"], existing)
                continue
            if item.get("kind") == "progress":
                task = next((t for t in self.manager.tasks if t.id == item["task"]), None)
                if task is not None:
                    ProgressWindow(self, task)
                continue
            if item.get("kind") == "video":
                VideoDialog(self, item["page"], item.get("title"), item.get("media"), item.get("cookie_jar"))
                continue
            from .service import browser_request
            headers, cookies = browser_request(item)
            AddDialog(self, item["url"], item.get("filename"), headers, title="Download do navegador",
                      cookies=cookies, final_url=item.get("final_url"),
                      rid=item.get("rid"))

    def _browser_duplicate(self, item: dict, existing) -> None:
        svc, rid = self.service, item.get("rid")

        def new():
            svc.add_from_browser(item)
            self._process_events()  # shows its progress window

        def resume():
            svc.bind_request(rid, existing)  # the browser shows the existing download
            self.manager.resume(existing)
            if self.settings.show_progress_window:
                ProgressWindow(self, existing)

        DuplicateDialog(self, existing, on_new=new, on_resume=resume,
                        on_close=lambda: svc.cancel_request(rid))

    def ask_if_duplicate(self, url: str, final_url: str | None, proceed) -> None:
        """Runs ``proceed()`` at once, or after the user chose "Baixar de novo"."""
        existing = self.manager.find_duplicate(url, final_url)
        if existing is None:
            proceed()
        else:
            DuplicateDialog(self, existing, on_new=proceed, on_resume=lambda: self.manager.resume(existing))

    def _extension_help(self) -> None:
        folder = str(install_dir() / "browser-extension")
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

    def _build_category_sidebar(self) -> None:
        for iid in self.side.get_children("all"):
            self.side.delete(iid)
        for rule in self.settings.category_rules():
            self.side.insert("all", "end", iid=f"cat:{rule['name']}", text=rule["name"])
        self.side.insert("all", "end", iid=f"cat:{OTHER}", text=OTHER)
        if getattr(self, "pill_buttons", None) and self.settings.filter_style == "pills":
            self._build_pills()

    def _category(self, t) -> str:
        if getattr(t, "kind", "") == "video":
            return t.category
        return pick_category(t.filename or name_from_url(t.url), t.url, rules=self.settings.category_rules())

    def _visible(self, t) -> bool:
        f = self._filter
        if f == "unfinished":
            return t.status != Status.COMPLETED
        if f == "finished":
            return t.status == Status.COMPLETED
        if f.startswith("cat:"):
            return self._category(t) == f[4:]
        return True

    def _row(self, t) -> tuple:
        status = t.status
        if status == Status.ERROR and t.error:
            status = f"Erro: {t.error}"
        elif status == Status.PAUSED and t.error:
            status = f"Pausado: {t.error}"  # e.g. the disk is full: says what to do
        if status == Status.COMPLETED and getattr(t, "check", ""):
            status = f"Concluído · {t.check}"
        elif status == Status.DOWNLOADING and t.speed_limit:
            status = f"Baixando (limite {format_size(t.speed_limit)}/s)"
        elif status == Status.DOWNLOADING and getattr(t, "connection_note", ""):
            status = f"Baixando ({t.connection_note})"
        running = t.is_running()
        return (
            t.filename or t.url,
            format_size(t.size),
            progress_bar(t.progress),
            f"{format_size(t.speed)}/s" if running else "",
            format_time(t.eta) if running else "",
            t.active_connections or "",
            status,
            self._category(t),
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
                    self.close_gui()
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
                self.tree.item(t.id, values=values, tags=(t.status,) + (("stripe",) if idx % 2 else ()))
                self.tree.move(t.id, "", idx)
            else:
                self.tree.insert("", idx, iid=t.id, values=values, tags=(t.status,) + (("stripe",) if idx % 2 else ()))
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
        if self.settings.filter_style == "pills":
            if [k for k, _l in self.SIDEBAR] + self._pill_categories() != getattr(self, "_pill_keys", None):
                self._build_pills()
            self._update_pills(counts)
        if self.details.task is not None or self.tree.selection():
            if self.details.task is not self._shown_task():
                self.details.show(self._shown_task())
            else:
                self.details.refresh()

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
