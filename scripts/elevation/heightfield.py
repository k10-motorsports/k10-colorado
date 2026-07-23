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


def snap_to_ground(z: list[float], *, spacing_m: float, grade_cap_pct: float,
                   median_m: float = MEDIAN_WINDOW_M) -> list[float]:
    """ROAD TOUCHING GROUND TAKES PRECEDENCE (Kevin). The old profile (median + wide double mean)
    floated ~1 m over every real dip — the DEM measured the actual road surface, and smoothing away
    its contact with the ground is what left the deck hovering with the world shaped 1 m beneath it.

    New contract: despike (median — kills canopy/underpass notches), ONE light 9 m mean for DEM
    surface noise, then a grade limiter that only CUTS: forward+backward min-clamp at the grade cap
    pulls crests DOWN into the hill (a cut, like real construction) and never lifts the line off
    the ground. Result is at-or-below the despiked terrain everywhere: floats are impossible by
    construction; drivability is the drive test's job to confirm."""
    n = len(z)
    if n == 0:
        return []
    mw = max(3, round(median_m / spacing_m)) | 1
    h = mw // 2
    out = [sorted(z[(i + k) % n] for k in range(-h, h + 1))[h] for i in range(n)]
    out = smooth_closed(out, max(3, round(9.0 / spacing_m)) | 1)
    step = grade_cap_pct / 100.0 * spacing_m
    for _ in range(2):                     # closed loop: two wraps converge the clamp
        for i in range(1, n):
            out[i] = min(out[i], out[i - 1] + step)
        for i in range(n - 2, -1, -1):
            out[i] = min(out[i], out[i + 1] + step)
        out[0] = min(out[0], out[-1] + step)
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

CORRIDOR_HALF_M = 60.0    # lateral reach each side of the centerline
CORRIDOR_STEP_S = 6.0     # along-road station step
CORRIDOR_STEP_L = 5.0     # lateral step


def fetch_corridor(coords, project_dir) -> None:
    """Sample 3DEP at NEAR-NATIVE resolution in a corridor around the centerline; cache to
    data/corridor.elev.json.

    THE LARIAT LESSON (rc6): a 40 m area grid cannot contain an 8 m bench cut — the deck rode
    2.35 m median above a mountainside that was smooth only because we sampled it blurry (Kevin's
    photos: sunlight under the deck, trees below the pavement, the road a wall seen from the
    grass). The lidar HAS the bench; this keeps it."""
    import json as _j
    import math as _m
    out_p = Path(project_dir) / "data" / "corridor.elev.json"
    n = len(coords)
    if out_p.exists():
        try:
            c = _j.loads(out_p.read_text())
            if c.get("n_src") == n:
                print(f"[corridor] cache hit ({len(c['stations_lonlat'])} stations)")
                return
        except Exception:
            pass
    from scripts.elevation import usgs_3dep
    lat0 = coords[0][1]
    m_lon = 111_320.0 * _m.cos(_m.radians(lat0))
    m_lat = 110_540.0
    st_pts = [tuple(coords[0])]
    acc = 0.0
    for i2 in range(1, n):
        dx = (coords[i2][0] - coords[i2 - 1][0]) * m_lon
        dz = (coords[i2][1] - coords[i2 - 1][1]) * m_lat
        acc += _m.hypot(dx, dz)
        if acc >= CORRIDOR_STEP_S:
            st_pts.append(tuple(coords[i2]))
            acc = 0.0
    offs = []
    o = -CORRIDOR_HALF_M
    while o <= CORRIDOR_HALF_M + 1e-6:
        offs.append(round(o, 3))
        o += CORRIDOR_STEP_L
    pts = []
    for i2, (lon, lat) in enumerate(st_pts):
        j2 = min(i2 + 1, len(st_pts) - 1)
        k2 = max(i2 - 1, 0)
        tx = (st_pts[j2][0] - st_pts[k2][0]) * m_lon
        tz = (st_pts[j2][1] - st_pts[k2][1]) * m_lat
        L = _m.hypot(tx, tz) or 1.0
        nx, nz = -tz / L, tx / L
        for off in offs:
            pts.append((lon + nx * off / m_lon, lat + nz * off / m_lat))
    print(f"[corridor] sampling {len(pts)} pts ({len(st_pts)} stations x {len(offs)} offsets) at 3DEP native...")
    z = usgs_3dep.sample_points(pts)
    field = [z[i2 * len(offs):(i2 + 1) * len(offs)] for i2 in range(len(st_pts))]
    out_p.write_text(_j.dumps({"n_src": n, "step_s": CORRIDOR_STEP_S, "step_l": CORRIDOR_STEP_L,
                               "half_l": CORRIDOR_HALF_M, "stations_lonlat": st_pts,
                               "offsets": offs, "z": field}))
    print(f"[corridor] cached -> {out_p}")


def build(project_dir: str | Path) -> dict:
    """Phase 2: sample 3DEP along the centerline, smooth, emit elevation json + heightfield + profile."""
    project_dir = Path(project_dir)
    data = project_dir / "data"
    gj = json.loads((data / "centerline.geojson").read_text(encoding="utf-8"))
    full = next(f for f in gj["features"] if f["properties"].get("kind") == "full")
    coords = [(lon, lat) for lon, lat in full["geometry"]["coordinates"]]

    z_raw = usgs_3dep.sample_points(coords)
    import json as _json
    _cfgj = _json.loads((Path(project_dir) / "track.config.json").read_text())
    _cap_pct = float((_cfgj.get("road_profile", {}) or {}).get("max_grade_pct", 9.0))
    z_smooth = snap_to_ground(z_raw, spacing_m=CENTERLINE_SPACING_M, grade_cap_pct=_cap_pct)
    fetch_corridor(coords, project_dir)   # near-native corridor terrain (the rc6 Lariat lesson)

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
