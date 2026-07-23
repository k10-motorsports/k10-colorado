"""Compute AC dummy object placements along the centerline (local ENU metres).

Required by AC (see skill: ac-track-modding):
  AC_START_0..N, AC_PIT_0..N, AC_TIME_0_L/R (start-finish), AC_TIME_n_L/R (sectors), AC_HOTLAP_START_0
Timing gates are emitted as L/R pairs (the two ends of the timing line across the road).
"""

from __future__ import annotations

import math

Vertex = tuple[float, float, float]


def _cum_length(pts: list[Vertex]) -> list[float]:
    d = [0.0]
    for i in range(1, len(pts)):
        d.append(d[-1] + math.hypot(pts[i][0] - pts[i - 1][0], pts[i][2] - pts[i - 1][2]))
    return d


def _tangent(pts: list[Vertex], i: int) -> tuple[float, float]:
    n = len(pts)
    a, b = pts[(i - 1) % n], pts[(i + 1) % n]
    dx, dz = b[0] - a[0], b[2] - a[2]
    L = math.hypot(dx, dz) or 1.0
    return dx / L, dz / L


def place_dummies(
    centerline_m: list[Vertex],
    widths_m: list[float],
    n_sectors: int = 3,
    n_pits: int = 4,
) -> dict[str, list[float]]:
    """Return {dummy_name: [x, y, z]}. Gates split the lap into ``n_sectors`` equal-length arcs."""
    pts = centerline_m
    n = len(pts)
    cum = _cum_length(pts)
    total = cum[-1]

    def index_at(fraction: float) -> int:
        target = fraction * total
        for i in range(n):
            if cum[i] >= target:
                return i
        return n - 1

    def offset(i: int, signed_half: float) -> list[float]:
        x, y, z = pts[i]
        tx, tz = _tangent(pts, i)
        nx, nz = -tz, tx
        return [round(x + nx * signed_half, 3), round(y, 3), round(z + nz * signed_half, 3)]

    # THE START BELONGS ON A STRAIGHT (Kevin: "standardize starting on straights") — pts[0] is
    # wherever the source KML/OSM loop happened to begin, which put grids on corners. Scan for
    # the straightest, flattest ~260 m window (room for grid + timing line) and rotate the whole
    # start complex there. Score = worst 4-vertex heading change + a grade penalty.
    import math as _m

    def _heading(i):
        tx, tz = _tangent(pts, i)
        return _m.atan2(tz, tx)

    W = 260.0
    step = max(1, n // 1200)
    best_score, best_i = None, 0
    for i0 in range(0, n - 1, step):
        j = i0
        worst_turn = 0.0
        while j < n - 1 and cum[j] - cum[i0] < W:
            d = abs((_m.degrees(_heading(min(j + 4, n - 1)) - _heading(j)) + 180.0) % 360.0 - 180.0)
            worst_turn = max(worst_turn, d)
            j += 4
        if cum[min(j, n - 1)] - cum[i0] < W * 0.9:
            continue                      # tail too short for the full window
        grade = abs(pts[min(j, n - 1)][1] - pts[i0][1]) / max(1.0, cum[min(j, n - 1)] - cum[i0])
        score = worst_turn + 300.0 * grade
        if best_score is None or score < best_score:
            best_score, best_i = score, i0

    def index_at_arc(arc_m: float) -> int:
        target = arc_m % total
        for i in range(n):
            if cum[i] >= target:
                return i
        return n - 1

    start_arc = cum[best_i] + 45.0        # a car-grid's length into the straight
    s_i = index_at_arc(start_arc)

    out: dict[str, list[float]] = {}
    # Start-finish line 30 m AHEAD of the grid: AC arms the lap clock only when the car
    # CROSSES the line — spawning ON it (start at the same station) never registered laps.
    line_arc = start_arc + 30.0
    # Start-finish (sector 0) at the LINE + intermediate sector gates, as L/R pairs.
    for k in range(n_sectors):
        i = index_at_arc(line_arc + (k / n_sectors) * total)
        half = widths_m[i] / 2.0
        out[f"AC_TIME_{k}_L"] = offset(i, +half)
        out[f"AC_TIME_{k}_R"] = offset(i, -half)

    sx, sy, sz = pts[s_i]
    out["AC_START_0"] = [round(sx, 3), round(sy, 3), round(sz, 3)]
    out["AC_HOTLAP_START_0"] = [round(sx, 3), round(sy, 3), round(sz, 3)]

    # Pit slots ON the road along the start straight (no separate pit lane is modeled). Offsetting them
    # onto the verge dropped the car — the graded grass sits below the road there, so a pit-spawned car
    # fell off the edge (the hotlap start, which sits ON the road, was fine). Stagger them back along the
    # centerline and alternate left/right WITHIN the lane so cars don't overlap, at road height like
    # AC_START. staggered ~12 m apart (>2 car lengths), ~40% of a half-lane off-centre each side.
    for p in range(n_pits):
        i = index_at_arc(start_arc + (p + 1) * 10.0)     # staggered up the start straight
        side = (widths_m[i] / 2.0) * 0.4 * (1.0 if p % 2 == 0 else -1.0)
        out[f"AC_PIT_{p}"] = offset(i, side)
    return out
