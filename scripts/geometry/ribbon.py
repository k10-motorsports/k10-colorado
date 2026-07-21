"""Build the road ribbon (1ROAD) and the grass heightfield mesh (GRASS).

The ribbon is swept along the projected centerline using per-vertex width; the grass mesh is the
triangulated terrain grid. All inputs are local ENU metres (X-east, Y-up, Z-north). Meshes are
returned as ``{"vertices": [(x,y,z)...], "tris": [(a,b,c)...]}`` with 0-based triangle indices.
"""

from __future__ import annotations

import math

Vertex = tuple[float, float, float]


def _horiz_tangent(pts: list[Vertex], i: int, closed: bool) -> tuple[float, float]:
    """Unit tangent in the horizontal X-Z plane at vertex i (centered difference)."""
    n = len(pts)
    a = pts[(i - 1) % n] if closed else pts[max(0, i - 1)]
    b = pts[(i + 1) % n] if closed else pts[min(n - 1, i + 1)]
    dx, dz = b[0] - a[0], b[2] - a[2]
    L = math.hypot(dx, dz) or 1.0
    return dx / L, dz / L


def road_ribbon(centerline_m: list[Vertex], widths_m: list[float], *, tile_m: float = 4.0,
                bank_at=None) -> dict:
    """Sweep a drivable ribbon (1ROAD) along the centerline, offsetting ±width/2 per vertex.

    Also emits per-vertex UVs (parallel to vertices) in metres/``tile_m``: U across the width
    (±half/tile), V along the lap (cumulative arc/tile) — so a seamless asphalt tiles correctly down
    the road and a detail/markings layer can key off the same coordinates.

    ``bank_at`` (station_m -> roll radians) rolls the cross-section: a vertex at lateral offset ``o``
    (left +ve) is lifted by ``o*tan(bank)`` so the road banks as one plane (see ``profile.py``)."""
    pts = centerline_m
    n = len(pts)
    closed = abs(pts[0][0] - pts[-1][0]) < 1e-6 and abs(pts[0][2] - pts[-1][2]) < 1e-6
    m = n - 1 if closed else n  # unique cross-sections (drop the duplicate closing vertex)
    verts: list[Vertex] = []
    uvs: list[tuple[float, float]] = []
    arc = 0.0
    for i in range(m):
        if i > 0:
            arc += math.hypot(pts[i][0] - pts[i - 1][0], pts[i][2] - pts[i - 1][2])
        x, y, z = pts[i]
        tx, tz = _horiz_tangent(pts, i, closed)
        nx, nz = -tz, tx  # left normal (tangent rotated +90° in X-Z)
        half = widths_m[i] / 2.0
        tb = math.tan(bank_at(arc)) if bank_at else 0.0
        verts.append((x + nx * half, y + half * tb, z + nz * half))  # left  -> 2i  (outside/raised)
        verts.append((x - nx * half, y - half * tb, z - nz * half))  # right -> 2i+1
        uvs.append((half / tile_m, arc / tile_m))        # left
        uvs.append((-half / tile_m, arc / tile_m))       # right
    tris: list[tuple[int, int, int]] = []
    last = m if closed else m - 1
    for i in range(last):
        j = (i + 1) % m
        l0, r0, l1, r1 = 2 * i, 2 * i + 1, 2 * j, 2 * j + 1
        tris.append((l0, r0, r1))
        tris.append((l0, r1, l1))
    return {"vertices": verts, "uvs": uvs, "tris": tris}


