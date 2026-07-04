"""Sample elevation along the centerline (+ a margin grid), smooth, and emit the heightfield.

Smooth aggressively ALONG the racing line to avoid stepping; keep the margin grid natural for grass.
Outputs (data/): centerline.elevation.json (per-vertex z), heightfield.npy + heightfield.meta.json,
elevation_profile.svg. Pure stdlib (the .npy is written by hand).

Run:  python -m scripts.elevation.heightfield projects/sand-creek-raceway
"""

from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path

from scripts.elevation import usgs_3dep
from scripts.gps.centerline import haversine

Vertex = tuple[float, float]

CENTERLINE_SPACING_M = 3.0   # Phase 1 resample spacing
MEDIAN_WINDOW_M = 33.0       # median de-spike window (removes DEM notches: underpasses, bridges, trees)
SMOOTH_WINDOW_M = 39.0       # mean smoothing window — narrow on purpose: removes ±0.2 m DEM surface
SMOOTH_PASSES = 2            # noise but PRESERVES real drops (a wider window flattens them; see notes)
HF_SPACING_M = 40.0          # terrain grid resolution
HF_MARGIN_M = 150.0          # grid margin around the loop


# --- smoothing -----------------------------------------------------------------

def smooth_closed(z: list[float], window: int) -> list[float]:
    """Centered moving average with wrap-around (the racing line is a closed loop)."""
    n = len(z)
    if n == 0:
        return []
    half = window // 2
    return [sum(z[(i + k) % n] for k in range(-half, half + 1)) / (2 * half + 1) for i in range(n)]


def smooth_open(z: list[float], window: int) -> list[float]:
    """Centered moving average, clamped at the ends (for open connectors)."""
    n = len(z)
    half = window // 2
    out = []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        out.append(sum(z[lo:hi]) / (hi - lo))
    return out


def smooth_profile(z: list[float], *, spacing_m: float, median_m: float = MEDIAN_WINDOW_M,
                   mean_m: float = SMOOTH_WINDOW_M, passes: int = SMOOTH_PASSES) -> list[float]:
    """Turn raw sampled elevations into a launch-free racing-line profile (closed loop).

    A plain moving average *smears* a sharp DEM artifact (e.g. the elevation notch where the route
    crosses under I-70, or a bridge/tree return) into a residual bump that still kicks the car. So
    first a **median** filter to delete those spikes outright (it removes V-notches but preserves
    real *steps* — a genuine road descent survives it intact), then a **narrow** mean to scrub the
    ±0.2 m DEM surface noise.

    Window choice matters: the mean window must stay small. A wide one (≈90 m) spreads a real tight
    drop over ~180 m and guts it (Sand Creek Drive's ~7 m drop fell to ~25%); ~39 m removes noise yet
    keeps such drops at ~90–100% of their depth, at a drivable kink (~3 pts/3 m, no launch). The I-70
    notch still dies because the median already deleted it. Don't widen this to chase a lower kink —
    you'll flatten the features that make the track fun."""
    n = len(z)
    if n == 0:
        return []
    mw = max(3, round(median_m / spacing_m)) | 1  # odd
    sw = max(3, round(mean_m / spacing_m)) | 1
    h = mw // 2
    out = [sorted(z[(i + k) % n] for k in range(-h, h + 1))[h] for i in range(n)]  # median de-spike
    for _ in range(passes):
        out = smooth_closed(out, sw)  # mean passes
    return out


# --- pure-python .npy writer (float64, C-order, 2D) ----------------------------

def write_npy(path: Path, grid: list[list[float]]) -> None:
    ny = len(grid)
    nx = len(grid[0]) if ny else 0
    header = "{'descr': '<f8', 'fortran_order': False, 'shape': (%d, %d), }" % (ny, nx)
    magic = b"\x93NUMPY\x01\x00"
    total = len(magic) + 2 + len(header) + 1
    header += " " * ((64 - total % 64) % 64) + "\n"
    buf = bytearray(magic + struct.pack("<H", len(header)) + header.encode("latin1"))
    for row in grid:
        for v in row:
            buf += struct.pack("<d", float(v))
    path.write_bytes(bytes(buf))


# --- grid ----------------------------------------------------------------------

def build_heightfield(coords: list[Vertex], spacing_m: float, margin_m: float) -> tuple[list[list[float]], dict]:
    """Sample a regular terrain grid over the centerline bbox + margin (row 0 = north)."""
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    midlat = (min(lats) + max(lats)) / 2
    mlat = margin_m / 111_000.0
    mlon = margin_m / (111_000.0 * math.cos(math.radians(midlat)))
    s, w, n, e = min(lats) - mlat, min(lons) - mlon, max(lats) + mlat, max(lons) + mlon
    gy = spacing_m / 111_000.0
    gx = spacing_m / (111_000.0 * math.cos(math.radians(midlat)))
    ny = int((n - s) / gy) + 1
    nx = int((e - w) / gx) + 1
    points = [(w + i * gx, n - j * gy) for j in range(ny) for i in range(nx)]
    flat = usgs_3dep.sample_points(points)
    grid = [flat[j * nx:(j + 1) * nx] for j in range(ny)]
    meta = {"bbox_swne": [round(s, 6), round(w, 6), round(n, 6), round(e, 6)],
            "nx": nx, "ny": ny, "spacing_m": spacing_m, "margin_m": margin_m, "row0": "north"}
    return grid, meta


# --- orchestration -------------------------------------------------------------

