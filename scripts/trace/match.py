"""Map-match the georeferenced drawn pixels to OSM ways.

For each way we measure how much of its length runs under the drawn line (within ``tol`` metres).
Roads with enough covered length ARE the drawn route — loop and interior alike. Returns them ranked,
so the caller can assemble the ordered road list that feeds gps-extraction.
"""

from __future__ import annotations

import math

from scripts.trace.georef import haversine_m

LAT_M = 111_132.0


def build_drawn_index(drawn_lonlat, *, cell_m: float = 22.0, lat0: float = 39.79):
    dlat = cell_m / LAT_M
    dlon = cell_m / (LAT_M * math.cos(math.radians(lat0)))
    grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for lon, lat in drawn_lonlat:
        grid.setdefault((int(lon / dlon), int(lat / dlat)), []).append((lon, lat))
    return grid, dlon, dlat


def _near_drawn(index, lon: float, lat: float, tol: float) -> bool:
    grid, dlon, dlat = index
    ci, cj = int(lon / dlon), int(lat / dlat)
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            for plon, plat in grid.get((ci + di, cj + dj), ()):
                if haversine_m((lon, lat), (plon, plat)) < tol:
                    return True
    return False


def road_coverage(way, index, *, tol: float = 22.0, sample_m: float = 8.0) -> tuple[float, float]:
    """Return (covered_metres, total_metres) of a way under the drawn line."""
    g = way["geom"]
    covered = total = 0.0
    for i in range(len(g) - 1):
        a, b = g[i], g[i + 1]
        d = haversine_m(a, b)
        steps = max(1, int(d / sample_m))
        seglen = d / steps
        for t in range(steps):
            f = (t + 0.5) / steps
            lon, lat = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
            total += seglen
            if _near_drawn(index, lon, lat, tol):
                covered += seglen
    return covered, total


def match_roads(ways, drawn_lonlat, *, tol: float = 22.0, min_covered_m: float = 60.0,
                min_fraction: float = 0.30) -> list[dict]:
    """Rank ways the drawn line runs along: covered length ≥ min_covered_m and fraction ≥ min_fraction."""
    index = build_drawn_index(drawn_lonlat, cell_m=tol)
    hits = []
    for way in ways:
        covered, total = road_coverage(way, index, tol=tol)
        if total <= 0:
            continue
        frac = covered / total
        if covered >= min_covered_m and frac >= min_fraction:
            hits.append({"name": way["name"], "highway": way["highway"],
                         "covered_m": round(covered), "total_m": round(total),
                         "fraction": round(frac, 2), "geom": way["geom"]})
    hits.sort(key=lambda h: -h["covered_m"])
    return hits
