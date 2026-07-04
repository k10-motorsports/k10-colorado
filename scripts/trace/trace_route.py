"""Trace a hand-drawn route off a map screenshot and find the exact OSM roads it follows.

Pipeline: detect drawn pixels → georeference by aligning to the road network → map-match to OSM ways.
Outputs (data/): traced_route.json (ranked roads + the solved georef) and traced_overlay.svg (the
matched roads drawn back onto your screenshot, to verify the selection against your line).

Run:  python -m scripts.trace.trace_route projects/<slug> [screenshot.png]
"""

from __future__ import annotations

import base64
import json
import sys
from collections import defaultdict
from pathlib import Path

from scripts.config import load_config
from scripts.trace import georef, match, osm
from scripts.trace.detect import detect


def build(project_dir: str | Path, screenshot: str | Path | None = None) -> dict:
    pd = Path(project_dir)
    data = pd / "data"
    data.mkdir(exist_ok=True)
    cfg = load_config(pd)
    png = Path(screenshot) if screenshot else next((pd / "source").glob("*.png"))

    red, W, H = detect(png, step=2)
    xs = [p[0] for p in red]; ys = [p[1] for p in red]
    red_bbox = (min(xs), max(xs), min(ys), max(ys))

    rb = cfg.raw.get("route", {}).get("bbox")
    if rb:
        s, w, n, e = rb
    else:
        s, w, n, e = cfg.lat - 0.012, cfg.lon - 0.02, cfg.lat + 0.012, cfg.lon + 0.02
    ways = osm.fetch_drivable((s - 0.004, w - 0.004, n + 0.004, e + 0.004))

    index = georef.build_road_index(ways)
    sw_px = max(red, key=lambda p: p[1] - p[0])  # bottom-left drawn pixel → config location (the pin)
    anchor_ne = cfg.raw.get("route", {}).get("anchor_ne")  # [lon, lat] of the loop's NE corner
    if anchor_ne:
        # Two control points → exact north-up affine (robust; no grid ambiguity).
        ne_px = max(red, key=lambda p: p[0] - p[1])  # top-right drawn pixel
        params = georef.exact_from_anchors(sw_px, (cfg.lon, cfg.lat), ne_px, tuple(anchor_ne))
    else:
        # One anchor only — refine scale (slides on the uniform grid; add anchor_ne for accuracy).
        sx0 = (e - w) / (red_bbox[1] - red_bbox[0])
        sy0 = (s - n) / (red_bbox[3] - red_bbox[2])
        params = georef.refine_anchored(red, index, sw_px, (cfg.lon, cfg.lat), sx0, sy0)[0]
    score = georef._score(params, red, index)
    drawn = [georef.to_lonlat(params, x, y) for x, y in red]
    hits = match.match_roads(ways, drawn, tol=22.0, min_covered_m=80.0, min_fraction=0.30)

    by_name: dict[str, dict] = defaultdict(lambda: {"covered_m": 0, "highway": None})
    for h in hits:
        if not h["name"]:
            continue
        by_name[h["name"]]["covered_m"] += h["covered_m"]
        by_name[h["name"]]["highway"] = h["highway"]
    roads = sorted(({"name": n, **v} for n, v in by_name.items()), key=lambda r: -r["covered_m"])
    roads = [r for r in roads if r["covered_m"] >= 100]

    out = {
        "georef": dict(zip(("lon0", "sx", "lat0", "sy"), params)),
        "avg_drawn_to_road_m": round(score / len(red), 1),
        "drawn_pixels": len(red), "osm_ways": len(ways), "roads": roads,
    }
    (data / "traced_route.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    write_overlay(png, W, H, params, hits, data / "traced_overlay.svg")
    return out


def write_overlay(png: Path, W: int, H: int, params, hits, path: Path) -> None:
    """Draw the matched roads back onto the screenshot (inverse georef → pixels)."""
    lon0, sx, lat0, sy = params

    def to_px(lon, lat):
        return round((lon - lon0) / sx, 1), round((lat - lat0) / sy, 1)

    b64 = base64.b64encode(png.read_bytes()).decode()
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
             f'width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
             f'<image xlink:href="data:image/png;base64,{b64}" width="{W}" height="{H}"/>']
    for h in hits:
        if not h["name"]:
            continue
        d = " ".join(("M" if i == 0 else "L") + f"{x},{y}" for i, (x, y) in
                     enumerate(to_px(lo, la) for lo, la in h["geom"]))
        parts.append(f'<path d="{d}" fill="none" stroke="#00e5ff" stroke-width="3" opacity="0.85"/>')
    parts.append(f'<rect x="8" y="8" width="430" height="34" fill="#000" opacity="0.55"/>'
                 f'<text x="18" y="31" fill="#00e5ff" font-size="18" font-family="monospace">'
                 f'cyan = OSM roads matched to your red line</text>')
    parts.append("</svg>")
    path.write_text("".join(parts), encoding="utf-8")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m scripts.trace.trace_route <project-dir> [screenshot.png]")
    out = build(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(f"georef avg drawn→road: {out['avg_drawn_to_road_m']} m  "
          f"({out['drawn_pixels']} px vs {out['osm_ways']} ways)")
    print(f"{len(out['roads'])} named roads matched (by covered length):")
    for r in out["roads"]:
        print(f"  {r['covered_m']:5d} m  {r['highway']:13s} {r['name']}")
    print("→ data/traced_route.json, data/traced_overlay.svg")


if __name__ == "__main__":
    main()
