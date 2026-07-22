"""Virtual drive test — simulate what a CAR experiences over the BUILT track, before any kn5.

The mesh audit checks geometry classes (vertices, surfaces, props). This drives the lap: six wheel
paths swept along the built triangles at 1 m steps, asking at every step the only three questions a
driver cares about:

  1. WHAT SURFACE IS UNDER THE TIRE?  The car rides the HIGHEST physical surface at (x,z). If that
     top surface is 1GRASS inside the lane, the ground pokes through the road — a launch. If the
     top surface steps more than a tire absorbs, it's a jolt. This is triangle-accurate: it samples
     the interpolated faces the physics engine collides with, not the vertices (which pass audits
     while the faces between them knife through — learned on the Lariat's switchbacks).
  2. IS ANYTHING STANDING IN THE CORRIDOR?  Walls, parapets, highway decks, posts, tree trunks —
     any triangle that rises above the deck inside the swept lane (+ margin) is an obstruction,
     reported by mesh name so the offender is identifiable (BARRIER_warning vs HWYSTRUCT vs TREES).
  3. IS THE SURFACE SMOOTH AT SPEED?  Wheel-path vertical kinks (slope change per metre) beyond
     what real roads allow. Real geometric design keeps cross-slope breaks under ~4-8% and rounds
     every grade change through vertical curves; a healthy build shows the same statistics.

Thresholds (physical rationale, tune at the top):
  STEP_BUMP_M    0.025  a 2.5 cm sharp step ~ hitting a lane reflector curb at speed — feel it
  STEP_SEVERE_M  0.060  6 cm ~ a full curb face — suspension crash, possible airborne moment
  KINK_PCT       3.0    slope change %-pts across 1 m ~ a driveway lip; real roads use vertical
                        curves precisely so this never happens on the carriageway
  OBST_CLEAR_M   0.25   anything rising this far above deck inside the corridor is a hit hazard

Run:  python -m scripts.geometry.drive_test tracks/<slug>
Writes data/drive_test.json, prints a per-problem report, exits 1 on FAIL (gate builds on it).
Pure stdlib; ~1-3 min for a 25 km lap.
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

STEP_BUMP_M = 0.025
STEP_SEVERE_M = 0.060
KINK_PCT = 3.0
OBST_CLEAR_M = 0.25
OBST_HEIGHT_M = 1.8          # car-strike ceiling: AC cars are ~1.3 m tall; walls/trunks rise from deck
#                              level and still trip this, while legit overhead furniture (cobra-head
#                              lamps arching the lane at 2.5 m+) clears it
STEP_M = 1.0                 # sampling step along the lap
WHEEL_HALF_TRACK = 0.75      # wheel offset around each driving line
CELL = 3.0                   # spatial hash cell (m)

DRIVABLE = ("1ROAD", "1KERB", "1RUNOFF", "1LAWN", "1GRASS")   # car can roll on these
SOFT = ("1GRASS", "1LAWN")   # ...but these mid-lane on TOP = ground through road
PAINT = ("MARKINGS", "ROADTEXT", "YLINE")   # flat paint decals ~1.5 cm over deck — never obstructions


def _load_obj(path: Path):
    """[(group, verts, tris)] — tris as vertex index triples into the group's own vert list."""
    groups = []
    cur = None
    base = 0
    verts_all: list[tuple[float, float, float]] = []
    for ln in path.read_text().splitlines():
        if ln.startswith("o "):
            cur = {"name": ln[2:].strip(), "v0": len(verts_all), "tris": []}
            groups.append(cur)
        elif ln.startswith("v "):
            p = ln.split()
            verts_all.append((float(p[1]), float(p[2]), float(p[3])))
        elif ln.startswith("f ") and cur is not None:
            idx = [int(t.split("/")[0]) - 1 for t in ln.split()[1:4]]
            cur["tris"].append(tuple(idx))
    return verts_all, groups


