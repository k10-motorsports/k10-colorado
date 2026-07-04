"""Big painted street-name labels on the tarmac — the wayfinding the user asked for ("the names of
the streets in very large street-sign lettering on the road when making a turn onto it"), and exactly
what the Commerce City reference photos show ("big painted pavement street-names — 'E 45TH AVE'").

Two products, mirroring scripts/environment/signs.py but laid FLAT on the road:
  1. a transparent texture ATLAS — white condensed-bold name per row, no background, so only the
     letters paint (alpha cutout). Rendered from SVG via the macOS ``qlmanage`` rasteriser, so the
     font is a build-time tool only and nothing but the PNG ships (CC0-clean).
  2. decal GEOMETRY — one flat quad per label, placed just past the turn-onto a named street, sized
     so the word runs DOWN the lane (cap height ``cap_m`` across the road) and UV-mapped to its atlas
     cell. ``bank_at`` rolls each decal with the cambered road.

Emitted as one OBJ group ``ROADTEXT`` (textured from the atlas). Pure stdlib + qlmanage.
"""

from __future__ import annotations

import math
import re
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

Vertex = tuple[float, float, float]


def _read_png(path: Path):
    """Minimal PNG decode (8-bit RGB/RGBA, all filter types) -> (W, H, channels, unfiltered rows)."""
    b = path.read_bytes()
    assert b[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    i, W, H, ct, idat = 8, 0, 0, 0, b""
    while i < len(b):
        ln = struct.unpack(">I", b[i:i + 4])[0]
        typ = b[i + 4:i + 8]
        data = b[i + 8:i + 8 + ln]
        i += 12 + ln
        if typ == b"IHDR":
            W, H, _bd, ct = struct.unpack(">IIBB", data[:10])
        elif typ == b"IDAT":
            idat += data
        elif typ == b"IEND":
            break
    raw = zlib.decompress(idat)
    ch = 4 if ct == 6 else 3

    def paeth(a, bb, c):
        p = a + bb - c
        pa, pb, pc = abs(p - a), abs(p - bb), abs(p - c)
        return a if (pa <= pb and pa <= pc) else (bb if pb <= pc else c)

    rows, prev, off = [], bytearray(W * ch), 0
    for _y in range(H):
        ft = raw[off]; off += 1
        line = bytearray(raw[off:off + W * ch]); off += W * ch
        if ft:
            for x in range(W * ch):
                a = line[x - ch] if x >= ch else 0
                bb = prev[x]
                c = prev[x - ch] if x >= ch else 0
                if ft == 1: line[x] = (line[x] + a) & 255
                elif ft == 2: line[x] = (line[x] + bb) & 255
                elif ft == 3: line[x] = (line[x] + ((a + bb) >> 1)) & 255
                elif ft == 4: line[x] = (line[x] + paeth(a, bb, c)) & 255
        rows.append(line); prev = line
    return W, H, ch, rows


def _write_rgba_png(path: Path, W: int, H: int, pix: bytes) -> None:
    """Write 8-bit RGBA (pix = W*H*4 bytes), filter-none rows."""
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        raw += pix[y * W * 4:(y + 1) * W * 4]
    comp = zlib.compress(bytes(raw), 9)

    def chunk(typ, data):
        return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff)

    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", comp) + chunk(b"IEND", b""))

# glyph metrics for the condensed bold face we render with (Helvetica Neue Condensed Bold-ish):
# average cap advance ≈ ``ADV`` × font-size; cap height ≈ ``CAP`` × font-size.
ADV = 0.60
CAP = 0.72
LINE = 1.06   # line pitch / font-size for stacked lines
_ROAD_TYPES = {"AVE", "ST", "BLVD", "DR", "RD", "LN", "CT", "PL", "PKWY", "HWY", "WAY", "CIR", "TER"}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _wrap(name: str) -> list[str]:
    """Street-sign wrap: put the road type (AVE/ST/BLVD/DR…) on its own second line so the name above
    stays big. 'N COLORADO BLVD' -> ['N COLORADO','BLVD']; 'E 52ND AVE' -> ['E 52ND','AVE']."""
    toks = name.split()
    if len(toks) >= 2 and toks[-1] in _ROAD_TYPES:
        return [" ".join(toks[:-1]), toks[-1]]
    return [name]