def road_shoulder(centerline_m: list[Vertex], widths_m: list[float], *, lift: float = 0.1,
                  verge_w: float = 2.5, tile_m: float = 4.0, bank_at=None, ground_drop: float = 0.0,
                  ground=None, ratio: float = 2.0, max_w: float = 60.0) -> dict:
    """A graded **verge + embankment** that ramps each road edge down (fill) or up (cut) to the REAL
    ground, so the road reads like a real shouldered/embanked road instead of a ribbon floating on a
    hard edge. Three points out from the lane edge: inner lip (road edge, ``lift`` proud) → a short
    ``verge_w`` band just below it (the gentle drivable band) → a GROUND-MEET point DRAPED onto
    ``ground(x,z)`` at a ``ratio``:1 slope, so the outer edge sits flush on the terrain however far
    below/above the road it is (no floating ribbon edge, no cliff). Without ``ground`` it falls back to
    the old two-point verge at ``road_edge − ground_drop``. Physical (``1GRASS_*`` in build_mesh)."""
    pts = centerline_m
    n = len(pts)
    closed = abs(pts[0][0] - pts[-1][0]) < 1e-6 and abs(pts[0][2] - pts[-1][2]) < 1e-6
    m = n - 1 if closed else n
    verts: list[Vertex] = []
    uvs: list[tuple[float, float]] = []
    tris: list[tuple[int, int, int]] = []
    P = 3 if ground is not None else 2
    for side in (1.0, -1.0):
        rows: list[int] = []
        arc = 0.0
        for i in range(m):
            if i > 0:
                arc += math.hypot(pts[i][0] - pts[i - 1][0], pts[i][2] - pts[i - 1][2])
            x, y, z = pts[i]
            tx, tz = _horiz_tangent(pts, i, closed)
            nx, nz = -tz * side, tx * side  # outward normal on this side
            half = widths_m[i] / 2.0
            tb = math.tan(bank_at(arc)) if bank_at else 0.0
            r = len(verts)
            inner_y = y + lift + side * half * tb
            verts.append((x + nx * half, inner_y, z + nz * half))                 # inner lip (road edge)
            uvs.append((0.0, arc / tile_m))
            if ground is not None:
                verge_y = y + side * (half + verge_w) * tb - ground_drop          # gentle band just off the edge
                verts.append((x + nx * (half + verge_w), verge_y, z + nz * (half + verge_w)))
                uvs.append((0.5, arc / tile_m))
                gy0 = ground(x + nx * (half + verge_w), z + nz * (half + verge_w))
                extra = min(max_w, max(verge_w, abs(verge_y - gy0) * ratio))
                o = half + verge_w + extra
                verts.append((x + nx * o, ground(x + nx * o, z + nz * o), z + nz * o))   # ground meet (draped)
                uvs.append((1.0, arc / tile_m))
            else:
                verts.append((x + nx * (half + verge_w), y + side * (half + verge_w) * tb - ground_drop, z + nz * (half + verge_w)))
                uvs.append((1.0, arc / tile_m))
            rows.append(r)
        last = m if closed else m - 1
        for k in range(last):
            p = rows[k]
            q = rows[(k + 1) % m]
            for u in range(P - 1):
                tris.append((p + u, p + u + 1, q + u + 1))
                tris.append((p + u, q + u + 1, q + u))
    return {"vertices": verts, "uvs": uvs, "tris": tris}


