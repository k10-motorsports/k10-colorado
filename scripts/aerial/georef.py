"""Pixel ↔ lon/lat for a north-up aerial screenshot, from two control points.

A Google-Maps-style satellite capture is north-up Web-Mercator and ~linear over a track's footprint,
so pixel→lon/lat is a north-up affine with independent axis scales (no rotation):
``lon = lon0 + x*sx``, ``lat = lat0 + y*sy`` (sy < 0, since pixel-y grows southward). Two control
points at opposite corners pin all four parameters exactly — the same exact-affine idea as
``route-tracing``'s two-pin path, reused here. Drop two Google Maps pins (``?q=lat,lon``) on
recognisable corners of the track and record their pixel positions; that's the whole georeference.

This real-world tie is what makes the trace elevation-correct: once the centerline is in true
lon/lat, the existing USGS 3DEP elevation phase samples real ground under it, no survey required.
"""

from __future__ import annotations

import math

Params = tuple[float, float, float, float]  # lon0, sx, lat0, sy
LAT_M = 111_132.0  # metres per degree latitude (mean)


def affine_from_corners(p1_px: tuple[float, float], p1_ll: tuple[float, float],
                        p2_px: tuple[float, float], p2_ll: tuple[float, float]) -> Params:
    """Exact north-up affine from two pixel↔(lon,lat) control points. Corners must differ in x and y."""
    (x1, y1), (lon1, lat1) = p1_px, p1_ll
    (x2, y2), (lon2, lat2) = p2_px, p2_ll
    if x1 == x2 or y1 == y2:
        raise ValueError("control points must differ in both x and y (use opposite corners)")
    sx = (lon2 - lon1) / (x2 - x1)
    sy = (lat2 - lat1) / (y2 - y1)
    return (lon1 - x1 * sx, sx, lat1 - y1 * sy, sy)


def to_lonlat(p: Params, x: float, y: float) -> tuple[float, float]:
    return p[0] + x * p[1], p[2] + y * p[3]


def to_px(p: Params, lon: float, lat: float) -> tuple[float, float]:
    """Inverse transform — lon/lat back to pixel coords (for drawing overlays)."""
    return (lon - p[0]) / p[1], (lat - p[2]) / p[3]


def meters_per_pixel(p: Params, lat0: float) -> tuple[float, float]:
    """(x, y) ground metres per pixel at latitude ``lat0`` — a sanity-check on the solved scale."""
    m_lon = LAT_M * math.cos(math.radians(lat0))
    return abs(p[1]) * m_lon, abs(p[3]) * LAT_M
