"""Build the centerline: chain ways, trim at corners, resample (~3 m), tag width, close the loop.

Reads ``track.config.json`` (the ``route`` block: bbox + ordered road names + connectors), pulls the
named roads from Overpass, stitches them into a single ordered ring, resamples to even spacing, and
writes ``data/centerline.geojson`` (+ a quick ``data/centerline_preview.svg``). Pure stdlib.

Run:  python -m scripts.gps.centerline projects/sand-creek-raceway
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

from scripts.config import load_config
from scripts.gps import kml, overpass

RESAMPLE_SPACING_M = 3.0  # target even spacing along the route (spec: ~2–5 m)

Vertex = tuple[float, float]  # (lon, lat)


# --- geometry helpers ----------------------------------------------------------

def haversine(a: Vertex, b: Vertex) -> float:
    """Great-circle distance in meters between two (lon, lat) points."""
    r = 6_371_000.0
    (lon1, lat1), (lon2, lat2) = a, b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def polyline_length(pl: list[Vertex]) -> float:
    return sum(haversine(pl[i], pl[i + 1]) for i in range(len(pl) - 1))


def _key(pt: Vertex, prec: int = 5) -> tuple[float, float]:
    return (round(pt[0], prec), round(pt[1], prec))  # ~1 m snapping for endpoint matching


def merge_ways(ways: list[list[Vertex]]) -> list[Vertex]:
    """Chain way segments that share endpoints into polylines; return the longest one."""
    segs = [list(w) for w in ways if len(w) >= 2]
    if not segs:
        return []
    used = [False] * len(segs)
    polylines: list[list[Vertex]] = []
    for i, s in enumerate(segs):
        if used[i]:
            continue
        used[i] = True
        chain = list(s)
        extended = True
        while extended:
            extended = False
            for j, t in enumerate(segs):
                if used[j]:
                    continue
                if _key(chain[-1]) == _key(t[0]):
                    chain += t[1:]
                elif _key(chain[-1]) == _key(t[-1]):
                    chain += list(reversed(t))[1:]
                elif _key(chain[0]) == _key(t[-1]):
                    chain = t[:-1] + chain
                elif _key(chain[0]) == _key(t[0]):
                    chain = list(reversed(t))[:-1] + chain
                else:
                    continue
                used[j] = True
                extended = True
        polylines.append(chain)
    return max(polylines, key=polyline_length)


def nearest_pair(a: list[Vertex], b: list[Vertex]) -> tuple[int, int, float]:
    """Indices (ia, ib) and distance of the closest approach between two polylines."""
    best = (0, 0, float("inf"))
    for ia, pa in enumerate(a):
        for ib, pb in enumerate(b):
            d = haversine(pa, pb)
            if d < best[2]:
                best = (ia, ib, d)
    return best


def build_ring(road_lines: list[list[Vertex]]) -> tuple[list[Vertex], list[float]]:
    """Stitch ordered road polylines into a closed ring by trimming each at its two corners."""
    n = len(road_lines)
    junc = [nearest_pair(road_lines[k], road_lines[(k + 1) % n]) for k in range(n)]
    ring: list[Vertex] = []
    for k in range(n):
        line = road_lines[k]
        a = junc[(k - 1) % n][1]  # index where prev road met this one
        b = junc[k][0]            # index where this road meets the next
        seg = line[a:b + 1] if a <= b else list(reversed(line[b:a + 1]))
        ring += seg
    corner_gaps = [round(j[2], 1) for j in junc]
    return ring, corner_gaps


def resample(pl: list[Vertex], spacing_m: float = RESAMPLE_SPACING_M) -> list[Vertex]:
    """Resample a polyline to even ``spacing_m`` spacing (linear interp; fine at metre scale)."""
    if len(pl) < 2:
        return list(pl)
    out = [pl[0]]
    prev = pl[0]
    carried = 0.0
    for i in range(1, len(pl)):
        a, b = prev, pl[i]
        d = haversine(a, b)
        if d == 0:
            continue
        while carried + d >= spacing_m:
            t = (spacing_m - carried) / d
            a = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            out.append(a)
            d = haversine(a, b)
            carried = 0.0
        carried += d
        prev = b
    if out[-1] != pl[-1]:
        # Avoid a tiny closing "stub" segment: if the leftover tail is much shorter than the spacing,
        # snap the last sample onto the endpoint instead of appending a near-duplicate. Otherwise a
        # normal ΔY across a 0.3 m stub reads as a huge fake grade — a bump at the start/finish line.
        if len(out) > 1 and haversine(out[-1], pl[-1]) < spacing_m * 0.5:
            out[-1] = pl[-1]
        else:
            out.append(pl[-1])
    return out


# --- orchestration -------------------------------------------------------------

def _feature(name: str, kind: str, coords: list[Vertex], extra: dict) -> dict:
    return {
        "type": "Feature",
        "properties": {"name": name, "kind": kind, **extra},
        "geometry": {"type": "LineString", "coordinates": [[lon, lat] for lon, lat in coords]},
    }


def build_centerline(project_dir: str | Path) -> tuple[Path, dict]:
    """Full Phase 1: config -> Overpass -> stitch/resample/tag -> write centerline.geojson."""
    project_dir = Path(project_dir)
    cfg = load_config(project_dir)
    route = cfg.raw["route"]
    bbox = tuple(route["bbox"])
    road_names = route["roads"]
    connectors_cfg = route.get("connectors", {})

    all_names = list(road_names) + [n for v in connectors_cfg.values() for n in v]
    fetched = overpass.fetch_ways(bbox, all_names)

    road_lines = [merge_ways(fetched[n]) for n in road_names]
    missing = [n for n, line in zip(road_names, road_lines) if not line]
    if missing:
        raise SystemExit(f"No geometry returned for: {missing} — check road names/bbox in config.")

    ring, corner_gaps = build_ring(road_lines)
    if cfg.loop and ring and _key(ring[0]) != _key(ring[-1]):
        ring.append(ring[0])
    ring = resample(ring, RESAMPLE_SPACING_M)
    widths = [cfg.default_width_m] * len(ring)

    connectors: dict[str, list[Vertex]] = {}
    for cname, rnames in connectors_cfg.items():
        merged = merge_ways([w for rn in rnames for w in fetched.get(rn, [])])
        connectors[cname] = resample(merged, RESAMPLE_SPACING_M) if merged else []

    length_m = polyline_length(ring)
    features = [
        _feature(
            "centerline", "full", ring,
            {"closed": cfg.loop, "default_width_m": cfg.default_width_m,
             "widths_m": [round(w, 2) for w in widths], "length_m": round(length_m, 1),
             "point_count": len(ring), "roads": road_names},
        )
    ]
    for cname, line in connectors.items():
        if line:
            features.append(_feature(cname, "connector", line,
                                     {"length_m": round(polyline_length(line), 1), "point_count": len(line)}))

    out = project_dir / "data" / "centerline.geojson"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")

    write_preview_svg(ring, connectors, project_dir / "data" / "centerline_preview.svg")

    stats = {
        "length_km": round(length_m / 1000, 2),
        "points": len(ring),
        "closed": cfg.loop,
        "corner_gaps_m": corner_gaps,
        "connectors": {c: len(p) for c, p in connectors.items()},
        "bbox": bbox,
    }
    return out, stats


def _wp_order(name: str | None) -> float:
    """Sort key for named turn waypoints: start* first, last*/finish* last, else the embedded number."""
    n = (name or "").lower()
    if "start" in n:
        return -1.0
    if "last" in n or "finish" in n:
        return 1e6
    m = re.search(r"\d+", n)
    return float(m.group()) if m else 5e5


def _catmull_rom(pts: list[Vertex], *, closed: bool, samples: int = 12) -> list[Vertex]:
    """Smooth curve that passes THROUGH each waypoint (so the track keeps your turns)."""
    n = len(pts)
    if n < 3:
        return list(pts)

    def comp(a, b, c, d, t):
        t2, t3 = t * t, t * t * t
        return 0.5 * (2 * b + (-a + c) * t + (2 * a - 5 * b + 4 * c - d) * t2 + (-a + 3 * b - 3 * c + d) * t3)

    out = []
    for i in (range(n) if closed else range(n - 1)):
        p0, p1, p2, p3 = pts[(i - 1) % n], pts[i], pts[(i + 1) % n], pts[(i + 2) % n]
        for s in range(samples):
            t = s / samples
            out.append((comp(p0[0], p1[0], p2[0], p3[0], t), comp(p0[1], p1[1], p2[1], p3[1], t)))
    if not closed:
        out.append(pts[-1])
    return out


def centerline_from_kml(project_dir: str | Path, kml_path: str | Path | None = None, *,
                        snap_connectors: bool = False) -> tuple[Path, dict]:
    """Build the centerline from a KML/KMZ (Google My Maps export) — a drawn line OR ordered turn points.

    A LineString is used directly (longest = loop, others = connectors). If the KML is only Points
    (named turns), they're ordered by name and splined through with Catmull-Rom. Precise — your route,
    no road guessing.
    """
    project_dir = Path(project_dir)
    cfg = load_config(project_dir)
    if kml_path is None:
        kml_path = next((p for p in sorted((project_dir / "source").glob("*.km*"))), None)
        if kml_path is None:
            raise SystemExit("no .kml/.kmz found in source/")
    feats = kml.parse_kml(kml_path)
    lines = [f for f in feats if f["type"] == "line"]
    points = [f for f in feats if f["type"] == "point"]
    kml_connectors: dict[str, list[Vertex]] = {}  # name -> raw coords
    if lines:
        main = max(lines, key=lambda f: polyline_length(f["coords"]))
        ring = list(main["coords"])
        kml_name, source = main["name"], "kml-line"
        for i, f in enumerate(g for g in lines if g is not main):
            kml_connectors[f["name"] or f"connector_{i}"] = f["coords"]
    elif points:
        # My Maps default-named pins ("Point NN") = interior junctions → a connector; the rest = loop.
        conn_re = re.compile(r"^\s*point\s*\d+", re.I)
        main_pts = [f for f in points if not conn_re.match(f["name"] or "")]
        conn_pts = [f for f in points if conn_re.match(f["name"] or "")]
        ordered = sorted(main_pts, key=lambda f: _wp_order(f["name"]))
        ring = _catmull_rom([f["coords"][0] for f in ordered], closed=cfg.loop, samples=12)
        kml_name, source = f"{len(ordered)} ordered turns", "kml-points"
        if conn_pts:
            co = sorted(conn_pts, key=lambda f: _wp_order(f["name"]))
            cpts = [f["coords"][0] for f in co]
            if snap_connectors:
                from scripts.gps import road_route  # lazy: needs network/OSM
                kml_connectors["connector_a"] = road_route.route_waypoints(cpts)
            else:
                kml_connectors["connector_a"] = _catmull_rom(cpts, closed=False, samples=10)
    else:
        raise SystemExit(f"no LineString or Points found in {kml_path}")

    if cfg.loop and _key(ring[0]) != _key(ring[-1]):
        ring.append(ring[0])
    ring = resample(ring, RESAMPLE_SPACING_M)
    widths = [cfg.default_width_m] * len(ring)
    features = [_feature("centerline", "full", ring,
                         {"closed": cfg.loop, "default_width_m": cfg.default_width_m,
                          "widths_m": [round(w, 2) for w in widths],
                          "length_m": round(polyline_length(ring), 1), "point_count": len(ring),
                          "source": source, "kml_name": kml_name})]
    connectors = {}
    for name, coords in kml_connectors.items():
        line = resample(list(coords), RESAMPLE_SPACING_M)
        connectors[name] = line
        features.append(_feature(name, "connector", line,
                                 {"length_m": round(polyline_length(line), 1), "point_count": len(line)}))

    out = project_dir / "data" / "centerline.geojson"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    write_preview_svg(ring, connectors, project_dir / "data" / "centerline_preview.svg")
    return out, {"length_km": round(polyline_length(ring) / 1000, 2), "points": len(ring),
                 "lines_in_kml": len(lines), "points_in_kml": len(points), "closed": cfg.loop,
                 "connectors": list(connectors)}


def write_preview_svg(ring: list[Vertex], connectors: dict[str, list[Vertex]], path: Path, size: int = 900) -> None:
    """Tiny dependency-free SVG so you can eyeball the extracted loop."""
    pts = list(ring) + [p for line in connectors.values() for p in line]
    if not pts:
        return
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    minlon, maxlon, minlat, maxlat = min(lons), max(lons), min(lats), max(lats)
    midlat = (minlat + maxlat) / 2
    aspect = math.cos(math.radians(midlat))  # lon compression at this latitude
    w_deg = (maxlon - minlon) * aspect or 1e-6
    h_deg = (maxlat - minlat) or 1e-6
    pad = 20
    scale = (size - 2 * pad) / max(w_deg, h_deg)
    W = w_deg * scale + 2 * pad
    H = h_deg * scale + 2 * pad

    def proj(p: Vertex) -> tuple[float, float]:
        x = (p[0] - minlon) * aspect * scale + pad
        y = (maxlat - p[1]) * scale + pad  # invert y for screen coords
        return round(x, 1), round(y, 1)

    def path_d(line: list[Vertex]) -> str:
        return " ".join(("M" if i == 0 else "L") + f"{x},{y}" for i, (x, y) in enumerate(map(proj, line)))

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" '
             f'viewBox="0 0 {W:.0f} {H:.0f}"><rect width="100%" height="100%" fill="#10151b"/>']
    parts.append(f'<path d="{path_d(ring)}" fill="none" stroke="#ff3b30" stroke-width="3"/>')
    for line in connectors.values():
        if line:
            parts.append(f'<path d="{path_d(line)}" fill="none" stroke="#34c759" '
                         f'stroke-width="2" stroke-dasharray="6,4"/>')
    sx, sy = proj(ring[0])
    parts.append(f'<circle cx="{sx}" cy="{sy}" r="5" fill="#0a84ff"/>')  # start/finish
    parts.append("</svg>")
    path.write_text("".join(parts), encoding="utf-8")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m scripts.gps.centerline <project-dir>")
    out, stats = build_centerline(sys.argv[1])
    print(f"wrote {out}")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