def build(project_dir: str | Path) -> dict:
    """Phase 2: sample 3DEP along the centerline, smooth, emit elevation json + heightfield + profile."""
    project_dir = Path(project_dir)
    data = project_dir / "data"
    gj = json.loads((data / "centerline.geojson").read_text(encoding="utf-8"))
    full = next(f for f in gj["features"] if f["properties"].get("kind") == "full")
    coords = [(lon, lat) for lon, lat in full["geometry"]["coordinates"]]

    z_raw = usgs_3dep.sample_points(coords)
    z_smooth = smooth_profile(z_raw, spacing_m=CENTERLINE_SPACING_M)

    dist = [0.0]
    for i in range(1, len(coords)):
        dist.append(dist[-1] + haversine(coords[i - 1], coords[i]))

    grades = [abs(z_smooth[i] - z_smooth[i - 1]) / max(1e-6, haversine(coords[i - 1], coords[i])) * 100
              for i in range(1, len(coords))]
    climb = sum(max(0.0, z_smooth[i] - z_smooth[i - 1]) for i in range(1, len(z_smooth)))
    stats = {
        "min_m": round(min(z_smooth), 1), "max_m": round(max(z_smooth), 1),
        "range_m": round(max(z_smooth) - min(z_smooth), 1),
        "total_climb_m": round(climb, 1), "max_grade_pct": round(max(grades), 1),
        "mean_grade_pct": round(sum(grades) / len(grades), 2), "lap_m": round(dist[-1], 1),
    }
    (data / "centerline.elevation.json").write_text(json.dumps({
        "spacing_m": CENTERLINE_SPACING_M, "median_window_m": MEDIAN_WINDOW_M,
        "smooth_window_m": SMOOTH_WINDOW_M, "smooth_passes": SMOOTH_PASSES, "point_count": len(coords),
        "distance_m": [round(x, 1) for x in dist],
        "z_raw_m": [round(v, 2) for v in z_raw], "z_smooth_m": [round(v, 2) for v in z_smooth],
        "stats": stats,
    }), encoding="utf-8")

    # Widen the sampled terrain to cover any declared connector streets (route.connectors_kml), so a
    # branch road that leaves the loop's own bbox still has real ground under it. Connector geometry only
    # extends the grid EXTENT here; the along-lap elevation profile above stays centerline-only.
    bbox_coords = list(coords)
    try:
        from scripts.gps.connectors_from_kml import _load_kml_text, _placemark_line
        from scripts.config import load_config
        cfg = load_config(project_dir)
        for spec in cfg.raw.get("route", {}).get("connectors_kml", []):
            kml_path = (project_dir / spec["kml"]).resolve()
            if kml_path.exists():
                line = _placemark_line(kml_path, spec["match"])
                bbox_coords.extend(line)
                if line:
                    print(f"[heightfield] +connector extent '{spec['name']}' ({len(line)} pts)")
    except Exception as e:
        print(f"[heightfield] connector-extent widening skipped: {e}")

    grid, meta = build_heightfield(bbox_coords, HF_SPACING_M, HF_MARGIN_M)
    write_npy(data / "heightfield.npy", grid)
    (data / "heightfield.meta.json").write_text(json.dumps(meta), encoding="utf-8")

    write_profile_svg(dist, z_raw, z_smooth, data / "elevation_profile.svg")
    return {**stats, "heightfield": f"{meta['nx']}x{meta['ny']} @ {HF_SPACING_M}m"}


def write_profile_svg(dist: list[float], z_raw: list[float], z_smooth: list[float], path: Path,
                      width: int = 1000, height: int = 280) -> None:
    pad = 40
    dmax = dist[-1] or 1.0
    zlo, zhi = min(z_raw), max(z_raw)
    zr = (zhi - zlo) or 1.0

    def pt(d: float, z: float) -> tuple[float, float]:
        x = pad + d / dmax * (width - 2 * pad)
        y = height - pad - (z - zlo) / zr * (height - 2 * pad)
        return round(x, 1), round(y, 1)

    def poly(zs: list[float]) -> str:
        return " ".join(("M" if i == 0 else "L") + f"{x},{y}" for i, (x, y) in
                        enumerate(pt(dist[i], zs[i]) for i in range(len(zs))))

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="#10151b"/>']
    for gz in range(int(zlo // 5 * 5), int(zhi) + 5, 5):  # 5 m gridlines
        _, y = pt(0, gz)
        parts.append(f'<line x1="{pad}" y1="{y}" x2="{width - pad}" y2="{y}" stroke="#2a3340" stroke-width="1"/>')
        parts.append(f'<text x="4" y="{y + 4}" fill="#6b7785" font-size="11" font-family="monospace">{gz}m</text>')
    parts.append(f'<path d="{poly(z_raw)}" fill="none" stroke="#3a4a5a" stroke-width="1"/>')
    parts.append(f'<path d="{poly(z_smooth)}" fill="none" stroke="#ff3b30" stroke-width="2"/>')
    parts.append(f'<text x="{pad}" y="20" fill="#cdd6e0" font-size="13" font-family="monospace">'
                 f'Sand Creek Raceway — elevation along lap ({zlo:.0f}–{zhi:.0f} m over {dmax/1000:.2f} km)</text>')
    parts.append("</svg>")
    path.write_text("".join(parts), encoding="utf-8")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m scripts.elevation.heightfield <project-dir>")
    stats = build(sys.argv[1])
    print("wrote data/centerline.elevation.json, heightfield.npy (+meta), elevation_profile.svg")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
