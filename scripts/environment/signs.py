"""Street-name blade signs at the loop's junctions — the eye-level orientation aid that makes the
track readable ("oh, this is the Quebec turn"), complementing the big painted pavement names. Two
products:

  1. a texture ATLAS (one green street-name panel per junction name), rendered from an SVG via the
     macOS qlmanage rasteriser — the font is only a BUILD-TIME tool; nothing but the PNG ships, so
     it stays CC0-clean.
  2. sign GEOMETRY — a post + a double-sided panel at each major junction, the panel UV-mapped to its
     name's cell in the atlas and turned to face the approaching driver.

Placement is driven by the SAME OSM street-label segments the painted road decals use
(``data/street_labels.json`` → :func:`scripts.geometry.road_text.choose_placements`), so the blade
signs name exactly the turns the pavement lettering does — no separate, drift-prone corner heuristic.

Emitted as two OBJ groups: ``SIGNS`` (panels, textured from the atlas) and ``SIGNPOST`` (grey posts).
Pure stdlib + qlmanage (same rasteriser scripts/ac/track_folder.py already uses).
"""

from __future__ import annotations

import math
import subprocess
import tempfile
from pathlib import Path

Vertex = tuple[float, float, float]

_ATLAS = 1024  # square atlas px — qlmanage pads to square, so author it square to avoid dead margins


def render_atlas(names: list[str], out_png: Path) -> dict[str, tuple[float, float, float, float]]:
    """Render a SQUARE atlas of green street-name panels to ``out_png`` (via qlmanage). Returns
    ``{name: (u0, v0, u1, v1)}`` UV cells (v measured top-down, 0 at the top row)."""
    names = list(dict.fromkeys(names))            # de-dupe, keep order
    n = max(1, len(names))
    w = h = _ATLAS
    ch = h / n  # row height
    rows = []
    for i, nm in enumerate(names):
        y = i * ch
        fs = min(int(ch * 0.5), int(w * 1.7 / max(len(nm), 1)))  # shrink to fit long names
        rows.append(
            f'<g>'
            f'<rect x="6" y="{y + 6:.1f}" width="{w - 12}" height="{ch - 12:.1f}" rx="14" '
            f'fill="#15642a" stroke="#ffffff" stroke-width="5"/>'
            f'<text x="{w / 2}" y="{y + ch / 2:.1f}" fill="#ffffff" '
            f'font-family="Helvetica,Arial,sans-serif" font-weight="bold" font-size="{fs}" '
            f'text-anchor="middle" dominant-baseline="central" '
            f'letter-spacing="1">{nm}</text></g>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
           f'viewBox="0 0 {w} {h}"><rect width="100%" height="100%" fill="#0a3318"/>'
           + "".join(rows) + "</svg>")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as f:
        f.write(svg)
        tmp = f.name
    try:
        subprocess.run(["qlmanage", "-t", "-s", str(h), "-o", str(out_png.parent), tmp],
                       capture_output=True, timeout=60)
        made = out_png.parent / (Path(tmp).name + ".png")
        if made.exists():
            made.replace(out_png)
    except Exception:
        pass
    cells = {}
    for i, nm in enumerate(names):
        cells[nm] = (0.0, i / n, 1.0, (i + 1) / n)
    return cells


def placements_from_segments(segments: list[dict], *, min_leg_m: float = 200.0) -> list[tuple[int, str]]:
    """One sign per major leg's ENTRY corner, named for the street turned onto. Mirrors the filter
    :func:`road_text.choose_placements` applies (leg length ≥ ``min_leg_m``; skip a leg whose name
    repeats the previous signed one) but positions the sign AT the junction (``start_idx``), where a
    real street blade lives — not mid-block like a pavement decal."""
    out: list[tuple[int, str]] = []
    last = None
    for s in segments:
        if s["length_m"] < min_leg_m or s["name"] == last:
            continue
        out.append((int(s["start_idx"]), s["name"]))
        last = s["name"]
    return out


