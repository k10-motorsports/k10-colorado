"""Build environment placement from real OSM zones + render the fully dressed track.

Reads data/environment.geojson (zones), centerline.geojson (loop + interior connector), the projected
loop (centerline.local.json) and the heightfield, then generates, in local ENU metres:
  - the interior connector road (layout polish),
  - Sand Creek + basins as water,
  - I-70/I-270/Vasquez as raised visual highway decks,
  - tree billboards scattered in green/wetland zones,
  - streetlight posts along the lap.
Writes data/environment.obj and a dressed 3/4 render. Pure stdlib (reuses the geometry back-half).

Run:  python -m scripts.environment.build_env projects/<slug>
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from scripts.capture import evidence
from scripts.config import load_config
from scripts.geometry import kerbs, ribbon
from scripts.geometry import profile as profile_mod
from scripts.geometry.build_mesh import (
    ROAD_LIFT_M, finished_centerline, project_grid, read_npy, render_iso, write_mtl, write_obj)
from scripts.geometry.projection import _meters_per_degree
from scripts.environment import buildings, signs

Vertex = tuple[float, float, float]


def _grid_sampler(grid, meta, elev0):
    s, w, n, e = meta["bbox_swne"]
    nx, ny, sp = meta["nx"], meta["ny"], meta["spacing_m"]
    midlat = (s + n) / 2
    gy = sp / 111_000.0
    gx = sp / (111_000.0 * math.cos(math.radians(midlat)))

    def y_at(lon, lat):
        j = min(ny - 1, max(0, int(round((n - lat) / gy))))
        i = min(nx - 1, max(0, int(round((lon - w) / gx))))
        return grid[j][i] - elev0
    return y_at


def _line_ribbon(line, project, half_w, y_off=0.0):
    """Sweep a flat ribbon along a projected lon/lat line."""
    pts = [project(lo, la) for lo, la in line]
    verts, tris = [], []
    m = len(pts)
    for i in range(m):
        x, y, z = pts[i]
        a = pts[max(0, i - 1)]; b = pts[min(m - 1, i + 1)]
        tx, tz = b[0] - a[0], b[2] - a[2]
        tl = math.hypot(tx, tz) or 1e-6
        nx, nz = -tz / tl, tx / tl
        verts.append((x + nx * half_w, y + y_off, z + nz * half_w))
        verts.append((x - nx * half_w, y + y_off, z - nz * half_w))
    for i in range(m - 1):
        a0, a1, b0, b1 = 2 * i, 2 * i + 1, 2 * i + 2, 2 * i + 3
        tris.append((a0, a1, b1)); tris.append((a0, b1, b0))
    return {"vertices": verts, "tris": tris}


def _fan(poly, project, y_off):
    pts = [project(lo, la) for lo, la in poly]
    if len(pts) < 3:
        return {"vertices": [], "tris": []}
    pts = [(x, y + y_off, z) for x, y, z in pts]
    return {"vertices": pts, "tris": [(0, i, i + 1) for i in range(1, len(pts) - 1)]}


def _merge(meshes):
    verts, uvs, tris = [], [], []
    has_uv = bool(meshes) and all(m.get("uvs") for m in meshes)
    for m in meshes:
        off = len(verts)
        verts += m["vertices"]
        if has_uv:
            uvs += m["uvs"]
        tris += [(a + off, b + off, c + off) for a, b, c in m["tris"]]
    out = {"vertices": verts, "tris": tris}
    if has_uv:
        out["uvs"] = uvs
    return out


def _poly_area(poly):  # |shoelace| of a [(x,z)...] footprint, in m²
    s = 0.0
    n = len(poly)
    for i in range(n):
        x0, z0 = poly[i]
        x1, z1 = poly[(i + 1) % n]
        s += x0 * z1 - x1 * z0
    return abs(s) / 2.0


def _pip(x, z, poly):  # ray-cast point-in-polygon (poly = [(x,z)...])
    inside = False
    n = len(poly)
    for i in range(n):
        (x1, z1), (x2, z2) = poly[i], poly[(i + 1) % n]
        if (z1 > z) != (z2 > z) and x < (x2 - x1) * (z - z1) / (z2 - z1 + 1e-12) + x1:
            inside = not inside
    return inside


def _corridor_rejecter(loop, widths, extra_lines=(), *, margin=3.0, cell=20.0):
    """Return ``on_road(x, z)`` → True when (x,z) is within (local road half-width + ``margin``) of ANY
    road centreline point — the MAIN loop plus any interior connectors. Spatial-hashed for speed.

    A purely local perpendicular offset (what the scatters use) keeps foliage off the road *next to the
    current point*, but not where the road folds back on itself (the corkscrew) or runs parallel a few
    metres away (the interior grid) — there a 7–40 m sideways throw lands on a DIFFERENT segment. Testing
    against the whole network fixes the 'bushes/trees through the road' clipping for any track."""
    pts = [(loop[i][0], loop[i][2], (widths[i] / 2.0 if i < len(widths) else 4.5)) for i in range(len(loop))]
    for ln, lw in extra_lines:
        for j, p in enumerate(ln):
            pts.append((p[0], p[2], (lw[j] / 2.0 if j < len(lw) else 4.5)))
    buckets = {}
    for px, pz, hw in pts:
        buckets.setdefault((int(px // cell), int(pz // cell)), []).append((px, pz, hw))

    def on_road(x, z):
        ci, cj = int(x // cell), int(z // cell)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for px, pz, hw in buckets.get((ci + di, cj + dj), ()):
                    if math.hypot(x - px, z - pz) < hw + margin:
                        return True
        return False

    return on_road


def _billboard(x, y, z, h=7.0, w=5.5, yaw=0.0):  # crossed tree billboards (two perpendicular cards) + UVs
    hw = w / 2
    c, s = math.cos(yaw), math.sin(yaw)               # per-instance yaw so the cross-quads aren't all
    ax, az = c * hw, s * hw                            # axis-aligned identical clones
    bx, bz = -s * hw, c * hw                           # perpendicular card
    verts = [(x - ax, y, z - az), (x + ax, y, z + az), (x + ax, y + h, z + az), (x - ax, y + h, z - az),
             (x - bx, y, z - bz), (x + bx, y, z + bz), (x + bx, y + h, z + bz), (x - bx, y + h, z - bz)]
    uvs = [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0), (1, 0), (1, 1), (0, 1)]  # V=0 trunk → V=1 canopy
    tris = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)]
    return {"vertices": verts, "uvs": uvs, "tris": tris}


def _billboard_cell(x, y, z, col, row, ncols, nrows, h, w, yaw=0.0):
    """Crossed billboard mapped to one cell of a foliage atlas (ncols x nrows). V=0 (base) → bottom of
    the cell. Used for the dry scrub bushes (one random plant per billboard). yaw varies orientation."""
    hw = w / 2
    c, s = math.cos(yaw), math.sin(yaw)
    ax, az = c * hw, s * hw
    bx, bz = -s * hw, c * hw
    verts = [(x - ax, y, z - az), (x + ax, y, z + az), (x + ax, y + h, z + az), (x - ax, y + h, z - az),
             (x - bx, y, z - bz), (x + bx, y, z + bz), (x + bx, y + h, z + bz), (x - bx, y + h, z - bz)]
    u0, u1 = col / ncols, (col + 1) / ncols
    vb, vt = (nrows - 1 - row) / nrows, (nrows - row) / nrows   # bottom/top of cell (v=0 = image bottom)
    uvs = [(u0, vb), (u1, vb), (u1, vt), (u0, vt), (u0, vb), (u1, vb), (u1, vt), (u0, vt)]
    tris = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)]
    return {"vertices": verts, "uvs": uvs, "tris": tris}


def _streetlight(x, y, z, nx, nz, *, H=9.0, arm=1.6, r=0.14,
                 head_w=0.5, head_l=0.28, head_h=0.28, droop=0.05):
    """A cobra-head streetlight, split into two meshes so ONLY the lamp glows at night:
      - shaft  = crossed vertical mast quads (read from any angle) + a thin horizontal cantilever arm
                 reaching ``arm`` m road-ward along (nx, nz) — the NON-emissive LIGHTPOST material.
      - lamphead = a small down-facing box at the arm tip near y+H — the emissive LIGHTS material that
                 CSP lights up at night (see ext_config LIGHT_SERIES/MATERIAL_ADJUSTMENT_STREETLIGHTS).
    (nx, nz) is the road-normal pointing FROM the pole TOWARD the road, so the arm overhangs the lane."""
    # mast: two crossed quads (mirror the _utility_pole pattern) so the post is visible from any angle
    v = [(x - r, y, z), (x + r, y, z), (x + r, y + H, z), (x - r, y + H, z),            # quad along X
         (x, y, z - r), (x, y, z + r), (x, y + H, z + r), (x, y + H, z - r)]            # quad along Z
    t = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)]
    ax, az = x + nx * arm, z + nz * arm                       # arm tip out over the lane
    px, pz = -nz * 0.08, nx * 0.08                            # thin arm half-width (perp to the arm)
    b = len(v)
    v += [(x + px, y + H, z + pz), (x - px, y + H, z - pz),
          (ax - px, y + H - droop, az - pz), (ax + px, y + H - droop, az + pz)]
    t += [(b, b + 1, b + 2), (b, b + 2, b + 3)]
    shaft = {"vertices": v, "tris": t}
    # lamphead: a small down-facing box hung just under the arm tip
    y1, y0 = y + H - droop, y + H - droop - head_h
    hv = [(ax - head_w, y0, az - head_l), (ax + head_w, y0, az - head_l),
          (ax + head_w, y0, az + head_l), (ax - head_w, y0, az + head_l),
          (ax - head_w, y1, az - head_l), (ax + head_w, y1, az - head_l),
          (ax + head_w, y1, az + head_l), (ax - head_w, y1, az + head_l)]
    ht = [(0, 1, 2), (0, 2, 3),          # bottom (the luminaire lens, faces down)
          (4, 6, 5), (4, 7, 6),          # top
          (0, 4, 5), (0, 5, 1), (1, 5, 6), (1, 6, 2),   # sides
          (2, 6, 7), (2, 7, 3), (3, 7, 4), (3, 4, 0)]
    lamphead = {"vertices": hv, "tris": ht}
    return shaft, lamphead


def _utility_pole(x, y, z, tx, tz, *, H=11.0, ch=9.6, arm=1.4, r=0.16):
    """A wooden power pole: crossed vertical quads (reads from any angle) + a crossarm across the road
    normal. Returns (mesh, [3 wire-attach tips] left/centre/right along the crossarm)."""
    nx, nz = -tz, tx                                          # road normal = crossarm direction
    v = [(x - r, y, z), (x + r, y, z), (x + r, y + H, z), (x - r, y + H, z),            # quad along X
         (x, y, z - r), (x, y, z + r), (x, y + H, z + r), (x, y + H, z - r)]            # quad along Z
    t = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)]
    b = len(v)
    v += [(x - nx * arm, y + ch, z - nz * arm), (x + nx * arm, y + ch, z + nz * arm),   # crossarm board
          (x + nx * arm, y + ch + 0.22, z + nz * arm), (x - nx * arm, y + ch + 0.22, z - nz * arm)]
    t += [(b, b + 1, b + 2), (b, b + 2, b + 3)]
    tips = [(x + nx * o, y + ch, z + nz * o) for o in (-arm, 0.0, arm)]
    return {"vertices": v, "tris": t}, tips


def _wire(p0, p1, *, sag=0.9, w=0.06, segs=4):
    """A thin sagging cable from p0 to p1 as a slim vertical ribbon (parabolic droop)."""
    pts = []
    for k in range(segs + 1):
        f = k / segs
        pts.append((p0[0] + (p1[0] - p0[0]) * f, p0[1] + (p1[1] - p0[1]) * f - sag * 4 * f * (1 - f),
                    p0[2] + (p1[2] - p0[2]) * f))
    v, t = [], []
    for k in range(segs):
        a, c = pts[k], pts[k + 1]
        b = len(v)
        v += [(a[0], a[1], a[2]), (a[0], a[1] + w, a[2]), (c[0], c[1] + w, c[2]), (c[0], c[1], c[2])]
        t += [(b, b + 1, b + 2), (b, b + 2, b + 3)]
    return {"vertices": v, "tris": t}


def _fence_ring(foot, ground_y, *, off=2.5, h=2.3, tile_m=1.6):
    """A chain-link fence around a building footprint, pushed ``off`` m outward from its centroid (a
    fenced industrial yard). Double-sided alpha-cutout quads (CHAINLINK material); ``u`` tiles by edge
    length / ``tile_m`` and ``v`` by height / ``tile_m`` so the diamond stays square at any edge length."""
    cx = sum(p[0] for p in foot) / len(foot)
    cz = sum(p[1] for p in foot) / len(foot)
    ring = []
    for x, z in foot:
        dx, dz = x - cx, z - cz
        d = math.hypot(dx, dz) or 1e-6
        ring.append((x + dx / d * off, z + dz / d * off))
    v, uv, t = [], [], []
    vt = h / tile_m
    for i in range(len(ring)):
        x0, z0 = ring[i]
        x1, z1 = ring[(i + 1) % len(ring)]
        seg = math.hypot(x1 - x0, z1 - z0)
        if seg < 0.5:
            continue
        u = seg / tile_m
        y0, y1 = ground_y(x0, z0), ground_y(x1, z1)
        b = len(v)
        v += [(x0, y0, z0), (x1, y1, z1), (x1, y1 + h, z1), (x0, y0 + h, z0)]
        uv += [(0.0, 0.0), (u, 0.0), (u, vt), (0.0, vt)]
        t += [(b, b + 1, b + 2), (b, b + 2, b + 3), (b, b + 2, b + 1), (b, b + 3, b + 2)]  # double-sided
    return {"vertices": v, "uvs": uv, "tris": t}


def _container_box(cx, cz, gy, yaw, *, L=12.2, H=5.2, D=2.5):
    """A stacked-shipping-container box (industrial-yard prop). Long faces map the FULL container atlas
    (rows of weathered containers); short ends a single-container slice. DOUBLE-SIDED so it renders
    regardless of the mirror_x winding flip (no need to track it for a handful of boxes)."""
    c, s = math.cos(yaw), math.sin(yaw)
    hl, hd = L / 2.0, D / 2.0

    def R(dx, dz, y):
        return (cx + dx * c - dz * s, y, cz + dx * s + dz * c)

    V = [R(-hl, -hd, gy), R(hl, -hd, gy), R(hl, hd, gy), R(-hl, hd, gy),
         R(-hl, -hd, gy + H), R(hl, -hd, gy + H), R(hl, hd, gy + H), R(-hl, hd, gy + H)]
    verts, uvs, tris = [], [], []

    def face(a, b, cc, d, uv):
        n = len(verts)
        verts.extend([V[a], V[b], V[cc], V[d]])
        uvs.extend(uv)
        tris.extend([(n, n + 1, n + 2), (n, n + 2, n + 3),
                     (n, n + 2, n + 1), (n, n + 3, n + 2)])   # both windings → double-sided
    A = [(0, 0), (1, 0), (1, 1), (0, 1)]            # long faces: full stacked-container atlas
    E = [(0, 0), (0.28, 0), (0.28, 1), (0, 1)]      # short ends: one-container slice
    T = [(0, 0.82), (1, 0.82), (1, 1), (0, 1)]      # top: thin slice
    face(0, 1, 5, 4, A); face(2, 3, 7, 6, A)
    face(1, 2, 6, 5, E); face(3, 0, 4, 7, E)
    face(4, 5, 6, 7, T)
    return {"vertices": verts, "uvs": uvs, "tris": tris}


def _resample_xz(pts, step=8.0):
    """Resample a polyline of (x,y,z) to ~step spacing in the X-Z plane (y zeroed)."""
    seg = [0.0]
    for i in range(1, len(pts)):
        seg.append(seg[-1] + math.hypot(pts[i][0] - pts[i - 1][0], pts[i][2] - pts[i - 1][2]))
    total = seg[-1]
    if total < step:
        return pts
    n = max(2, int(total / step))
    out, si = [], 1
    for s in range(n + 1):
        d = total * s / n
        while si < len(pts) - 1 and seg[si] < d:
            si += 1
        a, b = pts[si - 1], pts[si]
        den = (seg[si] - seg[si - 1]) or 1.0
        t = (d - seg[si - 1]) / den
        out.append((a[0] + (b[0] - a[0]) * t, 0.0, a[2] + (b[2] - a[2]) * t))
    return out


def _build_highways(env, project, ground_y, on_track=None, *, half_w=11.0, parapet=0.85, pier_w=1.4,
                    pier_step=24.0, min_len=240.0, smooth_win=22, bridge_gap=2.5):
    """Re-build the I-70 / US-36 decks ALONGSIDE the route from real OSM geometry. The deck rides the
    terrain crests but stays LEVEL over the dips, carried on PIERS (bridges) — instead of the old flat
    +5 m deck that floated over everything. ``on_track(x, z)`` (when given) marks the drivable corridor:
    deck points falling inside it are clipped out and a deck is rebuilt for each surviving run still long
    enough to read as a highway — so frontage roads/ramps tagged ``highway`` that hug the route don't lay
    a 22 m slab on the street. Returns (deck[asphalt], struct[concrete parapets+slab+piers])."""
    dv, duv, dt = [], [], []
    sv, suv, stt = [], [], []

    def quad(V, UV, T, a, b, c, d, uvs):
        base = len(V); V += [a, b, c, d]; UV += uvs
        T += [(base, base + 1, base + 2), (base, base + 2, base + 3)]

    def column(cx, cz, y0, y1, w):
        h2 = w / 2.0
        c = [(cx - h2, cz - h2), (cx + h2, cz - h2), (cx + h2, cz + h2), (cx - h2, cz + h2)]
        base = len(sv)
        for bx, bz in c:
            sv.append((bx, y0, bz)); suv.append((0, 0))
        for bx, bz in c:
            sv.append((bx, y1, bz)); suv.append((0, 1))
        for i in range(4):
            j = (i + 1) % 4
            stt.extend([(base + i, base + j, base + j + 4), (base + i, base + j + 4, base + i + 4)])

    def emit_deck(pts):
        terr = [ground_y(x, z) for x, _, z in pts]
        sm = [sum(terr[max(0, i - smooth_win):i + smooth_win + 1]) /
              len(terr[max(0, i - smooth_win):i + smooth_win + 1]) for i in range(len(terr))]
        deck_e = [max(sm[i], terr[i] + 0.2) for i in range(len(terr))]   # ride crests, bridge dips
        edges = []
        for i in range(len(pts)):
            x, _, z = pts[i]
            a, b = pts[max(0, i - 1)], pts[min(len(pts) - 1, i + 1)]
            tx, tz = b[0] - a[0], b[2] - a[2]; tl = math.hypot(tx, tz) or 1.0
            nx, nz = -tz / tl, tx / tl
            de = deck_e[i]
            edges.append(((x + nx * half_w, de, z + nz * half_w), (x - nx * half_w, de, z - nz * half_w),
                          (x, de, z), terr[i], nx, nz))
        for k in range(len(edges) - 1):
            L0, R0, C0, t0, _, _ = edges[k]
            L1, R1, C1, t1, _, _ = edges[k + 1]
            quad(dv, duv, dt, L0, R0, R1, L1, [(0, k), (1, k), (1, k + 1), (0, k + 1)])          # deck top
            bL0 = (L0[0], L0[1] - 0.7, L0[2]); bR0 = (R0[0], R0[1] - 0.7, R0[2])
            bL1 = (L1[0], L1[1] - 0.7, L1[2]); bR1 = (R1[0], R1[1] - 0.7, R1[2])
            quad(sv, suv, stt, bR0, bL0, bL1, bR1, [(0, 0), (1, 0), (1, 1), (0, 1)])             # underside
            quad(sv, suv, stt, L0, (L0[0], L0[1] + parapet, L0[2]),
                 (L1[0], L1[1] + parapet, L1[2]), L1, [(0, 0), (0, 1), (1, 1), (1, 0)])          # L parapet
            quad(sv, suv, stt, R1, (R1[0], R1[1] + parapet, R1[2]),
                 (R0[0], R0[1] + parapet, R0[2]), R0, [(0, 0), (0, 1), (1, 1), (1, 0)])          # R parapet
        acc = pier_step
        for k in range(len(edges)):
            if k > 0:
                acc += math.hypot(edges[k][2][0] - edges[k - 1][2][0], edges[k][2][2] - edges[k - 1][2][2])
            _, _, C, t, _, _ = edges[k]
            if C[1] - t > bridge_gap and acc >= pier_step:
                acc = 0.0
                column(C[0], C[2], t - 0.4, C[1] - 0.6, pier_w)                                  # bridge pier

    def _run_len(run):
        return sum(math.hypot(run[i][0] - run[i - 1][0], run[i][2] - run[i - 1][2])
                   for i in range(1, len(run)))

    for f in env:
        if f.get("cls") != "highway" or f.get("kind") != "line" or len(f.get("coords", [])) < 2:
            continue
        raw = [project(lo, la) for lo, la in f["coords"]]
        if sum(math.hypot(raw[i][0] - raw[i - 1][0], raw[i][2] - raw[i - 1][2])
               for i in range(1, len(raw))) < min_len:
            continue
        pts = _resample_xz(raw)
        # split the resampled centerline into runs CLEAR of the drivable corridor; drop on-track points
        if on_track is None:
            runs = [pts]
        else:
            runs, cur = [], []
            for p in pts:
                if on_track(p[0], p[2]):
                    if len(cur) >= 2:
                        runs.append(cur)
                    cur = []
                else:
                    cur.append(p)
            if len(cur) >= 2:
                runs.append(cur)
        for run in runs:                      # only a run still long enough to read as a highway
            if _run_len(run) >= min_len:
                emit_deck(run)
    return {"vertices": dv, "uvs": duv, "tris": dt}, {"vertices": sv, "uvs": suv, "tris": stt}


def _box_column(cx, cz, y0, y1, w):
    """A square concrete column from y0 to y1 (4 sides). Returns (verts, tris) to append into a mesh."""
    h = w / 2.0
    c = [(cx - h, cz - h), (cx + h, cz - h), (cx + h, cz + h), (cx - h, cz + h)]
    verts = [(x, y0, z) for x, z in c] + [(x, y1, z) for x, z in c]
    tris = []
    for i in range(4):
        j = (i + 1) % 4
        tris += [(i, j, j + 4), (i, j + 4, i + 4)]
    return verts, tris


def _creek_bridge(loop, widths, env, project, ground_y, *, parapet=1.0, thick=0.7, pad=4):
    """Dress every actual creek crossing as a bridge: concrete parapets along both road edges, an
    underside slab, and piers down into the water. Sand Creek is crossed twice — at E 56th and at Quebec.
    The drivable road surface stays (it's the track mesh); this is the structure around/under it. Returns
    a concrete mesh."""
    # Densify the water polylines so the nearest-point test below approximates PERPENDICULAR distance to
    # the creek LINE, not to its (sparsely sampled) vertices. Each crossing sits ~0.9 m from the line but
    # up to ~23 m from the nearest mapped vertex, so a bare-vertex test bridged only Quebec and missed 56th.
    wp = []
    for f in env:
        if f.get("cls") != "water":
            continue
        cs = [project(lo, la) for lo, la in f.get("coords", [])]
        for a, b in zip(cs, cs[1:]):
            seg = math.hypot(b[0] - a[0], b[2] - a[2])
            steps = max(1, int(seg / 4.0))
            for k in range(steps + 1):
                t = k / steps
                wp.append((a[0] + (b[0] - a[0]) * t, 0.0, a[2] + (b[2] - a[2]) * t))
    from collections import defaultdict
    C = 40.0
    wb = defaultdict(list)
    for x, _, z in wp:
        wb[(int(x // C), int(z // C))].append((x, z))

    def near_w(x, z):
        ci, cj = int(x // C), int(z // C); best = 1e18
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for wx, wz in wb.get((ci + di, cj + dj), ()):
                    best = min(best, (x - wx) ** 2 + (z - wz) ** 2)
        return math.sqrt(best)

    n = len(loop)
    flag = [near_w(loop[i][0], loop[i][2]) < widths[i] / 2 + 8 for i in range(n)]
    spans, i = [], 0
    while i < n:
        if flag[i]:
            j = i
            while j < n and flag[j]:
                j += 1
            spans.append((max(1, i - pad), min(n - 1, j + pad))); i = j
        else:
            i += 1
    V, UV, T = [], [], []

    def quad(a, b, c, d):
        base = len(V); V.extend([a, b, c, d]); UV.extend([(0, 0), (1, 0), (1, 1), (0, 1)])
        T.extend([(base, base + 1, base + 2), (base, base + 2, base + 3)])

    for a, b in spans:
        rows = []
        for k in range(a, b + 1):
            x, y, z = loop[k]
            pa, pb = loop[max(0, k - 1)], loop[min(n - 1, k + 1)]
            tx, tz = pb[0] - pa[0], pb[2] - pa[2]; tl = math.hypot(tx, tz) or 1.0
            nx, nz = -tz / tl, tx / tl
            hw = widths[k] / 2 + 0.3
            rows.append(((x + nx * hw, y + ROAD_LIFT_M, z + nz * hw),
                         (x - nx * hw, y + ROAD_LIFT_M, z - nz * hw), (x, y, z)))
        for k in range(len(rows) - 1):
            L0, R0, _ = rows[k]; L1, R1, _ = rows[k + 1]
            quad(L0, (L0[0], L0[1] + parapet, L0[2]), (L1[0], L1[1] + parapet, L1[2]), L1)   # L rail
            quad(R1, (R1[0], R1[1] + parapet, R1[2]), (R0[0], R0[1] + parapet, R0[2]), R0)   # R rail
            bL0 = (L0[0], L0[1] - thick, L0[2]); bR0 = (R0[0], R0[1] - thick, R0[2])
            bL1 = (L1[0], L1[1] - thick, L1[2]); bR1 = (R1[0], R1[1] - thick, R1[2])
            quad(bR0, bL0, bL1, bR1)                                                          # underside
        for frac in (0.33, 0.66):                                                            # piers in the water
            x, y, z = loop[int(a + (b - a) * frac)]
            cv, ct = _box_column(x, z, ground_y(x, z) - 0.6, y - thick, 1.1)
            base = len(V); V.extend(cv); UV.extend([(0, 0)] * len(cv)); T.extend([(base + p, base + q, base + r) for p, q, r in ct])
    return {"vertices": V, "uvs": UV, "tris": T}


def build(project_dir: str | Path) -> dict:
    project_dir = Path(project_dir)
    data = project_dir / "data"
    cfg = load_config(project_dir)
    local = json.loads((data / "centerline.local.json").read_text())
    origin = (local["origin"]["lon"], local["origin"]["lat"])
    elev0 = local["origin"]["elev_m"]
    loop = [tuple(p) for p in local["points_xyz_m"]]
    widths = local["widths_m"]

    # REAL ELEVATION (must match build_mesh): correct the road-surface Y from a Prodrive Scan capture
    # where it confidently diverges from the heightfield, so the terrain conforms to the real road.
    # No capture → loop unchanged (byte-identical). Applied pre-mirror (Y only; mirror is X-only).
    cap = evidence.load_capture(project_dir)
    if cap is not None and evidence.use_capture_elevation(project_dir):
        loop, _estats = evidence.corrected_elevation(loop, cap, bridges=evidence.bridge_spans(project_dir))

    # MIRROR_X (must match build_mesh): mirror the east axis of everything so the scenery lines up with
    # the mirrored track. Visual mesh winding is reversed at the end (a mirror flips it inside-out).
    mirror_x = bool(cfg.raw.get("mirror_x", False))
    sx = -1.0 if mirror_x else 1.0

    # Run the SAME centerline pipeline as the shipped road (corner-round + mirror + bake the corkscrew
    # dip). Without this the scenery anchored to the raw loop — poles/signs/bridge floated ~2 m over the
    # dipped road through the corkscrew. dip_of feeds the terrain bowl below.
    loop, dip_of, _bank_at, profile_active = finished_centerline(loop, cfg.raw, mirror_x=mirror_x)

    grid = read_npy(data / "heightfield.npy")
    meta = json.loads((data / "heightfield.meta.json").read_text())
    grid, meta = ribbon.upsample_grid(grid, meta, 3)  # 40 m → ~13 m so grading hugs the road cleanly
    m_lon, m_lat = _meters_per_degree(origin[1])

    # GROUND HEIGHT for scenery: prefer the CONFORMED grass surface (data/ground.local.json, written by
    # build_mesh AFTER conform_terrain_to_road + clamp_terrain_below_road) so poles/trees/signs/buildings
    # stand on the surface that actually renders — not the raw bare-earth DEM, which near the track sits
    # ~0.5-few m off after the road conform (the "objects not flush with the ground" defect). Falls back
    # to the raw heightfield sampler if the mesh stage hasn't run yet, so build_env alone still works.
    gl_path = data / "ground.local.json"
    if gl_path.exists():
        _gl = json.loads(gl_path.read_text())
        gx0, gz0, gdx, gdz, gnx, gny, GY = (_gl["x0"], _gl["z0"], _gl["dx"], _gl["dz"],
                                            _gl["nx"], _gl["ny"], _gl["y"])

        def ground_y(x, z):
            """Conformed-ground height under a LOCAL (mirrored) x,z, bilinear on the shipped grass grid."""
            fi = (x - gx0) / gdx if gdx else 0.0
            fj = (z - gz0) / gdz if gdz else 0.0
            i0 = max(0, min(gnx - 1, int(fi))); j0 = max(0, min(gny - 1, int(fj)))
            i1 = min(gnx - 1, i0 + 1); j1 = min(gny - 1, j0 + 1)
            ti = max(0.0, min(1.0, fi - i0)); tj = max(0.0, min(1.0, fj - j0))
            a = GY[j0][i0] * (1 - ti) + GY[j0][i1] * ti
            b = GY[j1][i0] * (1 - ti) + GY[j1][i1] * ti
            return a * (1 - tj) + b * tj

        def y_at(lon, lat):
            return ground_y(sx * (lon - origin[0]) * m_lon, (lat - origin[1]) * m_lat)
        print("  [build_env] scenery height = conformed ground surface (ground.local.json)")
    else:
        y_at = _grid_sampler(grid, meta, elev0)

        def ground_y(x, z):
            """Terrain height under a LOCAL (mirrored) x,z — inverts the projection (incl. mirror) before
            sampling the heightfield, so scattered foliage sits on the real ground, not its mirror image."""
            return y_at(origin[0] + sx * x / m_lon, origin[1] + z / m_lat)
        print("  [build_env] WARNING: ground.local.json missing — scenery on RAW DEM (run mesh stage first)")

    def project(lon, lat):
        return (sx * (lon - origin[0]) * m_lon, y_at(lon, lat), (lat - origin[1]) * m_lat)

    # environment.geojson (OSM buildings/water/landuse zones) is OPTIONAL: an aerial- or GPS-sourced
    # track (e.g. a private complex) has no OSM zones to fetch. Absent → no scenery zones, not a crash.
    env_path = data / "environment.geojson"
    env = json.loads(env_path.read_text())["features"] if env_path.exists() else []
    if not env:
        print("[build_env] no environment.geojson — building terrain/foliage only, no OSM zones")
    gj = json.loads((data / "centerline.geojson").read_text())["features"]

    # --- track + interior connector ---
    grid_xyz = project_grid(grid, meta, origin, elev0, mirror_x=mirror_x)
    if profile_active:   # sink the terrain into the corkscrew bowl (match build_mesh) before grading
        profile_mod.apply_dip_bowl(grid_xyz, loop, dip_of)
    ribbon.conform_terrain_to_road(grid_xyz, loop, widths)  # grade terrain to the (dipped) road
    grass = ribbon.grass_terrain(grid_xyz)
    road = ribbon.road_ribbon(loop, widths)
    road["vertices"] = [(x, y + ROAD_LIFT_M, z) for x, y, z in road["vertices"]]
    kerb = kerbs.corner_kerbs(loop, widths)
    kerb["vertices"] = [(x, y + ROAD_LIFT_M + 0.02, z) for x, y, z in kerb["vertices"]]
    # connector "side roads" DISABLED — they were bare raised ribbons with no shoulders, reading as
    # concrete slabs jutting out of the ground, and aren't part of the drivable lap. (If interior
    # connectors become real layout variants later, they need shoulders + terrain blend like 1ROAD.)
    connector = {"vertices": [], "uvs": [], "tris": []}

    # --- water (Sand Creek lines + basins) ---
    water = _merge([_line_ribbon(f["coords"], project, 4.0, -0.3) if f["kind"] == "line"
                    else _fan(f["coords"], project, -0.3)
                    for f in env if f["cls"] == "water"])
    # connector centerlines (interior-grid layout roads) in the mirrored frame — feed the road-corridor
    # rejecters below (scatter exclusion + highway-deck clipping).
    connector_lines = []
    cpath = data / "connectors.local.json"
    if cpath.exists():
        for c in json.loads(cpath.read_text()).get("connectors", []):
            cp = c.get("points_xyz_m", [])
            cw = c.get("widths_m", [])
            if mirror_x:
                cp = [(-p[0], p[1], p[2]) for p in cp]
            connector_lines.append((cp, cw))

    # --- highways: I-70 / US-36 decks ALONGSIDE the route (real OSM geometry), riding the terrain crests
    #     but staying LEVEL over the dips on piers. hwy_struct = parapets + underside slab + piers. CLIP
    #     every deck OUT of the drivable corridor: many OSM "highway" ways here are frontage roads / ramps
    #     that run right on top of 46th (one is 3 m from the lane), and a 22 m-wide deck there reads as
    #     concrete cruft sitting on the street. margin 16 m clears the deck EDGES too (deck half-width is
    #     11 m, so a centreline kept at the corridor edge still throws an edge ~5 m onto the road at a
    #     smaller margin). The real I-70 mainline runs farther off and keeps its backdrop.
    hwy_corridor = _corridor_rejecter(loop, widths, connector_lines, margin=16.0)
    hwy_deck, hwy_struct = _build_highways(env, project, ground_y, on_track=hwy_corridor)
    # creek bridges (E 56th + Quebec over Sand Creek): parapets + underside + piers at the crossings.
    bridge = _creek_bridge(loop, widths, env, project, ground_y)

    # --- scenery knobs (per-track, config-driven; defaults reproduce the original Sand Creek look) and
    #     the road-corridor index that keeps every scatter OFF the asphalt for ANY track ---
    import random
    scn = cfg.raw.get("scenery", {})
    on_road = _corridor_rejecter(loop, widths, connector_lines,
                                 margin=float(scn.get("corridor_margin_m", 3.0)))
    # Verge furniture (streetlights/poles/signs) is placed deliberately CLOSE to the road edge — inside
    # the foliage keep-back. Gate it with a tight on-SURFACE test instead: drop a placement only when it
    # lands on the actual pavement of a DIFFERENT road (a fold-back/crossing leg at a junction), never on
    # the edge of the road it hugs (its own offset of 1.5–2 m always clears this 0.3 m margin).
    on_surface = _corridor_rejecter(loop, widths, connector_lines, margin=0.3)

    # --- trees scattered in green/wetland polygons (capped) ---
    tree_meshes, ntrees = [], 0
    _tg = random.Random(55)
    t_step = float(scn.get("tree_step_m", 28.0))
    t_cap = int(scn.get("tree_cap", 700))
    t_ac = int(scn.get("tree_atlas_cols", 2))     # TREES atlas grid (mined Colorado trees4 = 2x2)
    t_ar = int(scn.get("tree_atlas_rows", 2))
    for f in env:
        if f["cls"] not in ("green", "wetland") or f["kind"] != "poly" or len(f["coords"]) < 3:
            continue
        poly = [(project(lo, la)[0], project(lo, la)[2]) for lo, la in f["coords"]]
        xs = [p[0] for p in poly]; zs = [p[1] for p in poly]
        step = t_step
        gx = int((max(xs) - min(xs)) / step)
        gz = int((max(zs) - min(zs)) / step)
        for ix in range(gx + 1):
            for iz in range(gz + 1):
                x = min(xs) + ix * step + (iz % 2) * 9
                z = min(zs) + iz * step
                if ntrees < t_cap and _pip(x, z, poly) and not on_road(x, z):
                    h = 5.5 + _tg.random() * 4.0                  # 5.5..9.5 m, varied
                    tree_meshes.append(_billboard_cell(x, ground_y(x, z), z,
                                                       _tg.randint(0, t_ac - 1), _tg.randint(0, t_ar - 1),
                                                       t_ac, t_ar, h, h * 0.85, yaw=_tg.random() * math.pi))
                    ntrees += 1

    # DENSE riparian trees along Sand Creek (the green corridor — cottonwoods/willows on the banks).
    import random as _trnd
    _tr = _trnd.Random(77)
    nriver = 0
    for f in env:
        if f.get("cls") != "water" or f.get("kind") != "line" or len(f.get("coords", [])) < 2:
            continue
        pls = [project(lo, la) for lo, la in f["coords"]]
        for i in range(1, len(pls)):
            x0, _, z0 = pls[i - 1]; x1, _, z1 = pls[i]
            seg = math.hypot(x1 - x0, z1 - z0)
            tx, tz = (x1 - x0) / (seg or 1), (z1 - z0) / (seg or 1)
            nx, nz = -tz, tx
            for s in range(max(1, int(seg / 8))):           # ~ every 8 m along the bank
                t = s / max(1, int(seg / 8))
                bx, bz = x0 + (x1 - x0) * t, z0 + (z1 - z0) * t
                for side in (1.0, -1.0):
                    if _tr.random() > 0.8:                  # dense but not a solid wall
                        continue
                    off = 5.0 + _tr.random() * 18.0         # 5..23 m off the water, on the banks
                    rx, rz = bx + nx * off * side, bz + nz * off * side
                    if on_road(rx, rz):                     # the road crosses the creek (bridge) — keep off it
                        continue
                    h = 6.0 + _tr.random() * 5.0            # 6..11 m cottonwoods
                    tree_meshes.append(_billboard_cell(rx, ground_y(rx, rz), rz,
                                                       _tr.randint(0, t_ac - 1), _tr.randint(0, t_ar - 1),
                                                       t_ac, t_ar, h, h * 0.85, yaw=_tr.random() * math.pi))
                    nriver += 1
    trees = _merge(tree_meshes)

    # --- dry scrub bushes scattered trackside (the dry-prairie look: sparse low shrubs on dirt) ---
    _r = random.Random(1234)                        # seeded → reproducible
    bush_meshes, nbush = [], 0
    NC, NR = 3, 3                                    # bo_bushes_11 atlas = 3x3 dry plants
    b_spacing = float(scn.get("bush_spacing_m", 6.0))
    b_density = float(scn.get("bush_density", 0.55))
    b_off_min = float(scn.get("bush_off_min_m", 7.0))
    b_off_span = float(scn.get("bush_off_span_m", 33.0))
    b_sz_min = float(scn.get("bush_size_min_m", 1.2))
    b_sz_span = float(scn.get("bush_size_span_m", 1.4))
    acc = 0.0
    for i in range(1, len(loop)):
        acc += math.hypot(loop[i][0] - loop[i - 1][0], loop[i][2] - loop[i - 1][2])
        if acc < b_spacing:
            continue
        acc = 0.0
        x, y, z = loop[i]
        a, b = loop[i - 1], loop[min(len(loop) - 1, i + 1)]
        tx, tz = b[0] - a[0], b[2] - a[2]
        tl = math.hypot(tx, tz) or 1e-6
        nx, nz = -tz / tl, tx / tl
        for side in (1.0, -1.0):
            if _r.random() > b_density:             # sparse — dry prairie, not a hedge
                continue
            off = widths[i] / 2 + b_off_min + _r.random() * b_off_span   # off the road, out on the grass
            bx, bz = x + nx * off * side, z + nz * off * side
            if on_road(bx, bz):                     # reject where the throw lands on a fold-back/parallel road
                continue
            by = ground_y(bx, bz) - 0.4             # seat into the ground (correct height even when mirrored)
            sz = b_sz_min + _r.random() * b_sz_span
            bush_meshes.append(_billboard_cell(bx, by, bz, _r.randint(0, NC - 1), _r.randint(0, NR - 1),
                                               NC, NR, sz * 0.78, sz, yaw=_r.random() * math.pi))
            nbush += 1
    bushes = _merge(bush_meshes)

    # --- streetlights along the lap (~ every 48 m, on the right verge) ---
    # Split geometry: the mast (LIGHTPOST, dark) and the lamp head (LIGHTS, emissive) are separate meshes
    # so only the head glows at night — not the whole 9 m stick (the old single-quad LIGHTS defect).
    lightpost_meshes, lighthead_meshes, nlights = [], [], 0
    l_spacing = float(scn.get("light_spacing_m", 48.0))
    acc = 0.0
    for i in range(1, len(loop)):
        acc += math.hypot(loop[i][0] - loop[i - 1][0], loop[i][2] - loop[i - 1][2])
        if acc >= l_spacing:
            acc = 0.0
            x, y, z = loop[i]
            a, b = loop[i - 1], loop[min(len(loop) - 1, i + 1)]
            tx, tz = b[0] - a[0], b[2] - a[2]
            tl = math.hypot(tx, tz) or 1e-6
            nx, nz = -tz / tl, tx / tl
            off = widths[i] / 2 + 2.0
            px, pz = x - nx * off, z - nz * off
            if on_surface(px, pz):                     # verge offset landed on a crossing/fold-back road — skip
                continue
            # Seat on the CONFORMED ground at the pole's OWN verge location — not the road centerline y,
            # which left the base floating ~0.25 m+ over the clamped grass beside the road (and more where
            # the verge slopes off). trees/bushes already use ground_y; poles now match.
            shaft, lamphead = _streetlight(px, ground_y(px, pz), pz, nx, nz)   # arm reaches +n back over the lane
            lightpost_meshes.append(shaft)
            lighthead_meshes.append(lamphead)
            nlights += 1
    lightposts = _merge(lightpost_meshes)
    lightheads = _merge(lighthead_meshes)

    # --- overhead power lines (poles + sagging cables) — ubiquitous in industrial Commerce City; the
    #     real-world capture shows them in nearly every frame. Poles on the LEFT verge (streetlights are
    #     on the right), cables strung pole-to-pole down the lap. ---
    pole_meshes, wire_meshes, npole = [], [], 0
    pl_spacing = float(scn.get("powerline_spacing_m", 52.0))
    prev_tips, acc = None, 0.0
    for i in range(1, len(loop)):
        acc += math.hypot(loop[i][0] - loop[i - 1][0], loop[i][2] - loop[i - 1][2])
        if acc < pl_spacing:
            continue
        acc = 0.0
        x, y, z = loop[i]
        a, b = loop[i - 1], loop[min(len(loop) - 1, i + 1)]
        tx, tz = b[0] - a[0], b[2] - a[2]
        tl = math.hypot(tx, tz) or 1e-6
        tx, tz = tx / tl, tz / tl
        nx, nz = -tz, tx
        off = widths[i] / 2 + 3.5                      # left verge, just beyond the streetlight line
        bx, bz = x + nx * off, z + nz * off
        if on_surface(bx, bz):                         # base would sit on a crossing/fold-back road — skip the
            continue                                   # pole (the wire then spans to the next kept pole, i.e. an
                                                       # overhead crossing — realistic, and wires are visual-only)
        pole, tips = _utility_pole(bx, ground_y(bx, bz), bz, tx, tz)   # seat on conformed ground, not road-centreline y
        pole_meshes.append(pole)
        npole += 1
        if prev_tips is not None:
            for t0, t1 in zip(prev_tips, tips):
                wire_meshes.append(_wire(t0, t1))
        prev_tips = tips
    poles = _merge(pole_meshes)
    wires = _merge(wire_meshes)

    # --- Front Range backdrop: hazy ridges far to the (real) WEST. Placed by real longitude so the
    #     projection + mirror drop it at true west in-game, and the physical CSP sun (true-north, real
    #     lat/lon) then SETS BEHIND it. Flat double-sided silhouettes; CSP aerial haze blurs them. ---
    import random as _mrnd
    _mr = _mrnd.Random(9)
    mnt_v, mnt_t = [], []
    for layer, (dlon, base_h, amp) in enumerate([(-0.10, 560.0, 240.0), (-0.15, 920.0, 340.0)]):
        wlon = origin[0] + dlon                      # west of the origin
        prev = None
        for i in range(81):
            lat = origin[1] - 0.11 + 0.22 * i / 80
            x = sx * (wlon - origin[0]) * m_lon       # mirror-aware: ends up at true west in-game
            z = (lat - origin[1]) * m_lat
            h = base_h + amp * (0.5 * math.sin(i * 0.4 + layer) + 0.3 * math.sin(i * 1.3 + layer * 2)
                                + 0.2 * (2 * _mr.random() - 1))
            b = len(mnt_v)
            mnt_v.append((x, -200.0, z)); mnt_v.append((x, max(250.0, h), z))
            if prev is not None:
                p0, p1 = prev
                mnt_t += [(p0, p1, b + 1), (p0, b + 1, b), (p0, b + 1, p1), (p0, b, b + 1)]  # double-sided
            prev = (b, b + 1)
    mountains = {"vertices": mnt_v, "tris": mnt_t}

    # --- buildings from OSM footprints (extruded boxes: walls + corrugated roofs) ---
    bld_path = data / "buildings.geojson"
    bld = json.loads(bld_path.read_text())["buildings"] if bld_path.exists() else []
    loop_xz = [(p[0], p[2]) for p in loop[::4]]
    # Commerce City reads as industrial: big footprints = metal WAREHOUSES (corrugated skin), smaller
    # ones = concrete/commercial. Splitting the walls by footprint area gives that variety instead of a
    # field of identical grey boxes. Bury the base 2.5 m so boxes never float off sloped ground.
    # Commercial (smaller) buildings get VARIED façades — tilt-up concrete / brick / stucco (all mined
    # Hamburg) — picked deterministically per footprint, so the build is reproducible and you don't get a
    # field of identical grey boxes. Big footprints stay metal/concrete WAREHOUSES.
    COMMERCIAL = ["BUILDINGS", "BRICK", "STUCCO"]   # group name -> pbr prefix (BUILDING/BRICK/STUCCO)
    WAREHOUSES = ["WAREHOUSE", "WHMETAL"]           # weathered concrete + corrugated metal (real C.C. mix)
    nbld = 0
    commercial = {g: [] for g in COMMERCIAL}
    warehouse = {g: [] for g in WAREHOUSES}
    roof = {"ROOFS": [], "RFMETAL": []}             # PVC flat roofs; metal sheds get corrugated roofs
    fence_meshes = []                                # chain-link around the big (warehouse) yards
    for b in bld:
        foot = [(project(lo, la)[0], project(lo, la)[2]) for lo, la in b["coords"]]
        if len(foot) < 3 or _poly_area(foot) < 35:  # skip tiny sheds / noise
            continue
        cx = sum(p[0] for p in foot) / len(foot)
        cz = sum(p[1] for p in foot) / len(foot)
        if min(math.hypot(cx - lx, cz - lz) for lx, lz in loop_xz) > 330:  # keep near-lap only
            continue
        # ...but never ON the track: drop any building whose footprint crowds the drivable surface
        # (within 16 m of the racing line) so buildings don't sit over the road.
        if min(math.hypot(fx - lx, fz - lz) for fx, fz in foot for lx, lz in loop_xz) < 16.0:
            continue
        bury = 2.5
        base = min(y_at(lo, la) for lo, la in b["coords"]) - bury  # sink so it meets the ground on slopes
        box = buildings.extrude(foot, base, b["height_m"] + bury)   # +bury so the roof stays at true height
        if _poly_area(foot) > 700:
            g = WAREHOUSES[int(abs(round(cx) * 5 + round(cz) * 11)) % len(WAREHOUSES)]
            warehouse[g].append(box["walls"])
            roof["RFMETAL" if g == "WHMETAL" else "ROOFS"].append(box["roof"])  # metal shed -> metal roof
            fence_meshes.append(_fence_ring(foot, ground_y))                    # fenced industrial yard
        else:
            g = COMMERCIAL[int(abs(round(cx) * 7 + round(cz) * 13)) % len(COMMERCIAL)]
            commercial[g].append(box["walls"])
            roof["ROOFS"].append(box["roof"])
        nbld += 1
    bld_comm = {g: _merge(v) for g, v in commercial.items()}
    bld_wh = {g: _merge(v) for g, v in warehouse.items()}
    bld_roof = {k: _merge(v) for k, v in roof.items()}
    fences = _merge(fence_meshes)

    # --- shipping-container stacks in the warehouse yards (industrial Commerce City character) ---
    _rc = random.Random(99)
    cont_meshes, ncont = [], 0
    cont_cap = int(scn.get("container_cap", 60))
    for b in bld:
        if ncont >= cont_cap:
            break
        foot = [(project(lo, la)[0], project(lo, la)[2]) for lo, la in b["coords"]]
        if len(foot) < 3 or _poly_area(foot) < 700:      # warehouses only (big footprints get yards)
            continue
        cxf = sum(p[0] for p in foot) / len(foot)
        czf = sum(p[1] for p in foot) / len(foot)
        if min(math.hypot(cxf - lx, czf - lz) for lx, lz in loop_xz) > 330:
            continue
        ext = math.sqrt(_poly_area(foot))
        for _ in range(_rc.randint(1, 3)):               # a small cluster beside each warehouse
            ang = _rc.random() * 2 * math.pi
            r = ext * 0.5 + 6.0 + _rc.random() * 14.0
            px, pz = cxf + math.cos(ang) * r, czf + math.sin(ang) * r
            if on_road(px, pz):
                continue
            cont_meshes.append(_container_box(px, pz, ground_y(px, pz) - 0.1, _rc.random() * math.pi,
                                              L=_rc.choice([6.1, 12.2]), H=_rc.choice([2.6, 5.2, 5.2])))
            ncont += 1
    containers = _merge(cont_meshes)

    # --- street-name blade signs at each major junction, from the OSM street-label map
    #     (data/street_labels.json) — the SAME leg source the painted road decals use, so the signs
    #     name exactly the turns the pavement lettering does. UVs follow road_text's proven layout;
    #     flip_read/flip_up in scenery.signs are one-bit fixes if text reads mirrored in-game. ---
    signs_panels = {"vertices": [], "uvs": [], "tris": []}
    signposts = {"vertices": [], "tris": []}
    nsign = 0
    sg_cfg = scn.get("signs", {})
    labels_path = data / "street_labels.json"
    if sg_cfg.get("enabled", True) and labels_path.exists():
        sl = json.loads(labels_path.read_text(encoding="utf-8"))
        min_leg = float(cfg.raw.get("road_text", {}).get("min_leg_m", 200.0))
        placements = signs.placements_from_segments(sl["segments"], min_leg_m=min_leg)
        if placements:
            tex_dir = Path(__file__).resolve().parents[2] / "assets" / "textures"
            cells = signs.render_atlas([nm for _i, nm in placements], tex_dir / "signs_atlas.png")
            sg = signs.build_signs(loop, widths, placements, cells,
                                   flip_read=bool(sg_cfg.get("flip_read", False)),
                                   flip_up=bool(sg_cfg.get("flip_up", False)), reject=on_surface,
                                   ground=ground_y)
            signs_panels, signposts = sg["SIGNS"], sg["SIGNPOST"]
            nsign = len(sg["kept"])
            print(f"  street signs: {nsign}/{len(placements)} -> {sg['kept']}")

    # mirroring the east axis flips every face inside-out; reverse the visual meshes' winding so they
    # render front-side again (the drivable track handles this via orient_up in build_mesh).
    if mirror_x:
        for _m in (water, *bld_comm.values(), *bld_wh.values(), *bld_roof.values(), trees, bushes,
                   lightposts, lightheads,
                   poles, wires, signs_panels, signposts, fences, hwy_deck, hwy_struct, bridge):
            _m["tris"] = [(a, c, b) for (a, b, c) in _m["tris"]]

    # --- write environment OBJ + dressed render ---
    write_obj(data / "environment.obj", "environment.mtl",
              [("WATER", "grass", water), ("BUILDINGS", "road", bld_comm["BUILDINGS"]),
               ("BRICK", "road", bld_comm["BRICK"]), ("STUCCO", "road", bld_comm["STUCCO"]),
               ("WAREHOUSE", "road", bld_wh["WAREHOUSE"]), ("WHMETAL", "road", bld_wh["WHMETAL"]),
               ("ROOFS", "road", bld_roof["ROOFS"]), ("RFMETAL", "road", bld_roof["RFMETAL"]),
               ("TREES", "grass", trees), ("BUSHES", "grass", bushes),
               ("LIGHTPOST", "road", lightposts), ("LIGHTS", "road", lightheads),
               ("POLE", "road", poles), ("WIRE", "road", wires),
               ("SIGNS", "road", signs_panels), ("SIGNPOST", "road", signposts),
               ("CHAINLINK", "grass", fences),
               ("MOUNTAINS", "grass", mountains), ("HIGHWAY", "road", hwy_deck),
               ("HWYSTRUCT", "road", hwy_struct), ("HWYSTRUCT_bridge", "road", bridge),
               ("CONTAINERS", "road", containers)])
    write_mtl(data / "environment.mtl")
    render_iso([("grass", (0.30, 0.47, 0.22), grass), ("water", (0.16, 0.45, 0.74), water),
                ("buildings", (0.58, 0.55, 0.50), bld_comm["BUILDINGS"]), ("brick", (0.52, 0.30, 0.24), bld_comm["BRICK"]),
                ("stucco", (0.62, 0.34, 0.30), bld_comm["STUCCO"]), ("warehouse", (0.50, 0.52, 0.55), bld_wh["WAREHOUSE"]),
                ("whmetal", (0.58, 0.60, 0.62), bld_wh["WHMETAL"]),
                ("roofs", (0.30, 0.36, 0.48), bld_roof["ROOFS"]), ("rfmetal", (0.55, 0.57, 0.60), bld_roof["RFMETAL"]),
                ("road", (0.82, 0.83, 0.86), road),
                ("connector", (0.74, 0.76, 0.80), connector), ("kerb", (0.88, 0.24, 0.20), kerb),
                ("trees", (0.18, 0.42, 0.16), trees),
                ("lightpost", (0.30, 0.31, 0.33), lightposts), ("lights", (0.95, 0.85, 0.35), lightheads),
                ("poles", (0.34, 0.24, 0.16), poles), ("wires", (0.10, 0.10, 0.12), wires),
                ("signs", (0.10, 0.45, 0.20), signs_panels), ("signpost", (0.50, 0.51, 0.53), signposts),
                ("fences", (0.62, 0.63, 0.66), fences)],
               {}, data / "dressed_render.svg")

    return {"connector_pts": len(connector["vertices"]) // 2, "water_tris": len(water["tris"]),
            "highway_deck_tris": len(hwy_deck["tris"]), "highway_struct_tris": len(hwy_struct["tris"]),
            "buildings": nbld, "trees": ntrees, "bushes": nbush, "river_trees": nriver,
            "streetlights": nlights, "power_poles": npole, "street_signs": nsign,
            "yard_fences": len(fence_meshes), "containers": ncont,
            "render": str(data / "dressed_render.svg")}


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m scripts.environment.build_env <project-dir>")
    stats = build(sys.argv[1])
    print("wrote data/environment.obj + dressed_render.svg")
    for k, v in stats.items():
        if k != "render":
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