def render_text_atlas(names: list[str], out_png: Path, *, S: int = 1024, wrap: bool = True):
    """Render a SQUARE transparent atlas (one white name per row) to ``out_png`` via qlmanage.
    Returns ``{name: {"cell": (u0, v0, u1, v1), "aspect": w/h}}`` — cells are TIGHT to the glyphs
    (v top-down) so the decal quad carries no dead margin. ``wrap`` stacks the road type on a second
    line (for the across/sign-flat layout); pass ``wrap=False`` for lengthwise text (one line)."""
    names = list(dict.fromkeys(names))            # de-dupe, keep order
    n = max(1, len(names))
    rowH = S / n
    rows = []
    info: dict[str, dict] = {}
    for i, nm in enumerate(names):
        cy = (i + 0.5) * rowH
        lines = _wrap(nm) if wrap else [nm]
        nL = len(lines)
        widest = max(len(ln) for ln in lines)
        # fit: widest line into 92% of the row width AND nL lines into 86% of the row height
        fs = min((S * 0.92) / (ADV * max(widest, 1)), (rowH * 0.86) / (nL * LINE))
        block_w = ADV * fs * widest
        block_h = (nL - 1) * LINE * fs + CAP * fs        # cap of top line .. baseline of bottom line
        y0 = cy - block_h / 2 + CAP * fs / 2             # central y of the first line
        spans = "".join(
            f'<text x="{S/2:.1f}" y="{y0 + k*LINE*fs:.1f}" fill="#ffffff" '
            f'font-family="\'Helvetica Neue\',Helvetica,Arial,sans-serif" font-weight="bold" '
            f'font-stretch="condensed" font-size="{fs:.1f}" text-anchor="middle" '
            f'dominant-baseline="central" letter-spacing="0.5">{ln}</text>'
            for k, ln in enumerate(lines))
        rows.append(spans)
        info[nm] = {
            "cell": ((S/2 - block_w/2)/S, (cy - block_h/2)/S, (S/2 + block_w/2)/S, (cy + block_h/2)/S),
            "aspect": (block_w / block_h) if block_h else 1.0,
        }
    # qlmanage always composites onto OPAQUE white, so we can't author transparency directly. Render
    # WHITE text on BLACK, then rebuild a true RGBA cutout: alpha = luminance, RGB = white (so the
    # antialiased edges stay white at partial alpha). Result is a standard alpha-cutout texture.
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{S}" height="{S}" '
           f'viewBox="0 0 {S} {S}"><rect width="100%" height="100%" fill="#000000"/>'
           + "".join(rows) + "</svg>")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as f:
        f.write(svg)
        tmp = f.name
    try:
        subprocess.run(["qlmanage", "-t", "-s", str(S), "-o", str(out_png.parent), tmp],
                       capture_output=True, timeout=60)
        made = out_png.parent / (Path(tmp).name + ".png")
        if made.exists():
            W, H, ch, prows = _read_png(made)
            pix = bytearray(W * H * 4)
            for y in range(H):
                ln = prows[y]
                for x in range(W):
                    r, g, b = ln[x * ch], ln[x * ch + 1], ln[x * ch + 2]
                    a = max(r, g, b)                 # white text -> opaque, black bg -> transparent
                    o = (y * W + x) * 4
                    pix[o] = pix[o + 1] = pix[o + 2] = 255
                    pix[o + 3] = a
            _write_rgba_png(out_png, W, H, bytes(pix))
            made.unlink(missing_ok=True)
    except Exception as e:
        print(f"[road_text] atlas render/convert failed: {e}")
    return info


