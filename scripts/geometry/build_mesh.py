"""Phase 4: build the track mesh (1ROAD ribbon + GRASS terrain + AC dummies) and write OBJ/MTL.

Consumes the projected local-metre data from Phases 1-3 and emits, in data/:
  track.obj + track.mtl (named groups 1ROAD / GRASS, for Blender → kn5 in Phase 5),
  dummies.json (AC_* placements), and track_render.svg (a 3/4 view, elevation exaggerated, to eyeball).
Pure stdlib.

Run:  python -m scripts.geometry.build_mesh projects/sand-creek-raceway
"""

from __future__ import annotations

import ast
import json
import math
import struct
import sys
from pathlib import Path

from scripts.capture import evidence
from scripts.geometry import dummies as dummies_mod
from scripts.geometry import kerbs, ribbon, road_text
from scripts.geometry import profile as profile_mod
from scripts.geometry.projection import _meters_per_degree

Vertex = tuple[float, float, float]
# FLUSH EDGES (the "road hovering" root cause): 0.1 lift + 0.25 clearance stacked to a permanent
# 35 cm step down from every pavement edge to the world — the whole road read as a plank floating
# over the landscape, in every photo, on every track. Real dirt meets real pavement within ~2 cm.
# The poke risk these big margins papered over is the drive test's and audit H's job to catch.
ROAD_LIFT_M = 0.0001  # 0.1 mm (Kevin: zero visible ledge anywhere)
# 0.03 is NOT a visible gap: it lives strictly UNDER the asphalt slab (anti z-fight/anti poke — at
# literal 0.0 the drive test found grass coplanar IN THE LANE). The visible seam is the shoulder
# draping onto the grass, which meets at exactly 0.
GRASS_CLEARANCE_M = 0.01  # Kevin: "0.01" — the world sits ONE CENTIMETER under the deck


def read_npy(path: Path) -> list[list[float]]:
    """Minimal float64 2D .npy reader (matches the writer in elevation/heightfield.py)."""
    with open(path, "rb") as f:
        assert f.read(6) == b"\x93NUMPY"
        f.read(2)
        hlen = struct.unpack("<H", f.read(2))[0]
        header = ast.literal_eval(f.read(hlen).decode())
        ny, nx = header["shape"]
        data = struct.unpack(f"<{ny * nx}d", f.read(8 * ny * nx))
    return [list(data[j * nx:(j + 1) * nx]) for j in range(ny)]


def project_grid(grid: list[list[float]], meta: dict, origin: tuple[float, float], elev0: float,
                 *, mirror_x: bool = False) -> list[list[Vertex]]:
    """Project the lat/lon terrain grid into the same local ENU frame as the centerline (mirroring the
    east axis too when ``mirror_x`` so the grass stays under the mirrored road)."""
    s, w, n, e = meta["bbox_swne"]
    nx, ny, sp = meta["nx"], meta["ny"], meta["spacing_m"]
    midlat = (s + n) / 2
    gy = sp / 111_000.0
    gx = sp / (111_000.0 * math.cos(math.radians(midlat)))
    lon0, lat0 = origin
    m_lon, m_lat = _meters_per_degree(lat0)
    sx = -1.0 if mirror_x else 1.0
    out = []
    for j in range(ny):
        lat = n - j * gy
        row = [(sx * ((w + i * gx) - lon0) * m_lon, grid[j][i] - elev0, (lat - lat0) * m_lat) for i in range(nx)]
        out.append(row)
    return out


def write_obj(path: Path, mtl_name: str, groups: list[tuple[str, str, dict]]) -> tuple[int, int]:
    """Write an OBJ with named objects/materials. groups: (object_name, material, mesh). Emits per-
    vertex UVs (``vt`` + ``f v/vt``) for any mesh carrying a parallel ``uvs`` list; v and vt use
    independent global offsets so textured and untextured groups can share one file."""
    lines = [f"mtllib {mtl_name}"]
    voff = vtoff = 0
    nv = nf = 0
    for name, mat, mesh in groups:
        lines.append(f"o {name}")
        lines.append(f"usemtl {mat}")
        for x, y, z in mesh["vertices"]:
            lines.append(f"v {x:.3f} {y:.3f} {z:.3f}")
        uvs = mesh.get("uvs")
        if uvs:
            for u, v in uvs:
                lines.append(f"vt {u:.4f} {v:.4f}")
        for a, b, c in mesh["tris"]:
            if uvs:
                lines.append(f"f {a+1+voff}/{a+1+vtoff} {b+1+voff}/{b+1+vtoff} {c+1+voff}/{c+1+vtoff}")
            else:
                lines.append(f"f {a + 1 + voff} {b + 1 + voff} {c + 1 + voff}")
        voff += len(mesh["vertices"])
        if uvs:
            vtoff += len(uvs)
        nv += len(mesh["vertices"])
        nf += len(mesh["tris"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return nv, nf


def split_mesh_under_cap(name: str, mat: str, mesh: dict, cap: int = 30000) -> list[tuple[str, str, dict]]:
    """Split a mesh into uniquely-named chunks under AC's 65,535 per-mesh vertex cap.

    The kn5 exporter auto-splits an oversized mesh into pieces with the SAME name — and AC keys
    PHYSICAL meshes by name, so all but one piece silently drop out of collision (the 25 km Lariat
    kerb strip shipped as '1KERB_corners' x3 with two-thirds of it fall-through). Pre-splitting here
    with _a/_b/... suffixes keeps every piece collidable. Chunks by triangle order (swept strips are
    station-ordered, so chunks stay contiguous); vertices are re-indexed per chunk.

    cap 30,000, NOT ~65k: the kn5 export re-splits shared vertices at UV/normal seams, so the
    exported count can run up to ~2x the OBJ count — a 62k chunk shipped as '..._a' x2 again."""
    if len(mesh["vertices"]) <= cap:
        return [(name, mat, mesh)]
    uvs = mesh.get("uvs")
    out: list[tuple[str, str, dict]] = []
    tri_i = 0
    while tri_i < len(mesh["tris"]):
        remap: dict[int, int] = {}
        cv: list = []
        cuv: list = []
        ct: list = []
        while tri_i < len(mesh["tris"]) and len(cv) <= cap - 3:
            new_tri = []
            for v in mesh["tris"][tri_i]:
                k = remap.get(v)
                if k is None:
                    k = remap[v] = len(cv)
                    cv.append(mesh["vertices"][v])
                    if uvs:
                        cuv.append(uvs[v])
                new_tri.append(k)
            ct.append(tuple(new_tri))
            tri_i += 1
        chunk = {"vertices": cv, "tris": ct}
        if uvs:
            chunk["uvs"] = cuv
        out.append((f"{name}_{chr(ord('a') + len(out))}", mat, chunk))
    print(f"  [split] {name}: {len(mesh['vertices'])} verts > {cap} cap -> "
          f"{len(out)} uniquely-named meshes ({[g[0] for g in out]})")
    return out


def write_mtl(path: Path) -> None:
    path.write_text(
        "newmtl road\nKd 0.20 0.20 0.23\nKa 0.05 0.05 0.05\n\n"
        "newmtl grass\nKd 0.26 0.44 0.20\nKa 0.05 0.08 0.04\n\n"
        "newmtl kerb\nKd 0.80 0.20 0.18\nKa 0.10 0.03 0.03\n",
        encoding="utf-8",
    )


def write_ground_local(path: Path, grid_xyz: list[list[Vertex]]) -> None:
    """Persist the CONFORMED+CLAMPED grass surface (the grid AFTER conform_terrain_to_road and
    clamp_terrain_below_road, i.e. the exact surface the 1GRASS mesh triangulates) as a regular
    bilinear-samplable heightfield in the local (mirrored) ENU frame.

    This is the single source of truth for "what height is the ground here": build_env samples it so
    scenery (poles, trees, signs, buildings) stands on the surface that actually renders — NOT the raw
    bare-earth DEM, which near the track differs by the road conform/clamp (poles floated/sank ~0.5-few m
    beside the road before this). audit_mesh's prop-float check (G) reads it too. X,Z are unchanged by
    the conform (Y-only), so the grid stays axis-regular; dx/dz are signed (dx<0 on mirror_x tracks)."""
    ny, nx = len(grid_xyz), len(grid_xyz[0])
    x0, z0 = grid_xyz[0][0][0], grid_xyz[0][0][2]
    dx = (grid_xyz[0][1][0] - x0) if nx > 1 else 1.0
    dz = (grid_xyz[1][0][2] - z0) if ny > 1 else 1.0
    y = [[grid_xyz[j][i][1] for i in range(nx)] for j in range(ny)]
    path.write_text(json.dumps({"x0": x0, "z0": z0, "dx": dx, "dz": dz, "nx": nx, "ny": ny, "y": y}),
                    encoding="utf-8")


def _grid_trisurf(grid_xyz: list[list[Vertex]], x: float, z: float) -> float:
    """Height of the TRIANGULATED grid at (x,z) — the same diagonal split grass_terrain renders
    ((a,b,c)+(a,c,d) per quad), so 'the ground' here IS the ground on screen."""
    ny, nx = len(grid_xyz), len(grid_xyz[0])
    x0, z0 = grid_xyz[0][0][0], grid_xyz[0][0][2]
    dx = (grid_xyz[0][1][0] - x0) if nx > 1 else 1.0
    dz = (grid_xyz[1][0][2] - z0) if ny > 1 else 1.0
    fi = (x - x0) / dx if dx else 0.0
    fj = (z - z0) / dz if dz else 0.0
    i0 = max(0, min(nx - 2, int(fi))); j0 = max(0, min(ny - 2, int(fj)))
    u = max(0.0, min(1.0, fi - i0)); v = max(0.0, min(1.0, fj - j0))
    ya = grid_xyz[j0][i0][1]; yb = grid_xyz[j0][i0 + 1][1]
    yc = grid_xyz[j0 + 1][i0 + 1][1]; yd = grid_xyz[j0 + 1][i0][1]
    # quad corners a=(0,0) b=(1,0) c=(1,1) d=(0,1); tris (a,b,c) and (a,c,d) — diagonal a-c (u==v)
    if u >= v:      # tri (a,b,c): y = ya + u*(yb-ya) + v*(yc-yb)
        return ya + u * (yb - ya) + v * (yc - yb)
    return ya + v * (yd - ya) + u * (yc - yd)   # tri (a,c,d)


def _grid_bilinear(grid_xyz: list[list[Vertex]], x: float, z: float) -> float:
    """Bilinear Y of a regular (x,z) grid of (x,y,z) verts. dx/dz may be signed (mirror_x)."""
    ny, nx = len(grid_xyz), len(grid_xyz[0])
    x0, z0 = grid_xyz[0][0][0], grid_xyz[0][0][2]
    dx = (grid_xyz[0][1][0] - x0) if nx > 1 else 1.0
    dz = (grid_xyz[1][0][2] - z0) if ny > 1 else 1.0
    fi = (x - x0) / dx if dx else 0.0
    fj = (z - z0) / dz if dz else 0.0
    i0 = max(0, min(nx - 1, int(fi))); j0 = max(0, min(ny - 1, int(fj)))
    i1 = min(nx - 1, i0 + 1); j1 = min(ny - 1, j0 + 1)
    ti = max(0.0, min(1.0, fi - i0)); tj = max(0.0, min(1.0, fj - j0))
    a = grid_xyz[j0][i0][1] * (1 - ti) + grid_xyz[j0][i1][1] * ti
    b = grid_xyz[j1][i0][1] * (1 - ti) + grid_xyz[j1][i1][1] * ti
    return a * (1 - tj) + b * tj


BRIDGE_MIN_H_M = 3.5       # road must ride this far above bare earth to earn a bridge (else it's an embankment)
BRIDGE_MIN_SPAN_M = 12.0   # ...over at least this long a run (a real span, not a one-vertex bump)


def _bridge_detector(centerline_m: list[Vertex], natural_grid: list[list[Vertex]],
                     raw_ground: tuple[list[float], list[float]] | None = None):
    """Return ``bridge_of(station_m) -> bool``: True where the road rides a sustained height above the
    bare earth (a real gap to span — Sand Creek's creek). Those stations get NO embankment fill (the
    valley stays open under the deck) and a bridge structure instead. Returns a no-op if there are none.

    ``raw_ground`` = (stations_m, ground_y_local): the ALONG-ROAD raw 3DEP profile. Prefer it over the
    heightfield — on a steep sidehill a coarse (40 m) bilinear grid cell reads metres below the true
    ground under the road, which minted phantom 400-600 m viaducts down Paradise Rd / Mt Vernon Canyon.
    The 3 m raw profile keeps the one real span (19th St over US-6) and kills the fakes."""
    st = [0.0]
    for i in range(1, len(centerline_m)):
        st.append(st[-1] + math.hypot(centerline_m[i][0] - centerline_m[i - 1][0],
                                      centerline_m[i][2] - centerline_m[i - 1][2]))
    if raw_ground is not None:
        gst, gy = raw_ground

        def _ground_at(s: float) -> float:
            import bisect
            k = bisect.bisect_left(gst, s)
            if k <= 0:
                return gy[0]
            if k >= len(gst):
                return gy[-1]
            t = (s - gst[k - 1]) / max(1e-9, gst[k] - gst[k - 1])
            return gy[k - 1] + t * (gy[k] - gy[k - 1])

        high = [(centerline_m[i][1] - _ground_at(st[i])) > BRIDGE_MIN_H_M for i in range(len(centerline_m))]
    else:
        high = [(cy - _grid_bilinear(natural_grid, cx, cz)) > BRIDGE_MIN_H_M for cx, cy, cz in centerline_m]
    spans: list[tuple[float, float]] = []
    i = 0
    n = len(centerline_m)
    while i < n:
        if not high[i]:
            i += 1
            continue
        j = i
        while j < n and high[j]:
            j += 1
        if st[j - 1] - st[i] >= BRIDGE_MIN_SPAN_M:
            spans.append((st[i], st[j - 1]))
        i = j
    if not spans:
        return None
    print(f"  bridge spans: {len(spans)} ({[f'{a:.0f}-{b:.0f}m' for a, b in spans]}) — deck+piers, no fill")
    _bridge_detector.last_spans = [(float(a), float(b)) for a, b in spans]   # for persistence

    def bridge_of(station_m: float) -> bool:
        return any(a <= station_m <= b for a, b in spans)
    return bridge_of


def orient_up(mesh: dict) -> dict:
    """Flip any triangle whose geometric normal faces down so every face points +Y (up).

    AC track collision is **one-sided**: a ground/drivable surface whose faces point down lets the
    car drop straight through from above (and renders backface-culled). The ribbon/terrain/kerb
    generators wind their quads downward, so re-orient them up before export. Mutates and returns
    the mesh. Only safe for near-horizontal surfaces — don't apply to vertical geometry (barriers)."""
    V = mesh["vertices"]
    out = []
    for a, b, c in mesh["tris"]:
        ax, _, az = V[a]
        bx, _, bz = V[b]
        cx, _, cz = V[c]
        ny = (bz - az) * (cx - ax) - (bx - ax) * (cz - az)  # y-component of (b-a)×(c-a)
        out.append((a, c, b) if ny < 0.0 else (a, b, c))
    mesh["tris"] = out
    return mesh


def smooth_centerline(pts: list[Vertex], *, lam: float = 0.45, iterations: int = 4) -> list[Vertex]:
    """Light GLOBAL Laplacian smoothing to kill the GPS-level wobble (3-15° per-vertex jogs in the
    OSM/KML centerline) that makes the road EDGES ripple on curves. Each vertex is pulled only a
    fraction toward its neighbours' midpoint, so straights stay straight and the route is preserved —
    it just removes the high-frequency noise. Closed-loop aware; vertex count preserved."""
    closed = math.hypot(pts[0][0] - pts[-1][0], pts[0][2] - pts[-1][2]) < 1e-6
    P = [list(p) for p in (pts[:-1] if closed else pts)]
    m = len(P)
    for _ in range(iterations):
        new = [p[:] for p in P]
        for i in range(m):
            a, b = P[(i - 1) % m], P[(i + 1) % m]
            for c in (0, 1, 2):
                new[i][c] = P[i][c] + lam * ((a[c] + b[c]) / 2.0 - P[i][c])
        P = new
    out = [tuple(p) for p in P]
    if closed:
        out.append(out[0])
    return out


def round_sharp_corners(pts: list[Vertex], *, angle_thr: float = 28.0,
                        iterations: int = 7, window: int = 3) -> list[Vertex]:
    """Round only the SHARP kinks (a hard ~90° street corner has no zero-radius point — real ones have
    a curb radius). Localized corner-cutting near kinks > ``angle_thr``; straights and gentle bends are
    untouched, so the route shape is preserved. Fixes the ribbon self-pinching + the stub kerbs at
    corners. Closed-loop aware; vertex count (and the parallel widths) is preserved."""
    closed = math.hypot(pts[0][0] - pts[-1][0], pts[0][2] - pts[-1][2]) < 1e-6
    P = [list(p) for p in (pts[:-1] if closed else pts)]
    m = len(P)

    def kink(Q: list, i: int) -> float:
        a, b, c = Q[(i - 1) % m], Q[i], Q[(i + 1) % m]
        v1 = (b[0] - a[0], b[2] - a[2])
        v2 = (c[0] - b[0], c[2] - b[2])
        l1 = math.hypot(*v1) or 1e-9
        l2 = math.hypot(*v2) or 1e-9
        d = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)))
        return math.degrees(math.acos(d))

    for _ in range(iterations):
        mark = [False] * m
        for i in range(m):
            if kink(P, i) > angle_thr:
                for w in range(-window, window + 1):
                    mark[(i + w) % m] = True
        new = [p[:] for p in P]
        for i in range(m):
            if mark[i]:
                a, b = P[(i - 1) % m], P[(i + 1) % m]
                for c in (0, 1, 2):
                    new[i][c] = 0.5 * P[i][c] + 0.25 * (a[c] + b[c])
        P = new
    out = [tuple(p) for p in P]
    if closed:
        out.append(out[0])
    return out


