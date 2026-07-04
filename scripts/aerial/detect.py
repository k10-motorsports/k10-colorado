"""Read the screenshot's pixel dimensions, and (for PNG sources) isolate the asphalt ribbon.

Two jobs:

1. ``image_size`` — width/height of the source, for **any** screenshot. PNGs go through the stdlib
   decoder reused from ``scripts.trace.detect``; JPEGs are parsed just far enough (the SOFn frame
   header) to read their dimensions without a full decode — enough to turn the normalised control
   points / hand-trace in ``track.config.json`` into absolute pixels.

2. ``asphalt_mask`` — a boolean ribbon mask from a fully-decoded PNG, for the auto-trace path. The
   drivable surface in an aerial shot is near-neutral grey (low saturation, mid brightness), which
   separates it from tan dirt/soil (warm: r>g>b) and vegetation (green). Tune ``is_asphalt`` per
   source. JPEG cannot be auto-traced here (no stdlib JPEG decoder) — supply ``source.trace_px``.
"""

from __future__ import annotations

import struct
from pathlib import Path

from scripts.trace.detect import read_png  # stdlib PNG decoder, shared with route-tracing

# JPEG Start-Of-Frame markers carrying [precision, height, width, ...]; everything else is skipped.
_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """Width/height from a JPEG's SOFn frame header (no full decode)."""
    assert data[:2] == b"\xff\xd8", "not a JPEG"
    i = 2
    while i + 1 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:  # SOI/EOI/RSTn carry no length
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in _SOF_MARKERS:
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + seg_len
    raise ValueError("no SOF marker found in JPEG")


def image_size(path: str | Path) -> tuple[int, int]:
    """(width, height) in pixels for a PNG or JPEG screenshot."""
    p = Path(path)
    head = p.read_bytes()[:4]
    if head[:2] == b"\xff\xd8":
        return _jpeg_size(p.read_bytes())
    if head == b"\x89PNG":
        w, h, _ch, _px = read_png(p)
        return w, h
    raise ValueError(f"unsupported image type for {p.name} (need PNG or JPEG)")


def is_asphalt(r: int, g: int, b: int) -> bool:
    """Default drivable-surface test — near-neutral grey, mid brightness (not tan dirt, not grass)."""
    mx, mn = max(r, g, b), min(r, g, b)
    sat = mx - mn          # chroma: low for asphalt, high for soil/vegetation
    bright = (r + g + b) / 3
    return sat <= 30 and 55 <= bright <= 175


def asphalt_mask(path: str | Path, step: int = 1, is_surface=is_asphalt
                 ) -> tuple[list[list[bool]], int, int]:
    """Decode a PNG and return (mask grid [row][col], grid_w, grid_h). ``step`` subsamples for speed.

    The returned grid is ``step``-downsampled; multiply coordinates by ``step`` to get source pixels.
    """
    w, h, ch, px = read_png(path)
    gw, gh = (w + step - 1) // step, (h + step - 1) // step
    mask = [[False] * gw for _ in range(gh)]
    for gy, y in enumerate(range(0, h, step)):
        row = y * w * ch
        mrow = mask[gy]
        for gx, x in enumerate(range(0, w, step)):
            o = row + x * ch
            if is_surface(px[o], px[o + 1], px[o + 2]):
                mrow[gx] = True
    return mask, gw, gh
