"""Icon next to the Windows clock (notification area): speed on hover, a small menu, and a
Windows notification when a download finishes. Written straight against the Windows API
(ctypes, part of Python): no extra library, a few hundred KB of memory.
"""

from __future__ import annotations

import os
import threading
import time

SHOW, PAUSE_ALL, RESUME_ALL, LIMIT, QUIT = 1, 2, 3, 4, 9
ACTIONS = {SHOW: "show", PAUSE_ALL: "pause_all", RESUME_ALL: "resume_all", LIMIT: "limit", QUIT: "quit"}

WM_DESTROY, WM_CLOSE, WM_COMMAND, WM_APP = 0x0002, 0x0010, 0x0111, 0x8000
WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0202, 0x0203, 0x0205
CALLBACK = WM_APP + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO, NIIF_WARNING = 0x1, 0x2
MF_STRING, MF_GRAYED, MF_CHECKED, MF_SEPARATOR = 0x0, 0x1, 0x8, 0x800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x2, 0x80, 0x100


def supported() -> bool:
    return os.name == "nt"


class Tray:
    """``on_action(name)`` gets "show", "pause_all", "resume_all", "limit" or "quit" (called
    from the tray's own thread: hand it over to the app). ``status()`` returns the hover text;
    ``menu_state()`` returns {"running": bool, "paused": bool, "limit_kb": int, "limit_on": bool}."""

    def __init__(self, on_action, status=None, menu_state=None, icon_path: str | None = None):
        self.on_action, self.status, self.menu_state = on_action, status, menu_state
        self.icon_path = icon_path
        self.hwnd = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._tip = "Flow Downloads"
        self._thread: threading.Thread | None = None
        self._stopping = False

    # ---------------------------------------------------------------- public
    def start(self) -> bool:
        if not supported():
            return False
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        if self.hwnd:
            threading.Thread(target=self._keep_tip_fresh, name="tray-tip", daemon=True).start()
        return bool(self.hwnd)

    def update(self, tip: str) -> None:
        tip = (tip or "Flow Downloads")[:127]
        if tip == self._tip or not self.hwnd:
            return
        self._tip = tip
        self._notify_icon(NIM_MODIFY, NIF_TIP)

    def notify(self, title: str, text: str, warning: bool = False) -> None:
        """A Windows notification (the balloon / toast of this icon)."""
        if self.hwnd:
            self._notify_icon(NIM_MODIFY, NIF_INFO, info=(title[:63], text[:255], NIIF_WARNING if warning else NIIF_INFO))

    def stop(self) -> None:
        self._stopping = True
        if self.hwnd:
            import ctypes
            ctypes.windll.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            if self._thread:
                self._thread.join(3)

    # --------------------------------------------------------------- windows
    def _structs(self):
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                        ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                        ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]
        return NOTIFYICONDATAW

    def _notify_icon(self, message: int, flags: int, info=None) -> bool:
        import ctypes
        nid = self._nid_type()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = CALLBACK
        nid.hIcon = self._icon
        nid.szTip = self._tip
        if info:
            nid.szInfoTitle, nid.szInfo, nid.dwInfoFlags = info
        with self._lock:
            return bool(ctypes.windll.shell32.Shell_NotifyIconW(message, ctypes.byref(nid)))

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int,
                                      ctypes.c_int, wintypes.UINT]
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                          wintypes.HWND, wintypes.LPVOID]
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        self._nid_type = self._structs()
        taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

        def wndproc(hwnd, msg, wparam, lparam):
            try:
                if msg == CALLBACK:
                    if lparam == WM_LBUTTONDBLCLK:
                        self.on_action("show")
                    elif lparam == WM_RBUTTONUP:
                        self._menu(hwnd)
                    return 0
                if msg == WM_COMMAND:
                    action = ACTIONS.get(wparam & 0xFFFF)
                    if action:
                        self.on_action(action)
                    return 0
                if msg == taskbar_created:  # Explorer restarted: put the icon back
                    self._notify_icon(NIM_ADD, NIF_MESSAGE | NIF_ICON | NIF_TIP)
                    return 0
                if msg == WM_CLOSE:
                    self._notify_icon(NIM_DELETE, 0)
                    user32.DestroyWindow(hwnd)
                    return 0
                if msg == WM_DESTROY:
                    user32.PostQuitMessage(0)
                    return 0
            except Exception:  # noqa: BLE001 - never let an error escape into Windows
                pass
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROC(wndproc)  # keep a reference: Windows calls it

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = "FlowDownloadsTray"
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, "FlowDownloadsTray", "Flow Downloads", 0, 0, 0, 0, 0,
                                           None, None, hinst, None)
        icon = None
        if self.icon_path and os.path.exists(self.icon_path):
            icon = user32.LoadImageW(None, self.icon_path, 1, 0, 0, 0x10 | 0x40)  # file, default size
        self._icon = icon or user32.LoadIconW(None, 32512)  # the standard application icon
        if not self.hwnd or not self._notify_icon(NIM_ADD, NIF_MESSAGE | NIF_ICON | NIF_TIP):
            self.hwnd = None
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self.hwnd = None

    def _menu(self, hwnd) -> None:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        state = (self.menu_state() if self.menu_state else None) or {}
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, SHOW, "Abrir Flow Downloads")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING | (0 if state.get("running") else MF_GRAYED), PAUSE_ALL, "Pausar tudo")
        user32.AppendMenuW(menu, MF_STRING | (0 if state.get("paused") else MF_GRAYED), RESUME_ALL, "Retomar tudo")
        kb = state.get("limit_kb", 0)
        if kb:
            user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if state.get("limit_on") else 0), LIMIT,
                               f"Limitar velocidade ({kb} KB/s)")
        else:
            user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, LIMIT, "Limitar velocidade (defina em Opções)")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, QUIT, "Sair")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)  # so the menu closes when clicking elsewhere
        chosen = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD, pt.x, pt.y, 0, hwnd, None)
        user32.DestroyMenu(menu)
        if chosen in ACTIONS:
            self.on_action(ACTIONS[chosen])

    def _keep_tip_fresh(self) -> None:
        """Speed on hover: refreshed every 2 s (every 5 s when nothing is downloading)."""
        while self.hwnd and not self._stopping:
            text = None
            try:
                text = self.status() if self.status else None
            except Exception:  # noqa: BLE001
                pass
            if text:
                self.update(text)
            time.sleep(2 if text and "a baixar" in text else 5)
