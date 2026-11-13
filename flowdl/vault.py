"""Keeps login cookies out of plain text on disk.

On Windows the data is encrypted with DPAPI (CryptProtectData): only the same Windows user on
the same computer can read it back, which is how browsers protect their own cookies there.
Elsewhere the files are made readable by the owner only (see manager.save), as browsers do on
Linux without a keyring; the values are not encrypted.
"""

from __future__ import annotations

import base64
import json
import sys

PREFIX_DPAPI = "dpapi:"
PREFIX_PLAIN = "plain:"


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dst = Blob()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    ok = fn(ctypes.byref(src), None, None, None, None, flags, ctypes.byref(dst))
    if not ok:
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def protect(value) -> str | None:
    """Turn a JSON-able value (cookies) into a string safe to keep in a settings file."""
    if not value:
        return None
    raw = json.dumps(value).encode()
    if sys.platform.startswith("win"):
        try:
            return PREFIX_DPAPI + base64.b64encode(_dpapi(raw, True)).decode()
        except (OSError, AttributeError):
            pass
    return PREFIX_PLAIN + base64.b64encode(raw).decode()


def unprotect(text: str | None, default=None):
    """Read back what protect() wrote. Anything unreadable gives ``default`` (the download
    then simply goes on without the login cookies)."""
    if not text:
        return default
    try:
        if text.startswith(PREFIX_DPAPI):
            raw = _dpapi(base64.b64decode(text[len(PREFIX_DPAPI):]), False)
        elif text.startswith(PREFIX_PLAIN):
            raw = base64.b64decode(text[len(PREFIX_PLAIN):])
        else:
            return default
        return json.loads(raw)
    except (OSError, ValueError, AttributeError):
        return default