def finished_centerline(raw_pts: list[Vertex], cfg_raw: dict, *, mirror_x: bool):
    """The exact centerline the road is swept over: corner-rounded, mirrored to the AC frame, and with the
    hand-authored ``road_profile`` dip (the Sand Creek corkscrew) baked into Y.

    Both build_mesh AND build_env call this so the dressing (power-line poles, street signs, the creek
    bridge, the terrain conform) anchors to the SAME road the kn5 ships — not the un-dipped raw loop, which
    left poles floating ~2 m over the road through the corkscrew. The caller applies the capture-elevation
    correction to ``raw_pts`` first (Y only, guarded/independent). Returns
    (centerline, dip_of, bank_at, profile_active); ``bank_at`` rolls the swept cross-sections, ``dip_of``
    drives the terrain bowl. Vertex count (and the parallel widths) is preserved throughout."""
    centerline = round_sharp_corners(smooth_centerline(raw_pts))
    if mirror_x:
        centerline = [(-x, y, z) for (x, y, z) in centerline]
    dip_of, bank_at, profile_active = profile_mod.build_profile(cfg_raw.get("road_profile"))
    if profile_active:
        cst = [0.0]
        for i in range(1, len(centerline)):
            cst.append(cst[-1] + math.hypot(centerline[i][0] - centerline[i - 1][0],
                                            centerline[i][2] - centerline[i - 1][2]))
        centerline = [(x, y - dip_of(cst[i]), z) for i, (x, y, z) in enumerate(centerline)]
    return centerline, dip_of, bank_at, profile_active