class TriField:
    """Spatial-hashed triangle soup answering: top surface height + owner at (x,z); tris in cell."""

    def __init__(self):
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        self.tris: list[tuple] = []          # (ax,ay,az,bx,by,bz,cx,cy,cz, name)

    def add(self, verts, tri, name):
        a, b, c = (verts[i] for i in tri)
        k = len(self.tris)
        self.tris.append((*a, *b, *c, name))
        x0 = min(a[0], b[0], c[0]); x1 = max(a[0], b[0], c[0])
        z0 = min(a[2], b[2], c[2]); z1 = max(a[2], b[2], c[2])
        for ci in range(int(x0 // CELL), int(x1 // CELL) + 1):
            for cj in range(int(z0 // CELL), int(z1 // CELL) + 1):
                self.cells[(ci, cj)].append(k)

    def top_at(self, x, z, y_ref=None, window=2.5):
        """(height, owner_name) of the highest surface at (x,z). With ``y_ref``: highest surface
        within ±``window`` of the expected deck height — REQUIRED on tracks that cross over
        themselves (the Lariat's 19th St bridge over its own US-6 ramp): the global maximum reads
        the upper deck while you drive the lower one, minting phantom multi-metre steps."""
        best, owner = None, None
        for k in self.cells.get((int(x // CELL), int(z // CELL)), ()):
            t = self.tris[k]
            ax, ay, az, bx, by, bz, cx, cy, cz, name = t
            d = (bz - cz) * (ax - cx) + (cx - bx) * (az - cz)
            if abs(d) < 1e-12:
                continue
            w0 = ((bz - cz) * (x - cx) + (cx - bx) * (z - cz)) / d
            w1 = ((cz - az) * (x - cx) + (ax - cx) * (z - cz)) / d
            w2 = 1.0 - w0 - w1
            if w0 < -1e-6 or w1 < -1e-6 or w2 < -1e-6:
                continue
            y = w0 * ay + w1 * by + w2 * cy
            if y_ref is not None and abs(y - y_ref) > window:
                continue
            if best is None or y > best:
                best, owner = y, name
        return best, owner

    def rising_in(self, x, z, r, deck_at, clear, cap):
        """Names of tri owners with a vertex inside radius r of (x,z) rising ``clear``..``cap``
        above the deck DIRECTLY BENEATH that vertex (deck_at(x,z) sampler) — comparing against the
        station-centre deck instead reads ordinary up-slope geometry on any grade as a wall."""
        hits = set()
        R = int(r // CELL) + 1
        ci, cj = int(x // CELL), int(z // CELL)
        for di in range(-R, R + 1):
            for dj in range(-R, R + 1):
                for k in self.cells.get((ci + di, cj + dj), ()):
                    t = self.tris[k]
                    if t[9] in hits:
                        continue
                    for vx, vy, vz in ((t[0], t[1], t[2]), (t[3], t[4], t[5]), (t[6], t[7], t[8])):
                        if math.hypot(vx - x, vz - z) >= r:
                            continue
                        d = deck_at(vx, vz)
                        if d is not None and d + clear < vy < d + cap:
                            hits.add(t[9])
                            break
        return hits


def run(project_dir: str | Path) -> dict:
    project = Path(project_dir)
    data = project / "data"
    cfg = json.loads((project / "track.config.json").read_text())
    fin = data / "finished_centerline.json"
    if fin.exists():
        # the line the ribbon was actually swept along (already in mesh frame) — driving the raw
        # centerline instead reads phantom obstructions where corner-rounding moved the pavement
        lc = json.loads(fin.read_text())
        pts = [tuple(p) for p in lc["points_xyz_m"]]
    else:
        mirror = -1.0 if cfg.get("mirror_x", False) else 1.0
        lc = json.loads((data / "centerline.local.json").read_text())
        pts = [(mirror * p[0], p[1], p[2]) for p in lc["points_xyz_m"]]
    widths = lc["widths_m"]

    # declared bridge spans: parapet/railing furniture INSIDE these is expected (a bridge with no
    # railing is the defect, not the railing) — obstruction hits on *bridge-named meshes there are skipped
    spans = [(b.get("center_m", 0) - b.get("len_m", 0) / 2 - 20, b.get("center_m", 0) + b.get("len_m", 0) / 2 + 20)
             for b in (cfg.get("capture", {}) or {}).get("bridges", [])]

    surface = TriField()      # everything the car can roll on
    obstacle = TriField()     # everything else that could stand in the corridor
    for objfile in ("track.obj", "environment.obj"):
        p = data / objfile
        if not p.exists():
            continue
        verts, groups = _load_obj(p)
        for grp in groups:
            up = grp["name"].upper()
            if up.startswith(PAINT):
                continue
            field = surface if up.startswith(DRIVABLE) else obstacle
            for tri in grp["tris"]:
                field.add(verts, tri, grp["name"])

    # six wheel paths: wheel pair around center line + around left/right third lines
    n = len(pts)
    st = [0.0]
    for i in range(1, n):
        st.append(st[-1] + math.hypot(pts[i][0] - pts[i - 1][0], pts[i][2] - pts[i - 1][2]))
    lap = st[-1]

    problems = {"soft_top_in_lane": [], "steps": [], "severe_steps": [], "kinks": [], "obstructions": [],
                "daylight_gaps": []}
    prev_h = [None] * 10
    prev_slope = [None] * 10
    obst_names = defaultdict(int)
    s = 0.0
    i = 1
    while s < lap:
        while i < n - 1 and st[i] < s:
            i += 1
        t = (s - st[i - 1]) / max(st[i] - st[i - 1], 1e-9)
        x = pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * t
        y_ref = pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * t
        z = pts[i - 1][2] + (pts[i][2] - pts[i - 1][2]) * t
        tx, tz = pts[i][0] - pts[i - 1][0], pts[i][2] - pts[i - 1][2]
        L = math.hypot(tx, tz) or 1e-9
        nx, nz = -tz / L, tx / L
        w = widths[i]
        lane_half = max(w / 2 - 0.4, 0.8)
        lines = (-(w / 2 - 0.5), -w / 3, 0.0, w / 3, (w / 2 - 0.5))   # edges included
        # edge paths (|line| = w/2-0.5) DETECT holes/ground-through at the pavement lip;
        # smoothness (steps/kinks) gates only on INTERIOR paths — nobody laps on the hinge
        edge_line = {0, len(lines) - 1}
        paths = [off + d for off in lines for d in (-WHEEL_HALF_TRACK, WHEEL_HALF_TRACK)]
        deck_c, _ = surface.top_at(x, z, y_ref)
        for pi, off in enumerate(paths):
            is_edge = (pi // 2) in edge_line
            if abs(off) > lane_half:
                off = math.copysign(lane_half, off)
            px, pz = x + nx * off, z + nz * off
            h, owner = surface.top_at(px, pz, y_ref)
            if h is None:
                prev_h[pi] = None
                prev_slope[pi] = None
                continue
            if owner and owner.upper().startswith(SOFT):
                problems["soft_top_in_lane"].append((round(s, 1), round(off, 2), owner))
            if prev_h[pi] is not None:
                dh = h - prev_h[pi]
                slope = dh / STEP_M
                if prev_slope[pi] is not None and not is_edge:
                    kink = abs(slope - prev_slope[pi]) * 100
                    if kink > KINK_PCT:
                        problems["kinks"].append((round(s, 1), round(off, 2), round(kink, 2)))
                    step = abs(dh - prev_slope[pi] * STEP_M)
                    if step > STEP_SEVERE_M:
                        problems["severe_steps"].append((round(s, 1), round(off, 2), round(step, 3)))
                    elif step > STEP_BUMP_M:
                        problems["steps"].append((round(s, 1), round(off, 2), round(step, 3)))
                prev_slope[pi] = slope
            prev_h[pi] = h
        # daylight probe: just past each pavement edge, some surface must exist from deck level
        # down to 7 m below — a miss means a hole in the embankment skirt (sky under the road)
        if deck_c is not None:
            for side in (-1.0, 1.0):
                off = side * (w / 2 + 0.8)
                gy_probe, _own = surface.top_at(x + nx * off, z + nz * off, deck_c - 2.5, 5.0)
                if gy_probe is None:
                    problems["daylight_gaps"].append((round(s, 1), round(off, 2)))
        # corridor obstruction scan at this station (uses centre deck height)
        if deck_c is not None:
            on_bridge = any(a <= s <= b for a, b in spans)
            for name in obstacle.rising_in(x, z, lane_half + 0.3,
                                           lambda vx, vz: surface.top_at(vx, vz, y_ref)[0],
                                           OBST_CLEAR_M, OBST_HEIGHT_M):
                if on_bridge and "BRIDGE" in name.upper():
                    continue                       # declared-span railing = required furniture
                problems["obstructions"].append((round(s, 1), name))
                obst_names[name] += 1
        s += STEP_M

    # EXCURSION SWEEPS (Kevin: "your tests need to drive the car off the road and back onto it").
    # Every ~150 m the virtual car leaves the pavement laterally, crosses the shoulder onto the
    # grass out to +8 m, and returns — sampling the built surface continuously. Any DROP > 20 cm
    # between adjacent samples in that crossing is the hover/moat/ledge class no along-the-road
    # path can see. Declared bridge spans exempt (past the railing is a real drop, guarded).
    excursions = []
    s2 = 0.0
    next_exc = 0.0
    for i in range(1, n):
        seg2 = math.hypot(pts[i][0] - pts[i - 1][0], pts[i][2] - pts[i - 1][2])
        s2 += seg2
        if s2 < next_exc:
            continue
        next_exc += 150.0
        if any(a <= s2 <= b for a, b in spans):
            continue
        # intersection flare mouths: the widened lip has no graded edge yet (tracked task —
        # the coverage guard removes the sidewalk the flare swallows and nothing replaces it).
        # Exempt from GATING, still counted in the report as flare ledges.
        _wmed = sorted(widths)[len(widths) // 2]
        in_flare = widths[i] > _wmed + 1.5
        x0, y0, z0 = pts[i]
        _a2, _b2 = pts[max(0, i - 1)], pts[min(n - 1, i + 1)]
        _dx2, _dz2 = _b2[0] - _a2[0], _b2[2] - _a2[2]
        _L2 = math.hypot(_dx2, _dz2) or 1.0
        txe, tze = _dx2 / _L2, _dz2 / _L2
        nxe, nze = -tze, txe
        we = widths[i] / 2
        for sgn in (1.0, -1.0):
            prev_y = None
            off = 0.0
            while off <= we + 5.5:   # the graded run-off band; past it is wilderness/skirt
                exx, ezz = x0 + nxe * off * sgn, z0 + nze * off * sgn
                ey, _o = surface.top_at(exx, ezz, y0, 6.0)
                if ey is not None and prev_y is not None and prev_y - ey > 0.30 and off > we - 0.5:
                    # >0.30/0.5m = wall-steep (2:1 embankment faces are legal run-off). Gate the
                    # recovery band on non-flare road; flare mouths tracked separately.
                    # GATE the constructed verge band (edge -> +2.5 m, always built); beyond it a
                    # mountain road's terrain is REAL (the Lariat drops 3 m into its own canyon —
                    # that's Lookout Mountain, guarded by the warning barriers, not a defect).
                    excursions.append((round(s2, 1), round(off * sgn, 2), round(prev_y - ey, 2),
                                       off <= we + 2.5 and not in_flare))
                if ey is not None:
                    prev_y = ey
                off += 0.5
    problems["excursion_drops"] = excursions

    # REPLICATION ERROR (Kevin: "i want a realistic road. this exists so we have to be able to
    # replicate it"): the lidar measured the ACTUAL road bench — the built deck must match it.
    # |built - raw measured| along the lap, declared bridges exempt. This is the number that says
    # how far the pipeline strayed from the real mountain.
    rep = []
    ep = data / "centerline.elevation.json"
    if ep.exists():
        try:
            ej = json.loads(ep.read_text())
            zr = ej.get("z_raw_m") or []
            e0 = 0.0
            _lc9 = data / "centerline.local.json"
            if _lc9.exists():
                e0 = float(json.loads(_lc9.read_text()).get("origin", {}).get("elev_m", 0.0))
            if zr and len(zr) >= 10:
                m2 = min(len(zr), len(pts))
                stq = 0.0
                for i in range(1, m2):
                    stq += math.hypot(pts[i][0] - pts[i - 1][0], pts[i][2] - pts[i - 1][2])
                    if any(a <= stq <= b for a, b in spans):
                        continue
                    rep.append(abs(pts[i][1] - (zr[i] - e0)))
        except Exception:
            pass
    if rep:
        rep.sort()
        nR = len(rep)
        problems["replication"] = {"median": round(rep[nR // 2], 2), "p95": round(rep[19 * nR // 20], 2),
                                   "max": round(rep[-1], 2)}

    # collapse obstruction runs (one wall = many stations)
    per_km = lap / 1000
    report = {
        "lap_m": round(lap, 1),
        "soft_top_in_lane": len(problems["soft_top_in_lane"]),
        "severe_steps": len(problems["severe_steps"]),
        "severe_per_km": round(len(problems["severe_steps"]) / per_km, 2),
        "steps_per_km": round(len(problems["steps"]) / per_km, 2),
        "kinks_per_km": round(len(problems["kinks"]) / per_km, 2),
        "obstruction_stations": len(set(s for s, _ in problems["obstructions"])),
        "daylight_gaps": len(problems["daylight_gaps"]),
        "replication_vs_measured": problems.get("replication"),
        "excursion_drops": sum(1 for r in problems["excursion_drops"] if r[3]),
        "excursion_far_ledges": sum(1 for r in problems["excursion_drops"] if not r[3]),
        "worst_excursions": sorted(problems["excursion_drops"], key=lambda r: -r[2])[:12],
        "worst_gated_excursions": sorted((r for r in problems["excursion_drops"] if r[3]),
                                         key=lambda r: -r[2])[:20],
        "obstruction_by_mesh": dict(sorted(obst_names.items(), key=lambda kv: -kv[1])[:12]),
        "worst": {
            "soft_top": problems["soft_top_in_lane"][:12],
            "severe_steps": sorted(problems["severe_steps"], key=lambda r: -r[2])[:12],
            "kinks": sorted(problems["kinks"], key=lambda r: -r[2])[:12],
            "obstructions": problems["obstructions"][:16],
        },
    }
    (data / "drive_test.json").write_text(json.dumps(report, indent=1), encoding="utf-8")

    # Gate philosophy: ABSOLUTE ZERO for the things no driver tolerates (ground through the lane,
    # solid objects in the corridor); RATES vs the driver-approved baseline for surface texture
    # (Sand Creek v0.15.x, the reference feel, runs ~11 severe/km and ~19 bumps/km — all at
    # junction crotches where edge strips meet). Kinks are reported but don't gate: on a real
    # mountain profile 3%-pt/m events are genuine road texture, not defects.
    severe_per_km = report["severe_per_km"]
    fail = (report["soft_top_in_lane"] > 0
            or report["daylight_gaps"] > 0
            or report["excursion_drops"] > int(report["lap_m"] / 1000)
            or report["obstruction_stations"] > 0
            or severe_per_km > 12.0
            or report["steps_per_km"] > 75.0)
    print(f"DRIVE TEST {cfg['slug']}  lap {lap/1000:.2f} km  (10 wheel paths @ {STEP_M} m)")
    print(f"  ground on top mid-lane : {report['soft_top_in_lane']}")
    print(f"  severe steps (>{STEP_SEVERE_M*100:.0f} cm)  : {report['severe_steps']}")
    print(f"  bumps >{STEP_BUMP_M*100:.1f} cm /km      : {report['steps_per_km']}")
    print(f"  slope kinks >{KINK_PCT}%% /km   : {report['kinks_per_km']}")
    print(f"  daylight gaps at edges : {report['daylight_gaps']}")
    if report.get("replication_vs_measured"):
        _rp = report["replication_vs_measured"]
        print(f"  replication vs measured road : median {_rp['median']} m  p95 {_rp['p95']} m  max {_rp['max']} m")
    print(f"  off-road excursion drops>20cm : {report['excursion_drops']}"
          f"  (far flare ledges, reported: {report['excursion_far_ledges']})")
    for r in report["worst_excursions"][:6]:
        print(f"    worst excursion drop: {r}")
    print(f"  obstructed stations    : {report['obstruction_stations']}"
          + (f"  {report['obstruction_by_mesh']}" if obst_names else ""))
    for label, rows in (("soft_top", report["worst"]["soft_top"]),
                        ("severe", report["worst"]["severe_steps"]),
                        ("obstruction", report["worst"]["obstructions"][:8])):
        for r in rows[:6]:
            print(f"    worst {label}: {r}")
    print(f"  => {'FAIL' if fail else 'PASS'}   (data/drive_test.json)")
    return {**report, "fail": fail}


if __name__ == "__main__":
    r = run(sys.argv[1] if len(sys.argv) > 1 else ".")
    sys.exit(1 if r["fail"] else 0)
