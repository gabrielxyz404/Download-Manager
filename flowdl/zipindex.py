"""A ZIP's table of contents read from its last bytes, before the rest of the file has arrived.

A ZIP ends with the "end of central directory" record (PK 05 06), which says where the
central directory is: one entry per file (PK 01 02) with its name and sizes. When the
central directory lies within the bytes we have (the end of the file, asked for first), the
contents can be shown while the download goes on. ZIP64 (files over 4 GB, or more than
65535 entries) is understood too. Nothing is extracted; only names and sizes are read.
"""

from __future__ import annotations

import struct

MAX_NAMES = 500  # names kept for the window (the count and the totals cover all of them)


def read_listing(data: bytes, offset: int, total_size: int) -> dict | None:
    """(Never raises: a damaged or fake ZIP gives None - found in a review: a short ZIP64
    record made the whole download fail.)"""
    try:
        return _read_listing(data, offset, total_size)
    except (struct.error, IndexError, ValueError, OverflowError):
        return None


def _read_listing(data: bytes, offset: int, total_size: int) -> dict | None:
    """``data`` holds the file's bytes from ``offset`` to its end (``total_size``). Returns
    {"count", "uncompressed", "compressed", "names": [(name, size), ...]} or None when the
    table of contents is not in ``data`` (or this is not a ZIP)."""
    end = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))  # (22 bytes + a comment of 64 KB at most)
    if end < 0 or len(data) - end < 22:
        return None
    _sig, _disk, _cd_disk, _n_here, count, cd_size, cd_offset, _clen = struct.unpack("<IHHHHIIH", data[end:end + 22])
    if count == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        z64 = _zip64(data, end, offset)
        if z64 is None:
            return None
        count, cd_size, cd_offset = z64
    if cd_offset + cd_size > total_size:
        return None
    start = cd_offset - offset
    if start < 0 or start + cd_size > len(data):
        return None  # the table of contents is bigger than the end we have
    cd = data[start:start + cd_size]
    names, pos, total_u, total_c, seen = [], 0, 0, 0, 0
    while pos + 46 <= len(cd) and cd[pos:pos + 4] == b"PK\x01\x02":
        (flags, csize, usize, name_len, extra_len, comment_len) = (
            struct.unpack("<H", cd[pos + 8:pos + 10])[0], *struct.unpack("<II", cd[pos + 20:pos + 28]),
            *struct.unpack("<HHH", cd[pos + 28:pos + 34]))
        name_raw = cd[pos + 46:pos + 46 + name_len]
        extra = cd[pos + 46 + name_len:pos + 46 + name_len + extra_len]
        if usize == 0xFFFFFFFF or csize == 0xFFFFFFFF:
            usize, csize = _zip64_sizes(extra, usize, csize)
        name = name_raw.decode("utf-8" if flags & 0x800 else "cp437", "replace")
        total_u += usize
        total_c += csize
        seen += 1
        if len(names) < MAX_NAMES and not name.endswith("/"):
            names.append((name, usize))
        pos += 46 + name_len + extra_len + comment_len
    if seen == 0 and count:
        return None
    return {"count": seen, "uncompressed": total_u, "compressed": total_c, "names": names}


def _zip64(data: bytes, end: int, offset: int) -> tuple[int, int, int] | None:
    loc = end - 20
    if loc < 0 or data[loc:loc + 4] != b"PK\x06\x07":
        return None
    rec_offset = struct.unpack("<Q", data[loc + 8:loc + 16])[0] - offset
    if rec_offset < 0 or data[rec_offset:rec_offset + 4] != b"PK\x06\x06":
        return None
    count, cd_size, cd_offset = struct.unpack("<QQQ", data[rec_offset + 32:rec_offset + 56])
    return count, cd_size, cd_offset


def _zip64_sizes(extra: bytes, usize: int, csize: int) -> tuple[int, int]:
    pos = 0
    while pos + 4 <= len(extra):
        kind, size = struct.unpack("<HH", extra[pos:pos + 4])
        body = extra[pos + 4:pos + 4 + size]
        if kind == 0x0001:
            vals = list(struct.unpack("<" + "Q" * (len(body) // 8), body[:len(body) // 8 * 8]))
            if usize == 0xFFFFFFFF and vals:
                usize = vals.pop(0)
            if csize == 0xFFFFFFFF and vals:
                csize = vals.pop(0)
            break
        pos += 4 + size
    return usize, csize