def curb_sidewalk(centerline_m: list[Vertex], widths_m: list[float], *, lift: float = 0.1,
                  curb_h: float = 0.15, curb_face_w: float = 0.08, sidewalk_w: float = 1.5,
                  grade_w: float = 1.0, grass_clearance: float = 0.25, tile_m: float = 2.0,
                  bank_at=None, ground=None, ratio: float = 2.0, max_grade_w: float = 60.0) -> dict:
    """A continuous urban edge swept along BOTH road sides as ONE strip so road, curb, sidewalk and
    grass share seam vertices and nothing can hover. Per side, the cross-section is five profile points
    out from the lane edge:
      0  lane edge    — identical to the road-ribbon edge (road height, ``lift`` proud) → road↔curb meets
      1  curb top     — raised ``curb_h`` over a slight ``curb_face_w`` batter (mountable, not a wall)
      2  sidewalk back— flat walkway ``sidewalk_w`` wide at curb-top height
      3  slope start  — a short ``grade_w`` verge dropping just below the sidewalk (the gentle band)
      4  GROUND MEET  — an EMBANKMENT point DRAPED onto the actual grass surface: it marches outward at a
                        ``ratio``:1 fill/cut slope until it reaches ``ground(x,z)`` (the graded terrain),
                        so the edge sits flush on the ground however far below the road it is — no float,
                        no shadow-casting gap, at road resolution (independent of the coarse grass grid).
    Without ``ground`` it falls back to the old fixed ``road_edge − grass_clearance`` lip. Physical
    (``1KERB_sidewalk``). UVs: U across the profile, V along the road (m/``tile_m``)."""
    pts = centerline_m
    n = len(pts)
    closed = abs(pts[0][0] - pts[-1][0]) < 1e-6 and abs(pts[0][2] - pts[-1][2]) < 1e-6
    m = n - 1 if closed else n
    fixed = [(0.0, lift), (curb_face_w, lift + curb_h),
             (curb_face_w + sidewalk_w, lift + curb_h),
             (curb_face_w + sidewalk_w + grade_w, -grass_clearance)]   # points 0..3 (relative to banked edge)
    sidewalk_off = curb_face_w + sidewalk_w + grade_w
    P = 5
    verts: list[Vertex] = []
    uvs: list[tuple[float, float]] = []
    tris: list[tuple[int, int, int]] = []
    for side in (1.0, -1.0):
        rows: list[int] = []
        arc = 0.0
        for i in range(m):
            if i > 0:
                arc += math.hypot(pts[i][0] - pts[i - 1][0], pts[i][2] - pts[i - 1][2])
            x, y, z = pts[i]
            tx, tz = _horiz_tangent(pts, i, closed)
            nx, nz = -tz * side, tx * side  # outward normal on this side
            half = widths_m[i] / 2.0
            edge_bank = side * half * (math.tan(bank_at(arc)) if bank_at else 0.0)
            base_y = y + edge_bank
            r = len(verts)
            for off, h in fixed:
                o = half + off
                verts.append((x + nx * o, base_y + h, z + nz * o))
                uvs.append((off / sidewalk_off, arc / tile_m))
            # point 4: drape onto the ground at a fill/cut slope
            verge_y = base_y - grass_clearance             # height at the slope-start point (3)
            if ground is not None:
                gy0 = ground(x + nx * (half + sidewalk_off + grade_w), z + nz * (half + sidewalk_off + grade_w))
                extra = max(grade_w, abs(verge_y - gy0) * ratio)
                extra = min(extra, max_grade_w)
                o4 = half + sidewalk_off + extra
                g4 = ground(x + nx * o4, z + nz * o4)
                verts.append((x + nx * o4, g4, z + nz * o4))
                uvs.append(((sidewalk_off + extra) / sidewalk_off, arc / tile_m))
            else:
                o4 = half + sidewalk_off + grade_w
                verts.append((x + nx * o4, verge_y, z + nz * o4))
                uvs.append(((sidewalk_off + grade_w) / sidewalk_off, arc / tile_m))
            rows.append(r)
        last = m if closed else m - 1
        for k in range(last):
            a = rows[k]
            b = rows[(k + 1) % m]
            for p in range(P - 1):                      # curb face, sidewalk top, verge, embankment
                tris.append((a + p, a + p + 1, b + p + 1))
                tris.append((a + p, b + p + 1, b + p))
    return {"vertices": verts, "uvs": uvs, "tris": tris}


def _road_surface_samples(centerline_m: list[Vertex], widths_m: list[float] | None, bank_at=None):
    """Centre + both edge points per station, each tagged with its lap index. Grading to these — not
    just the centreline — makes the terrain meet the road at its real edges; with ``bank_at`` the edge
    samples carry the banked height (left = +o*tan, right = -o*tan) so the grass meets a cambered edge."""
    n = len(centerline_m)
    closed = abs(centerline_m[0][0] - centerline_m[-1][0]) < 1e-6 and abs(centerline_m[0][2] - centerline_m[-1][2]) < 1e-6
    out: list[tuple[float, float, float, int]] = []
    arc = 0.0
    for i, (x, y, z) in enumerate(centerline_m):
        if i > 0:
            arc += math.hypot(x - centerline_m[i - 1][0], z - centerline_m[i - 1][2])
        out.append((x, y, z, i))
        if widths_m:
            tx, tz = _horiz_tangent(centerline_m, i, closed)
            nx, nz = -tz, tx
            h = widths_m[i] / 2.0
            tb = math.tan(bank_at(arc)) if bank_at else 0.0
            out.append((x + nx * h, y + h * tb, z + nz * h, i))   # left edge (raised when bank>0)
            out.append((x - nx * h, y - h * tb, z - nz * h, i))   # right edge
    return out, n