def place_labels(centerline_m: list[Vertex], widths_m: list[float],
                 placements: list[tuple[int, str]], info: dict[str, dict], *,
                 cap_m: float = 4.6, max_across_m: float = 16.0, lift: float = 0.0,
                 bank_at=None, mode: str = "across", flip_read: bool = False,
                 flip_up: bool = False) -> dict:
    """Lay one flat name decal per ``(index, name)`` placement, UV-mapped to its atlas cell.

    ``mode="across"`` (default) lays the word like a **street sign read flat by the approaching driver**
    — letters upright with their tops pointing down-road (+travel), the word spanning ACROSS the road.
    Cap height is ``cap_m``; long names that would exceed ``max_across_m`` across the road shrink to fit.
    ``mode="along"`` runs the word down the lane instead (letters on their side, MUTCD-style).
    ``flip_read``/``flip_up`` mirror the two axes (a one-bit fix if it reads backwards in-game).
    ``bank_at`` rolls each decal with the cambered road. Returns a ``ROADTEXT`` mesh."""
    pts = centerline_m
    n = len(pts)
    verts: list[Vertex] = []
    uvs: list[tuple[float, float]] = []
    tris: list[tuple[int, int, int]] = []
    st = [0.0]
    for i in range(1, n):
        st.append(st[-1] + math.hypot(pts[i][0] - pts[i - 1][0], pts[i][2] - pts[i - 1][2]))

    def tangent(i):
        a, b = pts[max(0, i - 1)], pts[min(n - 1, i + 1)]
        tx, tz = b[0] - a[0], b[2] - a[2]
        tl = math.hypot(tx, tz) or 1e-6
        return tx / tl, tz / tl

    for idx, name in placements:
        meta = info.get(name)
        if not meta:
            continue
        i0 = max(1, min(n - 2, idx))
        ar = meta["aspect"]                       # word width / cap height
        cap = cap_m
        word_len = cap_m * ar                     # along-road reading length
        u0, v0, u1, v1 = meta["cell"]             # v is top-down (v0 = top of glyphs)
        if flip_up:
            v0, v1 = v1, v0
        base = len(verts)

        if mode == "across":
            # short word laid flat across the road, read by the approaching driver (one quad)
            if word_len > max_across_m:
                s = max_across_m / word_len
                word_len *= s
                cap *= s
            x, y, z = pts[i0]
            tx, tz = tangent(i0)
            nx, nz = -tz, tx
            er = (-nx, -nz) if not flip_read else (nx, nz)     # reading L->R = driver L->R
            eu = (tx, tz)                                       # tops point down-road
            tb = math.tan(bank_at(st[i0])) if bank_at else 0.0
            for ru, rv, u, v in [(-0.5, +0.5, u0, v0), (+0.5, +0.5, u1, v0),
                                 (+0.5, -0.5, u1, v1), (-0.5, -0.5, u0, v1)]:
                ox = er[0] * (ru * word_len) + eu[0] * (rv * cap)
                oz = er[1] * (ru * word_len) + eu[1] * (rv * cap)
                verts.append((x + ox, y + lift + (ox * nx + oz * nz) * tb, z + oz))
                uvs.append((u, v))
            tris += [(base, base + 1, base + 2), (base, base + 2, base + 3)]
            continue

        # mode == "along": lay the word as a RIBBON that FOLLOWS the centerline (so a long name bends
        # with the road instead of chording across a corner). Walk forward from i0 covering word_len;
        # at each centerline node drop a left/right pair (±cap/2 across the road), partitioning the
        # atlas cell's u-range by arc travelled. v0(top of glyphs) -> +N side (tops point left).
        arc0 = st[i0]
        rows = []
        k = i0
        while k <= n - 1 and (st[k] - arc0) <= word_len:
            x, y, z = pts[k]
            tx, tz = tangent(k)
            nx, nz = -tz, tx
            f = (st[k] - arc0) / word_len                     # 0..1 along the word
            u = u1 + (u0 - u1) * f if flip_read else u0 + (u1 - u0) * f
            tb = math.tan(bank_at(st[k])) if bank_at else 0.0
            r = len(verts)
            verts.append((x + nx * (cap / 2), y + lift + (cap / 2) * tb, z + nz * (cap / 2)))   # left (+N) = glyph top
            verts.append((x - nx * (cap / 2), y + lift - (cap / 2) * tb, z - nz * (cap / 2)))   # right (-N) = glyph bottom
            uvs.append((u, v0))
            uvs.append((u, v1))
            rows.append(r)
            k += 1
        for r in range(len(rows) - 1):
            p, q = rows[r], rows[r + 1]
            tris += [(p, p + 1, q + 1), (p, q + 1, q)]
    return {"vertices": verts, "uvs": uvs, "tris": tris}


def choose_placements(labels: list[str], segments: list[dict], *, min_leg_m: float = 200.0,
                      into_m: float = 22.0, spacing_m: float = 3.0,
                      centerline=None, info: dict | None = None, cap_m: float = 4.8,
                      straight_thr_deg: float = 1.3) -> list[tuple[int, str]]:
    """From the street_labels segments, pick where to paint each name: one per leg longer than
    ``min_leg_m`` (skipping a leg whose name repeats the previous painted one).

    When ``centerline`` + ``info`` are given, the label is snapped to the **straightest window** inside
    its leg that fits the word (so lengthwise text never lands on a corner), preferring the window
    nearest the turn-in. Otherwise it falls back to ``into_m`` past the leg start."""
    # per-vertex turn angle (deg) between consecutive segments
    turn = None
    if centerline is not None:
        n = len(centerline)
        turn = [0.0] * n
        for i in range(1, n - 1):
            ax, _, az = centerline[i - 1]; bx, _, bz = centerline[i]; cx, _, cz = centerline[i + 1]
            v1 = (bx - ax, bz - az); v2 = (cx - bx, cz - bz)
            l1 = math.hypot(*v1) or 1e-9; l2 = math.hypot(*v2) or 1e-9
            d = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)))
            turn[i] = math.degrees(math.acos(d))

    out: list[tuple[int, str]] = []
    last = None
    for s in segments:
        if s["length_m"] < min_leg_m or s["name"] == last:
            continue
        a, b = s["start_idx"], s["end_idx"]
        idx = min(a + int(into_m / spacing_m), b - 2)
        if turn is not None and info and s["name"] in info:
            span = max(4, int(cap_m * info[s["name"]]["aspect"] / spacing_m) + 2)  # vertices the word needs
            best_j, best_cost = None, 1e18
            for j in range(a, max(a + 1, b - span)):
                window = turn[j:j + span]
                if not window:
                    continue
                # cost = worst curvature in the window + a small bias toward the turn-in (earlier j)
                cost = max(window) + 0.002 * (j - a)
                if cost < best_cost:
                    best_cost, best_j = cost, j
            if best_j is not None:
                # nudge a touch into the straight so it doesn't start exactly on the entry vertex
                idx = min(best_j + 2, b - 2)
        out.append((idx, s["name"]))
        last = s["name"]
    return out
