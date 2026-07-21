"""Project lat/lon/elev -> local meters about an origin, in AC's Y-up frame.

We build in a local ENU tangent plane centered on the centerline centroid:
  X = east (m),  Y = up (m, height above the track's lowest point),  Z = north (m).
This frame is true-north-aligned by construction, so ``true_north_rotation_deg = 0`` (the kn5 export
handles any AC axis flip later). The origin (lon0, lat0, elev0) is written to data/ so every later
phase projects identically. Metres-per-degree use the standard WGS84 series (sub-metre over a few km).

Run:  python -m scripts.geometry.projection projects/sand-creek-raceway
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from scripts.config import load_config

Vertex = tuple[float, float]


def centroid(points: list[Vertex]) -> Vertex:
    """Mean lon/lat of the centerline — the default projection origin."""
    n = len(points)
    return (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n)


def _meters_per_degree(lat0: float) -> tuple[float, float]:
    """(m_per_deg_lon, m_per_deg_lat) at latitude lat0 — WGS84 series, accurate to <1 m."""
    phi = math.radians(lat0)
    m_lat = 111132.954 - 559.822 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi)
    m_lon = 111412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi) + 0.118 * math.cos(5 * phi)
    return m_lon, m_lat


def project_to_local(
    lonlat: list[Vertex],
    z: list[float],
    origin: Vertex,
    elev0: float,
) -> list[tuple[float, float, float]]:
    """Project (lon, lat) + elevation to local ENU meters about ``origin`` (X-east, Y-up, Z-north)."""
    lon0, lat0 = origin
    m_lon, m_lat = _meters_per_degree(lat0)
    return [((lon - lon0) * m_lon, elev - elev0, (lat - lat0) * m_lat)
            for (lon, lat), elev in zip(lonlat, z)]


def build(project_dir: str | Path) -> dict:
    """Phase 3: project centerline (xy from Phase 1 + z from Phase 2) to local meters; set origin/north."""
    project_dir = Path(project_dir)
    data = project_dir / "data"
    cfg = load_config(project_dir)

    gj = json.loads((data / "centerline.geojson").read_text(encoding="utf-8"))
    full = next(f for f in gj["features"] if f["properties"].get("kind") == "full")
    coords = [(lon, lat) for lon, lat in full["geometry"]["coordinates"]]
    widths = full["properties"].get("widths_m") or [cfg.default_width_m] * len(coords)

    elev = json.loads((data / "centerline.elevation.json").read_text(encoding="utf-8"))
    z = elev["z_smooth_m"]

    # ROUTE ORIENTATION — applied here (not just in the OSM gps stage) so EVERY source type
    # (aerial trace, KML, GPS drive) gets it, and the parallel widths/elevation arrays stay aligned.
    #   route.reverse  = true       -> run the lap in the opposite direction (the REAL race direction;
    #                                  practicing a circuit backwards is worthless).
    #   route.start_at = [lat, lon] -> rotate the ring so station 0 (start/finish, pits, hotlap spawn)
    #                                  sits at the real start line.
    route = cfg.raw.get("route", {}) or {}
    if route.get("reverse") or route.get("start_at"):
        was_closed = len(coords) > 2 and coords[0] == coords[-1]
        if was_closed:
            coords, widths, z = coords[:-1], widths[:-1], z[:-1]
        if route.get("reverse"):
            coords, widths, z = coords[::-1], widths[::-1], z[::-1]
            print("  [projection] route.reverse: lap direction flipped")
        sa = route.get("start_at")
        if sa and cfg.loop and len(coords) > 2:
            m_lon, m_lat = _meters_per_degree(float(sa[0]))
            k = min(range(len(coords)),
                    key=lambda i: ((coords[i][0] - float(sa[1])) * m_lon) ** 2
                                + ((coords[i][1] - float(sa[0])) * m_lat) ** 2)
            if k:
                coords, widths, z = coords[k:] + coords[:k], widths[k:] + widths[:k], z[k:] + z[:k]
                print(f"  [projection] route.start_at: ring rotated by {k} points to the real start line")
        if was_closed:
            coords, widths, z = coords + [coords[0]], widths + [widths[0]], z + [z[0]]

    # origin: default = centroid of THIS centerline; but config.origin may pin a SHARED origin (a
    # {lon,lat,elev_m} dict) so a detailed track builds in another map's frame (K10 merge) — its mesh
    # then lands directly in that frame, ready to import into one shared kn5 with no post-translation.
    origin_cfg = cfg.raw.get("origin", "centroid")
    if isinstance(origin_cfg, dict) and "lon" in origin_cfg and "lat" in origin_cfg:
        origin = (float(origin_cfg["lon"]), float(origin_cfg["lat"]))
        elev0 = float(origin_cfg.get("elev_m", min(z)))
    else:
        origin = centroid(coords)
        elev0 = min(z)
    local = project_to_local(coords, z, origin, elev0)

    xs = [p[0] for p in local]
    ys = [p[1] for p in local]
    zs = [p[2] for p in local]
    out = {
        "frame": "ENU local meters (X=east, Y=up, Z=north)",
        "origin": {"lon": origin[0], "lat": origin[1], "elev_m": round(elev0, 2)},
        "true_north_rotation_deg": 0.0,
        "extent_x_m": round(max(xs) - min(xs), 1),
        "extent_z_m": round(max(zs) - min(zs), 1),
        "height_m": round(max(ys) - min(ys), 1),
        "point_count": len(local),
        "points_xyz_m": [[round(x, 2), round(y, 2), round(zz, 2)] for x, y, zz in local],
        "widths_m": widths,
    }
    (data / "centerline.local.json").write_text(json.dumps(out), encoding="utf-8")

    # Persist the resolved true north back to the source of truth — but only when it has never been
    # resolved. The lighting stage (csp_config.resolve_true_north) owns the real value (180 on
    # mirror_x tracks); a re-run of projection must not clobber it back to 0 or the exported sun
    # sets in the east.
    if cfg.raw.get("true_north_rotation_deg") is None:
        cfg.write_back(true_north_rotation_deg=0.0)

    write_plan_svg(local, ys, data / "plan_view.svg")
    return {k: out[k] for k in ("extent_x_m", "extent_z_m", "height_m", "point_count")} | {
        "origin_lonlat": [round(origin[0], 6), round(origin[1], 6)], "true_north_deg": 0.0}


def _elev_color(t: float) -> str:
    """t in [0,1] -> blue(low) → green → red(high)."""
    if t < 0.5:
        u = t / 0.5
        r, g, b = int(40 + u * 20), int(120 + u * 135), int(220 - u * 120)
    else:
        u = (t - 0.5) / 0.5
        r, g, b = int(60 + u * 195), int(255 - u * 165), int(100 - u * 60)
    return f"#{r:02x}{g:02x}{b:02x}"


def write_plan_svg(local: list[tuple[float, float, float]], ys: list[float], path: Path, size: int = 900) -> None:
    """Top-down plan (X east →, Z north ↑) with each segment colored by elevation."""
    xs = [p[0] for p in local]
    zs = [p[2] for p in local]
    minx, maxx, minz, maxz = min(xs), max(xs), min(zs), max(zs)
    miny, maxy = min(ys), max(ys)
    yr = (maxy - miny) or 1.0
    pad = 30
    scale = (size - 2 * pad) / max(maxx - minx, maxz - minz)
    W = (maxx - minx) * scale + 2 * pad
    H = (maxz - minz) * scale + 2 * pad

    def proj(p: tuple[float, float, float]) -> tuple[float, float]:
        x = pad + (p[0] - minx) * scale
        y = H - pad - (p[2] - minz) * scale  # north up
        return round(x, 1), round(y, 1)

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" '
             f'viewBox="0 0 {W:.0f} {H:.0f}"><rect width="100%" height="100%" fill="#10151b"/>']
    pts = [proj(p) for p in local]
    for i in range(len(pts) - 1):
        (x1, y1), (x2, y2) = pts[i], pts[i + 1]
        col = _elev_color((ys[i] - miny) / yr)
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{col}" stroke-width="3"/>')
    sx, sy = pts[0]
    parts.append(f'<circle cx="{sx}" cy="{sy}" r="5" fill="#ffffff"/>')
    # 500 m scale bar
    bar = 500 * scale
    parts.append(f'<line x1="{pad}" y1="{H - 12}" x2="{pad + bar}" y2="{H - 12}" stroke="#cdd6e0" stroke-width="2"/>')
    parts.append(f'<text x="{pad}" y="{H - 16}" fill="#cdd6e0" font-size="11" font-family="monospace">500 m</text>')
    parts.append("</svg>")
    path.write_text("".join(parts), encoding="utf-8")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m scripts.geometry.projection <project-dir>")
    stats = build(sys.argv[1])
    print("wrote data/centerline.local.json, plan_view.svg; set true_north_rotation_deg in config")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