def conform_terrain_to_road(grid_xyz: list[list[Vertex]], centerline_m: list[Vertex],
                            widths_m: list[float] | None = None,
                            *, corridor: float = 12.0, blend: float = 12.0, bank_at=None,
                            target: str = "nearest", extra_roads=None, clearance: float = 0.0) -> None:
    """Grade the terrain so it MEETS the road at the edge (no cliff to fall off, no bulge to poke up).
    Each grid node within ``corridor`` m of the road surface (centre + both edges) is pulled to a road
    elevation; the cut then blends back to natural terrain over ``blend`` m. Mutates in place.

    ``target="nearest"`` (default) grades to the NEAREST road sample, so the grass sits right at the
    local road height and you can run off road->grass smoothly. ``target="min"`` grades to the lowest
    road sample within the corridor — only correct for a grade-SEPARATED route (one part passing under
    another), and on a normal single-loop street it carves the grass metres below the local road,
    leaving the shoulder floating on a cliff the car falls off (the "drive off the grass and collapse"
    bug). The Sand Creek loop is a simple closed loop (no self-crossing), so nearest is right.

    For the meeting to hold at every road point, ``corridor`` must exceed the terrain grid's
    half-diagonal — so the grid must be FINE (see ``upsample_grid``)."""
    from collections import defaultdict
    reach = corridor + blend
    corr2 = corridor * corridor
    samples, _ = _road_surface_samples(centerline_m, widths_m, bank_at)
    for ec, ew in (extra_roads or []):       # connector/interior roads: grass meets them too (flat, no bank)
        samples = samples + _road_surface_samples(ec, ew, None)[0]
    buckets: dict[tuple[int, int], list[tuple[float, float, float]]] = defaultdict(list)
    for x, y, z, _i in samples:
        buckets[(int(x // reach), int(z // reach))].append((x, y, z))
    for row in grid_xyz:
        for k in range(len(row)):
            gx, gy, gz = row[k]
            ci, cj = int(gx // reach), int(gz // reach)
            best_d2, near_y, corr_min = 1e18, None, None
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for cx, cy, cz in buckets.get((ci + di, cj + dj), ()):
                        d2 = (gx - cx) ** 2 + (gz - cz) ** 2
                        if d2 < best_d2:
                            best_d2, near_y = d2, cy
                        if d2 <= corr2 and (corr_min is None or cy < corr_min):
                            corr_min = cy
            if near_y is None:
                continue
            tgt = (corr_min if corr_min is not None else near_y) if target == "min" else near_y
            tgt -= clearance      # sink the graded grass a touch BELOW the road so the coarse grass grid
            #                       can't bulge a triangle up through the road (poke-through) on steep
            #                       sections like the corkscrew. The shoulder ramps to this same level.
            d = math.sqrt(best_d2)
            if d <= corridor:
                row[k] = (gx, tgt, gz)
            elif d <= reach:
                t = (d - corridor) / blend
                row[k] = (gx, tgt * (1 - t) + gy * t, gz)


def grade_embankment(grid_xyz: list[list[Vertex]], centerline_m: list[Vertex],
                     widths_m: list[float] | None = None, *, band: float = 4.0, ratio: float = 2.0,
                     clearance: float = 0.0, bank_at=None, extra_roads=None,
                     bridge_of=None) -> None:
    """Build the terrain UP from the real ground to the road — an EMBANKMENT (fill) where the road sits
    above bare earth, a CUT where it sits below — instead of pulling the whole corridor UP to a flat
    plateau (the old ``conform_terrain_to_road``, which buried valleys under a 10 m mesa the car floats on).

    For each grid node, referenced to the nearest road-surface sample (centre + both banked edges):
      • within ``band`` m of the road  → held at ``road_edge - clearance`` (a gentle, drivable shelf that
        the shoulder/kerb meets flush — the "hybrid" gentle band),
      • beyond ``band``                → ramps toward the node's OWN bare-earth height at a ``ratio``:1
        slope (2:1 ≈ 27°). Fill descends from the shelf down to real ground; cut rises up to it. Once it
        reaches natural ground it STAYS there — so the real valley/hillside is preserved, and running off
        is a slope to the real ground, not a fall off a tabletop edge.

    Mutates ``grid_xyz`` in place (Y only). ``bridge_of(station_m) -> bool`` (optional) marks stations the
    road crosses on a BRIDGE: fill is suppressed there (the terrain keeps its natural creek/valley height
    so the deck can span it). On a near-flat racetrack the fills are tiny, so this reads as the "smooth
    gradations to the ground" a purpose-built circuit wants — same code path, no special-casing."""
    from collections import defaultdict
    slope = 1.0 / max(ratio, 1e-3)
    samples, _ = _road_surface_samples(centerline_m, widths_m, bank_at)
    # tag each sample with its lap station so bridge spans can suppress fill under a deck
    stationed = []
    arc = 0.0
    prev = None
    # recompute per-sample station: samples come grouped (centre,left,right) per centerline vertex; use the
    # centerline arc at that vertex. Rebuild arc alongside _road_surface_samples' vertex order.
    cl_arc = [0.0]
    for i in range(1, len(centerline_m)):
        cl_arc.append(cl_arc[-1] + math.hypot(centerline_m[i][0] - centerline_m[i - 1][0],
                                              centerline_m[i][2] - centerline_m[i - 1][2]))
    for (sx, sy, sz, si) in samples:
        stationed.append((sx, sy, sz, cl_arc[si] if si < len(cl_arc) else 0.0))
    for ec, ew in (extra_roads or []):        # connectors: graded too, no bridge, no station
        for (sx, sy, sz, _si) in _road_surface_samples(ec, ew, None)[0]:
            stationed.append((sx, sy, sz, -1.0))
    reach = band + 60.0                        # search radius: band + a tall fill's run (30 m fill @2:1 = 60 m)
    cell = 24.0
    BRIDGE_CLEAR = 14.0                         # keep terrain NATURAL within this of a bridge-span road point
    buckets: dict[tuple[int, int], list] = defaultdict(list)
    bridge_buckets: dict[tuple[int, int], list] = defaultdict(list)
    for sx, sy, sz, sstn in stationed:
        buckets[(int(sx // cell), int(sz // cell))].append((sx, sy, sz))
        if bridge_of is not None and sstn >= 0 and bridge_of(sstn):
            bridge_buckets[(int(sx // cell), int(sz // cell))].append((sx, sz))
    R = int(reach // cell) + 1
    br2 = BRIDGE_CLEAR * BRIDGE_CLEAR
    for row in grid_xyz:
        for k in range(len(row)):
            gx, gy, gz = row[k]                # gy = natural bare earth
            ci, cj = int(gx // cell), int(gz // cell)
            best_d2, ref_y = 1e18, None
            on_bridge = False
            for di in range(-R, R + 1):
                for dj in range(-R, R + 1):
                    for sx, sy, sz in buckets.get((ci + di, cj + dj), ()):
                        d2 = (gx - sx) ** 2 + (gz - sz) ** 2
                        if d2 < best_d2:
                            best_d2, ref_y = d2, sy
                    if not on_bridge:
                        for bx, bz in bridge_buckets.get((ci + di, cj + dj), ()):
                            if (gx - bx) ** 2 + (gz - bz) ** 2 <= br2:
                                on_bridge = True
                                break
            if ref_y is None:
                continue
            shelf = ref_y - clearance
            d = math.sqrt(best_d2)
            over = max(0.0, d - band)
            if shelf >= gy:                     # FILL — descend from the shelf to real ground
                if on_bridge:
                    continue                    # leave the natural creek/valley open so the deck spans it
                tgt = max(gy, shelf - slope * over)
            else:                               # CUT — rise from the shelf to real ground
                tgt = min(gy, shelf + slope * over)
            row[k] = (gx, tgt, gz)


def clamp_terrain_below_road(grid_xyz: list[list[Vertex]], road_verts: list[Vertex],
                             *, reach: float = 11.0, clear: float = 0.25, cell: float = 12.0,
                             grid_spacing: float | None = None) -> None:
    """One-sided ANTI-POKE against the ACTUAL built road surface. For every terrain grid node within
    ``reach`` m of a road vertex, if the node sits above ``road_y - clear`` push it DOWN to that level so
    no grass triangle pokes up through the drivable ribbon. Never RAISES a node (natural dips survive).

    Why against the built road verts (not the centerline): on a BANKED turn the inner edge rides metres
    below the centre, so a centreline-based clamp leaves the inner edge poking; the real banked edge verts
    are the true surface. Run AFTER road_ribbon (post-bank, post-lift) and conform, BEFORE grass_terrain.
    Mutates ``grid_xyz`` in place. Mirror-agnostic: road_verts and grid_xyz must be in the SAME frame."""
    from collections import defaultdict
    # reach must cover the terrain grid's cell DIAGONAL past the road edge, or the first un-clamped
    # node up-slope bridges its triangle back over the pavement (195 wheel-path ground-throughs on
    # the Lariat at reach=11 vs 13.4 m cells — the drive test sees the faces, not the vertices).
    if grid_spacing is not None:
        reach = max(reach, grid_spacing * 1.5 + 0.5)
    cell = max(cell, reach)
    buckets: dict[tuple[int, int], list[Vertex]] = defaultdict(list)
    for x, y, z in road_verts:
        buckets[(int(x // cell), int(z // cell))].append((x, y, z))
    r2 = reach * reach
    for row in grid_xyz:
        for k in range(len(row)):
            gx, gy, gz = row[k]
            ci, cj = int(gx // cell), int(gz // cell)
            lim = None
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for rx, ry, rz in buckets.get((ci + di, cj + dj), ()):
                        if (gx - rx) ** 2 + (gz - rz) ** 2 <= r2:
                            t = ry - clear
                            if lim is None or t < lim:
                                lim = t
            if lim is not None and gy > lim:
                row[k] = (gx, lim, gz)


def upsample_grid(grid: list[list[float]], meta: dict, factor: int) -> tuple[list[list[float]], dict]:
    """Bilinearly upsample the raw DEM height grid by an integer factor (finer spacing). Free (no
    re-sampling) and lossless on gentle terrain — a fine grid lets ``conform_terrain_to_road`` hug the
    road with a small corridor and little float, instead of fighting 40 m facets. Returns (grid, meta)
    with nx/ny/spacing_m updated; the lon/lat bbox is unchanged so projection stays identical."""
    if factor <= 1:
        return grid, meta
    ny, nx = len(grid), len(grid[0])
    big_y, big_x = (ny - 1) * factor + 1, (nx - 1) * factor + 1
    out = [[0.0] * big_x for _ in range(big_y)]
    for jj in range(big_y):
        fy = jj / factor
        j0 = min(ny - 1, int(fy)); j1 = min(ny - 1, j0 + 1); ty = fy - j0
        for ii in range(big_x):
            fx = ii / factor
            i0 = min(nx - 1, int(fx)); i1 = min(nx - 1, i0 + 1); tx = fx - i0
            a = grid[j0][i0] * (1 - tx) + grid[j0][i1] * tx
            b = grid[j1][i0] * (1 - tx) + grid[j1][i1] * tx
            out[jj][ii] = a * (1 - ty) + b * ty
    meta2 = dict(meta)
    meta2["nx"], meta2["ny"], meta2["spacing_m"] = big_x, big_y, meta["spacing_m"] / factor
    return out, meta2


def grass_terrain(grid_xyz: list[list[Vertex]], *, tile_m: float = 6.0, skirt_m: float = 500.0) -> dict:
    """Triangulate the projected terrain grid (ny rows × nx cols of (x,y,z)) into the GRASS mesh.
    UVs are world-planar (x/tile, z/tile) so the grass texture tiles seamlessly over any terrain.

    A flat **skirt** (``skirt_m`` m) is extruded outward from every grid border at the border height,
    so the physical ground extends far past the loop — run wide onto the grass and you stay on a
    surface instead of dropping off the edge of the world."""
    ny = len(grid_xyz)
    nx = len(grid_xyz[0])
    verts = [grid_xyz[j][i] for j in range(ny) for i in range(nx)]
    uvs = [(grid_xyz[j][i][0] / tile_m, grid_xyz[j][i][2] / tile_m) for j in range(ny) for i in range(nx)]
    tris: list[tuple[int, int, int]] = []
    for j in range(ny - 1):
        for i in range(nx - 1):
            a, b, c, d = j * nx + i, j * nx + i + 1, (j + 1) * nx + i + 1, (j + 1) * nx + i
            tris.append((a, b, c))
            tris.append((a, c, d))

    if skirt_m > 0.0:
        # WATERTIGHT skirt: walk the grid's border as a closed perimeter loop and reference the grid's
        # ACTUAL border vertex indices (gi). Each skirt quad's inner edge IS a real grid edge, so it
        # shares with the grid face — no coincident copies, no reliance on welding, no open seam (the
        # old copy-and-weld approach left ~780 holes the car fell through).
        perim: list[tuple[int, float, float]] = []  # (grid_index, outward_dx, outward_dz)
        for i in range(nx):                 perim.append((i, 0.0, skirt_m))                 # north  j=0
        for j in range(1, ny):              perim.append((j * nx + nx - 1, skirt_m, 0.0))   # east   i=nx-1
        for i in range(nx - 2, -1, -1):     perim.append(((ny - 1) * nx + i, 0.0, -skirt_m))  # south j=ny-1
        for j in range(ny - 2, 0, -1):      perim.append((j * nx, -skirt_m, 0.0))           # west   i=0
        # Drop the far edge to one flat LOW level (below all terrain) instead of extruding each border
        # FLAT at its own height — otherwise a high DEM border (here up to +35 m vs a ~17 m track) makes
        # a 500 m grass plane hanging in the sky. The skirt now ramps gently down-and-out to a floor you
        # never reach, so there's a catch-surface off the world without any floating planes.
        low_y = min(v[1] for v in verts) - 5.0
        outer_idx = []
        for gi, dx, dz in perim:
            x, _y, z = verts[gi]
            outer_idx.append(len(verts))
            verts.append((x + dx, low_y, z + dz))
            uvs.append(((x + dx) / tile_m, (z + dz) / tile_m))
        P = len(perim)
        for k in range(P):
            gi0, gi1 = perim[k][0], perim[(k + 1) % P][0]      # consecutive grid border verts (a real edge)
            oi0, oi1 = outer_idx[k], outer_idx[(k + 1) % P]
            tris.append((gi0, oi0, oi1))
            tris.append((gi0, oi1, gi1))
    return {"vertices": verts, "uvs": uvs, "tris": tris}


def crosswalk(centerline_m: list[Vertex], widths_m: list[float], *, at_idx: int = 0,
              depth_m: float = 3.6, bar_w: float = 0.55, gap_w: float = 0.55, margin_m: float = 0.5,
              lift: float = 0.0, bank_at=None) -> dict:
    """A high-visibility **continental crosswalk** across the road at centerline index ``at_idx`` (the
    start/finish on Colorado Blvd). Longitudinal white bars — parallel to travel, arranged across the
    full width — straddling the line. Flat white (visual-only, MARKINGS material). ``bank_at`` rolls it
    with the road. UV-free (solid colour)."""
    pts = centerline_m
    n = len(pts)
    i = max(0, min(n - 1, at_idx))
    x, y, z = pts[i]
    tx, tz = _horiz_tangent(pts, i, abs(pts[0][0] - pts[-1][0]) < 1e-6 and abs(pts[0][2] - pts[-1][2]) < 1e-6)
    nx, nz = -tz, tx                      # left normal (across the road)
    # global station at this index (for bank lookup)
    arc = 0.0
    for k in range(1, i + 1):
        arc += math.hypot(pts[k][0] - pts[k - 1][0], pts[k][2] - pts[k - 1][2])
    tb = math.tan(bank_at(arc)) if bank_at else 0.0
    half = widths_m[i] / 2.0 - margin_m
    d = depth_m / 2.0
    verts: list[Vertex] = []
    tris: list[tuple[int, int, int]] = []
    u = -half
    step = bar_w + gap_w
    while u <= half - 1e-6:
        u1 = min(u + bar_w, half)
        for (uu, dd) in ((u, -d), (u1, -d), (u1, d), (u, d)):   # bar corners (across × along)
            px = x + nx * uu + tx * dd
            pz = z + nz * uu + tz * dd
            verts.append((px, y + lift + uu * tb, pz))
        b = len(verts) - 4
        tris.append((b, b + 1, b + 2))
        tris.append((b, b + 2, b + 3))
        u += step
    return {"vertices": verts, "tris": tris}


def road_markings(centerline_m: list[Vertex], widths_m: list[float], *, line_w: float = 0.14,
                  edge_inset: float = 0.4, lift: float = 0.0, dash_on: float = 3.0,
                  dash_gap: float = 4.5, tile_m: float = 1.0, bank_at=None) -> dict:
    """Thin white lane lines along the road: solid edge lines (inset from the kerb) + a dashed centre.
    Visual-only (no ``1`` prefix). Sits ``lift`` above the road — build_mesh raises it with the ribbon.
    ``bank_at`` rolls each line with the cambered road (offset*tan(bank) added to Y)."""
    pts = centerline_m
    n = len(pts)
    closed = abs(pts[0][0] - pts[-1][0]) < 1e-6 and abs(pts[0][2] - pts[-1][2]) < 1e-6
    m = n - 1 if closed else n
    stations = []
    arc = 0.0
    for i in range(m):
        if i > 0:
            arc += math.hypot(pts[i][0] - pts[i - 1][0], pts[i][2] - pts[i - 1][2])
        x, y, z = pts[i]
        tx, tz = _horiz_tangent(pts, i, closed)
        stations.append((x, y, z, -tz, tx, widths_m[i] / 2.0, arc))  # x,y,z, leftnormal nx,nz, half, arc
    verts: list[Vertex] = []
    uvs: list[tuple[float, float]] = []
    tris: list[tuple[int, int, int]] = []

    def add_line(offset_of, dashed):
        for i in range(m):
            j = (i + 1) % m
            if not closed and j == 0:
                break
            s0, s1 = stations[i], stations[j]
            if dashed and (s0[6] % (dash_on + dash_gap)) > dash_on:
                continue
            b = len(verts)
            for s in (s0, s1):
                x, y, z, nx, nz, half, a = s
                o = offset_of(s)
                tb = math.tan(bank_at(a)) if bank_at else 0.0
                for sign in (-1, 1):
                    off = o + sign * line_w / 2
                    verts.append((x + nx * off, y + lift + off * tb, z + nz * off))
                    uvs.append(((sign + 1) / 2, a / tile_m))
            tris.append((b, b + 1, b + 3))
            tris.append((b, b + 3, b + 2))

    add_line(lambda s: 0.0, True)                    # centre line, dashed
    add_line(lambda s: s[5] - edge_inset, False)     # left edge, solid
    add_line(lambda s: -(s[5] - edge_inset), False)  # right edge, solid
    return {"vertices": verts, "uvs": uvs, "tris": tris}
