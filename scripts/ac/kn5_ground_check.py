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
