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
    ROAD_LIFT_M, finished_centerline, project_grid, read_npy, render_iso, split_mesh_under_cap,
    write_mtl, write_obj)
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
    """THREE-card star billboard (cards at 60 deg) mapped to one cell of a foliage atlas. Two crossed
    cards read FLAT whenever the camera nears either card's plane (Kevin: "make the trees not so
    flat — crosses or something"); three at 60 deg always presents >=2 cards at a useful angle, so
    the canopy keeps visual volume from every direction. V=0 (base) -> bottom of the cell."""
    hw = w / 2
    u0, u1 = col / ncols, (col + 1) / ncols
    vb, vt = (nrows - 1 - row) / nrows, (nrows - row) / nrows   # bottom/top of cell (v=0 = image bottom)
    verts, uvs, tris = [], [], []
    for k in range(3):
        a = yaw + k * (math.pi / 3.0)
        cx2, sz2 = math.cos(a) * hw, math.sin(a) * hw
        b = len(verts)
        verts += [(x - cx2, y, z - sz2), (x + cx2, y, z + sz2),
                  (x + cx2, y + h, z + sz2), (x - cx2, y + h, z - sz2)]
        uvs += [(u0, vb), (u1, vb), (u1, vt), (u0, vt)]
        tris += [(b, b + 1, b + 2), (b, b + 2, b + 3)]
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


