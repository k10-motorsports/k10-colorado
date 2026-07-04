"""Detect hand-drawn route pixels in a map screenshot → a pixel point cloud.

Self-contained PNG decoder (8-bit RGB/RGBA, non-interlaced) using stdlib zlib — no Pillow/numpy.
The drawn line is isolated by colour (default: red, e.g. Google My Maps); tune ``is_drawn`` per source.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

_PAETH_CT = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}  # color type -> channels (grayscale/RGB/palette*/GA/RGBA)


def read_png(path: str | Path) -> tuple[int, int, int, bytes]:
    """Decode an 8-bit non-interlaced PNG → (width, height, channels, raw RGBA/RGB bytes)."""
    d = Path(path).read_bytes()
    assert d[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, w, h, bitd, ct, idat = 8, 0, 0, 0, 0, bytearray()
    while pos < len(d):
        ln = struct.unpack(">I", d[pos:pos + 4])[0]
        typ = d[pos + 4:pos + 8]
        chunk = d[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, bitd, ct = struct.unpack(">IIBB", chunk[:10])[:4]
        elif typ == b"IDAT":
            idat += chunk
        elif typ == b"IEND":
            break
    assert bitd == 8, f"unsupported bit depth {bitd} (need 8 — re-export the screenshot)"
    ch = _PAETH_CT[ct]
    raw = zlib.decompress(bytes(idat))
    stride = w * ch
    out = bytearray()
    prev = bytearray(stride)
    p = 0
    for _ in range(h):
        ft = raw[p]; p += 1
        line = bytearray(raw[p:p + stride]); p += stride
        if ft == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 255
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        out += line
        prev = line
    return w, h, ch, bytes(out)


def is_red(r: int, g: int, b: int) -> bool:
    """Default drawn-line test — saturated red (Google My Maps default line)."""
    return r > 150 and g < 105 and b < 105 and r - g > 60 and r - b > 60


def detect(path: str | Path, step: int = 2, is_drawn=is_red) -> tuple[list[tuple[int, int]], int, int]:
    """Return (drawn pixels [(x,y)...], width, height). ``step`` subsamples for speed."""
    w, h, ch, px = read_png(path)
    pts = []
    for y in range(0, h, step):
        row = y * w * ch
        for x in range(0, w, step):
            o = row + x * ch
            if is_drawn(px[o], px[o + 1], px[o + 2]):
                pts.append((x, y))
    return pts, w, h
