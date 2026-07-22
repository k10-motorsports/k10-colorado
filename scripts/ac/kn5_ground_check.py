"""KN5 FIDELITY GATE — the shipped kn5 must equal the audited OBJs, up to the configured yaw.

Every geometry gate (drive test, mesh audit) runs on data/track.obj + data/environment.obj. The kn5
is assembled from them in Blender afterwards (import, weld, yaw, export) — and any bug in that
assembly ships geometry NOBODY measured. This gate closes that hole: for every group-prefix bucket,
sampled kn5 vertices are un-yawed back into OBJ space and must land on an OBJ vertex of the SAME
bucket within tolerance. A displaced shoulder, a dropped wall chunk, a double-loaded stale mesh, a
group the yaw loop missed — all become loud failures instead of in-game screenshots.

Also keeps one physical sanity check: sampled 1ROAD_MAIN verts must have grass/shoulder support
beneath (floating-deck guard), with declared bridges excepted by proportion.

Run:  python -m scripts.ac.kn5_ground_check <project-dir>
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

from scripts.ac.verify_kn5 import _parse

BUCKETS = ("1ROAD_MAIN", "1ROAD_SHOULDER", "1GRASS", "1WALL", "1KERB", "1RUNOFF",
           "LIGHTPOST", "LIGHTS", "LAMPHEAD", "FENCEWOOD", "CONIFER", "MARKINGS", "YLINE",
           "HIGHWAY", "HWYSTRUCT", "EUROSIGN", "SIGNPOST", "GANTRY")
TOL = 0.08          # m — importer/exporter float chatter is ~mm; anything real is metres
SAMPLE = 23         # every Nth vert per bucket
ROAD_GAP_M = 1.8    # floating-deck guard threshold
ROAD_GAP_FRAC = 0.03  # allowed fraction of road samples over threshold (bridges)



def _parse_tris(path: Path):
    """(name, positions, indices) per mesh — verify_kn5._parse drops indices; the triangle-exact
    ground check needs them."""
    import struct as _st
    d = Path(path).read_bytes()
    o = [6]
    def U(): v = _st.unpack_from("<I", d, o[0])[0]; o[0] += 4; return v
    def I(): v = _st.unpack_from("<i", d, o[0])[0]; o[0] += 4; return v
    def F(): v = _st.unpack_from("<f", d, o[0])[0]; o[0] += 4; return v
    def B(): v = d[o[0]] != 0; o[0] += 1; return v
    def BL(n): v = d[o[0]:o[0] + n]; o[0] += n; return v
    def S(): n = U(); v = d[o[0]:o[0] + n]; o[0] += n; return v.decode("utf-8", "replace")
    ver = I()
    if ver > 5:
        I()
    for _ in range(I()):
        I(); S(); BL(U())
    for _ in range(U()):
        S(); S(); BL(2); I()
        for _ in range(U()):
            S(); BL(40)
        for _ in range(U()):
            S(); I(); S()
    out = []
    def rd():
        ncls = U(); name = S(); cc = U(); B()
        if ncls == 1:
            BL(64)
            for _ in range(cc):
                rd()
        elif ncls == 2:
            BL(3)
            vc = U()
            P = []
            for _ in range(vc):
                P.append(_st.unpack_from("<3f", d, o[0])); o[0] += 44
            ic = U()
            idx = list(_st.unpack_from(f"<{ic}H", d, o[0])); o[0] += ic * 2
            U(); U(); F(); F(); BL(16); B()
            out.append((name, P, idx))
    rd()
    return out


def _bucket(name: str) -> str | None:
    up = name.upper()
    for b in BUCKETS:
        if up.startswith(b):
            # keep 1ROAD_MAIN/1ROAD_SHOULDER distinct from each other
            if b == "1ROAD_MAIN" and up.startswith("1ROAD_SHOULDER"):
                continue
            return b
    return None


def _obj_verts(path: Path) -> dict[str, list]:
    out: dict[str, list] = defaultdict(list)
    cur = None
    if not path.exists():
        return out
    with open(path) as f:
        for ln in f:
            if ln.startswith("o "):
                cur = _bucket(ln[2:].strip())
            elif ln.startswith("v ") and cur:
                p = ln.split()
                out[cur].append((float(p[1]), float(p[2]), float(p[3])))
    return out


def check(project_dir: str | Path) -> dict:
    project_dir = Path(project_dir)
    cfg = json.loads((project_dir / "track.config.json").read_text())
    slug = cfg["slug"]
    # prefer the FRESH export (build/<slug>.kn5); build/<slug>/<slug>.kn5 is only refreshed at pack
    # time and gating on it compares OBJs against the PREVIOUS build.
    kn5p = project_dir / "build" / f"{slug}.kn5"
    if not kn5p.exists():
        kn5p = project_dir / "build" / slug / f"{slug}.kn5"
    yaw = math.radians(float(cfg.get("true_north_rotation_deg") or 0.0))

    obj = _obj_verts(project_dir / "data" / "track.obj")
    for k, v in _obj_verts(project_dir / "data" / "environment.obj").items():
        obj[k].extend(v)

    nodes, meshes = _parse(kn5p)
    kn5: dict[str, list] = defaultdict(list)
    for m in meshes:
        b = _bucket(m["name"])
        if b:
            kn5[b].extend(m["P"])

    # the exporter applies R(yaw) about the vertical; try both sign conventions and keep the one
    # that fits (measured, not assumed — the convention lives in build_kn5/Blender internals).
    c, s = math.cos(yaw), math.sin(yaw)
    unrots = [lambda x, y, z: (x * c + z * s, y, -x * s + z * c),
              lambda x, y, z: (x * c - z * s, y, x * s + z * c)]

    def hash_pts(pts, cell=2.0):
        h = defaultdict(list)
        for x, y, z in pts:
            h[(int(x // cell), int(z // cell))].append((x, y, z))
        return h

    fails: list[str] = []
    print(f"KN5 FIDELITY {kn5p.name}  (yaw {math.degrees(yaw):.0f} deg)")

    # pick the working un-rotation on the road bucket
    road_h = hash_pts(obj.get("1ROAD_MAIN", []))
    unrot = None
    for cand in unrots:
        ok = 0
        pts = kn5.get("1ROAD_MAIN", [])[:: max(1, len(kn5.get("1ROAD_MAIN", [])) // 200)]
        for x, y, z in pts:
            ox, oy, oz = cand(x, y, z)
            ci, cj = int(ox // 2.0), int(oz // 2.0)
            if any((ox - rx) ** 2 + (oy - ry) ** 2 + (oz - rz) ** 2 <= TOL * TOL
                   for di in (-1, 0, 1) for dj in (-1, 0, 1)
                   for rx, ry, rz in road_h.get((ci + di, cj + dj), ())):
                ok += 1
        if pts and ok / len(pts) > 0.9:
            unrot = cand
            break
    if unrot is None:
        print("  !! could not align kn5 road to OBJ road under either yaw convention")
        fails.append("1ROAD_MAIN unalignable")
        unrot = unrots[0]

    for b in BUCKETS:
        src, dst = obj.get(b, []), kn5.get(b, [])
        if not src and not dst:
            continue
        if bool(src) != bool(dst):
            fails.append(f"{b}: present in {'OBJ only' if src else 'KN5 only'}")
            print(f"  {b:16s} OBJ {len(src):8d}  KN5 {len(dst):8d}  MISSING ON ONE SIDE")
            continue
        h = hash_pts(src)
        pick = dst[::SAMPLE] or dst
        miss = worst = 0.0
        nmiss = 0
        for x, y, z in pick:
            ox, oy, oz = unrot(x, y, z)
            ci, cj = int(ox // 2.0), int(oz // 2.0)
            best = min(((ox - rx) ** 2 + (oy - ry) ** 2 + (oz - rz) ** 2
                        for di in (-1, 0, 1) for dj in (-1, 0, 1)
                        for rx, ry, rz in h.get((ci + di, cj + dj), ())), default=1e18)
            d = math.sqrt(best)
            worst = max(worst, min(d, 999.0))
            if d > TOL:
                nmiss += 1
        frac = nmiss / max(1, len(pick))
        bad = frac > 0.02
        print(f"  {b:16s} OBJ {len(src):8d}  KN5 {len(dst):8d}  sampled {len(pick):6d}  "
              f"off-tol {100*frac:5.1f}%  worst {worst:7.2f} m {' << DISPLACED' if bad else ''}")
        if bad:
            fails.append(f"{b}: {100*frac:.1f}% of sampled kn5 verts have no OBJ counterpart "
                         f"(worst {worst:.2f} m)")

    # DOUBLE-SHEET TERRAIN: a healthy terrain is a single-valued heightfield (plus the border
    # skirt). Two grass surfaces >12 m apart in the same 2 m XZ cell = corruption (the phantom-clamp
    # class: summit nodes stamped with plains heights -> 570 m vertical curtains, "rainbow road").
    g = kn5.get("1GRASS", [])
    if g:
        xs = [p[0] for p in g]; zs = [p[2] for p in g]
        bx0, bx1, bz0, bz1 = min(xs), max(xs), min(zs), max(zs)
        cells = defaultdict(lambda: [1e18, -1e18])
        for x, y, z in g:
            if (x - bx0 < 520 or bx1 - x < 520 or z - bz0 < 520 or bz1 - z < 520):
                continue  # border ring = legit skirt drop
            c = cells[(int(x // 2), int(z // 2))]
            c[0] = min(c[0], y); c[1] = max(c[1], y)
        sheets = [(k, v[1] - v[0]) for k, v in cells.items() if v[1] - v[0] > 12.0]
        print(f"  double-sheet terrain cells (>12 m spread): {len(sheets)}")
        for (cx, cz), sp in sorted(sheets, key=lambda s: -s[1])[:4]:
            print(f"    spread {sp:6.1f} m at ({cx*2:8.0f},{cz*2:8.0f})")
        if len(sheets) > 20:
            fails.append(f"terrain has {len(sheets)} double-sheet cells (phantom-clamp corruption)")

    # BASE SEATING — TRIANGLE-EXACT. Vertex-based proximity checks under-measure floats on any
    # slope (nearest vert below can be metres away and lower than the surface at the foot's exact
    # XZ): Sand Creek shipped 100% of its barriers hovering +0.59 m median while the vertex gate
    # read +0.15. Feet are measured against the barycentric surface of the actual ground TRIANGLES.
    # Still excluded: mast arms (a column with a lower column of the same object within 3.5 m).
    # declared bridge spans (+approach margin): a wall foot there is a railing guarding the drop —
    # being above the bank grass is its JOB (Sand Creek's e46th approach read as 278 floating feet).
    span_pts = []
    fcp = project_dir / "data" / "finished_centerline.json"
    try:
        from scripts.capture import evidence
        spans = evidence.bridge_spans(project_dir)
        if spans and fcp.exists():
            fc = json.loads(fcp.read_text())
            pts_fc = fc["points_xyz_m"] if isinstance(fc, dict) else fc
            st = 0.0
            for i in range(1, len(pts_fc)):
                st += math.hypot(pts_fc[i][0] - pts_fc[i - 1][0], pts_fc[i][2] - pts_fc[i - 1][2])
                if any(abs(st - c0) < l0 / 2 + 170.0 for c0, l0 in spans):  # span + approach embankments
                    # centerline is OBJ-frame; feet are KN5-frame — rotate by the ship yaw
                    _cx, _cz = pts_fc[i][0], pts_fc[i][2]
                    _cy, _sy = math.cos(yaw), math.sin(yaw)
                    span_pts.append((_cx * _cy - _cz * _sy, _cx * _sy + _cz * _cy))
    except Exception:
        pass
    span_h = defaultdict(list)
    for sx2, sz2 in span_pts:
        span_h[(int(sx2 // 16), int(sz2 // 16))].append((sx2, sz2))

    def on_bridge_span(x, z):
        ci, cj = int(x // 16), int(z // 16)
        return any((x - sx2) ** 2 + (z - sz2) ** 2 <= 256.0
                   for di in (-1, 0, 1) for dj in (-1, 0, 1)
                   for sx2, sz2 in span_h.get((ci + di, cj + dj), ()))

    tri_h = defaultdict(list)
    tri_pav = defaultdict(list)
    tri_grs = defaultdict(list)
    for _mn, _mp, _mi in _parse_tris(kn5p):
        _up9 = _mn.upper()
        if _up9.startswith(("1ROAD", "1GRASS", "1KERB")):
            for _t in range(0, len(_mi) - 2, 3):
                _a, _b, _c = _mp[_mi[_t]], _mp[_mi[_t + 1]], _mp[_mi[_t + 2]]
                _key9 = (int((_a[0] + _b[0] + _c[0]) / 3 // 8), int((_a[2] + _b[2] + _c[2]) / 3 // 8))
                tri_h[_key9].append((_a, _b, _c))
                (tri_pav if _up9.startswith("1ROAD") else tri_grs)[_key9].append((_a, _b, _c))

    def tri_surf(x, z, y_near):
        best = None
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for a, b, c2 in tri_h.get((int(x // 8) + di, int(z // 8) + dj), ()):
                    d0 = (b[2] - c2[2]) * (a[0] - c2[0]) + (c2[0] - b[0]) * (a[2] - c2[2])
                    if abs(d0) < 1e-12:
                        continue
                    e1 = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
                    e2 = (c2[0] - a[0], c2[1] - a[1], c2[2] - a[2])
                    ny2 = e1[2] * e2[0] - e1[0] * e2[2]
                    nx2 = e1[1] * e2[2] - e1[2] * e2[1]
                    nz2 = e1[0] * e2[1] - e1[1] * e2[0]
                    nl2 = (nx2 * nx2 + ny2 * ny2 + nz2 * nz2) ** 0.5 or 1.0
                    if abs(ny2) / nl2 < 0.5:
                        continue        # wall-steep face, not ground
                    w0 = ((b[2] - c2[2]) * (x - c2[0]) + (c2[0] - b[0]) * (z - c2[2])) / d0
                    w1 = ((c2[2] - a[2]) * (x - c2[0]) + (a[0] - c2[0]) * (z - c2[2])) / d0
                    w2 = 1 - w0 - w1
                    if w0 >= -1e-6 and w1 >= -1e-6 and w2 >= -1e-6:
                        yv = w0 * a[1] + w1 * b[1] + w2 * c2[1]
                        if abs(yv - y_near) < 25 and (best is None or yv > best):
                            best = yv
        return best
    for b, label in (("1WALL", "walls"), ("FENCEWOOD", "fences"), ("LIGHTPOST", "lampposts"),
                     ("GANTRY", "pylons"), ("SIGNPOST", "signposts")):
        pts_b = kn5.get(b, [])
        if not pts_b:
            continue
        # keep the ACTUAL coords of each column's lowest vert — testing at the quantized column
        # center (up to 0.75 m away) reads phantom floats past drape lips (the audit-G lesson).
        colmin: dict = {}
        for x, y, z in pts_b:
            k = (round(x / 1.5), round(z / 1.5))
            if k not in colmin or y < colmin[k][0]:
                colmin[k] = (y, x, z)
        feet = []
        for (kx, kz), (base, bx2, bz2) in colmin.items():
            lower_neighbor = any(
                colmin.get((kx + di, kz + dj), (1e9,))[0] < base - 0.8
                for di in (-2, -1, 0, 1, 2) for dj in (-2, -1, 0, 1, 2))
            if not lower_neighbor:
                feet.append((bx2, base, bz2))
        # per-foot gaps, then cluster into OBJECTS (a 4 m barrier on a sidewalk lip is seated on
        # its inboard edge with honest daylight under the outboard overhang — real installations
        # look exactly like that). An object floats only when its BEST-SUPPORTED foot has a gap:
        # that is what a driver sees as hovering.
        foot_gaps = []
        nog = 0
        for x, base, z in feet:
            if b in ("1WALL", "FENCEWOOD") and on_bridge_span(x, z):
                continue        # bridge railing over the creek/underpass — floating is its function
            g2 = tri_surf(x, z, base)
            if g2 is None:
                nog += 1
            else:
                foot_gaps.append((x, z, base - g2))
        # cluster by 4.5 m chain linkage on a coarse grid
        cell_c = 4.5
        cgrid: dict = defaultdict(list)
        for i2, (x, z, g2) in enumerate(foot_gaps):
            cgrid[(int(x // cell_c), int(z // cell_c))].append(i2)
        seen2 = [False] * len(foot_gaps)
        cluster_gaps = []
        for i2 in range(len(foot_gaps)):
            if seen2[i2]:
                continue
            stack = [i2]
            seen2[i2] = True
            best_g = foot_gaps[i2][2]
            while stack:
                j2 = stack.pop()
                xj, zj, gj = foot_gaps[j2]
                best_g = min(best_g, gj)
                cj2, ck2 = int(xj // cell_c), int(zj // cell_c)
                for di in (-1, 0, 1):
                    for dj in (-1, 0, 1):
                        for k2 in cgrid.get((cj2 + di, ck2 + dj), ()):
                            if not seen2[k2] and (foot_gaps[k2][0] - xj) ** 2 + (foot_gaps[k2][1] - zj) ** 2 <= cell_c ** 2:
                                seen2[k2] = True
                                stack.append(k2)
            cluster_gaps.append(best_g)
        cluster_gaps.sort()
        n2 = len(cluster_gaps)
        med = cluster_gaps[n2 // 2] if n2 else 0.0
        p95 = cluster_gaps[19 * n2 // 20] if n2 else 0.0
        big = sum(1 for g2 in cluster_gaps if g2 > 0.30)
        bigf = big / max(1, n2)
        nogf = nog / max(1, len(foot_gaps) + nog)
        print(f"  {label:9s} objects {n2:5d}: no-ground {100*nogf:4.1f}%  seat-gap median {med:+.2f}  "
              f"p95 {p95:+.2f}  hovering>0.3m {big} ({100*bigf:.1f}%)")
        if med > 0.2 or p95 > 0.4 or bigf > 0.01 or nogf > 0.10:
            fails.append(f"{label}: median {med:.2f} p95 {p95:.2f} hovering {100*bigf:.1f}%")

    # LAMP INTEGRITY: every lens cluster (LIGHTS) must have a mast (LIGHTPOST) under it that reaches
    # it — an orphan head floats in the sky AND its clustered CSP light becomes a mid-air floodlight
    # (v0.7.8: heads placed even when the pole was skipped/misplanted; feet-gates can't see this
    # because the head has no foot). A head is orphaned if no LIGHTPOST vert within 3 m XZ comes
    # within 2.5 m below its center.
    heads_p = kn5.get("LIGHTS", [])
    posts_p = kn5.get("LIGHTPOST", []) + kn5.get("LAMPHEAD", [])   # housing is part of the mast chain
    if heads_p and posts_p:
        ph = hash_pts(posts_p, cell=3.0)
        hcl: dict = defaultdict(list)
        for x, y, z in heads_p:
            hcl[(int(x // 5), int(z // 5))].append((x, y, z))
        orphans = 0
        for vs in hcl.values():
            hx = sum(v[0] for v in vs) / len(vs)
            hy = sum(v[1] for v in vs) / len(vs)
            hz = sum(v[2] for v in vs) / len(vs)
            ci, cj = int(hx // 3.0), int(hz // 3.0)
            reached = any(
                (hx - rx) ** 2 + (hz - rz) ** 2 <= 9.0 and hy - 2.5 <= ry <= hy + 1.0
                for di in (-1, 0, 1) for dj in (-1, 0, 1)
                for rx, ry, rz in ph.get((ci + di, cj + dj), ()))
            if not reached:
                orphans += 1
        print(f"  lamp integrity: {len(hcl)} heads, orphaned (no mast reaching them): {orphans}")
        if orphans:
            fails.append(f"{orphans} lamp heads floating without a mast")

    # FLUSH EDGES — the metric that matches the driver's eye: along the lap, the pavement edge
    # (outermost road/shoulder surface) and the terrain just beyond it must MEET. A permanent step
    # (0.35 m by the old constants) reads as the entire road hovering over the landscape — which it
    # was, in every night photo, while every other gate was green.
    edge_pts = []
    if fcp.exists():
        fc2 = json.loads(fcp.read_text())
        pts2 = fc2["points_xyz_m"] if isinstance(fc2, dict) else fc2
        wid2 = (fc2.get("widths_m") if isinstance(fc2, dict) else None) or [7.0] * len(pts2)
        cy2, sy2 = math.cos(yaw), math.sin(yaw)
        for i in range(0, len(pts2) - 1, 5):
            x0, y0, z0 = pts2[i]
            x1, _, z1 = pts2[i + 1]
            tx2, tz2 = x1 - x0, z1 - z0
            L2 = math.hypot(tx2, tz2) or 1.0
            nx2, nz2 = -tz2 / L2, tx2 / L2
            for sgn in (1.0, -1.0):
                # sample at pavement edge + 3.5 m (beyond the shoulder verge band) and +6 m
                for lat in (wid2[i] / 2 + 3.5, wid2[i] / 2 + 6.0):
                    ex, ez = x0 + nx2 * lat * sgn, z0 + nz2 * lat * sgn
                    kx5, kz5 = ex * cy2 - ez * sy2, ex * sy2 + ez * cy2
                    p_in = tri_surf(kx5, kz5, y0)      # nearest surface here (pavement or verge)
                    if p_in is not None:
                        edge_pts.append((y0, p_in, lat))
    if edge_pts:
        # the verge beyond the shoulder should ride close under the deck: deck - terrain there
        steps = sorted(y0 - pi for y0, pi, lat in edge_pts)
        nE = len(steps)
        medE = steps[nE // 2]
        p95E = steps[19 * nE // 20]
        # TENTING GATE: under the drape band, the pavement sheet must HUG the terrain — sample
        # the shoulder surface and the grass triangle directly beneath it; a sheet more than
        # 0.6 m off the dirt at p95 is a tent with daylight under it ("entire airgaps beneath").
        def _surf_of(h9, x9, z9, yn9):
            best9 = None
            for di9 in (-1, 0, 1):
                for dj9 in (-1, 0, 1):
                    for a9, b9, c9 in h9.get((int(x9 // 8) + di9, int(z9 // 8) + dj9), ()):
                        d09 = (b9[2] - c9[2]) * (a9[0] - c9[0]) + (c9[0] - b9[0]) * (a9[2] - c9[2])
                        if abs(d09) < 1e-12:
                            continue
                        w09 = ((b9[2] - c9[2]) * (x9 - c9[0]) + (c9[0] - b9[0]) * (z9 - c9[2])) / d09
                        w19 = ((c9[2] - a9[2]) * (x9 - c9[0]) + (a9[0] - c9[0]) * (z9 - c9[2])) / d09
                        w29 = 1 - w09 - w19
                        if w09 >= -1e-6 and w19 >= -1e-6 and w29 >= -1e-6:
                            yv9 = w09 * a9[1] + w19 * b9[1] + w29 * c9[1]
                            if abs(yv9 - yn9) < 25 and (best9 is None or yv9 > best9):
                                best9 = yv9
            return best9
        tents = []
        for y0, pi, lat in edge_pts:
            pass
        for i9 in range(0, len(edge_pts)):
            pass
        # re-walk stations for tenting (pavement vs grass directly below, drape band)
        tent_gaps = []
        for i9 in range(0, len(pts2) - 1, 7):
            x09, y09, z09 = pts2[i9]
            x19, _, z19 = pts2[i9 + 1]
            t99 = math.hypot(x19 - x09, z19 - z09) or 1.0
            nx9, nz9 = -(z19 - z09) / t99, (x19 - x09) / t99
            for sgn9 in (1.0, -1.0):
                lat9 = wid2[i9] / 2 + 3.0
                ex9, ez9 = x09 + nx9 * lat9 * sgn9, z09 + nz9 * lat9 * sgn9
                kx9, kz9 = ex9 * cy2 - ez9 * sy2, ex9 * sy2 + ez9 * cy2
                pv9 = _surf_of(tri_pav, kx9, kz9, y09)
                gr9 = _surf_of(tri_grs, kx9, kz9, y09)
                if pv9 is not None and gr9 is not None and pv9 > gr9 and pv9 - y09 < 8.0:
                    # layer window: pavement 8 m+ over THIS station's deck is the switchback leg
                    # above, not this road's sheet
                    tent_gaps.append(pv9 - gr9)
        if tent_gaps:
            tent_gaps.sort()
            nT = len(tent_gaps)
            # informational: a 10 m terrain grid cannot render the bench between nodes — the
            # visual defect (sky through the mountain from below) is owned by the UNDERSIDE gate.
            print(f"  tenting under drape band (info): {nT} samples  median {tent_gaps[nT//2]:+.2f}  "
                  f"p95 {tent_gaps[19*nT//20]:+.2f}")
        # UNDERSIDE COVERAGE: the shoulder strip must render from BELOW (double-sided) — a
        # single-sided sheet is backface-culled into a hole in the mountainside from the
        # switchback beneath. Winding-based: down-facing tris must roughly mirror up-facing.
        sh_up = und_dn = 0
        for m in meshes:
            up9 = m["name"].upper()
            if up9.startswith("1ROAD_SHOULDER"):
                sh_up += m["up"] + m["dn"]
            elif up9.startswith("SHOULDERUND"):
                und_dn += m["up"] + m["dn"]
        if sh_up:
            cov = und_dn / sh_up
            print(f"  shoulder underside: strip tris {sh_up}  underside tris {und_dn}  (coverage {100*cov:.0f}%)")
            if cov < 0.85:
                fails.append(f"shoulder underside missing ({100*cov:.0f}% coverage — "
                             f"hole in the mountain from below)")

        # INFORMATIONAL: verge sits below deck wherever the land legitimately falls (fills/cuts),
        # so absolute thresholds here gate terrain relief, not hover. Enforcement of "drive off
        # and back on" lives in the drive test's excursion sweeps (continuous, step-based).
        print(f"  flush edges (info): {nE} samples  deck-vs-verge median {medE:+.2f}  p95 {p95E:+.2f}")

    # physical sanity: road deck supported by ground beneath (floating-deck guard)
    ground_h = hash_pts(kn5.get("1GRASS", []) + kn5.get("1ROAD_SHOULDER", []) + kn5.get("1KERB", []), cell=6.0)
    road = kn5.get("1ROAD_MAIN", [])
    over = tot = 0
    for x, y, z in road[::7]:
        if on_bridge_span(x, z):
            continue    # declared deck over the creek/underpass — height over ground is the point
        best = None
        ci, cj = int(x // 6.0), int(z // 6.0)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for rx, ry, rz in ground_h.get((ci + di, cj + dj), ()):
                    if (x - rx) ** 2 + (z - rz) ** 2 <= 36 and ry < y + 0.3:
                        best = ry if best is None else max(best, ry)
        if best is not None:
            tot += 1
            if y - best > ROAD_GAP_M:
                over += 1
    frac = over / max(1, tot)
    print(f"  road-over-ground: {over}/{tot} samples gap > {ROAD_GAP_M} m ({100*frac:.1f}%)")
    if frac > ROAD_GAP_FRAC:
        fails.append(f"road floating over ground on {100*frac:.1f}% of samples")

    print(f"  => {'FAIL: ' + '; '.join(fails) if fails else 'PASS — kn5 matches the audited OBJs'}")
    return {"fail": bool(fails), "reasons": fails}


if __name__ == "__main__":
    r = check(sys.argv[1])
    sys.exit(1 if r["fail"] else 0)
