"""Generate 1KERB ribbons at the corners of the centerline (curvature-based).

Finds high-curvature runs (corners), lays a raised kerb strip on the inside edge of each, named
``1KERB_*`` so AC treats it as a kerb (rumble via surfaces.ini KEY=KERB). Inputs are local ENU metres.
"""

from __future__ import annotations

import math

Vertex = tuple[float, float, float]


def _turn_rate(pts: list[Vertex]) -> list[float]:
    """Signed turn angle per metre at each vertex (in the X-Z plane); +ve = left turn."""
    n = len(pts)
    tr = [0.0] * n
    for i in range(1, n - 1):
        (ax, _, az), (bx, _, bz), (cx, _, cz) = pts[i - 1], pts[i], pts[i + 1]
        v1, v2 = (bx - ax, bz - az), (cx - bx, cz - bz)
        l1, l2 = math.hypot(*v1) or 1e-6, math.hypot(*v2) or 1e-6
        cross = max(-1.0, min(1.0, (v1[0] * v2[1] - v1[1] * v2[0]) / (l1 * l2)))
        tr[i] = math.asin(cross) / ((l1 + l2) / 2)
    return tr


def _smooth(a: list[float], k: int = 3) -> list[float]:
    n = len(a)
    return [sum(a[max(0, i - k):min(n, i + k + 1)]) / len(a[max(0, i - k):min(n, i + k + 1)]) for i in range(n)]


def _inside_normal(pts: list[Vertex], k: int, side: float) -> tuple[float, float]:
    """Unit normal at vertex k pointing to the ``side`` of the road (left-normal × side)."""
    n = len(pts)
    a = pts[max(0, k - 1)]
    b = pts[min(n - 1, k + 1)]
    tx, tz = b[0] - a[0], b[2] - a[2]
    tl = math.hypot(tx, tz) or 1e-6
    return (-tz / tl * side, tx / tl * side)


def _fine_stations(poly: list[Vertex], step: float) -> list[tuple[float, float, float, float, float, float]]:
    """Resample a polyline to ~``step`` spacing; return (x, y, z, tx, tz, arc) with a unit X-Z tangent.
    Kerbs are densified like this so the per-metre ridges can be modelled (the 3 m centreline is far
    too coarse for that)."""
    seg = [0.0]
    for i in range(1, len(poly)):
        seg.append(seg[-1] + math.hypot(poly[i][0] - poly[i - 1][0], poly[i][2] - poly[i - 1][2]))
    total = seg[-1]
    if total < 1e-6:
        return []
    steps = max(1, round(total / step))
    out = []
    si = 1
    for s in range(steps + 1):
        d = total * s / steps
        while si < len(poly) - 1 and seg[si] < d:
            si += 1
        a, b = poly[si - 1], poly[si]
        denom = (seg[si] - seg[si - 1]) or 1e-6
        t = (d - seg[si - 1]) / denom
        tx, tz = b[0] - a[0], b[2] - a[2]
        tl = math.hypot(tx, tz) or 1e-6
        out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t,
                    tx / tl, tz / tl, d))
    return out


def _cum_stations(pts: list[Vertex]) -> list[float]:
    st = [0.0]
    for i in range(1, len(pts)):
        st.append(st[-1] + math.hypot(pts[i][0] - pts[i - 1][0], pts[i][2] - pts[i - 1][2]))
    return st