def _creek_bridge(loop, widths, env, project, ground_y, *, parapet=1.0, thick=0.7, pad=4,
                  declared_spans=()):
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
    # A bridge exists where the waterway passes UNDER the road — the line comes within ~2.5 m of the
    # centreline. The old test (w/2 + 8) also caught waterways running PARALLEL just off the verge
    # (plains irrigation ditches along US-6/Colfax) and built 50+ stations of parapet alongside the
    # lane — Kevin's "crazy wall stuff down in the plains".
    flag = [near_w(loop[i][0], loop[i][2]) < 2.5 for i in range(n)]
    # ALSO dress every DECLARED capture.bridges span: a real crossing over a channel that is not in
    # OSM waterways (Sand Creek's 46th-leg river dip) gets its deck leveled by evidence but showed
    # no parapets/railing — flag its stations directly by lap arc.
    if declared_spans:
        arc = [0.0]
        for i in range(1, n):
            arc.append(arc[-1] + math.hypot(loop[i][0] - loop[i - 1][0], loop[i][2] - loop[i - 1][2]))
        for center, ln in declared_spans:
            for i in range(n):
                if abs(arc[i] - center) <= ln / 2:
                    flag[i] = True
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
            """Conformed-ground height under a LOCAL (mirrored) x,z — TRIANGLE-exact on the shipped
            grass grid, same (a,b,c)+(a,c,d) diagonal split grass_terrain renders. Bilinear here sat
            up to ~1 m off the rendered triangle on steep corridor terrain and shipped hovering fences."""
            fi = (x - gx0) / gdx if gdx else 0.0
            fj = (z - gz0) / gdz if gdz else 0.0
            i0 = max(0, min(gnx - 2, int(fi))); j0 = max(0, min(gny - 2, int(fj)))
            u = max(0.0, min(1.0, fi - i0)); v = max(0.0, min(1.0, fj - j0))
            ya = GY[j0][i0]; yb = GY[j0][i0 + 1]; yc = GY[j0 + 1][i0 + 1]; yd = GY[j0 + 1][i0]
            if u >= v:      # tri (a,b,c)
                return ya + u * (yb - ya) + v * (yc - yb)
            return ya + v * (yd - ya) + u * (yc - yd)   # tri (a,c,d)

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
    bridge = _creek_bridge(loop, widths, env, project, ground_y,
                           declared_spans=evidence.bridge_spans(project_dir))

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
    # tree_style "conifer": pines/spruces — narrower billboards (a pine is a spire, not a lollipop)
    # drawn from the CONIFER atlas material instead of the broadleaf TREES one.
    conifer = scn.get("tree_style") == "conifer"
    tree_mat = "CONIFER" if conifer else "TREES"
    t_wfrac = 0.55 if conifer else 0.85

    def seat_y(x, z, half):
        """Ground height to seat a billboard's flat base: the MIN ground over its footprint, so on a
        steep cut/fill face the downhill corner still touches earth instead of hanging in the air.
        Eight directions, not four — the crossed quads are YAWED per instance, so their corners land
        between axis-aligned samples (4-point sampling still left tall conifers hanging on 2:1 cuts).
        A small extra sink hides the residual between-sample slope."""
        ring = [(0.0, 0.0)] + [(half * math.cos(k * math.pi / 4), half * math.sin(k * math.pi / 4))
                               for k in range(8)]
        vals = [ground_y(x + ox, z + oz) for ox, oz in ring]
        # cliff-lip guard: >0.95 m of relief inside the footprint means half the base hangs in air
        # however we seat it (the finer terrain grid exposes these) — signal the caller to skip.
        if max(vals) - min(vals) > 0.95:
            return None
        return min(vals) - 0.25
    # fill_terrain replaces the poly scatter wholesale: on a forest track the OSM wood polys cover the
    # same hillsides the corridor fill plants, and iterating polys first eats the whole tree cap in
    # OSM-poly order (forested climb, bald return leg). One scatter, one budget, even lap coverage.
    for f in (() if scn.get("fill_terrain") else env):
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
                    _sy9 = seat_y(x, z, h * t_wfrac / 2)
                    if _sy9 is None:
                        continue
                    tree_meshes.append(_billboard_cell(x, _sy9, z,
                                                       _tg.randint(0, t_ac - 1), _tg.randint(0, t_ar - 1),
                                                       t_ac, t_ar, h, h * t_wfrac, yaw=_tg.random() * math.pi))
                    ntrees += 1

    # --- fill_terrain: a continuous forest band swept along the WHOLE lap (mountain tracks — the
    #     non-road world is forest, not the OSM 'green'-polygon islands an urban grid has). Offsets are
    #     biased toward the road (r^1.6) so the tree wall reads dense from the driver's seat while the
    #     cap goes further; heights run taller than the urban scatter (mature ponderosa, 8..16 m). ---
    if scn.get("fill_terrain"):
        _tf = random.Random(91)
        f_range = float(scn.get("fill_range_m", 150.0))
        f_per = int(scn.get("fill_per_station", 3))
        # Thin by probability so the budget spreads over the WHOLE lap — a hard cap hit mid-loop
        # forests the climb and leaves the return leg bald (trees place in lap-station order).
        lap_len = sum(math.hypot(loop[i][0] - loop[i - 1][0], loop[i][2] - loop[i - 1][2])
                      for i in range(1, len(loop)))
        cand = max(1, int(lap_len / max(t_step, 1e-6)) * 2 * f_per)
        p_keep = min(1.0, max(0, t_cap - ntrees) / cand)
        acc = 0.0
        for i in range(1, len(loop)):
            acc += math.hypot(loop[i][0] - loop[i - 1][0], loop[i][2] - loop[i - 1][2])
            if acc < t_step:
                continue
            acc = 0.0
            x, y, z = loop[i]
            a, b = loop[i - 1], loop[min(len(loop) - 1, i + 1)]
            tx, tz = b[0] - a[0], b[2] - a[2]
            tl = math.hypot(tx, tz) or 1e-6
            nx, nz = -tz / tl, tx / tl
            for side in (1.0, -1.0):
                for _ in range(f_per):
                    if ntrees >= t_cap or _tf.random() > p_keep:
                        continue
                    off = widths[i] / 2 + float(scn.get("corridor_margin_m", 3.0)) \
                        + float(scn.get("fill_off_min_extra_m", 2.0)) \
                        + _tf.random() ** float(scn.get("fill_bias", 1.6)) * f_range
                    rx, rz = x + nx * off * side, z + nz * off * side
                    if on_road(rx, rz):
                        continue
                    h = 8.0 + _tf.random() * 8.0
                    _sy9 = seat_y(rx, rz, h * t_wfrac / 2)
                    if _sy9 is None:
                        continue
                    tree_meshes.append(_billboard_cell(rx, _sy9, rz,
                                                       _tf.randint(0, t_ac - 1), _tf.randint(0, t_ar - 1),
                                                       t_ac, t_ar, h, h * t_wfrac, yaw=_tf.random() * math.pi))
                    ntrees += 1

    # --- 3D FOREST (scenery.forest3d): real tree meshes in the corridor the driver reads.
    #     Billboards carry the mass on the slopes; within ~off_max of the verge the trees are
    #     actual geometry (Kevin: "a lot more trees... particularly the mountain track as it is
    #     pretty forested irl"). Modules from the Dropbox asset drop, decimated to ~2-3k tris.
    forest3d_meshes = {}
    f3 = scn.get("forest3d") or {}
    if f3:
        _f3r = random.Random(77)
        from scripts.environment import props as props_mod
        _models3 = Path(__file__).resolve().parents[2] / "assets" / "models"
        _mods3 = []
        if f3.get("module", "pine") == "pine":
            _mods3 = [("PINETREE", props_mod.load_module(_models3 / "pine_tree.obj"))]
        else:
            _mods3 = [("POPLARLEAF", props_mod.load_module(_models3 / "poplar_leaves.obj")),
                      ("POPLARBARK", props_mod.load_module(_models3 / "poplar_trunk.obj"))]
        for _nm3, _ in _mods3:
            forest3d_meshes[_nm3] = {"vertices": [], "uvs": [], "tris": []}
        # every-leg clearance hash: a tree placed off leg A can overhang leg B's lane where
        # legs run close (25 obstructed stations at the switchback stacks). Reject positions
        # whose canopy reaches ANY centerline within its own height layer.
        _lh3 = {}
        for _q3, _p3 in enumerate(loop):
            _lh3.setdefault((int(_p3[0] // 24.0), int(_p3[2] // 24.0)), []).append(_q3)

        def _too_close3(px, pz, py, reach):
            ci, cj = int(px // 24.0), int(pz // 24.0)
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for _q3 in _lh3.get((ci + di, cj + dj), ()):
                        lp = loop[_q3]
                        if abs(lp[1] - py) > 25.0:
                            continue
                        if math.hypot(px - lp[0], pz - lp[2]) < widths[_q3] / 2 + reach:
                            return True
            return False

        _sp3 = float(f3.get("spacing_m", 26.0))
        _o0 = float(f3.get("off_min_m", 7.0)); _o1 = float(f3.get("off_max_m", 26.0))
        _cap3 = int(f3.get("cap", 700))
        _s0 = float(f3.get("scale_min", 2.2)); _s1 = float(f3.get("scale_max", 4.2))
        _n3 = 0
        _acc3 = 0.0
        for i in range(1, len(loop)):
            _acc3 += math.hypot(loop[i][0] - loop[i - 1][0], loop[i][2] - loop[i - 1][2])
            if _acc3 < _sp3 or _n3 >= _cap3:
                continue
            _acc3 = 0.0
            x, y, z = loop[i]
            a, b = loop[i - 1], loop[min(len(loop) - 1, i + 1)]
            tx, tz = b[0] - a[0], b[2] - a[2]
            tl = math.hypot(tx, tz) or 1e-6
            nx, nz = -tz / tl, tx / tl
            side = 1.0 if (i // 7) % 2 == 0 else -1.0    # alternating, deterministic
            sc3 = _s0 + _f3r.random() * (_s1 - _s0)
            # canopy-aware clearance: a x4-scaled pine's canopy is ~9 m wide — the trunk must
            # stand canopy_half further out or branches overhang the lane (drive obstructions
            # at the flared corners). 2.2 = the module's canopy half-width at scale 1.
            off = widths[i] / 2 + _o0 + 2.2 * sc3 + _f3r.random() * (_o1 - _o0)
            rx, rz = x + nx * off * side, z + nz * off * side
            if on_road(rx, rz):
                continue
            gy3 = seat_y(rx, rz, 1.2)
            if gy3 is None:
                continue
            gy3 += 0.15                                 # seat_y sinks 0.25 for billboards; trees want less
            ya3 = _f3r.random() * math.pi * 2
            _cy, _sy = math.cos(ya3), math.sin(ya3)
            if abs(y - gy3) > 25.0:
                continue                                 # layer guard: never a tree from another leg
            if _too_close3(rx, rz, gy3, 2.2 * sc3 + 1.2):
                continue                                 # canopy would reach SOME leg's lane
            for _nm3, _mod3 in _mods3:
                M = forest3d_meshes[_nm3]
                base3 = len(M["vertices"])
                for mx, my, mz in _mod3["vertices"]:
                    wx = mx * sc3 * _cy - mz * sc3 * _sy
                    wz = mx * sc3 * _sy + mz * sc3 * _cy
                    M["vertices"].append((rx + wx, gy3 + my * sc3, rz + wz))
                M["uvs"].extend(_mod3["uvs"])
                M["tris"].extend((a3 + base3, b3_ + base3, c3 + base3) for a3, b3_, c3 in _mod3["tris"])
            _n3 += 1
        print(f"  forest3d: {_n3} {f3.get('module', 'pine')} instances "
              f"({sum(len(m['vertices']) for m in forest3d_meshes.values())} verts)")

    # DENSE riparian trees along Sand Creek (the green corridor — cottonwoods/willows on the banks).
    # Gated to waterways NEAR the lap: the zones bbox carries every stream in the region (Clear Creek
    # runs the whole north edge at Lookout), and forest planted along all of them is budget spent
    # where no driver ever looks.
    near_track = _corridor_rejecter(loop, widths, connector_lines, margin=250.0, cell=260.0)
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
                    if not near_track(rx, rz):              # stream nowhere near the lap — skip
                        continue
                    h = 6.0 + _tr.random() * 5.0            # 6..11 m cottonwoods
                    # CHANNEL GUARD: a footprint straddling the carved creek channel has wildly
                    # different ground across its ring — seating on the ring-min hangs the tree off
                    # the bank lip (17 cottonwoods shipped 15 m over the Sand Creek channel). A bank
                    # this steep is water's edge, not tree ground: skip.
                    _half_g = h * t_wfrac / 2
                    _ring_g = [ground_y(rx + _half_g * math.cos(k * math.pi / 4),
                                        rz + _half_g * math.sin(k * math.pi / 4)) for k in range(8)]
                    if max(_ring_g) - min(_ring_g) > 4.0:
                        continue
                    _sy9 = seat_y(rx, rz, h * t_wfrac / 2)
                    if _sy9 is None:
                        continue
                    tree_meshes.append(_billboard_cell(rx, _sy9, rz,
                                                       _tr.randint(0, t_ac - 1), _tr.randint(0, t_ar - 1),
                                                       t_ac, t_ar, h, h * t_wfrac, yaw=_tr.random() * math.pi))
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
            _sy9 = seat_y(bx, bz, 0.8)
            if _sy9 is None:
                continue                          # cliff-lip: don't perch a prop half in air
            by = _sy9 - 0.4          # footprint-min ground - sink (no corner hangs on a cut face)
            sz = b_sz_min + _r.random() * b_sz_span
            bush_meshes.append(_billboard_cell(bx, by, bz, _r.randint(0, NC - 1), _r.randint(0, NR - 1),
                                               NC, NR, sz * 0.78, sz, yaw=_r.random() * math.pi))
            nbush += 1
    bushes = _merge(bush_meshes)

    # --- prop modules (Kevin's pack): lamp post swap + ranch fences + pylon runs ---
    from scripts.environment import props as props_mod
    _models = Path(__file__).resolve().parents[2] / "assets" / "models"
    _lamp_module = None
    _lens_module = None
    _lamp_fixture = (0.0, 9.75, 0.0)   # (module-x, height, module-z) of the emitter
    _lamp_obj = scn.get("lamp_model") if isinstance(scn.get("lamp_model"), str) else "lamp_post.obj"
    if (_models / _lamp_obj).exists() and scn.get("lamp_model", True):
        _lamp_module = props_mod.load_module(_models / _lamp_obj)
        # MOUNTING HEIGHT (Kevin: "they might be too tall"): the model is a 10.5 m highway mast;
        # real urban/collector streets run 8-9 m. Vertical scale only — arm reach/thickness stay.
        _lamp_h = float(scn.get("lamp_height_m", 9.0))
        _native_h = max(v[1] for v in _lamp_module["vertices"]) or 1.0
        if abs(_lamp_h - _native_h) > 0.05:
            _sc9 = _lamp_h / _native_h
            _lamp_module["vertices"] = [(x, y * _sc9, z) for x, y, z in _lamp_module["vertices"]]
        if (_models / "lamp_lens.obj").exists():
            _lens_module = props_mod.load_module(_models / "lamp_lens.obj")
        # THE STYLE INDICATES the emitter position: centroid of the model's top-metre verts.
        # A post-top luminaire yields (~0, top, ~0) — emitter INSIDE the head atop the mast; an
        # arm style yields the arm tip. The old hardcoded (+1.2 m, 9.75) floated the glowing lens
        # BESIDE this model's actual fixture.
        # split the model at the neck: the HEAD SHELL becomes its own mesh (LAMPHEAD) with a faint
        # night emissive — a glowing lens inside a pitch-black housing reads as a detached orb
        # ("Floating. Floating. Floating."); a softly lit housing reads as a streetlight.
        def _split_module(mod, cut_y):
            lo = {"vertices": [], "uvs": [], "tris": []}
            hi = {"vertices": [], "uvs": [], "tris": []}
            lo_map, hi_map = {}, {}
            for t in mod["tris"]:
                dest, dmap = (hi, hi_map) if all(mod["vertices"][v][1] > cut_y for v in t) else (lo, lo_map)
                nt = []
                for v in t:
                    j = dmap.get(v)
                    if j is None:
                        j = dmap[v] = len(dest["vertices"])
                        dest["vertices"].append(mod["vertices"][v])
                        dest["uvs"].append(mod["uvs"][v])
                    nt.append(j)
                dest["tris"].append(tuple(nt))
            return lo, hi
        _lamp_shaft_mod, _lamp_head_mod = _split_module(_lamp_module, 0.78 * _lamp_h)
        # the WHOLE head (luminaire base ~7.4 m after the 9 m scale) goes to LAMPHEAD, and the
        # head is DOUBLE-SIDED: the model's shell normals face inward on part of the housing, so
        # single-sided culling erased the top from most angles ("pole tops are still invisible").
        _lamp_head_mod["tris"] = _lamp_head_mod["tris"] + [(c, b, a) for a, b, c in _lamp_head_mod["tris"]]
        # THE MISSING MAST (Kevin, three times: "top half invisible", "half a light pole",
        # "floating orb 12 feet above"): the OBJ literally contains a pedestal (0-2 m), a collar
        # at 5 m and the head (9-11 m) — the mast was never exported as faces. Bridge it with a
        # procedural 12-sided tapered cylinder so the head finally connects to the ground.
        _ys0 = [v[1] for v in _lamp_module["vertices"]]
        _ped_top = max((y for y in _ys0 if y < 3.0), default=0.0)
        _head_bot = min((y for y in _ys0 if y > 6.0), default=max(_ys0))
        if _head_bot - _ped_top > 2.0:
            import math as _mm
            _mv = _lamp_module["vertices"]; _mu = _lamp_module["uvs"]; _mt = _lamp_module["tris"]
            _base_i = len(_mv)
            _NS = 12
            for _lvl, (_yy, _rr) in enumerate(((_ped_top - 0.05, 0.11), (_head_bot + 0.10, 0.075))):
                for _k7 in range(_NS):
                    _a7 = 2 * _mm.pi * _k7 / _NS
                    _mv.append((_rr * _mm.cos(_a7), _yy, _rr * _mm.sin(_a7)))
                    _mu.append((_k7 / _NS, float(_lvl)))
            for _k7 in range(_NS):
                _n7 = (_k7 + 1) % _NS
                _b0 = _base_i + _k7; _b1 = _base_i + _n7
                _t0 = _base_i + _NS + _k7; _t1 = _base_i + _NS + _n7
                _mt.append((_b0, _t0, _t1)); _mt.append((_b0, _t1, _b1))
            print(f"  lamp mast bridged: {_ped_top:.1f} -> {_head_bot:.1f} m (the OBJ never had one)")
        _top_y = max(v[1] for v in _lamp_module["vertices"])
        _top = [v for v in _lamp_module["vertices"] if v[1] > _top_y - 1.0]
        _lamp_fixture = (sum(v[0] for v in _top) / len(_top),
                         sum(v[1] for v in _top) / len(_top) - 0.15,
                         sum(v[2] for v in _top) / len(_top))
        print(f"  lamp fixture (from model style): x {_lamp_fixture[0]:+.2f}  "
              f"h {_lamp_fixture[1]:.2f}  z {_lamp_fixture[2]:+.2f}")
    fences_cfg = scn.get("fences", [])
    ranch_fences = {"vertices": [], "uvs": [], "tris": []}
    if fences_cfg and (_models / "ranch_fence_panel.obj").exists():
        _fmod = props_mod.load_module(_models / "ranch_fence_panel.obj")
        ranch_fences = props_mod.instance_line(loop, _fmod, ranges=fences_cfg, widths_m=widths,
                                               ground=ground_y, module_len=2.02)
        print(f"  ranch fences: {len(ranch_fences['vertices'])} verts over {len(fences_cfg)} ranges")
    pylons_cfg = scn.get("pylons", [])
    pylon_line = {"vertices": [], "uvs": [], "tris": []}
    if pylons_cfg and (_models / "pylon.obj").exists():
        _pmod = props_mod.load_module(_models / "pylon.obj")
        pylon_line = props_mod.instance_line(loop, _pmod, ranges=pylons_cfg, widths_m=widths,
                                             ground=ground_y, module_len=134.0)
        print(f"  pylon runs: {len(pylon_line['vertices'])} verts over {len(pylons_cfg)} ranges")

    # --- euro road signs (scenery.road_signs): sharp-turn arrows at warning-barrier entries +
    #     config-listed speed discs. One EUROSIGN atlas material; galvanized SIGNPOST posts. ---
    rs_cfg = scn.get("road_signs", {}) or {}
    sign_plates = {"vertices": [], "uvs": [], "tris": []}
    sign_posts2 = {"vertices": [], "uvs": [], "tris": []}
    if rs_cfg.get("enabled"):
        def _euro_sign(x, z, face_i, face_dirx, face_dirz):
            gy2 = ground_y(x, z)
            V, U, T = sign_plates["vertices"], sign_plates["uvs"], sign_plates["tris"]
            px2, pz2 = -face_dirz, face_dirx          # plate right vector
            r = 0.45
            cy = gy2 + 2.2
            b = len(V)
            for sx2, sy2 in ((-r, -r), (r, -r), (r, r), (-r, r)):
                V.append((x + px2 * sx2, cy + sy2, z + pz2 * sx2))
            u0, v0 = (face_i % 2) * 0.5, 1.0 - (face_i // 2 + 1) * 0.5
            U.extend([(u0, v0), (u0 + 0.5, v0), (u0 + 0.5, v0 + 0.5), (u0, v0 + 0.5)])
            T.extend([(b, b + 1, b + 2), (b, b + 2, b + 3), (b, b + 2, b + 1), (b, b + 3, b + 2)])
            PV, PT = sign_posts2["vertices"], sign_posts2["tris"]
            pb = len(PV)
            for dx2, dz2 in ((-0.04, -0.04), (0.04, -0.04), (0.04, 0.04), (-0.04, 0.04)):
                PV.append((x + dx2, gy2, z + dz2))
                PV.append((x + dx2, gy2 + 2.65, z + dz2))
            for k2 in range(4):
                a2, b2 = 2 * k2, 2 * ((k2 + 1) % 4)
                PT.extend([(pb + a2, pb + b2, pb + b2 + 1), (pb + a2, pb + b2 + 1, pb + a2 + 1)])
            sign_posts2.setdefault("uvs", []).extend([(0.0, 0.0)] * 8)
        from scripts.geometry import kerbs as _kerbs
        _bar, _spots = _kerbs.warning_barriers(loop, widths)
        # BOTH DIRECTIONS (Kevin drives the reverse layout too): detect corners on the reversed
        # lap and place its warnings as well — positions are world-space, so the same placement
        # code runs on the reversed arrays and everything lands facing the reverse traffic.
        _bar_r, _spots_r = _kerbs.warning_barriers(loop[::-1], widths[::-1])
        _sst0 = [0.0]
        for _q2 in range(1, len(loop)):
            _sst0.append(_sst0[-1] + math.hypot(loop[_q2][0] - loop[_q2 - 1][0],
                                                loop[_q2][2] - loop[_q2 - 1][2]))
        for _loopD, _widthsD, _spotsD in ((loop, widths, _spots),
                                          (loop[::-1], widths[::-1], _spots_r)):
          for _sp in _spotsD:
            # TRACK convention, not road convention (Kevin): the warning stands WHERE THE DANGER
            # IS — at the corner apex on the OUTSIDE of the turn (the barrier line), the arrow
            # facing the approaching driver. Like chevron boards on a race circuit.
            _apx = min(_sp.get("apex_idx", _sp["start_idx"]), len(_loopD) - 2)
            i2 = max(1, _apx)
            x2, _, z2 = _loopD[i2]
            # face along the APPROACH tangent (a few stations before the apex), not the apex's own
            ia2 = max(1, i2 - 6)
            a2, b3 = _loopD[ia2 - 1], _loopD[min(len(_loopD) - 1, ia2 + 1)]
            tx2, tz2 = b3[0] - a2[0], b3[2] - a2[2]
            L2 = math.hypot(tx2, tz2) or 1e-9
            tx2, tz2 = tx2 / L2, tz2 / L2
            _outside = float(_sp["side"])           # the side the barriers guard
            nx2, nz2 = -tz2 * _outside, tx2 * _outside
            _soff = max(_widthsD[i2] / 2 + 2.8, 6.8)   # clear of the 6.3 m obstruction corridor
            sx3, sz3 = x2 + nx2 * _soff, z2 + nz2 * _soff
            if not on_surface(sx3, sz3):    # verge furniture: tight on-pavement test, not the 4 m foliage margin
                # MUTCD atlas cell by direction + severity: 0/1 curve L/R, 2/3 hairpin L/R
                _tdeg = float(_sp.get("turn_deg", 60.0))
                _left = _tdeg > 0
                _face = (2 if _left else 3) if abs(_tdeg) >= 100.0 else (0 if _left else 1)
                _euro_sign(sx3, sz3, _face, -tx2, -tz2)      # warning diamond faces traffic
        _sst = [0.0]
        for i2 in range(1, len(loop)):
            _sst.append(_sst[-1] + math.hypot(loop[i2][0] - loop[i2 - 1][0], loop[i2][2] - loop[i2 - 1][2]))
        for sp2 in rs_cfg.get("speed_signs", []):
            i2 = min(range(len(loop)), key=lambda k3: abs(_sst[k3] - float(sp2["station_m"])))
            x2, _, z2 = loop[i2]
            a2, b3 = loop[max(0, i2 - 1)], loop[min(len(loop) - 1, i2 + 1)]
            tx2, tz2 = b3[0] - a2[0], b3[2] - a2[2]
            L2 = math.hypot(tx2, tz2) or 1e-9
            tx2, tz2 = tx2 / L2, tz2 / L2
            sx3, sz3 = x2 - tz2 * -(widths[i2] / 2 + 2.2), z2 + tx2 * -(widths[i2] / 2 + 2.2)
            if not on_surface(sx3, sz3):
                _euro_sign(sx3, sz3, int(sp2.get("face", 0)), -tx2, -tz2)
        # DANGER BOARDS at the extreme entries (long straight into a hairpin): a red light-grid
        # board on posts, 60 m before the corner. Static bright-red emissive for now — swaps to
        # Kevin's flashing-light model when it lands (CSP animation then).
        danger_boards = {"vertices": [], "uvs": [], "tris": []}
        _nboards = 0
        for _loopD, _widthsD, _spotsD in ((loop, widths, _spots),
                                          (loop[::-1], widths[::-1], _spots_r)):
          for _sp in _spotsD:
            if abs(_sp.get("turn_deg", 0)) < 90:
                continue
            i2 = max(1, _sp["start_idx"] - 20)
            x2, _, z2 = _loopD[i2]
            a2, b3 = _loopD[i2 - 1], _loopD[min(len(_loopD) - 1, i2 + 1)]
            tx2, tz2 = b3[0] - a2[0], b3[2] - a2[2]
            L2 = math.hypot(tx2, tz2) or 1e-9
            tx2, tz2 = tx2 / L2, tz2 / L2
            nx2, nz2 = -tz2 * _sp["side"], tx2 * _sp["side"]
            bx2, bz2 = x2 + nx2 * (_widthsD[i2] / 2 + 2.6), z2 + nz2 * (_widthsD[i2] / 2 + 2.6)
            if on_surface(bx2, bz2):
                continue
            gyb = ground_y(bx2, bz2)
            V, U, T = danger_boards["vertices"], danger_boards["uvs"], danger_boards["tris"]
            bb = len(V)
            px2, pz2 = -tz2, tx2
            for sx2, sy2 in ((-1.2, 1.6), (1.2, 1.6), (1.2, 2.8), (-1.2, 2.8)):
                V.append((bx2 + px2 * sx2, gyb + sy2, bz2 + pz2 * sx2))
                U.append(((sx2 + 1.2) / 2.4, (sy2 - 1.6) / 1.2))
            T.extend([(bb, bb + 1, bb + 2), (bb, bb + 2, bb + 3),
                      (bb, bb + 2, bb + 1), (bb, bb + 3, bb + 2)])
            for _px in (-1.0, 1.0):
                pb = len(V)
                for dx2, dz2 in ((-0.05, -0.05), (0.05, -0.05), (0.05, 0.05), (-0.05, 0.05)):
                    V.append((bx2 + px2 * _px + dx2, gyb, bz2 + pz2 * _px + dz2))
                    V.append((bx2 + px2 * _px + dx2, gyb + 1.7, bz2 + pz2 * _px + dz2))
                    U.extend([(0.5, 0.5)] * 2)
                for k2 in range(4):
                    a3, b4 = 2 * k2, 2 * ((k2 + 1) % 4)
                    T.extend([(pb + a3, pb + b4, pb + b4 + 1), (pb + a3, pb + b4 + 1, pb + a3 + 1)])
            _nboards += 1
        print(f"  euro signs: {len(sign_plates['vertices']) // 4} placed; danger boards: {_nboards}")

    # --- streetlights along the lap (~ every 48 m, on the right verge) ---
    # Split geometry: the mast (LIGHTPOST, dark) and the lamp head (LIGHTS, emissive) are separate meshes
    # so only the head glows at night — not the whole 9 m stick (the old single-quad LIGHTS defect).
    lightpost_meshes, lighthead_meshes, headshell_meshes, nlights = [], [], [], 0
    lamp_xz: list[tuple[float, float]] = []   # pole-collision avoidance for the powerline pass
    l_spacing = float(scn.get("light_spacing_m", 48.0))
    acc = 0.0
    for i in range(1, len(loop)) if l_spacing > 0 else ():   # <=0 disables (mountain roads are unlit)
        acc += math.hypot(loop[i][0] - loop[i - 1][0], loop[i][2] - loop[i - 1][2])
        if acc >= l_spacing:
            acc = 0.0
            x, y, z = loop[i]
            a, b = loop[i - 1], loop[min(len(loop) - 1, i + 1)]
            tx, tz = b[0] - a[0], b[2] - a[2]
            tl = math.hypot(tx, tz) or 1e-6
            nx, nz = -tz / tl, tx / tl
            # ALTERNATE SIDES (Kevin): staggered left/right like a real two-lane — halves the
            # per-side density and reads correctly at night. Flipping the normal flips the whole
            # module frame, so the arm/fixture still faces the road from either verge.
            if nlights % 2:
                nx, nz = -nx, -nz
            off = widths[i] / 2 + 2.0
            px, pz = x - nx * off, z - nz * off
            if on_surface(px, pz):                     # verge offset landed on a crossing/fold-back road — skip
                continue
            # Seat on the CONFORMED ground at the pole's OWN verge location — not the road centerline y,
            # which left the base floating ~0.25 m+ over the clamped grass beside the road (and more where
            # the verge slopes off). trees/bushes already use ground_y; poles now match.
            gy_pole = ground_y(px, pz)
            # DOT PAD: a real pole stands on a pad graded to ITS road's level — never on whatever
            # raw terrain happens to be 2 m off the edge. The old one-sided skip (y-gy>4) missed the
            # opposite failure: a HIGH ground sample (cut face / the slope between stacked legs)
            # planted the whole lamp metres above the verge — stub-looking masts, cobra heads
            # floating in the sky, and their CSP lights turned into floodlights beaming from mid-air
            # (v0.7.8, caught by render_drive_views at the hairpins). Clamp the base to a pad within
            # [-1.5, +0.5] of the road; if the sample is another layer entirely, skip pole AND head.
            if abs(y - gy_pole) > 6.0:
                continue
            gy_pole = max(min(gy_pole, y + 0.5), y - 1.5)
            shaft, lamphead = _streetlight(px, gy_pole, pz, nx, nz)   # arm reaches +n back over the lane
            if _lamp_module is not None:
                # Kevin's black lamp model as the visible post (LIGHTPOST). The old procedural head
                # BOX is gone ("flooring boxes") — replaced by a 2 cm marker tri INSIDE the model's
                # head: invisible in game, but the CSP per-lamp lights still cluster from the LIGHTS
                # mesh in the kn5, so the glow comes from the pole itself with no frame math.
                tl2 = math.hypot(nx, nz) or 1e-9
                txl, tzl = nz / tl2, -nx / tl2

                def _inst(mod):
                    out2 = {"vertices": [(px + txl * mz + nx * mx, gy_pole + my, pz + tzl * mz + nz * mx)
                                         for mx, my, mz in mod["vertices"]],
                            "uvs": list(mod["uvs"]), "tris": list(mod["tris"])}
                    return out2
                shaft = _inst(_lamp_shaft_mod)
                headshell_meshes.append(_inst(_lamp_head_mod))
                _fx, _fy, _fz = _lamp_fixture
                _tl3 = math.hypot(nx, nz) or 1e-9
                _txl3, _tzl3 = nz / _tl3, -nx / _tl3
                hx2 = px + _txl3 * _fz + nx * _fx
                hy2 = gy_pole + _fy
                hz2 = pz + _tzl3 * _fz + nz * _fx
                if _lens_module is not None:
                    # the model's own glass lens: glows (LIGHTS_mat ksEmissive) at ANY distance even
                    # when CSP culls the dynamic light — why "the lights aren't all on at once" read
                    # as broken: no visible glow beyond the fade distance. Also the cluster source.
                    lamphead = {"vertices": [(hx2 + mx, hy2 + my, hz2 + mz)
                                             for mx, my, mz in _lens_module["vertices"]],
                                "uvs": list(_lens_module["uvs"]), "tris": list(_lens_module["tris"])}
                else:
                    lamphead = {"vertices": [(hx2, hy2, hz2), (hx2 + 0.02, hy2, hz2), (hx2, hy2 + 0.02, hz2)],
                                "uvs": [(0, 0), (1, 0), (0, 1)], "tris": [(0, 1, 2)]}
            lightpost_meshes.append(shaft)
            lighthead_meshes.append(lamphead)
            lamp_xz.append((px, pz))
            nlights += 1
    lightposts = _merge(lightpost_meshes)
    lightheads = _merge(lighthead_meshes)
    headshells = _merge(headshell_meshes)

    # --- overhead power lines (poles + sagging cables) — ubiquitous in industrial Commerce City; the
    #     real-world capture shows them in nearly every frame. Poles on the LEFT verge (streetlights are
    #     on the right), cables strung pole-to-pole down the lap. ---
    pole_meshes, wire_meshes, npole = [], [], 0
    pl_spacing = float(scn.get("powerline_spacing_m", 52.0))
    prev_tips, acc = None, 0.0
    for i in range(1, len(loop)) if pl_spacing > 0 else ():  # <=0 disables (no wires over the forest)
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
        # a wood pole within a lamp's immediate throw gets torched into a glowing yellow mast
        # (Sand Creek's "lightsaber" — side-alternation put every other lamp on the pole verge).
        # Real utilities co-locate or skip; we skip.
        if any((bx - lx2) ** 2 + (bz - lz2) ** 2 < 49.0 for lx2, lz2 in lamp_xz):
            continue
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
                   ranch_fences, pylon_line, sign_plates, sign_posts2,
                   lightposts, lightheads, headshells,
                   poles, wires, signs_panels, signposts, fences, hwy_deck, hwy_struct, bridge):
            _m["tris"] = [(a, c, b) for (a, b, c) in _m["tris"]]

    # --- write environment OBJ + dressed render ---
    write_obj(data / "environment.obj", "environment.mtl",
              [("WATER", "grass", water), ("BUILDINGS", "road", bld_comm["BUILDINGS"]),
               ("BRICK", "road", bld_comm["BRICK"]), ("STUCCO", "road", bld_comm["STUCCO"]),
               ("WAREHOUSE", "road", bld_wh["WAREHOUSE"]), ("WHMETAL", "road", bld_wh["WHMETAL"]),
               ("ROOFS", "road", bld_roof["ROOFS"]), ("RFMETAL", "road", bld_roof["RFMETAL"]),
               # trees split under the kn5 16-bit vertex cap: a 25 km forest is one ~78k-position
               # mesh, the exporter only WARNS past 65,535, and AC silently drops the over-limit
               # mesh in-game — the Lariat shipped with an invisible forest ("where did the trees
               # go"). Prefix-matched names (CONIFER_a...) keep materials/GrassFX behavior.
               *split_mesh_under_cap(tree_mat, "grass", trees),
               *split_mesh_under_cap("BUSHES", "grass", bushes),
               *split_mesh_under_cap("LIGHTPOST", "road", lightposts),
               *split_mesh_under_cap("LIGHTS", "road", lightheads),
               *split_mesh_under_cap("LAMPHEAD", "road", headshells),
               ("POLE", "road", poles), ("WIRE", "road", wires),
               ("SIGNS", "road", signs_panels), ("SIGNPOST", "road", signposts),
               ("CHAINLINK", "grass", fences),
               # Kevin's model-pack props — these were BUILT but never written in v0.7.5 (the
               # fences/pylons/euro-signs shipped as nothing). Keep them in this list forever.
               *split_mesh_under_cap("FENCEWOOD", "road", ranch_fences),
               *split_mesh_under_cap("GANTRY_pylons", "road", pylon_line),
               ("USSIGN", "road", sign_plates), ("SIGNPOST_euro", "road", sign_posts2),
               ("DANGERLITE_boards", "road", danger_boards if rs_cfg.get("enabled") else {"vertices": [], "uvs": [], "tris": []}),
               *[g for nm3, m3 in (forest3d_meshes.items() if f3 else [])
                 for g in split_mesh_under_cap(nm3, "grass", m3)],
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
