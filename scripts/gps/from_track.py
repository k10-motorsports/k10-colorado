"""Build a centerline straight from a recorded GPS drive — the "any drive → track" entry point.

This is the alternative to the OSM-route path in centerline.py: instead of picking road names from a
screenshot and stitching Overpass ways, take a real driven GPS track (dashcam via redtiger_gps, or the
Prodrive Scan phone app), smooth the jitter, optionally close the loop, and resample to the pipeline's
spacing. It writes ``data/centerline.geojson`` in the SAME format build_centerline emits, so every
downstream stage (elev → project → mesh → env → build) runs unchanged.

Quality note: a raw consumer-GPS centerline is wobblier than OSM geometry (±several m, smoothed here).
Map-matching the track to OSM ways is the quality upgrade and the natural next step; this is enough to
prototype a full track from a drive. Pure stdlib.

    python -m scripts.gps.from_track <project_dir> <track.json> [<track2.json> ...] [--loop] [--width 8]

Each ``track.json`` is a list of ``{"t","lat","lon",...}`` fixes (the redtiger_gps / dashcam_tracks
format). Multiple files are merged in time order (a drive split across 5-minute clips).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts.gps.centerline import (RESAMPLE_SPACING_M, _feature, haversine, polyline_length, resample,
                                    write_preview_svg)

Vertex = tuple[float, float]


def _load(paths: list[str]) -> list[tuple[str, float, float]]:
    """Merge fix lists from one or more dashcam/phone track JSONs, ordered by timestamp, de-duped."""
    fixes: list[tuple[str, float, float]] = []
    for p in paths:
        for f in json.loads(Path(p).read_text()):
            if f.get("lat") is not None and f.get("lon") is not None:
                fixes.append((f.get("t", ""), float(f["lat"]), float(f["lon"])))
    fixes.sort(key=lambda r: r[0])
    out: list[tuple[str, float, float]] = []
    for f in fixes:
        if not out or (abs(f[1] - out[-1][1]) + abs(f[2] - out[-1][2])) > 1e-7:
            out.append(f)
    return out


def _smooth(pts: list[Vertex], window: int = 5) -> list[Vertex]:
    """Moving-average smoother to tame consumer-GPS jitter (±several m at 1 Hz) without dropping turns —
    the window (~5 s ≈ a short arc at city speed) is small enough to keep corners."""
    if window < 2 or len(pts) < window:
        return pts
    h = window // 2
    out = []
    for i in range(len(pts)):
        lo, hi = max(0, i - h), min(len(pts), i + h + 1)
        seg = pts[lo:hi]
        out.append((sum(p[0] for p in seg) / len(seg), sum(p[1] for p in seg) / len(seg)))
    return out


def build_from_track(project_dir: str | Path, track_paths: list[str], *,
                     loop: bool = False, width_m: float = 8.0, smooth_window: int = 5) -> tuple[Path, dict]:
    """Drive GPS → resampled, optionally-closed centerline.geojson (pipeline format) + preview SVG."""
    project_dir = Path(project_dir)
    fixes = _load(track_paths)
    if len(fixes) < 10:
        raise SystemExit(f"only {len(fixes)} usable fixes — need a longer track")
    # (lon, lat) ring to match the geojson convention; smooth then close then resample
    ring: list[Vertex] = [(lon, lat) for _t, lat, lon in fixes]
    ring = _smooth(ring, smooth_window)
    close_gap = haversine(ring[0], ring[-1])
    if loop and ring[0] != ring[-1]:
        ring.append(ring[0])                      # bridge the start/end gap into a closed circuit
    ring = resample(ring, RESAMPLE_SPACING_M)
    widths = [width_m] * len(ring)
    length_m = polyline_length(ring)

    lons = [p[0] for p in ring]; lats = [p[1] for p in ring]
    bbox = [round(min(lats), 5), round(min(lons), 5), round(max(lats), 5), round(max(lons), 5)]
    feat = _feature("centerline", "full", ring,
                    {"closed": loop, "default_width_m": width_m,
                     "widths_m": [round(w, 2) for w in widths], "length_m": round(length_m, 1),
                     "point_count": len(ring), "roads": ["<from GPS drive>"]})
    out = project_dir / "data" / "centerline.geojson"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"type": "FeatureCollection", "features": [feat]}), encoding="utf-8")
    write_preview_svg(ring, {}, project_dir / "data" / "centerline_preview.svg")

    stats = {"length_km": round(length_m / 1000, 2), "points": len(ring), "closed": loop,
             "close_gap_m": round(close_gap), "bbox": bbox, "source_fixes": len(fixes)}
    return out, stats


def main() -> None:
    args = [a for a in sys.argv[1:]]
    loop = "--loop" in args
    if loop:
        args.remove("--loop")
    width = 8.0
    if "--width" in args:
        i = args.index("--width"); width = float(args[i + 1]); del args[i:i + 2]
    if len(args) < 2:
        raise SystemExit("usage: python -m scripts.gps.from_track <project_dir> <track.json> [more...] [--loop] [--width M]")
    proj, tracks = args[0], args[1:]
    out, stats = build_from_track(proj, tracks, loop=loop, width_m=width)
    print(f"wrote {out}")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