def build(project_dir: str | Path) -> dict:
    project_dir = Path(project_dir)
    data = project_dir / "data"
    local = json.loads((data / "centerline.local.json").read_text(encoding="utf-8"))
    raw_pts = [tuple(p) for p in local["points_xyz_m"]]
    # REAL ELEVATION: where a Prodrive Scan capture exists, correct the road-surface Y where it
    # confidently diverges from the bare-earth heightfield (bridges/overpasses/cuts). No capture →
    # raw_pts is returned unchanged, so the build stays byte-identical. (Y only; X/Z untouched.)
    cap = evidence.load_capture(project_dir)
    if cap is not None and evidence.use_capture_elevation(project_dir):
        raw_pts, estats = evidence.corrected_elevation(raw_pts, cap,
                                                       bridges=evidence.bridge_spans(project_dir))
        if estats["n_corrected"]:
            print(f"  elevation: corrected {estats['n_corrected']} centerline pts from capture "
                  f"(max {estats['max_corr_m']:.1f} m, off-bridge ≤{estats['max_offbridge_m']:.1f} m, "
                  f"{estats['n_outliers']} spikes rejected, offset {estats['offset_m']:.1f} m)")
    elif cap is not None:
        print("  elevation: capture present but capture.use_elevation=false — road follows USGS heightfield")
    # ACCURATE BRIDGE DECKS: ride each declared crossing (capture.bridges) as a LEVEL deck over the
    # dipping creek — the capture lift is unreliable where the phone data is noisy (Sand Creek's creek),
    # so without this the road dives into the creek bed (the 'corkscrew narnia'). Then cap the grade so
    # no residual stretch is steep enough to launch the car.
    _bridges = evidence.bridge_spans(project_dir)
    if _bridges:
        before = [p[1] for p in raw_pts]
        raw_pts = evidence.level_bridge_decks(raw_pts, _bridges)
        lifted = max((raw_pts[i][1] - before[i] for i in range(len(raw_pts))), default=0.0)
        print(f"  elevation: leveled {len(_bridges)} bridge deck(s) over the creek (max lift {lifted:.1f} m)")
    # MIRROR_X: AC's kn5 convert renders the track reflected east<->west (a real right-hander reads as a
    # left, north preserved). Cancel it by mirroring the SOURCE X (east) here — so the road, the dummies
    # (placed below) and the facing (build_kn5) are all COMPUTED in the mirrored frame, never reflected
    # as matrices (that's what broke the spawn before). orient_up re-faces the drivable surfaces.
    cfg_raw = json.loads((project_dir / "track.config.json").read_text())
    mirror_x = bool(cfg_raw.get("mirror_x", False))

    # Grade cap: a launch-spike safety net, NOT a terrain flattener. 6% suits flat street circuits;
    # a mountain track must set road_profile.max_grade_pct above its real steepest pitch (Lookout:
    # Paradise Rd runs -12..-14.5%) or the capped road drifts metres above the real earth downhill —
    # which is exactly what minted the phantom 400-600 m viaducts before this was configurable.
    _max_grade = float(cfg_raw.get("road_profile", {}).get("max_grade_pct", 6.0)) / 100.0
    raw_pts = evidence.cap_grade(raw_pts, max_grade=_max_grade)
    widths = local["widths_m"]
    origin = (local["origin"]["lon"], local["origin"]["lat"])
    elev0 = local["origin"]["elev_m"]

    # ROAD PROFILE: corner-round, mirror, and bake the hand-authored dip (the Sand Creek corkscrew) into
    # the centerline Y. Shared with build_env so the scenery anchors to the SAME road. dip_of/bank_at are
    # keyed by lap station (metres from pts[0]); bank rolls every swept cross-section, the dip is baked
    # into the centerline Y so the terrain grade, the grass conform and the dummies all follow it.
    centerline, dip_of, bank_at, profile_active = finished_centerline(raw_pts, cfg_raw, mirror_x=mirror_x)
    bnk = bank_at if profile_active else None     # None => flat path is byte-identical to before
    # MEASURED cross-slope (data/crossfall.json, from lateral 1 m-DEM pairs): real superelevation,
    # dead-banded + capped at 6% so the car never launches. Only when the track has no hand-authored
    # cambers (PPIR's oval banking stays authoritative). Sign: file is +ve = left-edge-up in the
    # UNMIRRORED frame; mirroring flips handedness, so flip the sign with it.
    cf_path = data / "crossfall.json"
    if bnk is None and cf_path.exists():
        _cf = json.loads(cf_path.read_text())
        _cst, _cbk = _cf["station_m"], _cf["bank_rad"]
        _sgn = -1.0 if mirror_x else 1.0

        def bnk(station_m, _cst=_cst, _cbk=_cbk, _sgn=_sgn):
            import bisect
            k = bisect.bisect_left(_cst, station_m)
            if k <= 0:
                return _sgn * _cbk[0]
            if k >= len(_cst):
                return _sgn * _cbk[-1]
            t2 = (station_m - _cst[k - 1]) / max(1e-9, _cst[k] - _cst[k - 1])
            return _sgn * (_cbk[k - 1] + t2 * (_cbk[k] - _cbk[k - 1]))
        print(f"  crossfall: measured camber active ({_cf.get('banked_pct')}% of lap, "
              f"max {_cf.get('max_bank_pct')}% — deadband {_cf.get('deadband')} cap {_cf.get('cap')})")

    # INTERIOR-GRID connector roads (the 2nd layout's extra streets, from connectors.local.json) — they
    # share this one mesh, laid flat asphalt (the corkscrew is main-loop only). Mirror to the same frame.
    connectors = []
    conn_path = data / "connectors.local.json"
    if conn_path.exists():
        for c in json.loads(conn_path.read_text(encoding="utf-8")).get("connectors", []):
            pts = [(-p[0], p[1], p[2]) if mirror_x else tuple(p) for p in c["points_xyz_m"]]
            connectors.append((c["name"], pts, c["widths_m"]))
        print(f"  interior connectors: {len(connectors)} ({[n for n, _p, _w in connectors]})")

    grid = read_npy(data / "heightfield.npy")
    meta = json.loads((data / "heightfield.meta.json").read_text(encoding="utf-8"))
    # Upsample as fine as a ~420k-vertex terrain budget allows (never below x3): the terrain
    # triangles must be fine enough to follow the road cut. The old rule dropped big loops to x1 to
    # keep 1GRASS one mesh under AC's 65,535 vertex cap — at 40 m cells the triangles BETWEEN the
    # clamped vertices bridged straight across the road cut (ground knifing +4 m through the
    # Lariat's switchbacks; the vertex-level audit can't see it) — and even x3 (~13 m) left the
    # audit's H check firing on PPIR's banking and Sand Creek's creek banks. The vertex cap is now
    # handled like the kerbs: split_mesh_under_cap ships the grass as uniquely-named collidable
    # chunks (every consumer prefix-matches 1GRASS*), so resolution is bounded by budget, not name.
    _ny0, _nx0 = len(grid), len(grid[0])
    # 1.1M budget (was 420k): 10 m cells cannot carry an 8 m mountain bench — adjacent nodes on a
    # 40% side-slope differ by 4 m and the triangles between them cut metres under the road: the
    # deck+apron raft perched a metre above the ACTUAL terrain along the whole Lariat while flat
    # Sand Creek sat perfectly (same code, different terrain frequency). ~6 m cells halve the
    # bench error at the source; split_mesh_under_cap absorbs the vertex count.
    up = 8
    while up > 3 and ((_nx0 - 1) * up + 1) * ((_ny0 - 1) * up + 1) > 1_100_000:
        up -= 1
    grid, meta = ribbon.upsample_grid(grid, meta, up)
    print(f"[build_mesh] grass upsample x{up} -> {meta['nx']}x{meta['ny']} = {meta['nx']*meta['ny']} verts (< 65535)")
    grid_xyz = project_grid(grid, meta, origin, elev0, mirror_x=mirror_x)
    # CORRIDOR SPLICE: override grid heights within the corridor with the near-native 3DEP field
    # (data/corridor.elev.json — the rc6 Lariat lesson: the 40 m area grid cannot contain the 8 m
    # bench; the corridor field can). Bilinear in (station, lateral) space; beyond the corridor the
    # coarse grid stands. Blend the last 10 m so the seam doesn't step.
    _cor_p = data / "corridor.elev.json"
    if _cor_p.exists():
        _cor = json.loads(_cor_p.read_text())
        _cst = _cor["stations_lonlat"]
        _coffs = _cor["offsets"]
        _cz = _cor["z"]
        _half_l = float(_cor["half_l"])
        _m_lon, _m_lat = _meters_per_degree(origin[1])
        _sxc = -1.0 if mirror_x else 1.0
        # corridor station points in the LOCAL (mirrored) frame
        _cpts = [(_sxc * (lo - origin[0]) * _m_lon, (la - origin[1]) * _m_lat) for lo, la in _cst]
        from collections import defaultdict as _cdd
        _ch = _cdd(list)
        for _ci, (_cx, _cz2) in enumerate(_cpts):
            _ch[(int(_cx // 30), int(_cz2 // 30))].append(_ci)
        _n_over = 0
        for _row in grid_xyz:
            for _k in range(len(_row)):
                _gx, _gy, _gz = _row[_k]
                _best = None
                for _di in (-1, 0, 1):
                    for _dj in (-1, 0, 1):
                        for _ci in _ch.get((int(_gx // 30) + _di, int(_gz // 30) + _dj), ()):
                            _d2 = (_cpts[_ci][0] - _gx) ** 2 + (_cpts[_ci][1] - _gz) ** 2
                            if _best is None or _d2 < _best[0]:
                                _best = (_d2, _ci)
                if _best is None:
                    continue
                _ci = _best[1]
                _j2 = min(_ci + 1, len(_cpts) - 1)
                _k2 = max(_ci - 1, 0)
                _tx = _cpts[_j2][0] - _cpts[_k2][0]
                _tz = _cpts[_j2][1] - _cpts[_k2][1]
                _L = (_tx * _tx + _tz * _tz) ** 0.5 or 1.0
                _tx, _tz = _tx / _L, _tz / _L
                _dxn = _gx - _cpts[_ci][0]
                _dzn = _gz - _cpts[_ci][1]
                _lat = -( -_tz * _dxn + _tx * _dzn) * _sxc   # signed lateral (mirror-aware)
                _lon_s = _dxn * _tx + _dzn * _tz             # along-track residual
                if abs(_lat) > _half_l or abs(_lon_s) > 9.0:
                    continue
                # lateral bilinear in the corridor row (station rows are ~6 m apart; use nearest)
                _fo = (_lat + _half_l) / float(_cor["step_l"])
                _io = max(0, min(len(_coffs) - 2, int(_fo)))
                _to = max(0.0, min(1.0, _fo - _io))
                _z1 = _cz[_ci][_io]
                _z2v = _cz[_ci][_io + 1]
                if _z1 is None or _z2v is None:
                    continue
                _zc = (_z1 * (1 - _to) + _z2v * _to) - elev0
                _edge = min(1.0, (_half_l - abs(_lat)) / 10.0)   # blend the outer 10 m
                _row[_k] = (_gx, _gy * (1 - _edge) + _zc * _edge, _gz)
                _n_over += 1
        print(f"  [corridor] spliced {_n_over} grid nodes with near-native terrain")
    # Sink the terrain into a wide smooth BOWL around any dip (the corkscrew) so it reads as a valley,
    # not a road in a walled trench with the surrounding grass towering overhead.
    if profile_active:
        profile_mod.apply_dip_bowl(grid_xyz, centerline, dip_of)
    # EMBANKMENT/CUT GRADING: build the terrain UP from the real ground to the road (fill slope where the
    # road is above bare earth, cut where below) instead of pulling the whole corridor UP to a flat mesa
    # the car floats on. A gentle drivable band at the edge, then a 2:1 slope to the real ground — so the
    # valley/hillside survives and running off is a slope, not a tabletop cliff. Racetracks (tiny fills)
    # get automatic smooth gradations from the same path. bridge_of suppresses fill under a deck (below).
    natural_grid = [[v for v in row] for row in grid_xyz]     # bare earth BEFORE grading (for bridge detect)
    # Along-road raw 3DEP ground for bridge detection (see _bridge_detector) — station-keyed off the
    # SAME points the road profile came from, in local y (elev - origin elev).
    raw_ground = None
    elev_path = data / "centerline.elevation.json"
    if elev_path.exists():
        _ej = json.loads(elev_path.read_text(encoding="utf-8"))
        if len(_ej.get("z_raw_m", [])) == len(raw_pts):
            _gst = [0.0]
            for i in range(1, len(raw_pts)):
                _gst.append(_gst[-1] + math.hypot(raw_pts[i][0] - raw_pts[i - 1][0],
                                                  raw_pts[i][2] - raw_pts[i - 1][2]))
            raw_ground = (_gst, [z - elev0 for z in _ej["z_raw_m"]])
    bridge_of = _bridge_detector(centerline, natural_grid, raw_ground=raw_ground)
    # persist AUTO-detected spans for the kn5 gate: the builder exempts them from fill/pins,
    # so the gate must exempt the same stations (declared-only exemption read auto bridge
    # valleys as 7 m of 'road hover' and wrecked the deck-vs-mountain metric).
    _auto_spans = getattr(_bridge_detector, "last_spans", [])
    (data / "bridge.spans.auto.json").write_text(json.dumps(_auto_spans), encoding="utf-8")
    # band=2.5 == the shoulder's verge_w: sheet and terrain must BREAK AT THE SAME LINE with the
    # same slopes, or a stagger wedge of air opens under every cut face (the tenting gate read a
    # constant ~2 m of daylight under the sheet with the old band=4.0 vs verge 2.5).
    ribbon.grade_embankment(grid_xyz, centerline, widths, bank_at=bnk, band=2.5,
                            extra_roads=[(p, w) for _n, p, w in connectors],
                            clearance=GRASS_CLEARANCE_M, bridge_of=bridge_of)

    # tile_m=8 m — the LA Canyons cracked-tarmac detail reads at its real scale (cracks crisp, not
    # stretched). The cracking is irregular enough to hide the repeat.
    road = ribbon.road_ribbon(centerline, widths, tile_m=4.0, bank_at=bnk)  # 4 m = the Lake Murray asphalt scale
    road["vertices"] = [(x, y + ROAD_LIFT_M, z) for x, y, z in road["vertices"]]
    # Wide tarmac RUNOFF apron on the outside of corners (replaces grass run-off / the old walls).
    # runoff.enabled=false skips it — a flat apron "at graded terrain height" makes sense beside a
    # flat circuit, but on a pitched mountainside it's a tarmac shelf with a step at the road band.
    if cfg_raw.get("runoff", {}).get("enabled", True):
        runoff = kerbs.corner_runoff(centerline, widths, bank_at=bnk)
        runoff["vertices"] = [(x, y + 0.05, z) for x, y, z in runoff["vertices"]]  # clear the grass, below road
    else:
        runoff = {"vertices": [], "uvs": [], "tris": []}
        print("  runoff aprons: disabled (runoff.enabled=false)")
    # Interior connector roads (2nd-layout streets / the PPIR infield roval) — flat drivable 1ROAD. Built
    # HERE (before the clamp) so the terrain is graded below them too — else the grass pokes up through the
    # connector (the roval poked +0.5 m before this moved up from after grass_terrain).
    conn_meshes = []
    for name, pts, w in connectors:
        cm = ribbon.road_ribbon(pts, w, tile_m=8.0)
        cm["vertices"] = [(x, y + ROAD_LIFT_M, z) for x, y, z in cm["vertices"]]
        conn_meshes.append((f"1ROAD_{name}", cm))
    conn_verts = [v for _n, cm in conn_meshes for v in cm["vertices"]]
    # ANTI-POKE pass 1: clamp the grid below the flat drivable ribbons (road + runoff + connectors) so the
    # graded grass surface is FINAL before the edge strips drape onto it. One-sided (natural dips survive).
    ribbon.clamp_terrain_below_road(grid_xyz, road["vertices"] + runoff["vertices"] + conn_verts,
                                    clear=GRASS_CLEARANCE_M, grid_spacing=float(meta["spacing_m"]),
                                    foot_m=8.0, recover=0.35)
    # The finalized grass SURFACE (bilinear on the graded+clamped grid) — the single source of truth the
    # edge strips DRAPE onto so their outer lip sits flush on the ground at road resolution (no float,
    # independent of the coarse grass grid). Same sampler build_env + audit use via ground.local.json.
    def grass_surf(gx, gz):
        # TRIANGLE-exact, not bilinear: the grass mesh splits each quad (a,b,c)+(a,c,d); bilinear
        # disagrees with those triangles by up to ~0.5 m mid-quad on 10 m mountain cells — the
        # drape's lip "touched" a surface that doesn't render, and daylight showed under the sheet.
        return _grid_trisurf(grid_xyz, gx, gz)
    # ROAD EDGE — config-gated. Default: a paved asphalt SHOULDER that ramps/embanks the lane edge down (or
    # up) to the REAL ground. `road_edge.profile == "sidewalk"` instead sweeps a continuous
    # curb→sidewalk→verge→embankment profile (urban streets, e.g. Sand Creek). Both DRAPE their outer edge
    # onto grass_surf, so nothing hovers over the terrain even on a 10 m embankment.
    edge_cfg = cfg_raw.get("road_edge", {})
    if edge_cfg.get("profile") == "sidewalk":
        # SIDEWALKS FOLLOW THE STREET'S LINE, not the pavement boundary (Kevin: "very
        # zigzaggy"): the walk's offset uses a rolling-median width (~30 m window) so flare
        # jitter and per-way width steps don't wiggle it; max() with the real width keeps the
        # curb outside the pavement at true junction mouths.
        _swin = 4
        _sw_line = [sorted(widths[max(0, i - _swin):i + _swin + 1])[len(widths[max(0, i - _swin):i + _swin + 1]) // 2]
                    for i in range(len(widths))]
        _sw_widths = [max(a, b) for a, b in zip(_sw_line, widths)]
        shoulder = ribbon.curb_sidewalk(centerline, _sw_widths, lift=ROAD_LIFT_M,
                                        grass_clearance=GRASS_CLEARANCE_M, bank_at=bnk, ground=grass_surf,
                                        curb_h=float(edge_cfg.get("curb_h", 0.15)),
                                        curb_face_w=float(edge_cfg.get("curb_face_w", 0.08)),
                                        sidewalk_w=float(edge_cfg.get("sidewalk_w", 1.5)),
                                        grade_w=float(edge_cfg.get("grade_w", 1.0)))
        edge_group, edge_mat = "1KERB_sidewalk", "kerb"
    else:
        shoulder = ribbon.road_shoulder(centerline, widths, lift=ROAD_LIFT_M, bank_at=bnk,
                                        ground_drop=GRASS_CLEARANCE_M, ground=grass_surf)
        edge_group, edge_mat = "1ROAD_shoulder", "road"
    # Kerb geometry is config-driven so a track can opt into taller RACING kerbs without touching the
    # default 5 cm street kerb. `kerb.height_m` / `kerb.width_m` / `kerb.top_frac` in track.config.json.
    kerb_cfg = cfg_raw.get("kerb", {})
    if kerb_cfg.get("enabled", True):
        kerb = kerbs.corner_kerbs(centerline, widths, bank_at=bnk, ground=grass_surf,
                                  kerb_h=float(kerb_cfg.get("height_m", 0.05)),
                                  kerb_w=float(kerb_cfg.get("width_m", 1.0)),
                                  top_frac=float(kerb_cfg.get("top_frac", 0.55)),
                                  edge_ramp=float(kerb_cfg.get("edge_ramp", 0.0)))
        if kerb_cfg:
            print(f"  racing kerbs: h={kerb_cfg.get('height_m', 0.05)}m w={kerb_cfg.get('width_m', 1.0)}m "
                  f"edge_ramp={kerb_cfg.get('edge_ramp', 0.0)}")
    else:
        # kerb.enabled=false: rural/mountain roads have NO kerbs — the curvature test calls the whole
        # mountain "corners" and lined 25 km of a 6.5 m lane with vertical-lipped rumble strips
        # (the Lariat's "undriveable slop"). The flush shoulder is the only edge treatment.
        kerb = {"vertices": [], "uvs": [], "tris": []}
        print("  kerbs: disabled (kerb.enabled=false)")
    kerb["vertices"] = [(x, y + ROAD_LIFT_M, z) for x, y, z in kerb["vertices"]]  # kerb lip at road-edge height
    # ANTI-POKE pass 2: the draped shoulder/kerb outer edges land on the bilinear grass_surf, but the grass
    # MESH is triangulated grid nodes — a node near the seam can sit a touch ABOVE the draped edge (small
    # +0.1–0.5 m pokes on banked ovals). Clamp the grid just below the draped strips (tiny 5 cm clearance,
    # short reach) so no grass triangle pokes up through them, without re-opening a visible gap.
    # FORCE CONTACT (Kevin: "THEY DON'T NEED TO BE THE SAME, THEY NEED TO TOUCH"): the grass
    # sheet is BENT to pass through the shoulder's ground-meet line. No samplers, no windows, no
    # tolerances — the strip's own meet vertices become the terrain's heights near the seam, so
    # the two meshes touch by construction, exactly like the pros' welded edge rings. Data
    # fidelity (corridor fetch) improves WHERE the seam sits; this guarantees THAT it seals.
    _P5 = 4 if True else 3     # road_shoulder emits lip/verge/meet/hem per station (P=4 with ground)
    _sv6 = shoulder["vertices"]
    _pull = []                  # (x, z, target_y) = the meet line
    for _i6 in range(2, len(_sv6), _P5):
        _pull.append(_sv6[_i6])           # meet point of each station/side
    from collections import defaultdict as _fdd
    _ph6 = _fdd(list)
    for _mx6, _my6, _mz6 in _pull:
        _ph6[(int(_mx6 // 8), int(_mz6 // 8))].append((_mx6, _my6, _mz6))
    _bent = 0
    for _row6 in grid_xyz:
        for _k6 in range(len(_row6)):
            _gx6, _gy6, _gz6 = _row6[_k6]
            _best6 = None
            for _di6 in (-1, 0, 1):
                for _dj6 in (-1, 0, 1):
                    for _mx6, _my6, _mz6 in _ph6.get((int(_gx6 // 8) + _di6, int(_gz6 // 8) + _dj6), ()):
                        _d6 = (_gx6 - _mx6) ** 2 + (_gz6 - _mz6) ** 2
                        if _best6 is None or _d6 < _best6[0]:
                            _best6 = (_d6, _my6)
            if _best6 is None:
                continue
            _d6 = _best6[0] ** 0.5
            if _d6 > 8.0 or abs(_best6[1] - _gy6) > 20.0:     # beyond seam reach / other leg
                continue
            _w6 = 1.0 - _d6 / 8.0
            _row6[_k6] = (_gx6, _gy6 * (1.0 - _w6) + _best6[1] * _w6, _gz6)
            _bent += 1
    print(f"  [contact] bent {_bent} grid nodes through the shoulder meet line (they TOUCH)")
    # the bend seals seams but must NEVER raise ground through pavement — re-run the ROAD clamp
    # after it (audit B caught exactly one bent node poking +2.74 through a hairpin deck).
    ribbon.clamp_terrain_below_road(grid_xyz, road["vertices"] + runoff["vertices"],
                                    clear=GRASS_CLEARANCE_M, grid_spacing=float(meta["spacing_m"]),
                                    foot_m=8.0, recover=0.35)
    ribbon.clamp_terrain_below_road(grid_xyz, shoulder["vertices"] + kerb["vertices"], clear=0.05, reach=6.0,
                                    grid_spacing=float(meta["spacing_m"]))
    mk_cfg = cfg_raw.get("road_markings", {}) or {}
    if mk_cfg.get("style") == "lane":
        # Lake Murray-style painted lines: solid double-yellow centre (two-way), solid white edge
        # lines, dashed dividers where width allows (ported ribbon.lane_markings)
        _lm = ribbon.lane_markings(centerline, widths, bank_at=bnk,
                                   center_yellow=bool(mk_cfg.get("center_yellow", True)))
        marks, yline = _lm["white"], _lm["yellow"]
    else:
        marks = ribbon.road_markings(centerline, widths, bank_at=bnk)
        yline = {"vertices": [], "uvs": [], "tris": []}
    marks["vertices"] = [(x, y + ROAD_LIFT_M + 0.012, z) for x, y, z in marks["vertices"]]
    yline["vertices"] = [(x, y + ROAD_LIFT_M + 0.011, z) for x, y, z in yline["vertices"]]

    # WARNING BARRIERS — guardrails on the OUTSIDE of sharp turns (and any corner hiding over a crest),
    # so you read "hard corner ahead" + get caught if you run wide. Vertical double-sided walls: NOT
    # oriented up (they're not ground), they collide as solid geometry.
    # ROAD-COVERAGE GUARD — the one invariant that needs no leg-ownership reasoning: nothing that
    # isn't road may stand ON or hang OVER the road surface at the same (x,z). Distance/station
    # filters both failed exactly where the lap stacks legs (M-curve switchbacks, the ramp gore):
    # the upper leg's shoulder draped 1.2 m above the lower leg's lane, and a gore barrier stood
    # 2.4 m from centreline on 7.7 m pavement. Tested against the BUILT road triangles.
    from collections import defaultdict as _rcdd
    _rc: dict = _rcdd(list)
    _rv = road["vertices"]
    for _t in road["tris"]:
        _xs = [_rv[_v][0] for _v in _t]; _zs = [_rv[_v][2] for _v in _t]
        for _ci in range(int(min(_xs) // 3.0), int(max(_xs) // 3.0) + 1):
            for _cj in range(int(min(_zs) // 3.0), int(max(_zs) // 3.0) + 1):
                _rc[(_ci, _cj)].append(_t)

    def _road_tops(px, pz):
        tops = []
        for _t in _rc.get((int(px // 3.0), int(pz // 3.0)), ()):
            _a, _b, _c = _rv[_t[0]], _rv[_t[1]], _rv[_t[2]]
            _d = (_b[2] - _c[2]) * (_a[0] - _c[0]) + (_c[0] - _b[0]) * (_a[2] - _c[2])
            if abs(_d) < 1e-12:
                continue
            _w0 = ((_b[2] - _c[2]) * (px - _c[0]) + (_c[0] - _b[0]) * (pz - _c[2])) / _d
            _w1 = ((_c[2] - _a[2]) * (px - _c[0]) + (_a[0] - _c[0]) * (pz - _c[2])) / _d
            _w2 = 1.0 - _w0 - _w1
            if _w0 >= -1e-6 and _w1 >= -1e-6 and _w2 >= -1e-6:
                tops.append(_w0 * _a[1] + _w1 * _b[1] + _w2 * _c[1])
        return tops

    def drop_over_road(mesh, label, *, lo, hi, mode="vert"):
        """Drop tris standing/hanging over the road: any vert (or the centroid) whose (x,z) is
        covered by a road tri with the vert lo..hi above that road surface."""
        if not mesh["tris"]:
            return
        mv = mesh["vertices"]
        keep = []
        for tri in mesh["tris"]:
            corners = [mv[v] for v in tri]
            # 7-point sampling: verts + edge midpoints + centroid. A hairpin-folded strip's FACE
            # spans the lane while all three corners sit off the road — vert- and centroid-only
            # tests both missed it.
            pts3 = list(corners)
            for a in range(3):
                b = (a + 1) % 3
                pts3.append(tuple((corners[a][k] + corners[b][k]) / 2.0 for k in range(3)))
            pts3.append(tuple(sum(c[k] for c in corners) / 3.0 for k in range(3)))
            bad = False
            for vx, vy, vz in pts3:
                for ry in _road_tops(vx, vz):
                    if lo < vy - ry < hi:
                        bad = True
                        break
                if bad:
                    break
            if bad:
                continue
            keep.append(tri)
        if len(keep) != len(mesh["tris"]):
            print(f"  {label}: dropped {len(mesh['tris']) - len(keep)} tris over/on the road surface")
            mesh["tris"] = keep

    # shoulder: SINK any geometry that rises over the deck to below it instead of dropping tris —
    # dropped tris left HOLES in the embankment skirt (visible daylight under the road at the
    # switchback stacks). Verts covered by road clamp under the local deck; tris still flagged by
    # 7-point sampling (spanning faces) sink whole. Mesh stays watertight.
    # LAYER WINDOW: _road_tops(x,z) returns EVERY deck in this XZ column — at a stacked switchback
    # that's both legs. min(_tops) is the LOWER leg, and sinking an upper-leg shoulder vert to
    # "lower deck - 0.4" teleports it a hundred metres down: vertical shoulder curtains, the
    # rainbow-road layers Kevin flew off. Only sink relative to decks within OWN_DECK_DY of the
    # vert itself — a shoulder rides within ~2 m of its own deck by construction.
    OWN_DECK_DY = 4.0
    # COVERAGE-AWARE deck sampling: min() over hash-cell VERTS spans BOTH carriageway edges on a
    # 6.5 m road (one 4 m cell) — on every crossfalled section the shoulder lip was compared
    # against the LOWER far edge and sunk 0.4 under it (the 0.6 m "shoulder separated" step Kevin
    # kept driving off, both tracks). "Rises over the deck" only means anything where a road TRI
    # actually covers this XZ — barycentric sample of the road surface, or leave the vert alone.
    from collections import defaultdict as _rdd
    _rc3: dict = _rdd(list)
    _rv3 = road["vertices"]
    for _t9 in road["tris"]:
        _xs9 = [_rv3[_v][0] for _v in _t9]; _zs9 = [_rv3[_v][2] for _v in _t9]
        for _ci9 in range(int(min(_xs9) // 4.0), int(max(_xs9) // 4.0) + 1):
            for _cj9 in range(int(min(_zs9) // 4.0), int(max(_zs9) // 4.0) + 1):
                _rc3[(_ci9, _cj9)].append(_t9)

    def _road_surf_at(px9, pz9, y_ref9):
        best9 = None
        for _t9 in _rc3.get((int(px9 // 4.0), int(pz9 // 4.0)), ()):
            _a9, _b9, _c9 = _rv3[_t9[0]], _rv3[_t9[1]], _rv3[_t9[2]]
            _d9 = (_b9[2] - _c9[2]) * (_a9[0] - _c9[0]) + (_c9[0] - _b9[0]) * (_a9[2] - _c9[2])
            if abs(_d9) < 1e-12:
                continue
            _w09 = ((_b9[2] - _c9[2]) * (px9 - _c9[0]) + (_c9[0] - _b9[0]) * (pz9 - _c9[2])) / _d9
            _w19 = ((_c9[2] - _a9[2]) * (px9 - _c9[0]) + (_a9[0] - _c9[0]) * (pz9 - _c9[2])) / _d9
            _w29 = 1.0 - _w09 - _w19
            if _w09 >= -1e-6 and _w19 >= -1e-6 and _w29 >= -1e-6:
                _y9 = _w09 * _a9[1] + _w19 * _b9[1] + _w29 * _c9[1]
                if abs(_y9 - y_ref9) <= OWN_DECK_DY and (best9 is None or _y9 < best9):
                    best9 = _y9
        return best9

    _sv2 = shoulder["vertices"]
    _sunk = 0
    for _vi in range(len(_sv2)):
        _vx, _vy, _vz = _sv2[_vi]
        _deck9 = _road_surf_at(_vx, _vz, _vy)
        if _deck9 is not None and _vy > _deck9 + 0.15:
            _sv2[_vi] = (_vx, _deck9 - 0.4, _vz)
            _sunk += 1
    for _t2 in shoulder["tris"]:
        _corners = [_sv2[_v] for _v in _t2]
        # 2 m barycentric LATTICE, not fixed-count samples: a long drape tri spans the whole 6.5 m
        # road between any 7 fixed points (the 5.3 km M-curve face survived vert, centroid AND
        # 7-point sampling). Lattice density scales with the tri's longest edge.
        _emax = max(math.hypot(_corners[_a][0] - _corners[(_a + 1) % 3][0],
                               _corners[_a][2] - _corners[(_a + 1) % 3][2]) for _a in range(3))
        _nsub = max(2, min(24, int(_emax / 2.0) + 1))
        _samples = []
        for _iu in range(_nsub + 1):
            for _iv in range(_nsub + 1 - _iu):
                _u = _iu / _nsub
                _v2 = _iv / _nsub
                _w2 = 1.0 - _u - _v2
                _samples.append(tuple(_u * _corners[0][_k] + _v2 * _corners[1][_k] + _w2 * _corners[2][_k]
                                      for _k in range(3)))
        _tri_y = sum(_c[1] for _c in _corners) / 3.0
        _local_tops = [_d9 for _sx, _sy, _sz in _samples
                       for _d9 in [_road_surf_at(_sx, _sz, _tri_y)] if _d9 is not None]
        if _local_tops and any(_sy > _d9 + 0.15 for (_sx, _sy, _sz), _d9 in
                               zip([_p for _p in _samples for _q in [_road_surf_at(_p[0], _p[2], _tri_y)] if _q is not None],
                                   _local_tops)):
            _floor = min(_local_tops) - 0.4
            for _v in _t2:
                _vx, _vy, _vz = _sv2[_v]
                if _vy > _floor:
                    _sv2[_v] = (_vx, _floor, _vz)
                    _sunk += 1
    if _sunk:
        print(f"  shoulder: sank {_sunk} verts under the deck (watertight — no drape holes)")

    # CONTACT PIN on the GRID — runs AFTER the shoulder sink pass (the LAST pavement mutation;
    # clamping before it left a +0.14 poke where the hem sank under already-clamped grass), and
    # BEFORE ground.local + grass meshing so props, audit check G and the rendered grass all see
    # the SAME final surface. Anything under the pavement footprint is pinned to deck - clearance
    # both ways; the edge feathers out as a graded fill face; bridges keep their valley.
    _rv7 = road["vertices"]
    from collections import defaultdict as _add7
    _rc7: dict = _add7(list)
    for _t7 in road["tris"]:
        _xs7 = [_rv7[_v][0] for _v in _t7]; _zs7 = [_rv7[_v][2] for _v in _t7]
        for _ci7 in range(int(min(_xs7) // 4.0), int(max(_xs7) // 4.0) + 1):
            for _cj7 in range(int(min(_zs7) // 4.0), int(max(_zs7) // 4.0) + 1):
                _rc7[(_ci7, _cj7)].append(_t7)
    _gv7 = [_n7g for _row7g in grid_xyz for _n7g in _row7g]   # flat view of the grid nodes
    # bridge spans keep their valley: under a bridge the pin may only push DOWN, never lift.
    _st7 = [0.0]
    for _q7 in range(1, len(centerline)):
        _st7.append(_st7[-1] + math.hypot(centerline[_q7][0] - centerline[_q7 - 1][0],
                                          centerline[_q7][2] - centerline[_q7 - 1][2]))
    _ch7: dict = _add7(list)
    for _q7, _pt7 in enumerate(centerline):
        _ch7[(int(_pt7[0] // 24.0), int(_pt7[2] // 24.0))].append((_pt7[0], _pt7[2], _st7[_q7]))
    def _near_station7(x, z):
        best_d2, best_s = 1e18, None
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for cx, cz, cs in _ch7.get((int(x // 24.0) + di, int(z // 24.0) + dj), ()):
                    d2 = (x - cx) ** 2 + (z - cz) ** 2
                    if d2 < best_d2:
                        best_d2, best_s = d2, cs
        return best_s
    _FEATHER7 = 24.0            # embankment reach: a 2:1 fill face can carry an 11 m edge drop
    _BENCH7 = 1.0               # flush bench beside the pavement before the face starts
    _FILL_SLOPE7 = 0.5          # 2:1 (H:V) engineered fill, per docs/ROAD-CONSTRUCTION.md
    _hs7 = float(meta["spacing_m"]) * 0.55

    def _deck_window_min7(px, pz, y_ref):
        """Lowest deck surface within half a grid cell of (px,pz), 9-point scan, layer-guarded."""
        lo = None
        for ox, oz in ((0.0, 0.0), (_hs7, 0.0), (-_hs7, 0.0), (0.0, _hs7), (0.0, -_hs7),
                       (_hs7, _hs7), (-_hs7, -_hs7), (_hs7, -_hs7), (-_hs7, _hs7)):
            for t in _rc7.get((int((px + ox) // 4.0), int((pz + oz) // 4.0)), ()):
                a, b, c = _rv7[t[0]], _rv7[t[1]], _rv7[t[2]]
                d = (b[2] - c[2]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[2] - c[2])
                if abs(d) < 1e-12:
                    continue
                w0 = ((b[2] - c[2]) * (px + ox - c[0]) + (c[0] - b[0]) * (pz + oz - c[2])) / d
                w1 = ((c[2] - a[2]) * (px + ox - c[0]) + (a[0] - c[0]) * (pz + oz - c[2])) / d
                w2 = 1.0 - w0 - w1
                if w0 >= -1e-6 and w1 >= -1e-6 and w2 >= -1e-6:
                    y = w0 * a[1] + w1 * b[1] + w2 * c[1]
                    if abs(y - y_ref) <= 20.0 and (lo is None or y < lo):
                        lo = y
        return lo

    def _closest_on_road7(px, pz):
        """(distance, deck_y) at the closest point of any road triangle within _FEATHER7 —
        a CONTINUOUS field. (Nearest-vertex distance oscillates with vertex spacing and
        printed a 0.2 m washboard into the feathered fill face.)"""
        best = None
        ci, cj = int(px // 4.0), int(pz // 4.0)
        _r7 = int(_FEATHER7 // 4.0) + 1
        for di in range(-_r7, _r7 + 1):
            for dj in range(-_r7, _r7 + 1):
                _cell7 = (ci + di, cj + dj)
                for t in [(_rv7[t7[0]], _rv7[t7[1]], _rv7[t7[2]]) for t7 in _rc7.get(_cell7, ())] \
                        + list(_rc8.get(_cell7, ())):
                    a, b, c = t
                    # closest point on tri in 2D: clamp to each edge, keep the nearest candidate
                    for (p1, p2) in ((a, b), (b, c), (c, a)):
                        ex, ez = p2[0] - p1[0], p2[2] - p1[2]
                        L2 = ex * ex + ez * ez or 1e-12
                        tt = max(0.0, min(1.0, ((px - p1[0]) * ex + (pz - p1[2]) * ez) / L2))
                        qx, qz = p1[0] + tt * ex, p1[2] + tt * ez
                        d2 = (px - qx) ** 2 + (pz - qz) ** 2
                        qy = p1[1] + tt * (p2[1] - p1[1])
                        if best is None or d2 < best[0]:
                            best = (d2, qy)
        if best is None or best[0] > _FEATHER7 * _FEATHER7:
            return None, None
        return math.sqrt(best[0]), best[1]
    # pavement beyond the main ribbon (shoulder/runoff/kerb) — its own tri hash, used for the
    # two-sided under-shoulder pin AND as the fill-face anchor (the face must start at the
    # shoulder HEM: anchoring at the road edge left grass 0.7-1.0 m below the shoulder — a
    # daylight strip under the hem for kilometres, THE 'ribbon above the mountain' look).
    _all8 = []
    for _m8 in (shoulder, runoff, kerb):
        for _t8 in _m8["tris"]:
            _all8.append((_m8["vertices"][_t8[0]], _m8["vertices"][_t8[1]], _m8["vertices"][_t8[2]]))
    _rc8: dict = _add7(list)
    for _tt8 in _all8:
        _xs8 = [_v[0] for _v in _tt8]; _zs8 = [_v[2] for _v in _tt8]
        for _ci8 in range(int(min(_xs8) // 4.0), int(max(_xs8) // 4.0) + 1):
            for _cj8 in range(int(min(_zs8) // 4.0), int(max(_zs8) // 4.0) + 1):
                _rc8[(_ci8, _cj8)].append(_tt8)

    def _shoulder_window_min8(px, pz, y_ref):
        """Lowest shoulder/runoff/kerb SURFACE within half a grid cell (9-point), layer-guarded.
        Near-vertical tris (battered wall faces, cut faces) are excluded — they are walls, and
        their barycentric heights dive metres down the face, poisoning the window minimum."""
        lo = None
        for ox, oz in ((0.0, 0.0), (_hs7, 0.0), (-_hs7, 0.0), (0.0, _hs7), (0.0, -_hs7),
                       (_hs7, _hs7), (-_hs7, -_hs7), (_hs7, -_hs7), (-_hs7, _hs7)):
            for a, b, c in _rc8.get((int((px + ox) // 4.0), int((pz + oz) // 4.0)), ()):
                ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
                vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
                nx8 = uy * vz - uz * vy; ny8 = uz * vx - ux * vz; nz8 = ux * vy - uy * vx
                nl8 = math.sqrt(nx8 * nx8 + ny8 * ny8 + nz8 * nz8) or 1e-12
                if abs(ny8) / nl8 < 0.5:
                    continue
                d = (b[2] - c[2]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[2] - c[2])
                if abs(d) < 1e-12:
                    continue
                w0 = ((b[2] - c[2]) * (px + ox - c[0]) + (c[0] - b[0]) * (pz + oz - c[2])) / d
                w1 = ((c[2] - a[2]) * (px + ox - c[0]) + (a[0] - c[0]) * (pz + oz - c[2])) / d
                w2 = 1.0 - w0 - w1
                if w0 >= -1e-6 and w1 >= -1e-6 and w2 >= -1e-6:
                    y = w0 * a[1] + w1 * b[1] + w2 * c[1]
                    if abs(y - y_ref) <= 20.0 and (lo is None or y < lo):
                        lo = y
        return lo

    # coarse presence filter: only verts within one 24 m cell of any PAVEMENT triangle pay for
    # the full closest-point scan (fine cells) — the rest of the 840k-vert grid skips in O(1).
    _near7 = set()
    for _t7 in road["tris"]:
        for _v7 in _t7:
            _near7.add((int(_rv7[_v7][0] // 24.0), int(_rv7[_v7][2] // 24.0)))
    for _tt8 in _all8:
        for _v8 in _tt8:
            _near7.add((int(_v8[0] // 24.0), int(_v8[2] // 24.0)))
    _near7 = {(ci + di, cj + dj) for (ci, cj) in _near7 for di in (-1, 0, 1) for dj in (-1, 0, 1)}
    _fixed7 = 0
    _feathered7 = 0
    for _i7 in range(len(_gv7)):
        _gx7, _gy7, _gz7 = _gv7[_i7]
        _best7 = None
        for _t7 in _rc7.get((int(_gx7 // 4.0), int(_gz7 // 4.0)), ()):
            _a7, _b7, _c7 = _rv7[_t7[0]], _rv7[_t7[1]], _rv7[_t7[2]]
            _d7 = (_b7[2] - _c7[2]) * (_a7[0] - _c7[0]) + (_c7[0] - _b7[0]) * (_a7[2] - _c7[2])
            if abs(_d7) < 1e-12:
                continue
            _w07 = ((_b7[2] - _c7[2]) * (_gx7 - _c7[0]) + (_c7[0] - _b7[0]) * (_gz7 - _c7[2])) / _d7
            _w17 = ((_c7[2] - _a7[2]) * (_gx7 - _c7[0]) + (_a7[0] - _c7[0]) * (_gz7 - _c7[2])) / _d7
            _w27 = 1.0 - _w07 - _w17
            if _w07 >= -1e-6 and _w17 >= -1e-6 and _w27 >= -1e-6:
                _y7 = _w07 * _a7[1] + _w17 * _b7[1] + _w27 * _c7[1]
                if abs(_y7 - _gy7) <= 20.0 and (_best7 is None or _y7 < _best7):
                    _best7 = _y7
        if _best7 is None:
            # outside the pavement footprint the WELDED EDGE RINGS own the transition (#17):
            # the grass boundary shares the shoulder's ground-meet vertices, so the grid here
            # stays at natural ground and rides ~2 cm under the ring annulus.
            continue
        # SAG-PROOF: pin to the MINIMUM deck height within half a grid cell (all 8 directions),
        # not the deck at the exact point — otherwise 6 m grass chords bridge ABOVE the deck
        # through sags and across cambered decks. With each vert at its local window minimum,
        # every chord between verts stays under the deck everywhere on the span.
        _wm7 = _deck_window_min7(_gx7, _gz7, _gy7)
        if _wm7 is not None and _wm7 < _best7:
            _best7 = _wm7
        if _gy7 < _best7 - GRASS_CLEARANCE_M:
            _s7 = _near_station7(_gx7, _gz7)
            if _s7 is not None and bridge_of(_s7):
                continue  # valley under a bridge deck stays open
        if abs(_gy7 - (_best7 - GRASS_CLEARANCE_M)) > 1e-4:
            # TWO-SIDED contact pin: under the pavement footprint the ground IS the deck
            # underside (rt_california ships 0.0 mm median vertical — terrain CONFORMED to
            # the road). Clamp-down alone left real mountainside ground metres below the
            # outer deck half; pin it to deck - clearance in both directions.
            _gv7[_i7] = (_gx7, _best7 - GRASS_CLEARANCE_M, _gz7)
            _fixed7 += 1
    if _fixed7 or _feathered7:
        print(f"  [contact pin] {_fixed7} grass verts pinned to deck underside, {_feathered7} raised onto the fill bench/face")
    # TWO-SIDED pin under the rest of the pavement (shoulder/runoff/kerb): the grass hugs the
    # shoulder underside exactly like it hugs the deck — down-only clamping left it 0.7-1.0 m
    # low (the daylight strip). Window-min for chord safety; road-footprint verts (already
    # pinned lower, chord-safe vs the deck) are never raised above their road pin.
    _down8 = 0
    for _i8 in range(len(_gv7)):
        _gx8, _gy8, _gz8 = _gv7[_i8]
        if (int(_gx8 // 24.0), int(_gz8 // 24.0)) not in _near7:
            continue
        _lo8 = _shoulder_window_min8(_gx8, _gz8, _gy8)
        if _lo8 is None:
            continue
        _tgt8 = _lo8 - GRASS_CLEARANCE_M
        if _tgt8 > _gy8:
            # two-sided under pavement: raise capped by the ROAD window-min (chord safety vs the
            # deck) and never under bridges. Down-only here let deck-edge gaps grow to ~1.2 m
            # where the shoulder overlaps the lip (the weld seals the VISIBLE seam; this keeps
            # the under-slab contact number tight too).
            _wm8 = _deck_window_min7(_gx8, _gz8, _gy8)
            if _wm8 is not None and _tgt8 > _wm8 - GRASS_CLEARANCE_M:
                _tgt8 = _wm8 - GRASS_CLEARANCE_M
            _s8 = _near_station7(_gx8, _gz8)
            if _s8 is not None and bridge_of(_s8):
                continue
            if _tgt8 <= _gy8:
                continue
        if abs(_tgt8 - _gy8) > 1e-4:
            _gv7[_i8] = (_gx8, _tgt8, _gz8)
            _down8 += 1
    if _down8:
        print(f"  [contact pin] {_down8} grass verts pinned to the shoulder/runoff underside")
    # write the pinned nodes back into the grid, THEN snapshot ground.local + mesh the grass
    _nx7w = len(grid_xyz[0])
    for _j7w in range(len(grid_xyz)):
        for _i7w in range(_nx7w):
            grid_xyz[_j7w][_i7w] = _gv7[_j7w * _nx7w + _i7w]
    # Persist the graded ground surface for build_env (scenery height) + audit_mesh check G. Reflects
    # grid_xyz exactly (post ALL anti-poke/contact passes) — the surface the grass mesh triangulates.
    write_ground_local(data / "ground.local.json", grid_xyz)
    grass = ribbon.grass_terrain(grid_xyz)

    # ================= #17 PRO CONSTRUCTION: welded edge rings + stone works =================
    # THE WELD: terrain's boundary ring re-uses the shoulder's ground-meet vertices EXACTLY, so
    # daylight at the pavement/terrain seam is geometrically impossible (the hillclimb standard:
    # 81% of grass boundary verts coincide <1 mm with the adjoining strip). Rings march outward
    # at a road-anchored density gradient (1.5/4/10/22 m) at natural ground +2 cm (the grid rides
    # just beneath — no z-fight, no hole). Stone works from the construction selector: RETWALL
    # skins on wall-warranted faces (drop>4 m, 1:6 batter), ROCKCUT skins on rock cuts (drop>2),
    # 1913 stone parapets (0.5x0.4 m) along fill edges with drop>2 m. Bridges keep open valleys.
    ring_mesh = {"vertices": [], "uvs": [], "tris": []}
    para_mesh = {"vertices": [], "uvs": [], "tris": []}
    wallskin = {"vertices": [], "uvs": [], "tris": []}
    rockskin = {"vertices": [], "uvs": [], "tris": []}
    _RINGS17 = (1.5, 4.0, 10.0, 22.0)

    def _pav_at17(px, pz, y_ref):
        """Lowest flat-ish pavement surface AT this exact XZ (road + shoulder/runoff/kerb),
        steep faces excluded, 20 m layer window — the audit's own geometry model."""
        lo = None
        cell = (int(px // 4.0), int(pz // 4.0))
        for a, b, c in ([(_rv7[t[0]], _rv7[t[1]], _rv7[t[2]]) for t in _rc7.get(cell, ())]
                        + list(_rc8.get(cell, ()))):
            ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
            vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
            nx9 = uy * vz - uz * vy; ny9 = uz * vx - ux * vz; nz9 = ux * vy - uy * vx
            nl9 = math.sqrt(nx9 * nx9 + ny9 * ny9 + nz9 * nz9) or 1e-12
            if abs(ny9) / nl9 < 0.5:
                continue
            d = (b[2] - c[2]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[2] - c[2])
            if abs(d) < 1e-12:
                continue
            w0 = ((b[2] - c[2]) * (px - c[0]) + (c[0] - b[0]) * (pz - c[2])) / d
            w1 = ((c[2] - a[2]) * (px - c[0]) + (a[0] - c[0]) * (pz - c[2])) / d
            w2 = 1.0 - w0 - w1
            if w0 >= -1e-6 and w1 >= -1e-6 and w2 >= -1e-6:
                y = w0 * a[1] + w1 * b[1] + w2 * c[1]
                if abs(y - y_ref) <= 20.0 and (lo is None or y < lo):
                    lo = y
        return lo
    _secs_all = shoulder.get("sections", {1.0: [], -1.0: []})
    for _sd17 in (1.0, -1.0):
        _secs = _secs_all.get(_sd17, [])
        _m17 = len(_secs)
        if not _m17:
            continue
        # ring columns (None = bridge span: valley stays open, strip breaks)
        _cols = []
        for _sc in _secs:
            if bridge_of(_sc["arc"]):
                _cols.append(None)
                continue
            _mx, _my, _mz = _sc["meet"]
            _nx17, _nz17 = _sc["n"]
            # ring0 IS the weld — untouched unless a DISTINCT lower pavement sheet (another leg's
            # shoulder at a switchback throat, >0.35 m below) passes beneath: then fold under it.
            _m0 = _sc["meet"]
            # at-point: catches both a distinct lower sheet (switchback throat) AND the own
            # face's neighbor tris sweeping under the meet at concave bends. No window — the
            # window smeared crossfall and false-folded banked sections.
            _pv0 = _pav_at17(_mx, _mz, _my)
            if _pv0 is not None and _pv0 < _my - 0.10:
                _m0 = (_mx, _pv0 - GRASS_CLEARANCE_M - 0.02, _mz)
            _col = [_m0]
            _kcut = len(_RINGS17) + 1     # how many ring rows of this column are usable
            _py17 = _m0[1]
            for _ki17, _dk in enumerate(_RINGS17):
                _px, _pz = _mx + _nx17 * _dk, _mz + _nz17 * _dk
                _gy17 = _grid_trisurf(grid_xyz, _px, _pz) + 0.02
                if abs(_gy17 - _py17) > 20.0:
                    # cliff edge / another leg's valley: TERMINATE the annulus here — a flat
                    # extension hung 30 m grass shelves over the real terrain (74 double-sheets)
                    _kcut = _ki17 + 1
                    break
                # hairpins: 'outward' from one leg sails toward the opposing leg. A small fold
                # (same-height crossing) tucks under its pavement; a DEEP fold (>3 m — a stacked
                # leg's corridor far below) must TERMINATE the strip instead: that space belongs
                # to the inter-leg cut face, and folding created 10-20 m grass curtains (the
                # double-sheet gate caught 76 of them).
                _pv17 = _deck_window_min7(_px, _pz, _gy17)
                _ps17 = _shoulder_window_min8(_px, _pz, _gy17)
                if _ps17 is not None and (_pv17 is None or _ps17 < _pv17):
                    _pv17 = _ps17
                _pa17 = _pav_at17(_px, _pz, _gy17)
                if _pa17 is not None and (_pv17 is None or _pa17 < _pv17):
                    _pv17 = _pa17
                if _pv17 is not None and _gy17 > _pv17 - GRASS_CLEARANCE_M - 0.02:
                    if _gy17 - _pv17 > 3.0:
                        _kcut = _ki17 + 1             # usable rows: ring0.._ki17 (exclusive of this)
                        break
                    _gy17 = _pv17 - GRASS_CLEARANCE_M - 0.02
                _col.append((_px, _gy17, _pz))
                _py17 = _gy17
            _cols.append((_col, _kcut))
        _pairs = [(_a, _a + 1) for _a in range(_m17 - 1)] + ([(_m17 - 1, 0)] if _m17 > 2 else [])
        for _a17, _b17 in _pairs:
            _ea, _eb = _cols[_a17], _cols[_b17]
            if _ea is None or _eb is None:
                continue
            _ca, _cb = _ea[0], _eb[0]
            _K17 = min(len(_ca), len(_cb), _ea[1], _eb[1])
            if _K17 < 2:
                continue
            _bs17 = len(ring_mesh["vertices"])
            ring_mesh["vertices"].extend(_ca[:_K17] + _cb[:_K17])
            ring_mesh["uvs"].extend([(_v[0] / 6.0, _v[2] / 6.0) for _v in (_ca[:_K17] + _cb[:_K17])])
            for _k17 in range(_K17 - 1):
                _i0, _i1 = _bs17 + _k17, _bs17 + _k17 + 1
                _j0, _j1 = _bs17 + _K17 + _k17, _bs17 + _K17 + _k17 + 1
                ring_mesh["tris"].append((_i0, _j0, _j1))
                ring_mesh["tris"].append((_i0, _j1, _i1))
        # ---- stone works along this side ----
        def _runs17(flag, exit_flag=None, min_run=5, max_gap=0):
            # HYSTERESIS + GAP-MERGE: the warrant flickers station-to-station on rolling drops,
            # which shipped parapets as short separated boxes ("tall tombstones"). Once a run is
            # open it stays open while exit_flag holds, and gaps up to max_gap stations are
            # bridged into one continuous wall.
            exit_flag = exit_flag or flag
            out, run, gap, open_ = [], [], 0, False
            for _q, _sc in enumerate(_secs):
                ok = (exit_flag(_sc) if open_ else flag(_sc)) and not bridge_of(_sc["arc"])
                if ok:
                    if gap and run:
                        run.extend(range(run[-1] + 1, _q))    # bridge the small gap
                    run.append(_q); gap = 0; open_ = True
                else:
                    gap += 1
                    if gap > max_gap:
                        if len(run) >= min_run:
                            out.append(run)
                        run, open_, gap = [], False, 0
            if len(run) >= min_run:
                out.append(run)
            return out
        # stone parapets: fill edges dropping > 2 m (incl. wall tops) — 0.5 h x 0.4 w at the verge
        for _run in _runs17(lambda sc: (not sc["cutting"]) and sc["drop"] > 2.0,
                            exit_flag=lambda sc: (not sc["cutting"]) and sc["drop"] > 1.4,
                            min_run=10, max_gap=4):
            _bs17 = len(para_mesh["vertices"])
            # smooth the cap line: a parapet is coursed masonry, its top runs fair, not jittery
            _vys = [_secs[_q]["verge"][1] for _q in _run]
            _vys = [sum(_vys[max(0, k - 2):k + 3]) / len(_vys[max(0, k - 2):k + 3]) for k in range(len(_vys))]
            for _qi, _q in enumerate(_run):
                _sc = _secs[_q]
                _vx, _vy, _vz = _sc["verge"][0], _vys[_qi], _sc["verge"][2]
                _nx17, _nz17 = _sc["n"]
                for _off17, _hy17 in ((-0.2, -0.15), (-0.2, 0.5), (0.2, 0.5), (0.2, -0.15)):
                    para_mesh["vertices"].append((_vx + _nx17 * _off17, _vy + _hy17, _vz + _nz17 * _off17))
                    para_mesh["uvs"].append((_sc["arc"] / 1.5, (_hy17 + 0.15) / 0.65))
            for _q in range(len(_run) - 1):
                _r0, _r1 = _bs17 + _q * 4, _bs17 + (_q + 1) * 4
                for _f0, _f1 in ((0, 1), (1, 2), (2, 3)):   # inner face, cap, outer face
                    para_mesh["tris"].append((_r0 + _f0, _r0 + _f1, _r1 + _f1))
                    para_mesh["tris"].append((_r0 + _f0, _r1 + _f1, _r1 + _f0))
            # end caps
            for _e0 in (_bs17, _bs17 + (len(_run) - 1) * 4):
                para_mesh["tris"].append((_e0, _e0 + 1, _e0 + 2))
                para_mesh["tris"].append((_e0, _e0 + 2, _e0 + 3))
        # face skins: quad strip verge->meet pushed 5 cm out, stone on walls / granite on rock cuts
        for _skin, _flag, _xflag in (
                (wallskin, lambda sc: sc.get("wall"), lambda sc: (not sc["cutting"]) and sc["drop"] > 3.2),
                (rockskin, lambda sc: sc["cutting"] and sc["drop"] > 2.0,
                 lambda sc: sc["cutting"] and sc["drop"] > 1.4)):
            for _run in _runs17(_flag, exit_flag=_xflag, min_run=8, max_gap=4):
                _bs17 = len(_skin["vertices"])
                for _q in _run:
                    _sc = _secs[_q]
                    _nx17, _nz17 = _sc["n"]
                    _vx, _vy, _vz = _sc["verge"]
                    _mx, _my, _mz = _sc["meet"]
                    _skin["vertices"].append((_vx + _nx17 * 0.05, _vy, _vz + _nz17 * 0.05))
                    _skin["uvs"].append((_sc["arc"] / 2.5, 0.0))
                    _skin["vertices"].append((_mx + _nx17 * 0.05, _my, _mz + _nz17 * 0.05))
                    _skin["uvs"].append((_sc["arc"] / 2.5, _sc["drop"] / 2.5))
                for _q in range(len(_run) - 1):
                    _r0, _r1 = _bs17 + _q * 2, _bs17 + (_q + 1) * 2
                    _skin["tris"].append((_r0, _r0 + 1, _r1 + 1))
                    _skin["tris"].append((_r0, _r1 + 1, _r1))
    print(f"  [#17] welded rings {len(ring_mesh['vertices'])}v; parapets {len(para_mesh['vertices'])}v; "
          f"wall skins {len(wallskin['vertices'])}v; rock skins {len(rockskin['vertices'])}v")
    # ==========================================================================================

    barrier, barrier_spots = kerbs.warning_barriers(centerline, widths)
    barrier_group = ("BARRIER_warning", "kerb")
    # FENCES, NOT BARRIERS (Kevin: "barriers are getting in the way of driving... reserve
    # barriers for only the tightest corners after fast sections to showcase danger"): keep the
    # concrete only where a FAST approach (last ~300 m nearly straight) meets a SHARP corner
    # (>=70 deg, or a blind crest corner); every other warning run becomes a wooden fence set
    # FURTHER off the driving surface.
    _stb = [0.0]
    for _i in range(1, len(centerline)):
        _stb.append(_stb[-1] + math.hypot(centerline[_i][0] - centerline[_i - 1][0],
                                          centerline[_i][2] - centerline[_i - 1][2]))

    def _hdb(i):
        a, b = max(0, i - 2), min(len(centerline) - 1, i + 2)
        return math.atan2(centerline[b][2] - centerline[a][2], centerline[b][0] - centerline[a][0])

    def _fast_approach(idx):
        a = idx
        while a > 0 and _stb[idx] - _stb[a] < 300.0:
            a -= 1
        worst = 0.0
        j = a
        while j < idx - 4:
            worst = max(worst, abs((math.degrees(_hdb(j + 4) - _hdb(j)) + 180.0) % 360.0 - 180.0))
            j += 4
        return worst < 18.0        # never turns hard in the approach = carrying speed

    _keep_spots, _fence_spots = [], []
    for _sp in barrier_spots:
        danger = (abs(_sp.get("turn_deg", 0)) >= 70 and _fast_approach(_sp["start_idx"])) \
                 or (_sp.get("crest") and abs(_sp.get("turn_deg", 0)) >= 45)
        (_keep_spots if danger else _fence_spots).append(_sp)
    print(f"  barriers: {len(_keep_spots)} danger runs keep concrete; "
          f"{len(_fence_spots)} runs become wooden fences")
    barrier_spots = _keep_spots
    fence_wood = {"vertices": [], "uvs": [], "tris": []}
    if _fence_spots:
        from scripts.environment import props as _props_f
        _wf_path = Path(__file__).resolve().parents[2] / "assets" / "models" / "wood_fence_panel.obj"
        if _wf_path.exists():
            _wf_mod = _props_f.load_module(_wf_path)
            _ranges = [{"start_m": _stb[_sp["start_idx"]], "end_m": _stb[min(_sp["end_idx"], len(_stb) - 1)],
                        "side": int(_sp["side"]), "offset_m": 3.0} for _sp in _fence_spots]
            # max_dy=3.5: a fence rides near its own road grade — a bank/creek drop under a
            # module means bridge territory, skip it (one panel shipped floating +1.62 m).
            fence_wood = _props_f.instance_line(centerline, _wf_mod, ranges=_ranges, widths_m=widths,
                                                ground=lambda x, z: _grid_trisurf(grid_xyz, x, z),
                                                module_len=1.73, max_dy=3.5)
    if cfg_raw.get("props", {}).get("concrete_barriers"):
        # Kevin's 4 m concrete barrier modules replace the procedural swept jersey — real geometry,
        # instanced along the SAME warning-barrier runs, shipped physical as 1WALL_* (collidable).
        from scripts.environment import props as props_mod
        _mod = props_mod.load_module(Path(cfg_raw["props"].get(
            "barrier_obj", str(Path(__file__).resolve().parents[2] / "assets" / "models" / "concrete_barrier_4m.obj"))))
        # edge-surface sampler: road + shoulder tri tops, so barrier bases meet the pavement bench
        from collections import defaultdict as _bdd
        _bc2: dict = _bdd(list)
        for _mesh2 in (road, shoulder, grass):   # grass too: SC barriers stand past the sidewalk lip
            _mv2 = _mesh2["vertices"]
            for _t3 in _mesh2["tris"]:
                _xs3 = [_mv2[_v][0] for _v in _t3]; _zs3 = [_mv2[_v][2] for _v in _t3]
                for _ci3 in range(int(min(_xs3) // 3.0), int(max(_xs3) // 3.0) + 1):
                    for _cj3 in range(int(min(_zs3) // 3.0), int(max(_zs3) // 3.0) + 1):
                        _bc2[(_ci3, _cj3)].append((_mesh2, _t3))

        def _edge_surface_y(px3, pz3, y_ref=None):
            # LAYER WINDOW: at a stacked switchback this XZ column holds BOTH legs' surfaces. The old
            # "highest wins" seated lower-leg barriers on the UPPER deck (hovering barriers). With
            # y_ref (the barrier's own station height) pick the surface CLOSEST to it, and never
            # accept one more than 4 m away — that's a different layer, not this barrier's ground.
            best3 = None
            for _mesh2, _t3 in _bc2.get((int(px3 // 3.0), int(pz3 // 3.0)), ()):
                _mv2 = _mesh2["vertices"]
                _a3, _b3, _c3 = _mv2[_t3[0]], _mv2[_t3[1]], _mv2[_t3[2]]
                _d3 = (_b3[2] - _c3[2]) * (_a3[0] - _c3[0]) + (_c3[0] - _b3[0]) * (_a3[2] - _c3[2])
                if abs(_d3) < 1e-12:
                    continue
                # GROUND means WALKABLE: a near-vertical face (drape lip, curb face, skirt)
                # barycentric-answers any height in its span — the closest-to-y_ref pick then
                # believes the ground is wherever the module already floats. Slope > ~60 deg is
                # a wall, not a seat.
                _e13 = (_b3[0] - _a3[0], _b3[1] - _a3[1], _b3[2] - _a3[2])
                _e23 = (_c3[0] - _a3[0], _c3[1] - _a3[1], _c3[2] - _a3[2])
                _ny3 = _e13[2] * _e23[0] - _e13[0] * _e23[2]
                _nx3 = _e13[1] * _e23[2] - _e13[2] * _e23[1]
                _nz3 = _e13[0] * _e23[1] - _e13[1] * _e23[0]
                _nl3 = (_nx3 * _nx3 + _ny3 * _ny3 + _nz3 * _nz3) ** 0.5 or 1.0
                if abs(_ny3) / _nl3 < 0.5:
                    continue
                _w03 = ((_b3[2] - _c3[2]) * (px3 - _c3[0]) + (_c3[0] - _b3[0]) * (pz3 - _c3[2])) / _d3
                _w13 = ((_c3[2] - _a3[2]) * (px3 - _c3[0]) + (_a3[0] - _c3[0]) * (pz3 - _c3[2])) / _d3
                _w23 = 1.0 - _w03 - _w13
                if _w03 >= -1e-6 and _w13 >= -1e-6 and _w23 >= -1e-6:
                    _y3 = _w03 * _a3[1] + _w13 * _b3[1] + _w23 * _c3[1]
                    if y_ref is not None:
                        if abs(_y3 - y_ref) > 4.0:
                            continue
                        if best3 is None or abs(_y3 - y_ref) < abs(best3 - y_ref):
                            best3 = _y3
                    elif best3 is None or _y3 > best3:
                        best3 = _y3
            return best3
        barrier = props_mod.instance_barriers(centerline, widths, barrier_spots, _mod,
                                              surface_y=_edge_surface_y)
        barrier_group = ("1WALL_barrier", "kerb")
        print(f"  concrete barriers: {len(barrier['vertices'])} verts instanced over {len(barrier_spots)} runs")
    # Lift BEFORE the road-guard + proximity trim so they compare in the same frame the audit (and
    # AC) sees. Trimming pre-lift left 137 hairpin-apex verts 1.19->1.09 m under the other leg's
    # edge: outside the trim window, inside the audit's.
    barrier["vertices"] = [(x, y + ROAD_LIFT_M, z) for x, y, z in barrier["vertices"]]
    # FINAL RE-SEAT, triangle-exact: the seat->lift->trim chain accumulates offsets differently per
    # edge profile (Sand Creek's sidewalk profile shipped EVERY barrier hovering +0.59 m median while
    # vertex-based gates read it as seated; one Lariat hairpin sat +4 m). Instead of chasing the
    # arithmetic, drop each 4 m module onto the barycentric surface of the FINAL road+shoulder tris
    # at its own footprint (y_ref-windowed so stacks stay layer-safe). Runs BEFORE trim/prune while
    # module vert-blocks are still contiguous.
    if cfg_raw.get("props", {}).get("concrete_barriers") and barrier["vertices"]:
        _nv_mod = len(_mod["vertices"])
        _bv5 = barrier["vertices"]
        _reseated = 0
        for _s5 in range(0, len(_bv5), _nv_mod):
            _blk = _bv5[_s5:_s5 + _nv_mod]
            _base5 = min(v[1] for v in _blk)
            _cx5 = sum(v[0] for v in _blk) / len(_blk)
            _cz5 = sum(v[2] for v in _blk) / len(_blk)
            # seat by the module's ACTUAL BASE VERTS: drop until the worst overhanging column
            # touches its local surface. Center- and bbox-corner sampling both lost the seesaw at
            # Sand Creek's sidewalk-drape lip (module rests on the lip, outboard base columns
            # overhang 0.6 m of daylight over the grass beyond it).
            _drops5 = []
            _sups5 = []
            _fit5 = []                            # (x, z, required dy) for the plane fit
            for _v5 in _blk:
                if _v5[1] > _base5 + 0.25:
                    continue                     # not a base vert
                _g5v = _edge_surface_y(_v5[0], _v5[2], _v5[1])
                if _g5v is not None:
                    _drops5.append(_g5v - _v5[1])
                    _fit5.append((_v5[0], _v5[2], _g5v - _v5[1]))
                # SUPPORT view (gate semantics): highest walkable at/below the vert
                _g5s = _edge_surface_y(_v5[0], _v5[2], None)
                if _g5s is not None and _g5s <= _v5[1] + 0.6:
                    _sups5.append(_g5s - _v5[1])
            if not _drops5:
                continue
            _dy5 = min(_drops5) + 0.02
            # HOVER REPAIR: if even the best-supported base vert would still be > 0.28 above its
            # support after the primary drop, the whole module hovers (the gate's criterion) —
            # drop it the rest of the way. Second pass, lower-only.
            if _sups5:
                _hover5 = -(max(_sups5) + _dy5)   # best vert's gap AFTER primary drop
                if _hover5 > 0.28:
                    _dy5 -= _hover5 - 0.02
            if abs(_dy5) > 3.5:
                continue
            # PLANE FIT (Kevin: barriers 75% proud / 25% buried): a level module on cambered
            # pavement shows daylight at one end and buries the other. Fit dy ~ a + b*x + c*z over
            # the base verts and TILT the module onto the local surface (clamped to 8% so modules
            # never lean drunkenly). Falls back to the rigid drop when the fit is degenerate.
            _a5 = _b5 = _c5 = None
            if len(_fit5) >= 8:
                _mx5 = sum(f[0] for f in _fit5) / len(_fit5)
                _mz5 = sum(f[1] for f in _fit5) / len(_fit5)
                _sxx = sum((f[0] - _mx5) ** 2 for f in _fit5)
                _szz = sum((f[1] - _mz5) ** 2 for f in _fit5)
                _sxy = sum((f[0] - _mx5) * (f[2] - sum(g[2] for g in _fit5) / len(_fit5)) for f in _fit5)
                _szy = sum((f[1] - _mz5) * (f[2] - sum(g[2] for g in _fit5) / len(_fit5)) for f in _fit5)
                if _sxx > 1e-6 and _szz > 1e-6:
                    _b5 = max(-0.08, min(0.08, _sxy / _sxx))
                    _c5 = max(-0.08, min(0.08, _szy / _szz))
                    _mean_dy5 = sum(f[2] for f in _fit5) / len(_fit5)
                    _a5 = _mean_dy5 + 0.02
            if _a5 is not None:
                for _i5 in range(_s5, _s5 + len(_blk)):
                    _x5, _y5, _z5 = _bv5[_i5]
                    _bv5[_i5] = (_x5, _y5 + _a5 + _b5 * (_x5 - _mx5) + _c5 * (_z5 - _mz5), _z5)
                _reseated += 1
            elif abs(_dy5) > 1e-4:
                for _i5 in range(_s5, _s5 + len(_blk)):
                    _x5, _y5, _z5 = _bv5[_i5]
                    _bv5[_i5] = (_x5, _y5 + _dy5, _z5)
                _reseated += 1
        print(f"  concrete barriers: re-seated {_reseated} modules onto the exact edge surface")
    drop_over_road(barrier, "warning barriers", lo=-1.0, hi=3.5)
    # proximity trim: drop barrier tris pressing within 1.0 m of the MAIN carriageway at deck
    # height (flare tapers pull a handful of modules tighter than any real installation)
    if barrier["tris"]:
        from collections import defaultdict as _pdd
        _mh: dict = _pdd(list)
        for _v4 in road["vertices"]:
            _mh[(int(_v4[0] // 4.0), int(_v4[2] // 4.0))].append(_v4)

        def _too_close(vx4, vy4, vz4):
            for _di4 in (-1, 0, 1):
                for _dj4 in (-1, 0, 1):
                    for _rx4, _ry4, _rz4 in _mh.get((int(vx4 // 4.0) + _di4, int(vz4 // 4.0) + _dj4), ()):
                        # margins slightly WIDER than audit D (1.0 m / 1.2 m): the trim must
                        # strictly superset the gate or boundary-epsilon verts survive to fail it
                        if (vx4 - _rx4) ** 2 + (vz4 - _rz4) ** 2 < 1.21 and abs(vy4 - _ry4) < 1.3:
                            return True
            return False
        _bv4 = barrier["vertices"]
        _keep4 = [t4 for t4 in barrier["tris"] if not any(_too_close(*_bv4[_v5]) for _v5 in t4)]
        if len(_keep4) != len(barrier["tris"]):
            print(f"  warning barriers: proximity-trimmed {len(barrier['tris']) - len(_keep4)} tris (<1 m to carriageway)")
            barrier["tris"] = _keep4
            # PRUNE the orphan verts the trimmed panels leave behind — they still count as wall
            # verts to the audit (D) and to AC's collision, exactly like the old procedural wall
            # pass (which prunes for the same reason). Remap tris onto the compacted vert list.
            _used4 = sorted({_v5 for _t5 in _keep4 for _v5 in _t5})
            _remap4 = {_o: _n for _n, _o in enumerate(_used4)}
            barrier["vertices"] = [_bv4[_o] for _o in _used4]
            barrier["uvs"] = [barrier["uvs"][_o] for _o in _used4]
            barrier["tris"] = [tuple(_remap4[_v5] for _v5 in _t5) for _t5 in _keep4]
    if barrier_spots:
        print(f"  warning barriers: {len(barrier_spots)} sharp/blind corners "
              f"(crest-blind: {sum(1 for s in barrier_spots if s['crest'])})")  # just above road

    # CROSSWALK across the start/finish (continental bars) — sits just above the lane lines.
    xwalk = ribbon.crosswalk(centerline, widths, at_idx=0, bank_at=bnk)
    xwalk["vertices"] = [(x, y + ROAD_LIFT_M + 0.013, z) for x, y, z in xwalk["vertices"]]

    # PAINTED STREET-NAME decals at each turn-onto, from the OSM street-label map (data/street_labels.json).
    # Big white pavement lettering matching the Commerce City reference ("E 45TH AVE" painted on the road).
    roadtext = {"vertices": [], "uvs": [], "tris": []}
    labels_path = data / "street_labels.json"
    rt_cfg = cfg_raw.get("road_text", {})
    # road_text.enabled=false skips decals entirely (mountain roads have no painted street names,
    # and placement is junction-keyed — on a rural loop the labels land in the wrong places). Also
    # keeps this track's build from overwriting the shared roadtext_atlas.png.
    if labels_path.exists() and rt_cfg.get("enabled", True):
        sl = json.loads(labels_path.read_text(encoding="utf-8"))
        mode = rt_cfg.get("mode", "along")        # "along" = lengthwise down the lane (drive over it)
        cap_m = float(rt_cfg.get("cap_m", 4.8))   # letter height across the road (≈half a 9 m lane)
        min_leg = rt_cfg.get("min_leg_m", 200.0)
        prelim = road_text.choose_placements(sl["labels"], sl["segments"], min_leg_m=min_leg)
        tex_dir = Path(__file__).resolve().parents[2] / "assets" / "textures"
        info = road_text.render_text_atlas([nm for _i, nm in prelim], tex_dir / "roadtext_atlas.png",
                                           wrap=(mode != "along"))
        # second pass: snap each label onto the straightest window in its leg (no text on corners)
        placements = road_text.choose_placements(sl["labels"], sl["segments"], min_leg_m=min_leg,
                                                 centerline=centerline, info=info, cap_m=cap_m)
        roadtext = road_text.place_labels(centerline, widths, placements, info, bank_at=bnk,
                                          mode=mode, cap_m=cap_m,
                                          flip_read=bool(rt_cfg.get("flip_read", False)),
                                          flip_up=bool(rt_cfg.get("flip_up", False)))
        roadtext["vertices"] = [(x, y + ROAD_LIFT_M + 0.014, z) for x, y, z in roadtext["vertices"]]
        print(f"  road-text decals ({mode}): {len(placements)} -> {[nm for _i, nm in placements]}")

    # AC track collision is one-sided — every ground/drivable surface must face up or the car
    # falls straight through. The ribbon/terrain/shoulder/runoff/kerb/markings generators wind their
    # quads downward, so re-orient them up. (No barriers — corners run off onto open tarmac.)
    for surface in (road, grass, shoulder, runoff, kerb, marks, yline, xwalk, roadtext):
        if surface["tris"]:
            orient_up(surface)
    for _gn, cm in conn_meshes:
        orient_up(cm)
    # SOLID UNDERSIDE — a SEPARATE VISUAL mesh (no '1' prefix: not collidable, not gated as
    # drivable), reversed winding, dirt-textured: the embankment/cut mass seen from the
    # switchback below. Single-sided, the sheet was backface-culled into a HOLE in the
    # mountainside with the road floating over it ("entire airgaps beneath it and the ground").
    # Built AFTER orient_up, which would otherwise flip it back up.
    shoulder_under = {"vertices": list(shoulder["vertices"]),
                      "uvs": list(shoulder.get("uvs", [])),
                      "tris": [(a, c, b) for a, b, c in shoulder["tris"]]}

    dummies = dummies_mod.place_dummies(centerline, widths, n_sectors=3)

    # Persist the FINISHED centerline (corner-rounded, mirrored — the exact line the ribbon was
    # swept along) for the drive test: near sharp junctions it deviates metres from the raw local
    # points, and a test driving the raw line reads phantom obstructions beside the real pavement.
    (data / "finished_centerline.json").write_text(json.dumps({
        "frame": "mesh (mirrored) local metres — same frame as track.obj",
        "points_xyz_m": [[round(x, 2), round(y, 2), round(z, 2)] for x, y, z in centerline],
        "widths_m": [round(w, 2) for w in widths],
    }), encoding="utf-8")

    # 1GRASS (not GRASS): the leading digit makes the terrain a PHYSICAL surface keyed to
    # surfaces.ini GRASS, so you don't fall through the world off the racing line either.
    # (Orientation confirmed in-game — the temporary CALIB calibration poles are removed.)
    groups = [*split_mesh_under_cap("1GRASS", "grass", grass),
              *split_mesh_under_cap("1GRASS_ring", "grass", orient_up(ring_mesh)),
              *split_mesh_under_cap(edge_group, edge_mat, shoulder),
               *split_mesh_under_cap("SHOULDERUND", edge_mat, shoulder_under),
              ("1RUNOFF_corners", "road", runoff),
              *split_mesh_under_cap("1ROAD_main", "road", road),
              *split_mesh_under_cap("1KERB_corners", "kerb", kerb),
              ("MARKINGS", "kerb", marks),
              ("MARKINGS_crosswalk", "kerb", xwalk)]
    if fence_wood["tris"]:
        groups.extend(split_mesh_under_cap("1WALL_WOODF_warn", "kerb", fence_wood))
    if para_mesh["tris"]:
        groups.extend(split_mesh_under_cap("1WALL_PARA_stone", "kerb", para_mesh))
    if wallskin["tris"]:
        groups.extend(split_mesh_under_cap("RETWALL_face", "kerb", wallskin))
    if rockskin["tris"]:
        groups.extend(split_mesh_under_cap("ROCKCUT_face", "kerb", rockskin))
    if roadtext["tris"]:
        groups.append(("ROADTEXT", "kerb", roadtext))
    if barrier["tris"]:
        groups.extend(split_mesh_under_cap(barrier_group[0], barrier_group[1], barrier))  # guardrails at sharp/blind corners
    if yline["tris"]:
        groups.append(("YLINE", "kerb", yline))
    for gname, cm in conn_meshes:                       # interior-grid roads (2nd layout) on the shared mesh
        groups.append((gname, "road", cm))
    nv, nf = write_obj(data / "track.obj", "track.mtl", groups)
    write_mtl(data / "track.mtl")
    (data / "dummies.json").write_text(json.dumps(dummies, indent=1), encoding="utf-8")

    # road surface area (sum of triangle areas in the horizontal plane)
    rv = road["vertices"]
    area = 0.0
    for a, b, c in road["tris"]:
        (ax, _, az), (bx, _, bz), (cx, _, cz) = rv[a], rv[b], rv[c]
        area += abs((bx - ax) * (cz - az) - (cx - ax) * (bz - az)) / 2
    render_layers = [("grass", (0.30, 0.47, 0.22), grass), ("shoulder", (0.40, 0.40, 0.43), shoulder),
                     ("runoff", (0.46, 0.46, 0.49), runoff), ("road", (0.82, 0.83, 0.86), road),
                     ("kerb", (0.88, 0.24, 0.20), kerb), ("marks", (0.95, 0.95, 0.92), marks),
                     ("xwalk", (1.0, 1.0, 1.0), xwalk)]
    if roadtext["tris"]:
        render_layers.append(("roadtext", (0.40, 0.85, 1.0), roadtext))
    if barrier["tris"]:
        render_layers.append(("barrier", (0.92, 0.78, 0.20), barrier))   # warning guardrails in yellow
    for gname, cm in conn_meshes:
        render_layers.append((gname, (0.80, 0.55, 0.30), cm))   # interior-grid roads in amber
    render_iso(render_layers, dummies, data / "track_render.svg")

    return {"vertices": nv, "triangles": nf, "road_verts": len(rv), "kerb_verts": len(kerb["vertices"]),
            "shoulder_verts": len(shoulder["vertices"]), "runoff_verts": len(runoff["vertices"]),
            "grass_grid": f"{meta['nx']}x{meta['ny']}", "road_area_m2": round(area), "dummies": len(dummies)}


# --- pure-python 3/4-view renderer (orthographic, flat-shaded, painter's algorithm) ----

def render_iso(layers: list[tuple[str, tuple, dict]], dummies: dict, path: Path, *,
               az: float = -0.72, pitch: float = 0.60, yexag: float = 6.0, size: int = 1200,
               light: tuple = (-0.45, 0.82, -0.35)) -> None:
    ca, sa, cp, sp = math.cos(az), math.sin(az), math.cos(pitch), math.sin(pitch)
    ll = math.sqrt(sum(c * c for c in light))
    lx, ly, lz = (c / ll for c in light)

    def tf(v: Vertex) -> tuple[float, float, float]:
        x, y, z = v[0], v[1] * yexag, v[2]
        x1, z1 = x * ca + z * sa, -x * sa + z * ca
        return x1, y * cp - z1 * sp, y * sp + z1 * cp  # screenX, screenY(up), depth

    layer_faces = []  # per layer, in draw order: list of (depth, (p0,p1,p2), rgb)
    for _, base, mesh in layers:
        V = mesh["vertices"]
        fl = []
        for a, b, c in mesh["tris"]:
            v0, v1, v2 = V[a], V[b], V[c]
            ux, uy, uz = v1[0] - v0[0], (v1[1] - v0[1]) * yexag, v1[2] - v0[2]
            wx, wy, wz = v2[0] - v0[0], (v2[1] - v0[1]) * yexag, v2[2] - v0[2]
            nx, ny, nz = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx
            nl = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            inten = 0.30 + 0.70 * max(0.0, abs((nx * lx + ny * ly + nz * lz) / nl))
            t0, t1, t2 = tf(v0), tf(v1), tf(v2)
            rgb = tuple(min(255, int(ch * inten * 255)) for ch in base)
            fl.append(((t0[2] + t1[2] + t2[2]) / 3, (t0, t1, t2), rgb))
        fl.sort(key=lambda f: f[0])  # far → near within the layer
        layer_faces.append(fl)

    allx = [p[0] for fl in layer_faces for _, tri, _ in fl for p in tri]
    ally = [p[1] for fl in layer_faces for _, tri, _ in fl for p in tri]
    minx, maxx, miny, maxy = min(allx), max(allx), min(ally), max(ally)
    pad = 24
    scale = (size - 2 * pad) / max(maxx - minx, maxy - miny)
    W, H = (maxx - minx) * scale + 2 * pad, (maxy - miny) * scale + 2 * pad

    def sc(p) -> tuple[float, float]:
        return round(pad + (p[0] - minx) * scale, 1), round(H - pad - (p[1] - miny) * scale, 1)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" '
           f'viewBox="0 0 {W:.0f} {H:.0f}"><rect width="100%" height="100%" fill="#0c1116"/>']
    for fl in layer_faces:  # grass first, then road painted on top so the ribbon always reads
        for _, tri, rgb in fl:
            pts = " ".join(f"{x},{y}" for x, y in map(sc, tri))
            out.append(f'<polygon points="{pts}" fill="rgb{rgb}"/>')
    # start/finish gate (AC_TIME_0_L → _R) drawn on top
    if "AC_TIME_0_L" in dummies and "AC_TIME_0_R" in dummies:
        x1, y1 = sc(tf(tuple(dummies["AC_TIME_0_L"])))
        x2, y2 = sc(tf(tuple(dummies["AC_TIME_0_R"])))
        out.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#ff3b30" stroke-width="6"/>')
    out.append(f'<text x="{pad}" y="24" fill="#cdd6e0" font-size="14" font-family="monospace">'
               f'Sand Creek Raceway — 1ROAD + GRASS (elevation ×{yexag:g})</text>')
    out.append("</svg>")
    path.write_text("".join(out), encoding="utf-8")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m scripts.geometry.build_mesh <project-dir>")
    stats = build(sys.argv[1])
    print("wrote data/track.obj (+track.mtl), dummies.json, track_render.svg")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
