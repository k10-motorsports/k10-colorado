"""Georeference a map screenshot by aligning the drawn pixels to the real OSM road network.

The screenshot is North-up Web-Mercator, ~linear over a few km, so pixel→lon/lat is an affine map
with no rotation: ``lon = lon0 + x*sx``, ``lat = lat0 + y*sy`` (sy < 0). We seed it from the drawn
bbox vs an estimated route bbox, then refine the 4 params by coordinate descent to minimise the
(capped) distance from every drawn pixel to the nearest road — i.e. the transform that best lays the
drawing onto the roads. No manual control points needed.
"""

from __future__ import annotations

import math

Params = tuple[float, float, float, float]  # lon0, sx, lat0, sy
LAT_M = 111_132.0


def haversine_m(a, b) -> float:
    (lo1, la1), (lo2, la2) = a, b
    mlon = LAT_M * math.cos(math.radians((la1 + la2) / 2))
    return math.hypot((lo2 - lo1) * mlon, (la2 - la1) * LAT_M)


def to_lonlat(p: Params, x: float, y: float) -> tuple[float, float]:
    return p[0] + x * p[1], p[2] + y * p[3]


def initial_params(red_bbox_px, route_bbox_lonlat) -> Params:
    """Seed transform: map drawn pixel bbox → estimated route lon/lat bbox (W,E,S,N)."""
    minx, maxx, miny, maxy = red_bbox_px
    w, e, s, n = route_bbox_lonlat
    sx = (e - w) / (maxx - minx)
    sy = (s - n) / (maxy - miny)  # px y grows downward = lat decreases
    return (w - minx * sx, sx, n - miny * sy, sy)


def exact_from_anchors(a1_px, a1_ll, a2_px, a2_ll) -> Params:
    """Exact north-up affine from two pixel↔lon/lat control points (no optimisation, no ambiguity)."""
    (x1, y1), (lon1, lat1) = a1_px, a1_ll
    (x2, y2), (lon2, lat2) = a2_px, a2_ll
    sx = (lon2 - lon1) / (x2 - x1)
    sy = (lat2 - lat1) / (y2 - y1)
    return (lon1 - x1 * sx, sx, lat1 - y1 * sy, sy)


def build_road_index(ways, *, sample_m: float = 6.0, cell_m: float = 18.0, lat0: float = 39.79):
    """Sample road geometry to dense points in a grid hash keyed by ~cell_m cells."""
    dlat = cell_m / LAT_M
    dlon = cell_m / (LAT_M * math.cos(math.radians(lat0)))
    grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for way in ways:
        g = way["geom"]
        for i in range(len(g) - 1):
            a, b = g[i], g[i + 1]
            d = haversine_m(a, b)
            steps = max(1, int(d / sample_m))
            for t in range(steps + 1):
                f = t / steps
                lon, lat = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
                grid.setdefault((int(lon / dlon), int(lat / dlat)), []).append((lon, lat))
    return grid, dlon, dlat


def nearest_road_m(index, lon: float, lat: float, cap: float = 25.0) -> float:
    grid, dlon, dlat = index
    ci, cj = int(lon / dlon), int(lat / dlat)
    best = cap
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            for rlon, rlat in grid.get((ci + di, cj + dj), ()):  # noqa: E501
                d = haversine_m((lon, lat), (rlon, rlat))
                if d < best:
                    best = d
    return best


def _score(p: Params, red_px, index) -> float:
    return sum(nearest_road_m(index, *to_lonlat(p, x, y)) for x, y in red_px)


def refine_anchored(red_px, index, anchor_px, anchor_ll, sx0: float, sy0: float, *,
                    rounds: int = 50, offset_bound: float = 5e-4) -> tuple[Params, float]:
    """Refine with the anchor pixel pinned to a known lon/lat (e.g. the dropped pin).

    Free params are scale (sx, sy); the translation is *derived* from the anchor each step, so scaling
    can't slide the fit onto a parallel road. A final small bounded offset (≤ offset_bound deg) absorbs
    anchor imprecision without allowing a full-block shift.
    """
    ax, ay = anchor_px
    alon, alat = anchor_ll

    def make(sx, sy, dlon=0.0, dlat=0.0) -> Params:
        return (alon - ax * sx + dlon, sx, alat - ay * sy + dlat, sy)

    sx, sy, dlon, dlat = sx0, sy0, 0.0, 0.0
    best = _score(make(sx, sy), red_px, index)
    step = [abs(sx0) * 0.12, abs(sy0) * 0.12, 4e-4, 4e-4]
    for _ in range(rounds):
        improved = False
        for k in range(4):
            for d in (step[k], -step[k]):
                cand = [sx, sy, dlon, dlat]
                cand[k] += d
                if k >= 2 and abs(cand[k]) > offset_bound:
                    continue
                sc = _score(make(*cand), red_px, index)
                if sc < best - 1e-9:
                    sx, sy, dlon, dlat, best, improved = *cand, sc, True
                    break
        if not improved:
            step = [s * 0.5 for s in step]
            if step[0] < 1e-9:
                break
    return make(sx, sy, dlon, dlat), best


def refine(red_px, index, init: Params, *, rounds: int = 40, offset_bound: float | None = None) -> tuple[Params, float]:
    """Coordinate-descent the 4 affine params to minimise total drawn→road distance.

    The avenue grid is periodic, so the bare objective has near-equal optima one block apart. Pass
    ``offset_bound`` (degrees) to clamp the translation (lon0, lat0) near the seed — e.g. when the seed
    anchors a known point (the dropped pin) — so the fit can't slide onto a parallel road.
    """
    p = list(init)
    init0 = tuple(init)
    steps = [2.0e-3, abs(init[1]) * 0.10, 2.0e-3, abs(init[3]) * 0.10]
    best = _score(p, red_px, index)
    for _ in range(rounds):
        improved = False
        for k in range(4):
            for d in (steps[k], -steps[k]):
                cand = list(p); cand[k] += d
                if offset_bound is not None and k in (0, 2) and abs(cand[k] - init0[k]) > offset_bound:
                    continue
                sc = _score(cand, red_px, index)
                if sc < best - 1e-9:
                    p, best, improved = cand, sc, True
                    break
        if not improved:
            steps = [st * 0.5 for st in steps]
            if steps[0] < 3e-6:
                break
    return tuple(p), best
