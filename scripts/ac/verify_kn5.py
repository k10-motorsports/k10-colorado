"""Phase 5c: assert an exported .kn5 is DRIVABLE before it ships — fail the build if not.

Every hard-won drivability lesson, encoded as a check that runs on the actual exported binary so a
regression can never ship silently again (all three of these have bitten us and cost a release):

  1. No duplicate physical mesh names. AC keys physical meshes BY NAME and drops duplicates from
     collision → car falls through. (Caused by a mesh exceeding the 65,535 vertex cap and auto-splitting
     into same-named halves.)  → see kn5-duplicate-name-fallthrough.
  2. Every drivable surface (1ROAD/1GRASS/1KERB/1RUNOFF/1LAWN) is wound FACE-UP. A face-down surface
     renders as ground but gives AC's one-sided physics no top to stand on → fall-through. Never recalc
     normals; build_mesh authors them up.  → see ac-fallthrough-real-cause.
  3. Every physical mesh is under the 65,535 vertex cap (would otherwise trip #1 on the next build).
  4. AC_START and the AC_PIT_* boxes sit ON a road surface at the same height as AC_HOTLAP_START — so
     you don't drop when spawning from the pits.

Pure-stdlib kn5 parse (no deps), reproducible on the Mac. Exit 0 = drivable, 1 = a check failed (with a
report of exactly which mesh/dummy and why).

    python -m scripts.ac.verify_kn5 projects/<slug>
"""

from __future__ import annotations

import json
import math
import struct
import sys
from collections import Counter
from pathlib import Path

DRIVABLE = ("1ROAD", "1GRASS", "1KERB", "1RUNOFF", "1LAWN")
VERT_CAP = 65535
FACE_UP_MIN = 0.90          # ≥90% of a drivable mesh's faces must point up (rest = holes_fill scraps)
PIT_ROAD_MAX_M = 13.5       # a pit/start box must be within this of a 1ROAD vertex — the nearest road vert
#                            is an EDGE vert, so a centreline spawn sits ~half-width away; real widths +
#                            junction flares reach ~25 m wide, so allow ~half of that (was 8 m for ~16 m roads)
PIT_DY_MAX_M = 1.0          # ...and within this height of it
POKE_ABOVE_M = 0.15         # a 1GRASS vert this far above a nearby drivable (road/kerb) vert = a poke
POKE_R = 5.0                # horizontal reach (matches geometry/audit_mesh.py check B)
POKE_MAX = 20               # tolerate a handful of holes_fill scraps; more = the seam regressed


def _parse(path: Path) -> tuple[list, list]:
    """Return (nodes, meshes). node = (name, world_xyz); mesh = dict(name, up, dn, nverts, road_pts)."""
    d = path.read_bytes()
    if d[:6] != b"sc6969":
        raise SystemExit(f"not a kn5: {path}")
    o = [6]
    U = lambda: (struct.unpack_from("<I", d, o[0])[0], o.__setitem__(0, o[0] + 4))[0]
    I = lambda: (struct.unpack_from("<i", d, o[0])[0], o.__setitem__(0, o[0] + 4))[0]
    H = lambda: (struct.unpack_from("<H", d, o[0])[0], o.__setitem__(0, o[0] + 2))[0]
    F = lambda: (struct.unpack_from("<f", d, o[0])[0], o.__setitem__(0, o[0] + 4))[0]
    B = lambda: (d[o[0]] != 0, o.__setitem__(0, o[0] + 1))[0]

    def BL(n):
        v = d[o[0]:o[0] + n]; o[0] += n; return v

    def S():
        n = U(); v = d[o[0]:o[0] + n]; o[0] += n; return v.decode("utf-8", "replace")

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

    ident = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]

    def mm(a, b):
        return [sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4)) for r in range(4) for c in range(4)]

    nodes: list = []
    meshes: list = []

    def rd(par):
        ncls = U(); name = S(); cc = U(); B()
        if ncls == 1:
            w = mm([F() for _ in range(16)], par)
            nodes.append((name, (w[12], w[13], w[14])))
            for _ in range(cc):
                rd(w)
        elif ncls == 2:
            B(); B(); B()
            vc = U(); P = []
            for _ in range(vc):
                px, py, pz = F(), F(), F(); F(); F(); F(); F(); F(); F(); F(); F()
                P.append((px, py, pz))
            ic = U(); idx = [H() for _ in range(ic)]
            U(); U(); F(); F(); BL(16); B()
            up = dn = 0
            for t in range(0, len(idx) - 2, 3):
                a, b, c = P[idx[t]], P[idx[t + 1]], P[idx[t + 2]]
                ny = (b[2] - a[2]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[2] - a[2])
                if ny > 0:
                    up += 1
                elif ny < 0:
                    dn += 1
            meshes.append({"name": name, "up": up, "dn": dn, "nverts": vc, "P": P})
            for _ in range(cc):
                rd(par)
        else:
            raise SystemExit(f"unknown node class {ncls}")

    rd(ident)
    return nodes, meshes


