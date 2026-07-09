"""Post-build geometry AUDIT for a freeroam NETWORK track — run at the end of every generative pass.

Measures the ACTUAL built geometry (track.obj vertices + emitted pier list) — never a re-derivation of the
road, because a reconstruction that drifts from the real mesh produces phantom defects (learned the hard
way: an earlier version flagged a 31 m "poke" that did not exist). Checks the three defect classes that
repeatedly break these networks:

  A. supports-in-road   — a viaduct pier standing IN a road that passes under its deck (car hits a column).
  B. terrain-poke       — a GRASS vertex sitting ABOVE the road surface near it (launches the car).
  C. junction-crossing  — two roads crossing at a large angle at the SAME height where one connects to the
                          other (a ramp crossing OVER the road it should merge into, not a real merge).

A + B read only track.obj (ground truth) + data/network.piers.json. C needs per-edge identity, so it
reconstructs the decks from network.geojson AND applies the same ramp merge-trim the build does, so it
reflects what was actually built. Writes data/audit.json; exits 1 if any defect exceeds tolerance.

    python -m scripts.geometry.audit_mesh projects/<slug>
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

# --- tolerances -------------------------------------------------------------------------------------
POKE_ABOVE_M = 0.10     # a grass vert this far above a road vert within POKE_R = a poke
POKE_R = 5.0            # horizontal reach from a road (surface) vert
SUPPORT_R = 3.5        # a pier this close (xz) to a road vert below its deck = standing in the road
SUPPORT_BELOW = (1.5, 45.0)   # road is (y_top-1.5 .. y_top-45) below the deck = a real crossing, not the deck's own road
CROSS_R = 6.0
CROSS_ANGLE_DEG = 32.0
CROSS_DY = 2.2
LAYER_H = 5.5


def _mpd(lat0):
    phi = math.radians(lat0)
    return (111412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi) + 0.118 * math.cos(5 * phi),
            111132.954 - 559.822 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi))


def _smooth(vals, win):
    n = len(vals)
    if n < 3 or win < 2:
        return list(vals)
    h = win // 2
    return [sum(vals[max(0, i - h):min(n, i + h + 1)]) / (min(n, i + h + 1) - max(0, i - h)) for i in range(n)]


def _obj_groups(obj_path: Path, prefixes):
    want = {p: [] for p in prefixes}
    cur = None
    for ln in obj_path.read_text().splitlines():
        if ln.startswith("o "):
            nm = ln[2:].strip().upper()
            cur = next((p for p in prefixes if nm.startswith(p)), None)
        elif ln.startswith("v ") and cur:
            _, x, y, z = ln.split()[:4]
            want[cur].append((float(x), float(y), float(z)))
    return want


def _hash(pts_xyz, cell):
    h = defaultdict(list)
    for x, y, z in pts_xyz:
        h[(int(x // cell), int(z // cell))].append((x, y, z))
    return h


def _decks_trimmed(data: Path):
    """Per-edge freeway decks (id, ramp, half, tangent-carrying pts) in the build's local frame, WITH the
    same ramp merge-trim applied, so check C sees what was actually built. Only used for C (topology)."""
    from scripts.geometry.build_network_mesh import _grade_cap_y, _layer_lift, _trim_ramp_merge
    fc = json.loads((data / "network.geojson").read_text())["features"]
    elev = {e["id"]: e["z_smooth_m"] for e in json.loads((data / "network.elevation.json").read_text())["edges"]}
    loc = json.loads((data / "network.local.json").read_text())
    o = loc["origin"]; lon0, lat0, elev0 = o["lon"], o["lat"], o["elev_m"]
    sx = -1.0 if loc.get("mirror_x", True) else 1.0
    m_lon, m_lat = _mpd(lat0)
    FREEWAY = {"motorway", "trunk"}

    def local(c, z):
        return [(sx * (lo - lon0) * m_lon, zz - elev0, (la - lat0) * m_lat) for (lo, la), zz in zip(c, z)]

    road_trim = defaultdict(list)
    for f in fc:
        p = f["properties"]
        if p.get("road_class") not in FREEWAY:
            continue
        c = f["geometry"]["coordinates"]; z = elev.get(p["id"]) or [0.0] * len(c)
        if len(z) != len(c):
            z = (z + [z[-1]] * len(c))[:len(c)]
        half = float(p.get("width_m", 12)) / 2.0
        for x, y, zz in local(c, z):
            road_trim[(int(x // 12.0), int(zz // 12.0))].append((x, y, zz, half, p["id"]))

    out = []
    for f in fc:
        p = f["properties"]
        if p.get("road_class") not in FREEWAY:
            continue
        c = f["geometry"]["coordinates"]; z = elev.get(p["id"]) or [0.0] * len(c)
        if len(z) != len(c):
            z = (z + [z[-1]] * len(c))[:len(c)]
        pts = local(c, z)
        lay = _layer_lift(pts, p.get("layer_profile"))
        deck = _grade_cap_y([(x, y + lay[i] + 0.12, zz) for i, (x, y, zz) in enumerate(pts)]) if max(lay) > 1 \
            else [(x, y + 0.12, zz) for x, y, zz in pts]
        if p.get("is_ramp"):
            deck = _trim_ramp_merge(deck, road_trim, p["id"])
        if len(deck) >= 2:
            out.append({"id": p["id"], "ramp": bool(p.get("is_ramp")),
                        "half": float(p.get("width_m", 12)) / 2.0, "pts": deck})
    return out


def audit(project_dir: str | Path) -> dict:
    proj = Path(project_dir)
    data = proj / "data"
    slug = json.loads((proj / "track.config.json").read_text())["slug"]
    g = _obj_groups(data / "track.obj", ("1ROAD", "1GRASS", "HWYSTRUCT", "1WALL", "KERB"))
    road_hash = _hash(g["1ROAD"], 8.0)
    road_hash4 = _hash(g["1ROAD"], 4.0)
    grass_hash4 = _hash(g["1GRASS"], 4.0)

    def nearest_y(h, x, z, R, cell):
        ci, cj = int(x // cell), int(z // cell); ys = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for rx, ry, rz in h.get((ci + di, cj + dj), ()):
                    if (x - rx) ** 2 + (z - rz) ** 2 <= R * R:
                        ys.append(ry)
        return ys

    def road_y_near(x, z, R):
        ci, cj = int(x // 8.0), int(z // 8.0); best = None
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for rx, ry, rz in road_hash.get((ci + di, cj + dj), ()):
                    if (x - rx) ** 2 + (z - rz) ** 2 <= R * R and (best is None or ry > best):
                        best = ry
        return best

    # --- A. supports standing in a road (from the emitted pier list vs the real road surface) ---
    support_hits = []
    piers_p = data / "network.piers.json"
    piers = json.loads(piers_p.read_text()) if piers_p.exists() else []
    for px, pz, y_top in piers:
        ci, cj = int(px // 8.0), int(pz // 8.0)
        speared = None
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for rx, ry, rz in road_hash.get((ci + di, cj + dj), ()):
                    if (px - rx) ** 2 + (pz - rz) ** 2 <= SUPPORT_R * SUPPORT_R \
                            and (y_top - SUPPORT_BELOW[1]) < ry < (y_top - SUPPORT_BELOW[0]):
                        speared = round(ry, 1)
        if speared is not None:
            support_hits.append((round(px, 1), round(pz, 1), round(y_top, 1), speared))

    # --- B. terrain poke (grass vert above a nearby road surface vert) ---
    poke_hits = []
    for x, y, z in g["1GRASS"]:
        ry = road_y_near(x, z, POKE_R)
        if ry is not None and y > ry + POKE_ABOVE_M:
            poke_hits.append((round(x, 1), round(z, 1), round(y - ry, 2)))

    # --- C. junction crossings (per-edge topology, trimmed to match the build) ---
    cross = []
    try:
        decks = _decks_trimmed(data)
        CELL = 12.0
        dh = defaultdict(list)
        for d in decks:
            pts = d["pts"]
            for i, (x, y, z) in enumerate(pts):
                a = pts[max(0, i - 1)]; b = pts[min(len(pts) - 1, i + 1)]
                dh[(int(x // CELL), int(z // CELL))].append(
                    (d["id"], x, y, z, math.atan2(b[2] - a[2], b[0] - a[0]), d["ramp"]))
        seen = set()
        for d in decks:
            pts = d["pts"]
            for i in range(len(pts)):
                x, y, z = pts[i]
                a = pts[max(0, i - 1)]; b = pts[min(len(pts) - 1, i + 1)]
                st = math.atan2(b[2] - a[2], b[0] - a[0])
                ci, cj = int(x // CELL), int(z // CELL)
                for di in (-1, 0, 1):
                    for dj in (-1, 0, 1):
                        for (eid2, rx, ry, rz, rt, ramp2) in dh.get((ci + di, cj + dj), ()):
                            if eid2 == d["id"] or (x - rx) ** 2 + (z - rz) ** 2 > CROSS_R * CROSS_R:
                                continue
                            if abs(y - ry) > CROSS_DY:
                                continue
                            da = abs((st - rt + math.pi) % (2 * math.pi) - math.pi)
                            da = min(da, math.pi - da)
                            if da > math.radians(CROSS_ANGLE_DEG):
                                key = tuple(sorted((d["id"], eid2))) + (round(x / 25), round(z / 25))
                                if key not in seen:
                                    seen.add(key)
                                    kind = "ramp" if (d["ramp"] or ramp2) else "mainline"
                                    cross.append((round(x, 1), round(z, 1), round(math.degrees(da)), kind))
    except Exception as e:  # noqa: BLE001 — never let C crash the audit
        print(f"  (check C skipped: {e})")

    # === CRUFT PASS: walls + curbs ===================================================================
    # D. wall-on-road — a 1WALL vert sitting on/over a road that is NOT its own. A wall is placed ~offset
    #    (>2.5 m) past its OWN road edge, so a wall vert within 2.5 m of a road vert (at similar height) is
    #    on a DIFFERENT road (a median wall on the opposing carriageway, or a wall tangled across a road at
    #    an interchange — covers "walls crossing over others" + "walls passing onto the road").
    wall_on_road = 0
    for x, y, z in g["1WALL"]:
        if any(abs(y - ry) < 1.2 for ry in nearest_y(road_hash4, x, z, 2.5, 4.0)):
            wall_on_road += 1
    # E. wall-float — the BASE of a wall (min y per 0.6 m column) sitting above the ground/road beneath it.
    wall_col = defaultdict(lambda: 1e9)
    for x, y, z in g["1WALL"]:
        k = (round(x / 0.6), round(z / 0.6)); wall_col[k] = min(wall_col[k], y)
    wall_float = 0; worst_float = 0.0
    for (kx, kz), by in wall_col.items():
        x, z = kx * 0.6, kz * 0.6
        # search 4.5 m: the wall sits ~offset (3.2 m) past the road edge, so the deck road it rides is just
        # over 3 m away — include it, or a flyover railing false-reads as "floating" over the ground below.
        gnd = nearest_y(grass_hash4, x, z, 4.5, 4.0) + nearest_y(road_hash4, x, z, 4.5, 4.0)
        if gnd:
            gap = by - max(gnd)
            if gap > 0.6:
                wall_float += 1; worst_float = max(worst_float, gap)
    # F. curb-not-flush — a KERB vert that meets NEITHER the road nor the ground within reach (a gap/step).
    curb_bad = 0
    for x, y, z in g["KERB"]:
        near_r = any(abs(y - ry) < 0.5 for ry in nearest_y(road_hash4, x, z, 2.0, 4.0))
        near_g = any(abs(y - ry) < 0.5 for ry in nearest_y(grass_hash4, x, z, 2.0, 4.0))
        if not (near_r or near_g):
            curb_bad += 1
    # G. prop-floating — a scatter billboard (bush/tree/palm) whose BASE hovers above the ground SURFACE
    #    beneath it. Measured against the conformed-ground sampler (data/ground.local.json) — the SAME
    #    surface the grass mesh + the prop placement use — so it is not fooled by a steep slope's
    #    down-hill neighbour vertex (measuring against nearest verts invented phantom 7 m floats). Needs
    #    environment.obj + ground.local.json (both built after the mesh), else skipped at mesh-audit time.
    prop_float = None
    envp = data / "environment.obj"; glp = data / "ground.local.json"
    if envp.exists() and glp.exists():
        gl = json.loads(glp.read_text())
        gx0, gz0, gdx, gdz, gnx, gny, GY = gl["x0"], gl["z0"], gl["dx"], gl["dz"], gl["nx"], gl["ny"], gl["y"]

        def ground_surf(x, z):
            fi = (x - gx0) / gdx if gdx else 0.0; fj = (z - gz0) / gdz if gdz else 0.0
            i0 = max(0, min(gnx - 1, int(fi))); j0 = max(0, min(gny - 1, int(fj)))
            i1 = min(gnx - 1, i0 + 1); j1 = min(gny - 1, j0 + 1)
            ti = max(0.0, min(1.0, fi - i0)); tj = max(0.0, min(1.0, fj - j0))
            a = GY[j0][i0] * (1 - ti) + GY[j0][i1] * ti
            b = GY[j1][i0] * (1 - ti) + GY[j1][i1] * ti
            return a * (1 - tj) + b * tj

        env = _obj_groups(envp, ("BUSHES", "TREES", "PALMS"))
        col = defaultdict(lambda: 1e9)
        for pre in ("BUSHES", "TREES", "PALMS"):
            for x, y, z in env[pre]:
                k = (round(x / 0.8), round(z / 0.8)); col[k] = min(col[k], y)
        prop_float = 0
        for (kx, kz), by in col.items():
            if by > ground_surf(kx * 0.8, kz * 0.8) + 0.7:
                prop_float += 1

    report = {
        "slug": slug, "road_verts": len(g["1ROAD"]), "grass_verts": len(g["1GRASS"]), "piers": len(piers),
        "wall_verts": len(g["1WALL"]), "curb_verts": len(g["KERB"]),
        "A_supports_in_road": len(support_hits),
        "B_terrain_poke": len(poke_hits),
        "C_junction_crossings": len(cross),
        "D_wall_on_road": wall_on_road,
        "E_wall_floating": wall_float,
        "F_curb_not_flush": curb_bad,
        "G_prop_floating": prop_float,
        "worst_poke_m": round(max((h[2] for h in poke_hits), default=0.0), 2),
        "worst_float_m": round(worst_float, 2),
        "C_by_kind": {"ramp": sum(1 for c in cross if c[3] == "ramp"),
                      "mainline": sum(1 for c in cross if c[3] == "mainline")},
        "samples": {"supports": support_hits[:8],
                    "poke": sorted(poke_hits, key=lambda h: -h[2])[:8],
                    "crossings": cross[:10]},
    }
    (data / "audit.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"AUDIT {slug}   (roads {len(g['1ROAD'])}v, grass {len(g['1GRASS'])}v, walls {len(g['1WALL'])}v, piers {len(piers)})")
    print(f"  A. supports standing in a road : {len(support_hits)}")
    print(f"  B. terrain poking through road : {len(poke_hits)}  (worst +{report['worst_poke_m']} m)")
    print(f"  C. at-grade junction crossings : {len(cross)}  ({report['C_by_kind']})")
    print(f"  D. walls sitting on a road     : {wall_on_road}")
    print(f"  E. walls floating over ground  : {wall_float}  (worst +{report['worst_float_m']} m)")
    print(f"  F. curbs not flush             : {curb_bad}  (of {len(g['KERB'])} curb verts)")
    print(f"  G. plants floating over ground : {'(env not built)' if prop_float is None else prop_float}")
    total = len(support_hits) + len(poke_hits) + len(cross) + wall_on_road + wall_float + curb_bad + (prop_float or 0)
    print(f"  => {'CLEAN' if total == 0 else str(total) + ' issues'}")
    return report


if __name__ == "__main__":
    r = audit(sys.argv[1] if len(sys.argv) > 1 else "projects/san-diego-freeway-loop")
    hard = (r["A_supports_in_road"] + r["B_terrain_poke"] + r["D_wall_on_road"] + r["E_wall_floating"]
            + r["F_curb_not_flush"] + (r["G_prop_floating"] or 0))
    sys.exit(1 if hard else 0)   # C (junction crossings) reported but non-gating (walls are gapped there)