def build_signs(loop: list[Vertex], widths_m: list[float], placements: list[tuple[int, str]],
                cells: dict[str, tuple[float, float, float, float]], *,
                panel_w: float = 2.6, panel_h: float = 0.62, panel_top: float = 2.9,
                flip_read: bool = False, flip_up: bool = False, reject=None, ground=None) -> dict[str, dict]:
    """Place a post + name panel at each ``(corner_index, street_name)`` placement, the panel turned
    flat to face the approaching driver and UV-mapped to its atlas cell.

    UV/orientation mirror scripts/geometry/road_text's confirmed-working "across" layout: reading runs
    driver-left→right (increasing u), glyph tops point up (+Y), and the cell's top row (v0) maps to the
    panel top. ``flip_read``/``flip_up`` mirror the two axes — a one-bit fix if it reads mirrored or
    upside-down in-game. ``reject(x, z)`` (optional) drops any sign whose post base lands on the road —
    signs sit AT junctions, where a crossing/fold-back leg can run under the chosen verge offset.
    Returns ``{"SIGNS": panel_mesh(uv->atlas), "SIGNPOST": post_mesh}``."""
    pv: list[Vertex] = []
    puv: list[tuple[float, float]] = []
    pt: list[tuple[int, int, int]] = []
    sv: list[Vertex] = []
    st: list[tuple[int, int, int]] = []
    kept: list[str] = []
    n = len(loop)
    for ci, nm in placements:
        u0, v0, u1, v1 = cells.get(nm, (0.0, 0.0, 1.0, 1.0))
        if flip_up:
            v0, v1 = v1, v0
        ci = max(1, min(n - 2, ci))
        x, y, z = loop[ci]
        # approach tangent a few vertices BEFORE the corner (stable through the turn itself)
        a, b = loop[max(0, ci - 3)], loop[ci]
        tx, tz = b[0] - a[0], b[2] - a[2]
        tl = math.hypot(tx, tz) or 1e-6
        tx, tz = tx / tl, tz / tl
        nx, nz = -tz, tx                              # left normal -> outside verge
        off = widths_m[ci] / 2.0 + 1.5
        bx, bz = x + nx * off, z + nz * off           # post base on the verge
        if reject is not None and reject(bx, bz):     # base on a crossing/fold-back road at the junction — skip
            continue
        kept.append(nm)
        by = ground(bx, bz) if ground is not None else y  # seat the post on the CONFORMED ground at the
        #                                                   verge, not the road centreline y (else it floats)
        cy = by + panel_top                           # panel centre height
        top = cy + panel_h / 2.0

        # post: crossed thin quads (double-sided) so it reads from any approach angle
        r = len(sv)
        pr = 0.07
        sv += [(bx - pr, by, bz), (bx + pr, by, bz), (bx + pr, top, bz), (bx - pr, top, bz),
               (bx, by, bz - pr), (bx, by, bz + pr), (bx, top, bz + pr), (bx, top, bz - pr)]
        st += [(r, r + 1, r + 2), (r, r + 2, r + 3), (r, r + 2, r + 1), (r, r + 3, r + 2),
               (r + 4, r + 5, r + 6), (r + 4, r + 6, r + 7), (r + 4, r + 6, r + 5), (r + 4, r + 7, r + 6)]

        # panel: vertical quad facing the approaching driver; reading along er, up along +Y
        er = (-nx, -nz) if not flip_read else (nx, nz)   # driver-left -> driver-right
        base = len(pv)
        for ru, rv, u, v in [(-0.5, 0.5, u0, v0), (0.5, 0.5, u1, v0),
                             (0.5, -0.5, u1, v1), (-0.5, -0.5, u0, v1)]:
            pv.append((bx + er[0] * (ru * panel_w), cy + rv * panel_h, bz + er[1] * (ru * panel_w)))
            puv.append((u, v))
        pt += [(base, base + 1, base + 2), (base, base + 2, base + 3),   # front
               (base, base + 2, base + 1), (base, base + 3, base + 2)]   # back (double-sided)
    return {"SIGNS": {"vertices": pv, "uvs": puv, "tris": pt},
            "SIGNPOST": {"vertices": sv, "tris": st},
            "kept": kept}
