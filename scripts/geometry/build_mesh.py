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
ROAD_LIFT_M = 0.1  # small crown above the graded terrain (a real road sits ~0.1 m proud, not floating)
GRASS_CLEARANCE_M = 0.25 # sink near-road grass this far below the road so the coarse grid can't bulge
#                          a triangle up through the road on steep bits (corkscrew); shoulder meets it.


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
    up = 8
    while up > 3 and ((_nx0 - 1) * up + 1) * ((_ny0 - 1) * up + 1) > 420_000:
        up -= 1
    grid, meta = ribbon.upsample_grid(grid, meta, up)
    print(f"[build_mesh] grass upsample x{up} -> {meta['nx']}x{meta['ny']} = {meta['nx']*meta['ny']} verts (< 65535)")
    grid_xyz = project_grid(grid, meta, origin, elev0, mirror_x=mirror_x)
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
    ribbon.grade_embankment(grid_xyz, centerline, widths, bank_at=bnk,
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
                                    clear=GRASS_CLEARANCE_M, grid_spacing=float(meta["spacing_m"]))
    # The finalized grass SURFACE (bilinear on the graded+clamped grid) — the single source of truth the
    # edge strips DRAPE onto so their outer lip sits flush on the ground at road resolution (no float,
    # independent of the coarse grass grid). Same sampler build_env + audit use via ground.local.json.
    def grass_surf(gx, gz):
        return _grid_bilinear(grid_xyz, gx, gz)
    # ROAD EDGE — config-gated. Default: a paved asphalt SHOULDER that ramps/embanks the lane edge down (or
    # up) to the REAL ground. `road_edge.profile == "sidewalk"` instead sweeps a continuous
    # curb→sidewalk→verge→embankment profile (urban streets, e.g. Sand Creek). Both DRAPE their outer edge
    # onto grass_surf, so nothing hovers over the terrain even on a 10 m embankment.
    edge_cfg = cfg_raw.get("road_edge", {})
    if edge_cfg.get("profile") == "sidewalk":
        shoulder = ribbon.curb_sidewalk(centerline, widths, lift=ROAD_LIFT_M,
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
    ribbon.clamp_terrain_below_road(grid_xyz, shoulder["vertices"] + kerb["vertices"], clear=0.05, reach=6.0,
                                    grid_spacing=float(meta["spacing_m"]))
    # Persist the graded ground surface for build_env (scenery height) + audit_mesh check G. Reflects
    # grid_xyz exactly (post both anti-poke passes) — the surface the grass mesh triangulates.
    write_ground_local(data / "ground.local.json", grid_xyz)
    grass = ribbon.grass_terrain(grid_xyz)
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
    _sv2 = shoulder["vertices"]
    _sunk = 0
    for _vi in range(len(_sv2)):
        _vx, _vy, _vz = _sv2[_vi]
        _tops = [_t for _t in _road_tops(_vx, _vz) if abs(_t - _vy) <= OWN_DECK_DY]
        if _tops and _vy > min(_tops) + 0.15:
            _sv2[_vi] = (_vx, min(_tops) - 0.4, _vz)
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
        _local_tops = [min(_tops2) for _sx, _sy, _sz in _samples
                       for _tops2 in [[_t for _t in _road_tops(_sx, _sz) if abs(_t - _tri_y) <= OWN_DECK_DY]]
                       if _tops2]
        if _local_tops and any(_sy > min(_local_tops) + 0.15 for _sx, _sy, _sz in _samples):
            _floor = min(_local_tops) - 0.4
            for _v in _t2:
                _vx, _vy, _vz = _sv2[_v]
                if _vy > _floor:
                    _sv2[_v] = (_vx, _floor, _vz)
                    _sunk += 1
    if _sunk:
        print(f"  shoulder: sank {_sunk} verts under the deck (watertight — no drape holes)")

    barrier, barrier_spots = kerbs.warning_barriers(centerline, widths)
    barrier_group = ("BARRIER_warning", "kerb")
    if cfg_raw.get("props", {}).get("concrete_barriers"):
        # Kevin's 4 m concrete barrier modules replace the procedural swept jersey — real geometry,
        # instanced along the SAME warning-barrier runs, shipped physical as 1WALL_* (collidable).
        from scripts.environment import props as props_mod
        _mod = props_mod.load_module(Path(cfg_raw["props"].get(
            "barrier_obj", str(Path(__file__).resolve().parents[2] / "assets" / "models" / "concrete_barrier_4m.obj"))))
        # edge-surface sampler: road + shoulder tri tops, so barrier bases meet the pavement bench
        from collections import defaultdict as _bdd
        _bc2: dict = _bdd(list)
        for _mesh2 in (road, shoulder):
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
              *split_mesh_under_cap(edge_group, edge_mat, shoulder),
              ("1RUNOFF_corners", "road", runoff),
              *split_mesh_under_cap("1ROAD_main", "road", road),
              *split_mesh_under_cap("1KERB_corners", "kerb", kerb),
              ("MARKINGS", "kerb", marks),
              ("MARKINGS_crosswalk", "kerb", xwalk)]
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
