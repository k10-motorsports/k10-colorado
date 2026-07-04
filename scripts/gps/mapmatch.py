"""Map-match a recorded GPS drive onto OSM road geometry → a clean centerline.

The quality upgrade over from_track's raw-GPS centerline (which wobbles ±several m off the road): snap the
drive onto the real OSM road network and stitch the actual OSM way geometry the drive traversed, in drive
order. The output follows true road centerlines (like the OSM-route path Sand Creek used) but the route —
which roads, in what order, where it leaves the network — comes from where you actually drove, not a
hand-picked list.

Algorithm:
  1. fetch every drivable OSM way in the track bbox (overpass.fetch_drivable_ways),
  2. snap each GPS fix to the nearest way segment (vectorised), recording (way, arc-along-way, distance),
  3. clean the per-fix way assignment (drop sub-``min_run`` blips from GPS jitter / parallel roads),
  4. for each run on a way, cut the OSM polyline between the entry and exit projections in travel order,
  5. stitch the cut portions (fixes with no road within ``max_snap_m`` keep their raw GPS — off-network
     stretches like lots/private drives survive),
  6. resample + width-tag + close the loop → data/centerline.geojson (build_centerline's format).

Needs numpy for the snap. Writes the same artifacts as scripts.gps.centerline / from_track, so the rest of
the pipeline (elev → project → mesh → env → build) runs unchanged. See [[companion-app-plan]].

    python -m scripts.gps.mapmatch <project_dir> <track.json> [more...] [--loop] [--width 8] [--snap 30]
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

from scripts.gps import overpass
from scripts.gps.centerline import RESAMPLE_SPACING_M, _feature, polyline_length, resample, write_preview_svg
from scripts.gps.from_track import _load, _smooth

Vertex = tuple[float, float]


def _arcs(coords_xy: list[tuple[float, float]]) -> list[float]:
    arc = [0.0]
    for i in range(1, len(coords_xy)):
        arc.append(arc[-1] + math.hypot(coords_xy[i][0] - coords_xy[i - 1][0], coords_xy[i][1] - coords_xy[i - 1][1]))
    return arc


def _interp_lonlat(geom: list[Vertex], arc: list[float], at: float) -> Vertex:
    """Point (lon,lat) at arc-length ``at`` along an OSM way."""
    at = max(arc[0], min(arc[-1], at))
    for i in range(len(arc) - 1):
        if arc[i] <= at <= arc[i + 1]:
            t = (at - arc[i]) / max(arc[i + 1] - arc[i], 1e-9)
            return (geom[i][0] + (geom[i + 1][0] - geom[i][0]) * t,
                    geom[i][1] + (geom[i + 1][1] - geom[i][1]) * t)
    return geom[-1]


def _portion(geom: list[Vertex], arc: list[float], a0: float, a1: float) -> list[Vertex]:
    """OSM way nodes (lon,lat) between arc lengths a0→a1, endpoints interpolated, in travel direction."""
    fwd = a1 >= a0
    lo, hi = (a0, a1) if fwd else (a1, a0)
    out = [_interp_lonlat(geom, arc, lo)]
    out += [geom[i] for i in range(len(arc)) if lo < arc[i] < hi]
    out.append(_interp_lonlat(geom, arc, hi))
    return out if fwd else out[::-1]


def map_match(gps: list[Vertex], ways: list[dict], *, max_snap_m: float = 30.0, min_run: int = 4):
    """Snap ``gps`` (lon,lat) onto ``ways`` → stitched centerline (lon,lat) following OSM geometry.
    Returns (centerline, stats)."""
    if not ways:
        return gps, {"matched_pct": 0.0, "note": "no OSM ways fetched"}
    lat0 = sum(p[1] for p in gps) / len(gps)
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 110540.0
    lon0 = gps[0][0]

    def loc(lon, lat):
        return ((lon - lon0) * kx, (lat - lat0) * ky)

    # flat segment arrays + per-way arc tables (in metres)
    sax, say, sbx, sby, sway, sarc, slen = [], [], [], [], [], [], []
    way_xy, way_arc = [], []
    for wi, w in enumerate(ways):
        xy = [loc(lon, lat) for lon, lat in w["geom"]]
        arc = _arcs(xy)
        way_xy.append(xy); way_arc.append(arc)
        for i in range(len(xy) - 1):
            sax.append(xy[i][0]); say.append(xy[i][1]); sbx.append(xy[i + 1][0]); sby.append(xy[i + 1][1])
            sway.append(wi); sarc.append(arc[i]); slen.append(arc[i + 1] - arc[i])
    sax = np.array(sax); say = np.array(say); sbx = np.array(sbx); sby = np.array(sby)
    sway = np.array(sway); sarc = np.array(sarc); slen = np.array(slen)
    dx = sbx - sax; dy = sby - say
    L2 = np.maximum(dx * dx + dy * dy, 1e-9)

    matched = []  # per gps fix: (way_idx or -1, arc_along_way, lon, lat)
    for lon, lat in gps:
        px, py = loc(lon, lat)
        t = np.clip(((px - sax) * dx + (py - say) * dy) / L2, 0.0, 1.0)
        cx = sax + t * dx; cy = say + t * dy
        d = np.hypot(px - cx, py - cy)
        k = int(np.argmin(d))
        if d[k] <= max_snap_m:
            matched.append((int(sway[k]), float(sarc[k] + t[k] * slen[k]), lon, lat))
        else:
            matched.append((-1, 0.0, lon, lat))

    # clean: drop runs shorter than min_run by merging into the longer neighbour (kills jitter to a
    # parallel road / brief mismatches), so each kept run is a real traversal of one way
    runs = []  # [way_idx, start, end]
    for i, m in enumerate(matched):
        if runs and runs[-1][0] == m[0]:
            runs[-1][2] = i
        else:
            runs.append([m[0], i, i])
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for r in range(len(runs)):
            if runs[r][2] - runs[r][1] + 1 < min_run and len(runs) > 1:
                nbrs = [runs[r - 1]] if r > 0 else []
                nbrs += [runs[r + 1]] if r < len(runs) - 1 else []
                big = max(nbrs, key=lambda x: x[2] - x[1])
                runs[r][0] = big[0]
                changed = True
        # coalesce adjacent same-way runs
        merged = [runs[0]]
        for r in runs[1:]:
            if r[0] == merged[-1][0]:
                merged[-1][2] = r[2]
            else:
                merged.append(r)
        runs = merged

    centerline: list[Vertex] = []
    n_matched = 0
    for wi, a, b in runs:
        if wi < 0:  # off-network stretch: keep the raw GPS
            centerline += [(matched[i][2], matched[i][3]) for i in range(a, b + 1)]
            continue
        n_matched += b - a + 1
        centerline += _portion(ways[wi]["geom"], way_arc[wi], matched[a][1], matched[b][1])

    # de-dupe near-duplicate consecutive points
    out: list[Vertex] = []
    for p in centerline:
        if not out or abs(p[0] - out[-1][0]) + abs(p[1] - out[-1][1]) > 1e-7:
            out.append(p)
    stats = {"matched_pct": round(100 * n_matched / max(len(gps), 1), 1),
             "ways_used": len({r[0] for r in runs if r[0] >= 0}), "osm_ways_fetched": len(ways)}
    return out, stats


def build_mapmatched(project_dir: str | Path, track_paths: list[str], *,
                     loop: bool = False, width_m: float = 8.0, max_snap_m: float = 30.0,
                     pad_deg: float = 0.003) -> tuple[Path, dict]:
    project_dir = Path(project_dir)
    fixes = _load(track_paths)
    if len(fixes) < 10:
        raise SystemExit(f"only {len(fixes)} usable fixes — need a longer track")
    gps = _smooth([(lon, lat) for _t, lat, lon in fixes], 3)   # light pre-smooth before snapping
    lats = [p[1] for p in gps]; lons = [p[0] for p in gps]
    bbox = (min(lats) - pad_deg, min(lons) - pad_deg, max(lats) + pad_deg, max(lons) + pad_deg)
    ways = overpass.fetch_drivable_ways(bbox)
    ring, mstats = map_match(gps, ways, max_snap_m=max_snap_m)
    if loop and ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    ring = resample(ring, RESAMPLE_SPACING_M)
    widths = [width_m] * len(ring)
    length_m = polyline_length(ring)
    feat = _feature("centerline", "full", ring,
                    {"closed": loop, "default_width_m": width_m,
                     "widths_m": [round(w, 2) for w in widths], "length_m": round(length_m, 1),
                     "point_count": len(ring), "roads": ["<map-matched from GPS drive>"]})
    out = project_dir / "data" / "centerline.geojson"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"type": "FeatureCollection", "features": [feat]}), encoding="utf-8")
    write_preview_svg(ring, {}, project_dir / "data" / "centerline_preview.svg")
    stats = {"length_km": round(length_m / 1000, 2), "points": len(ring), "closed": loop,
             "source_fixes": len(fixes), **mstats}
    return out, stats


def main() -> None:
    args = sys.argv[1:]
    loop = "--loop" in args
    if loop:
        args.remove("--loop")
    width, snap = 8.0, 30.0
    if "--width" in args:
        i = args.index("--width"); width = float(args[i + 1]); del args[i:i + 2]
    if "--snap" in args:
        i = args.index("--snap"); snap = float(args[i + 1]); del args[i:i + 2]
    if len(args) < 2:
        raise SystemExit("usage: python -m scripts.gps.mapmatch <project_dir> <track.json> [more...] [--loop] [--width M] [--snap M]")
    out, stats = build_mapmatched(args[0], args[1:], loop=loop, width_m=width, max_snap_m=snap)
    print(f"wrote {out}")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