def verify(project_dir: str | Path) -> list[str]:
    project = Path(project_dir)
    slug = json.loads((project / "track.config.json").read_text())["slug"]
    kn5 = project / "build" / f"{slug}.kn5"
    if not kn5.exists():
        return [f"kn5 not found: {kn5}"]
    nodes, meshes = _parse(kn5)
    fails: list[str] = []

    # 1. duplicate physical (1-prefixed) mesh names
    dups = {n: c for n, c in Counter(m["name"] for m in meshes if m["name"].startswith("1")).items() if c > 1}
    for name, c in dups.items():
        fails.append(f"DUPLICATE physical mesh '{name}' x{c} — AC drops dup-named meshes from collision")

    # 2 + 3. drivable meshes face-up + under the vertex cap
    for m in meshes:
        if not any(m["name"].upper().startswith(p) for p in DRIVABLE):
            continue
        tot = m["up"] + m["dn"]
        if tot and m["up"] / tot < FACE_UP_MIN:
            fails.append(f"FACE-DOWN drivable '{m['name']}' ({m['up']} up / {m['dn']} dn) — car falls through")
        if m["nverts"] > VERT_CAP:
            fails.append(f"OVER-CAP '{m['name']}' {m['nverts']} v > {VERT_CAP} — will split into dup meshes")

    # 4. AC_START + AC_PIT_* sit on a road surface
    road = [v for m in meshes if m["name"].upper().startswith("1ROAD") for v in m["P"]]
    spawns = [(nm, p) for nm, p in nodes if nm == "AC_START_0" or nm.startswith("AC_PIT")]
    if road:
        for nm, (x, y, z) in spawns:
            nr = min(road, key=lambda v: (v[0] - x) ** 2 + (v[2] - z) ** 2)
            d = math.hypot(nr[0] - x, nr[2] - z)
            if d > PIT_ROAD_MAX_M or abs(y - nr[1]) > PIT_DY_MAX_M:
                fails.append(f"SPAWN off-road '{nm}': {d:.1f} m from road, dY {y - nr[1]:+.1f} m — will drop")
    elif spawns:
        fails.append("no 1ROAD mesh found to validate spawns against")

    # 5. per-layout spawn kn5s (multi-layout network): the main kn5 carries no spawns; each
    #    build/<slug>__<layout>.kn5 holds THIS layout's AC_START/AC_PIT. Verify they sit on the shared
    #    main kn5's roads (same drop check as the baked-in case).
    if road:
        for spk in sorted(kn5.parent.glob(f"{slug}__*.kn5")):
            snodes, _sm = _parse(spk)
            sspawns = [(nm, p) for nm, p in snodes if nm == "AC_START_0" or nm.startswith("AC_PIT")]
            if not sspawns:
                fails.append(f"spawn kn5 '{spk.name}' has no AC_START/AC_PIT dummies")
                continue
            for nm, (x, y, z) in sspawns:
                nr = min(road, key=lambda v: (v[0] - x) ** 2 + (v[2] - z) ** 2)
                d = math.hypot(nr[0] - x, nr[2] - z)
                if d > PIT_ROAD_MAX_M or abs(y - nr[1]) > PIT_DY_MAX_M:
                    fails.append(f"SPAWN off-road '{spk.name}:{nm}': {d:.1f} m from road, dY {y - nr[1]:+.1f} m")

    # 6. terrain-poke ON THE SHIPPED BINARY — a 1GRASS vert sitting above a nearby drivable (road/kerb)
    #    vert launches the car. audit_mesh checks this on track.obj (pre-export); this re-checks the kn5
    #    AFTER the weld + holes_fill + export so a regression there can't ship silently (the whole point:
    #    the audited mesh and the shipped mesh are different meshes). A regular clean build reads 0.
    drive = [v for m in meshes if any(m["name"].upper().startswith(p) for p in ("1ROAD", "1KERB")) for v in m["P"]]
    grass = [v for m in meshes if m["name"].upper().startswith("1GRASS") for v in m["P"]]
    if drive and grass:
        cell = 8.0
        buckets: dict[tuple[int, int], list] = {}
        for x, y, z in drive:
            buckets.setdefault((int(x // cell), int(z // cell)), []).append((x, y, z))
        pokes = worst = 0
        for gx, gy, gz in grass:
            ci, cj = int(gx // cell), int(gz // cell); best = None
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for rx, ry, rz in buckets.get((ci + di, cj + dj), ()):
                        if (gx - rx) ** 2 + (gz - rz) ** 2 <= POKE_R * POKE_R and (best is None or ry > best):
                            best = ry
            if best is not None and gy > best + POKE_ABOVE_M:
                pokes += 1
                worst = max(worst, gy - best)
        if pokes > POKE_MAX:
            fails.append(f"TERRAIN POKE x{pokes} (worst +{worst:.2f} m) — 1GRASS pokes through the "
                         f"drivable surface in the exported kn5 (>{POKE_MAX} tolerated)")
    return fails


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m scripts.ac.verify_kn5 <project-dir>")
    fails = verify(sys.argv[1])
    if fails:
        print("✗ kn5 verification FAILED:")
        for f in fails:
            print("   -", f)
        raise SystemExit(1)
    print("✓ kn5 verification passed (no dup meshes, drivable surfaces face-up, spawns on road)")


if __name__ == "__main__":
    main()