def corner_kerbs(centerline_m: list[Vertex], widths_m: list[float], *, thr: float = 0.007,
                 min_run: int = 5, kerb_w: float = 1.0, kerb_h: float = 0.05, ridge_period: float = 0.6,
                 ridge_lo: float = 0.45, top_frac: float = 0.55, lift: float = 0.01,
                 fine_step: float = 0.3, sharp: float = 0.05, tile_m: float = 0.6, edge_ramp: float = 0.0,
                 bank_at=None) -> dict:
    """Real 3D **ridged** kerbs on corners — a raised, serrated rumble strip, not a flat texture plane.
    Each corner run is resampled fine (``fine_step``) and a cross-section is swept along it:
        road-edge lip (vertical, flush at the lane edge) -> raised top -> ramp back down on the verge,
    with the top height pulsing every ``ridge_period`` m (down to ``ridge_lo`` x height) to form the
    ridges you feel. ``kerb_h`` is the kerb height — tune it freely. Sits OUTSIDE the lane edge (on
    the verge), never on the racing surface. Sweepers get both-edge kerbs; tight junctions get a short
    kerb hugging the apex edge. UVs: U across the profile, V along the run."""
    pts = centerline_m
    n = len(pts)
    rate = _smooth(_turn_rate(pts))
    stations = _cum_stations(pts)
    verts: list[Vertex] = []
    uvs: list[tuple[float, float]] = []
    tris: list[tuple[int, int, int]] = []

    def sweep(run: list[int], side: float, half: float) -> None:
        st = _fine_stations([pts[k] for k in run], fine_step)
        if len(st) < 2:
            return
        base_s = stations[run[0]]                                # global station at the run's start
        rows = []
        for x, y, z, tx, tz, arc in st:
            nx, nz = -tz * side, tx * side                       # toward this side of the road
            h = kerb_h                                            # flat low kerb (no serrated ridges —
            #   the 3D serrations drove like an off-road rumble strip; the red/white stripe is the
            #   TEXTURE + a gentle surfaces.ini vibration, not a physical speed bump).
            # Kerb sits OUTSIDE the racing surface: the vertical lip is flush at the road edge
            # (``half``) and the raised body + ramp extend OUTWARD onto the verge (``half + kerb_w``),
            # so it never eats into the drivable lane — you clip it running wide, you don't drive over it.
            edge, top, ramp = half, half + kerb_w * top_frac, half + kerb_w
            lip = half + kerb_w * edge_ramp        # lane-side ramp: 0 = vertical street lip; >0 = rideable racing ramp
            tb = math.tan(bank_at(base_s + arc)) if bank_at else 0.0  # roll with the cambered road
            r = len(verts)
            verts.append((x + nx * edge, y + lift + side * edge * tb, z + nz * edge))        # 0 edge bottom (road edge)
            verts.append((x + nx * lip, y + lift + h + side * lip * tb, z + nz * lip))       # 1 edge top (lane-side face: vertical lip or rideable ramp)
            verts.append((x + nx * top, y + lift + h + side * top * tb, z + nz * top))       # 2 top (outward)
            verts.append((x + nx * ramp, y + lift + side * ramp * tb, z + nz * ramp))        # 3 ramp bottom (verge side)
            v = arc / tile_m
            uvs.extend([(0.0, v), (0.15, v), (0.5, v), (1.0, v)])
            rows.append(r)
        for r in range(len(rows) - 1):
            p, q = rows[r], rows[r + 1]
            for u in (0, 1, 2):  # outer vertical face (0->1), top (1->2), ramp (2->3)
                tris.append((p + u, p + u + 1, q + u + 1))
                tris.append((p + u, q + u + 1, q + u))

    i = 0
    while i < n:
        if abs(rate[i]) <= thr:
            i += 1
            continue
        j = i
        while j < n and abs(rate[j]) > thr:
            j += 1
        run = list(range(i, j))
        peak = max(abs(rate[k]) for k in run)
        side = 1.0 if rate[run[len(run) // 2]] > 0 else -1.0
        half = widths_m[run[len(run) // 2]] / 2.0
        if peak >= sharp and len(run) >= 2:
            # Tight street junction (~90°): an inside apex kerb would (a) be unrealistic on a street
            # corner and (b) pinch/self-intersect onto the lane as the inner offset curve collapses.
            # Put the kerb on the OUTSIDE (runoff) only — the outer offset curve expands, never crosses.
            sweep(run, -side, half)
        elif len(run) >= min_run:
            sweep(run, side, half)            # sweeper -> inside apex ...
            sweep(run, -side, half)           # ... and outside exit
        i = j
    return {"vertices": verts, "uvs": uvs, "tris": tris}


def corner_runoff(centerline_m: list[Vertex], widths_m: list[float], *, thr: float = 0.008,
                  min_run: int = 5, gap: float = 2.5, runoff_w: float = 9.0, pad: int = 5,
                  taper: int = 5, tile_m: float = 8.0, bank_at=None) -> dict:
    """A wide **paved tarmac apron** on the OUTSIDE of each corner — where the car runs wide — instead
    of grass. Replaces the old corner barriers: open, forgiving asphalt runoff rather than a wall.

    Starts at ``gap`` m off the lane edge (just past the paved shoulder) and extends ``runoff_w`` m out,
    tapering to nothing over ``taper`` stations at each end so it blends in. Flat at the graded terrain
    height (the conform corridor keeps the terrain near road level here, so it sits flush). Named
    ``1RUNOFF_*`` in build_mesh -> a drivable, slightly-low-grip, off-track surface. UVs: U across the
    apron, V along the run (metres/``tile_m``)."""
    pts = centerline_m
    n = len(pts)
    rate = _smooth(_turn_rate(pts))
    stations = _cum_stations(pts)
    verts: list[Vertex] = []
    uvs: list[tuple[float, float]] = []
    tris: list[tuple[int, int, int]] = []
    i = 0
    while i < n:
        if abs(rate[i]) <= thr:
            i += 1
            continue
        j = i
        while j < n and abs(rate[j]) > thr:
            j += 1
        if j - i >= min_run:
            side = -1.0 if rate[(i + j) // 2] > 0 else 1.0  # OUTSIDE of the turn (cars run wide here)
            run = list(range(max(0, i - pad), min(n, j + pad)))
            arc = 0.0
            rows = []
            for idx, k in enumerate(run):
                x, y, z = pts[k]
                if idx > 0:
                    p = pts[run[idx - 1]]
                    arc += math.hypot(x - p[0], z - p[2])
                a, b = pts[max(0, k - 1)], pts[min(n - 1, k + 1)]
                tx, tz = b[0] - a[0], b[2] - a[2]
                tl = math.hypot(tx, tz) or 1e-6
                nx, nz = -tz / tl * side, tx / tl * side
                w = runoff_w * min(1.0, idx / taper, (len(run) - 1 - idx) / taper)  # taper ends
                half = widths_m[k] / 2.0
                inner, outer = half + gap, half + gap + w
                tb = math.tan(bank_at(stations[k])) if bank_at else 0.0   # roll with the cambered road
                r = len(verts)
                verts.append((x + nx * inner, y + side * inner * tb, z + nz * inner))
                verts.append((x + nx * outer, y + side * outer * tb, z + nz * outer))
                uvs.append((0.0, arc / tile_m))
                uvs.append((max(w, 0.1) / tile_m, arc / tile_m))
                rows.append(r)
            for idx in range(len(rows) - 1):
                p, q = rows[idx], rows[idx + 1]
                tris.append((p, p + 1, q + 1))
                tris.append((p, q + 1, q))
        i = j
    return {"vertices": verts, "uvs": uvs, "tris": tris}


def _turn_angle(pts: list[Vertex], i: int) -> float:
    """Signed turn angle (deg) between consecutive segments at vertex i (+ve = left), X-Z plane."""
    n = len(pts)
    a, b, c = pts[(i - 1) % n], pts[i], pts[(i + 1) % n]
    v1 = (b[0] - a[0], b[2] - a[2]); v2 = (c[0] - b[0], c[2] - b[2])
    l1 = math.hypot(*v1) or 1e-9; l2 = math.hypot(*v2) or 1e-9
    cr = max(-1.0, min(1.0, (v1[0] * v2[1] - v1[1] * v2[0]) / (l1 * l2)))
    return math.degrees(math.asin(cr))


# Jersey-barrier half-profile (lateral offset from the barrier centreline, height) bottom→top, ~0.81 m
# tall: wide vertical toe, the lower 55° slope, then the steeper face to a narrow flat top.
_JERSEY = [(0.30, 0.0), (0.30, 0.09), (0.17, 0.34), (0.115, 0.81)]


def warning_barriers(centerline_m: list[Vertex], widths_m: list[float], *, sharp_deg: float = 45.0,
                     crest_deg: float = 24.0, per_vtx_thr: float = 0.9, off: float = 4.0,
                     height: float = 0.81, crest_height: float = 0.9, base_lift: float = -0.05,
                     lead_m: float = 28.0, crest_lead_m: float = 70.0, crest_prom_m: float = 1.2,
                     tile_m: float = 3.5):
    """Concrete JERSEY barriers on the OUTSIDE of sharp turns — a wayfinding cue ("a hard corner is
    coming") + a backstop that catches a car run wide. Targets corners whose total turn ≥ ``sharp_deg``;
    ALSO catches a more moderate corner (≥ ``crest_deg``) when it hides just over a CREST (blind brow),
    and for those extends the lead-in back over the crest (``crest_lead_m``) and raises it a touch so you
    see it as you climb. A sloped jersey cross-section (``_JERSEY``) is swept along each run, so it reads
    as concrete K-rail and collides as solid geometry. Returns (mesh_with_uvs, [placement dicts])."""
    pts = centerline_m
    n = len(pts)
    closed = abs(pts[0][0] - pts[-1][0]) < 1e-6 and abs(pts[0][2] - pts[-1][2]) < 1e-6
    ang = [_turn_angle(pts, i) for i in range(n)]
    sm = _smooth(ang, 2)
    elev = [p[1] for p in pts]
    st = [0.0]
    for i in range(1, n):
        st.append(st[-1] + math.hypot(pts[i][0] - pts[i - 1][0], pts[i][2] - pts[i - 1][2]))

    # crests: a local elevation max that stands ≥ crest_prom_m above the road ~60 m back (a real brow)
    def is_crest(i: int) -> bool:
        wlo = next((k for k in range(i, -1, -1) if st[i] - st[k] >= 60), 0)
        whi = next((k for k in range(i, n) if st[k] - st[i] >= 30), n - 1)
        local_max = elev[i] >= max(elev[wlo:whi + 1]) - 0.05
        return local_max and (elev[i] - elev[wlo]) >= crest_prom_m

    verts: list[Vertex] = []
    uvs: list[tuple[float, float]] = []
    tris: list[tuple[int, int, int]] = []
    placements = []

    def sweep(run: list[int], side: float, h: float) -> None:
        scale = h / 0.81
        prof = [(lat, ht * scale) for lat, ht in _JERSEY] + \
               [(-lat, ht * scale) for lat, ht in reversed(_JERSEY)]   # full outline: up one face, down the other
        plen = [0.0]
        for k in range(1, len(prof)):
            plen.append(plen[-1] + math.hypot(prof[k][0] - prof[k - 1][0], prof[k][1] - prof[k - 1][1]))
        ptot = plen[-1] or 1.0
        arc = 0.0
        rows = []
        for idx, k in enumerate(run):
            x, y, z = pts[k]
            if idx > 0:
                p = pts[run[idx - 1]]
                arc += math.hypot(x - p[0], z - p[2])
            a = pts[max(0, k - 1)]; b = pts[min(n - 1, k + 1)]
            tx, tz = b[0] - a[0], b[2] - a[2]
            tl = math.hypot(tx, tz) or 1e-6
            nx, nz = -tz / tl, tx / tl
            o = side * (widths_m[k] / 2.0 + off)
            bx, bz = x + nx * o, z + nz * o
            row = []
            for pi, (lat, ht) in enumerate(prof):                 # sweep the jersey cross-section
                row.append(len(verts))
                verts.append((bx + nx * lat, y + base_lift + ht, bz + nz * lat))
                uvs.append((plen[pi] / ptot, arc / tile_m))       # U around the profile, V along the run
            rows.append(row)
        for idx in range(len(rows) - 1):
            r0, r1 = rows[idx], rows[idx + 1]
            for pi in range(len(prof) - 1):
                a0, a1 = r0[pi], r0[pi + 1]; c1, c0 = r1[pi + 1], r1[pi]
                tris.append((a0, a1, c1)); tris.append((a0, c1, c0))

    i = 1
    while i < n - 1:
        if abs(sm[i]) <= per_vtx_thr:
            i += 1
            continue
        j = i
        while j < n - 1 and abs(sm[j]) > per_vtx_thr:
            j += 1
        total = sum(ang[k] for k in range(i, j))
        # is there a crest in the approach (≤ ~75 m before the corner start)?
        crest_near = any(is_crest(k) for k in range(max(0, i - 26), i + 1))
        if abs(total) >= sharp_deg or (abs(total) >= crest_deg and crest_near):
            side = -1.0 if total > 0 else 1.0          # OUTSIDE of the turn
            lead = crest_lead_m if crest_near else lead_m
            lead_v = int(lead / 3.0)                    # ~3 m vertex spacing
            a0 = max(1, i - lead_v); b0 = min(n - 1, j + 5)
            h = crest_height if crest_near else height
            sweep(list(range(a0, b0)), side, h)
            placements.append({"start_idx": a0, "apex_idx": (i + j) // 2, "turn_deg": round(total, 0),
                               "crest": crest_near, "station_m": round(st[(i + j) // 2], 0)})
        i = j
    return {"vertices": verts, "uvs": uvs, "tris": tris}, placements


def corner_barriers(centerline_m: list[Vertex], widths_m: list[float], *, thr: float = 0.013,
                    min_run: int = 7, off: float = 1.6, base_lift: float = -0.4, height: float = 1.2,
                    pad: int = 4, tile_m: float = 2.0) -> dict:
    """A low barrier wall on the OUTSIDE of each corner (opposite the kerb) — where cars run wide.
    Realistic for an open road circuit (guardrails at corners, open straights) rather than walling the
    whole lap. Returns a wall mesh with UVs (U up the 0→1 face, V along the run). build_mesh raises it
    with the road; ``base_lift`` sinks the foot into the graded verge so it doesn't float."""
    pts = centerline_m
    n = len(pts)
    rate = _smooth(_turn_rate(pts))
    verts: list[Vertex] = []
    uvs: list[tuple[float, float]] = []
    tris: list[tuple[int, int, int]] = []
    i = 0
    while i < n:
        if abs(rate[i]) <= thr:
            i += 1
            continue
        j = i
        while j < n and abs(rate[j]) > thr:
            j += 1
        if j - i >= min_run:
            side = -1.0 if rate[(i + j) // 2] > 0 else 1.0  # OUTSIDE of the turn
            run = list(range(max(0, i - pad), min(n, j + pad)))  # extend past the apex
            arc = 0.0
            ring = []
            for idx, k in enumerate(run):
                x, y, z = pts[k]
                if idx > 0:
                    p = pts[run[idx - 1]]
                    arc += math.hypot(x - p[0], z - p[2])
                a = pts[max(0, k - 1)]
                b = pts[min(n - 1, k + 1)]
                tx, tz = b[0] - a[0], b[2] - a[2]
                tl = math.hypot(tx, tz) or 1e-6
                nx, nz = -tz / tl, tx / tl
                o = side * (widths_m[k] / 2.0 + off)
                bx, bz = x + nx * o, z + nz * o
                bi = len(verts)
                verts.append((bx, y + base_lift, bz))
                verts.append((bx, y + base_lift + height, bz))
                uvs.append((0.0, arc / tile_m))
                uvs.append((1.0, arc / tile_m))
                ring.append((bi, bi + 1))
            for idx in range(len(ring) - 1):
                b0, t0 = ring[idx]
                b1, t1 = ring[idx + 1]
                tris.append((b0, b1, t1))
                tris.append((b0, t1, t0))
        i = j
    return {"vertices": verts, "uvs": uvs, "tris": tris}
