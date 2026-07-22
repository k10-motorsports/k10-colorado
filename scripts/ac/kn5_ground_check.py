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
           "LIGHTPOST", "LIGHTS", "FENCEWOOD", "CONIFER", "MARKINGS", "YLINE",
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
    tri_h = defaultdict(list)
    for _mn, _mp, _mi in _parse_tris(kn5p):
        if _mn.upper().startswith(("1ROAD", "1GRASS")):
            for _t in range(0, len(_mi) - 2, 3):
                _a, _b, _c = _mp[_mi[_t]], _mp[_mi[_t + 1]], _mp[_mi[_t + 2]]
                tri_h[(int((_a[0] + _b[0] + _c[0]) / 3 // 8),
                       int((_a[2] + _b[2] + _c[2]) / 3 // 8))].append((_a, _b, _c))

    def tri_surf(x, z, y_near):
        best = None
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for a, b, c2 in tri_h.get((int(x // 8) + di, int(z // 8) + dj), ()):
                    d0 = (b[2] - c2[2]) * (a[0] - c2[0]) + (c2[0] - b[0]) * (a[2] - c2[2])
                    if abs(d0) < 1e-12:
                        continue
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
        colmin: dict = defaultdict(lambda: 1e9)
        for x, y, z in pts_b:
            k = (round(x / 1.5), round(z / 1.5))
            colmin[k] = min(colmin[k], y)
        feet = []
        for (kx, kz), base in colmin.items():
            lower_neighbor = any(
                colmin.get((kx + di, kz + dj), 1e9) < base - 0.8
                for di in (-2, -1, 0, 1, 2) for dj in (-2, -1, 0, 1, 2))
            if not lower_neighbor:
                feet.append((kx * 1.5, base, kz * 1.5))
        gaps2, nog = [], 0
        for x, base, z in feet:
            g2 = tri_surf(x, z, base)
            if g2 is None:
                nog += 1
            else:
                gaps2.append(base - g2)
        gaps2.sort()
        n2 = len(gaps2)
        med = gaps2[n2 // 2] if n2 else 0.0
        p95 = gaps2[19 * n2 // 20] if n2 else 0.0
        big = sum(1 for g2 in gaps2 if g2 > 0.35)
        bigf = big / max(1, n2)
        nogf = nog / max(1, n2 + nog)
        print(f"  {label:9s} feet {n2+nog:5d}: no-ground {100*nogf:4.1f}%  gap median {med:+.2f}  "
              f"p95 {p95:+.2f}  floating>0.35m {big} ({100*bigf:.1f}%)")
        if med > 0.25 or p95 > 0.5 or bigf > 0.01 or nogf > 0.10:
            fails.append(f"{label}: median {med:.2f} p95 {p95:.2f} floating {100*bigf:.1f}%")

    # LAMP INTEGRITY: every lens cluster (LIGHTS) must have a mast (LIGHTPOST) under it that reaches
    # it — an orphan head floats in the sky AND its clustered CSP light becomes a mid-air floodlight
    # (v0.7.8: heads placed even when the pole was skipped/misplanted; feet-gates can't see this
    # because the head has no foot). A head is orphaned if no LIGHTPOST vert within 3 m XZ comes
    # within 2.5 m below its center.
    heads_p = kn5.get("LIGHTS", [])
    posts_p = kn5.get("LIGHTPOST", [])
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

    # physical sanity: road deck supported by ground beneath (floating-deck guard)
    ground_h = hash_pts(kn5.get("1GRASS", []) + kn5.get("1ROAD_SHOULDER", []), cell=6.0)
    road = kn5.get("1ROAD_MAIN", [])
    over = tot = 0
    for x, y, z in road[::7]:
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
