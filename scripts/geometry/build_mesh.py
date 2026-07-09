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
    raw_pts = evidence.cap_grade(raw_pts, max_grade=0.06)
    widths = local["widths_m"]
    origin = (local["origin"]["lon"], local["origin"]["lat"])
    elev0 = local["origin"]["elev_m"]

    # MIRROR_X: AC's kn5 convert renders the track reflected east<->west (a real right-hander reads as a
    # left, north preserved). Cancel it by mirroring the SOURCE X (east) here — so the road, the dummies
    # (placed below) and the facing (build_kn5) are all COMPUTED in the mirrored frame, never reflected
    # as matrices (that's what broke the spawn before). orient_up re-faces the drivable surfaces.
    cfg_raw = json.loads((project_dir / "track.config.json").read_text())
    mirror_x = bool(cfg_raw.get("mirror_x", False))

    # ROAD PROFILE: corner-round, mirror, and bake the hand-authored dip (the Sand Creek corkscrew) into
    # the centerline Y. Shared with build_env so the scenery anchors to the SAME road. dip_of/bank_at are
    # keyed by lap station (metres from pts[0]); bank rolls every swept cross-section, the dip is baked
    # into the centerline Y so the terrain grade, the grass conform and the dummies all follow it.
    centerline, dip_of, bank_at, profile_active = finished_centerline(raw_pts, cfg_raw, mirror_x=mirror_x)
    bnk = bank_at if profile_active else None     # None => flat path is byte-identical to before

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
    # Pick the FINEST upsample (<=3) that keeps 1GRASS under AC's 65,535 per-mesh vertex cap. Over the
    # cap, the exporter auto-splits the grass into TWO meshes with the SAME name; AC keys physical meshes
    # by name, so one half is dropped from collision and the car falls THROUGH the grass. Small loops
    # (Sand Creek) still get x3; big loops (Lake Murray) drop to x2/x1 so the grass is a single,
    # uniquely-named, fully-collidable mesh.
    _ny0, _nx0 = len(grid), len(grid[0])
    up = 3
    while up > 1 and ((_nx0 - 1) * up + 1) * ((_ny0 - 1) * up + 1) > 62000:
        up -= 1
    grid, meta = ribbon.upsample_grid(grid, meta, up)
    print(f"[build_mesh] grass upsample x{up} -> {meta['nx']}x{meta['ny']} = {meta['nx']*meta['ny']} verts (< 65535)")
    grid_xyz = project_grid(grid, meta, origin, elev0, mirror_x=mirror_x)
    # Sink the terrain into a wide smooth BOWL around any dip (the corkscrew) so it reads as a valley,
    # not a road in a walled trench with the surrounding grass towering overhead.
    if profile_active:
        profile_mod.apply_dip_bowl(grid_xyz, centerline, dip_of)
    # corridor 16 m (> the ~13 m grid's 9.4 m half-diagonal) so EVERY near-road node is graded below
    # the road — kills the grass poke-throughs the old 12 m corridor missed, without over-refining the
    # grid (×3 keeps 1GRASS ~48k verts, under AC's per-mesh 16-bit index limit).
    ribbon.conform_terrain_to_road(grid_xyz, centerline, widths, corridor=16.0, bank_at=bnk,
                                   extra_roads=[(p, w) for _n, p, w in connectors],
                                   clearance=GRASS_CLEARANCE_M)

    # tile_m=8 m — the LA Canyons cracked-tarmac detail reads at its real scale (cracks crisp, not
    # stretched). The cracking is irregular enough to hide the repeat.
    road = ribbon.road_ribbon(centerline, widths, tile_m=8.0, bank_at=bnk)
    road["vertices"] = [(x, y + ROAD_LIFT_M, z) for x, y, z in road["vertices"]]
    # ROAD EDGE — config-gated. Default: a paved asphalt SHOULDER that ramps the lane edge down to the
    # terrain (kills the floating-ribbon hard edge). `road_edge.profile == "sidewalk"` instead sweeps a
    # continuous curb→sidewalk→grass profile that shares seam vertices with the road and the grass (urban
    # streets, e.g. Sand Creek). Both are physical so there is no gap to drop into; both bake the lift so
    # the inner lip meets the road edge exactly. Other tracks (no road_edge) build byte-identically.
    edge_cfg = cfg_raw.get("road_edge", {})
    if edge_cfg.get("profile") == "sidewalk":
        shoulder = ribbon.curb_sidewalk(centerline, widths, lift=ROAD_LIFT_M,
                                        grass_clearance=GRASS_CLEARANCE_M, bank_at=bnk,
                                        curb_h=float(edge_cfg.get("curb_h", 0.15)),
                                        curb_face_w=float(edge_cfg.get("curb_face_w", 0.08)),
                                        sidewalk_w=float(edge_cfg.get("sidewalk_w", 1.5)),
                                        grade_w=float(edge_cfg.get("grade_w", 1.0)))
        edge_group, edge_mat = "1KERB_sidewalk", "kerb"
    else:
        shoulder = ribbon.road_shoulder(centerline, widths, lift=ROAD_LIFT_M, bank_at=bnk,
                                        ground_drop=GRASS_CLEARANCE_M)
        edge_group, edge_mat = "1ROAD_shoulder", "road"
    # Wide tarmac RUNOFF apron on the outside of corners (replaces grass run-off / the old walls).
    runoff = kerbs.corner_runoff(centerline, widths, bank_at=bnk)
    runoff["vertices"] = [(x, y + 0.05, z) for x, y, z in runoff["vertices"]]  # clear the grass, below road
    # ANTI-POKE (mesh-audit check B): clamp the terrain grid below the FULL drivable edge (road + shoulder +
    # corner runoff), THEN triangulate the grass. One-sided (only pushes pokes down; natural dips survive)
    # and measured against the ACTUAL banked verts, so banked-turn inner edges and shoulder lips can't poke
    # up through the ribbon. Must run after shoulder+runoff exist and before grass_terrain.
    ribbon.clamp_terrain_below_road(grid_xyz, road["vertices"] + shoulder["vertices"] + runoff["vertices"],
                                    clear=GRASS_CLEARANCE_M)
    # Persist the conformed ground surface for build_env (scenery height) + audit_mesh check G. Must be
    # AFTER conform+clamp and reflect grid_xyz exactly — it is the surface the grass mesh triangulates.
    write_ground_local(data / "ground.local.json", grid_xyz)
    grass = ribbon.grass_terrain(grid_xyz)
    # Kerb geometry is config-driven so a track can opt into taller RACING kerbs without touching the
    # default 5 cm street kerb. `kerb.height_m` / `kerb.width_m` / `kerb.top_frac` in track.config.json.
    kerb_cfg = cfg_raw.get("kerb", {})
    kerb = kerbs.corner_kerbs(centerline, widths, bank_at=bnk,
                              kerb_h=float(kerb_cfg.get("height_m", 0.05)),
                              kerb_w=float(kerb_cfg.get("width_m", 1.0)),
                              top_frac=float(kerb_cfg.get("top_frac", 0.55)),
                              edge_ramp=float(kerb_cfg.get("edge_ramp", 0.0)))
    if kerb_cfg:
        print(f"  racing kerbs: h={kerb_cfg.get('height_m', 0.05)}m w={kerb_cfg.get('width_m', 1.0)}m "
              f"edge_ramp={kerb_cfg.get('edge_ramp', 0.0)}")
    kerb["vertices"] = [(x, y + ROAD_LIFT_M, z) for x, y, z in kerb["vertices"]]  # kerb lip at road-edge height
    marks = ribbon.road_markings(centerline, widths, bank_at=bnk)
    marks["vertices"] = [(x, y + ROAD_LIFT_M + 0.012, z) for x, y, z in marks["vertices"]]

    # WARNING BARRIERS — guardrails on the OUTSIDE of sharp turns (and any corner hiding over a crest),
    # so you read "hard corner ahead" + get caught if you run wide. Vertical double-sided walls: NOT
    # oriented up (they're not ground), they collide as solid geometry.
    barrier, barrier_spots = kerbs.warning_barriers(centerline, widths)
    barrier["vertices"] = [(x, y + ROAD_LIFT_M, z) for x, y, z in barrier["vertices"]]
    if barrier_spots:
        print(f"  warning barriers: {len(barrier_spots)} sharp/blind corners "
              f"(crest-blind: {sum(1 for s in barrier_spots if s['crest'])})")  # just above road

    # Ribbon the interior connector roads — flat drivable 1ROAD asphalt (no bank/dip), so the interior
    # layout is drivable on the same kn5. Named 1ROAD_* -> asphalt material + physical (export DRIVABLE).
    conn_meshes = []
    for name, pts, w in connectors:
        cm = ribbon.road_ribbon(pts, w, tile_m=8.0)
        cm["vertices"] = [(x, y + ROAD_LIFT_M, z) for x, y, z in cm["vertices"]]
        conn_meshes.append((f"1ROAD_{name}", cm))

    # CROSSWALK across the start/finish (continental bars) — sits just above the lane lines.
    xwalk = ribbon.crosswalk(centerline, widths, at_idx=0, bank_at=bnk)
    xwalk["vertices"] = [(x, y + ROAD_LIFT_M + 0.013, z) for x, y, z in xwalk["vertices"]]

    # PAINTED STREET-NAME decals at each turn-onto, from the OSM street-label map (data/street_labels.json).
    # Big white pavement lettering matching the Commerce City reference ("E 45TH AVE" painted on the road).
    roadtext = {"vertices": [], "uvs": [], "tris": []}
    labels_path = data / "street_labels.json"
    if labels_path.exists():
        sl = json.loads(labels_path.read_text(encoding="utf-8"))
        rt_cfg = cfg_raw.get("road_text", {})
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
    for surface in (road, grass, shoulder, runoff, kerb, marks, xwalk, roadtext):
        if surface["tris"]:
            orient_up(surface)
    for _gn, cm in conn_meshes:
        orient_up(cm)

    dummies = dummies_mod.place_dummies(centerline, widths, n_sectors=3)

    # 1GRASS (not GRASS): the leading digit makes the terrain a PHYSICAL surface keyed to
    # surfaces.ini GRASS, so you don't fall through the world off the racing line either.
    # (Orientation confirmed in-game — the temporary CALIB calibration poles are removed.)
    groups = [("1GRASS", "grass", grass), (edge_group, edge_mat, shoulder),
              ("1RUNOFF_corners", "road", runoff), ("1ROAD_main", "road", road),
              ("1KERB_corners", "kerb", kerb), ("MARKINGS", "kerb", marks),
              ("MARKINGS_crosswalk", "kerb", xwalk)]
    if roadtext["tris"]:
        groups.append(("ROADTEXT", "kerb", roadtext))
    if barrier["tris"]:
        groups.append(("BARRIER_warning", "kerb", barrier))   # guardrails at sharp/blind corners
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
