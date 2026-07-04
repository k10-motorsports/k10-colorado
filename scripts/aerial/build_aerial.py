"""Phase 1 (aerial source): an aerial screenshot of a real track → data/centerline.geojson.

Pipeline: read the screenshot → get an ordered centerline in pixels (auto-trace the asphalt ribbon,
or take a hand ``source.trace_px``) → georeference pixels to real lon/lat from two control points →
resample to even spacing, tag width → write the same ``centerline.geojson`` the GPS front-end emits,
so elevation / projection / mesh all run unchanged. That shared output is what makes an aerial-
sourced track *elevation-correct*: the centerline is in true lon/lat, so the USGS 3DEP phase samples
real ground under it.

The track's name and the surrounding road names come straight off the image (e.g. "IMI Motorsports
Complex", "Summit Blvd") and live in ``track.config.json`` — recorded into the output's provenance.

Run:  python -m scripts.aerial.build_aerial projects/<slug> [screenshot]
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from scripts.aerial import detect, georef, skeleton
from scripts.config import load_config
from scripts.gps.centerline import _feature, polyline_length, resample, write_preview_svg

Vertex = tuple[float, float]  # (lon, lat)
RESAMPLE_SPACING_M = 3.0  # match the GPS pipeline


def _to_px(coord, w: int, h: int) -> tuple[float, float]:
    """Accept a control/trace coordinate as normalised [0,1] fractions or absolute pixels."""
    x, y = float(coord[0]), float(coord[1])
    if abs(x) <= 1.5 and abs(y) <= 1.5:  # fractions of image size
        return x * w, y * h
    return x, y


def _control_points(src: dict, w: int, h: int):
    cps = src.get("control_points")
    if not cps or len(cps) < 2:
        raise SystemExit("aerial source needs source.control_points: two {px, lonlat} corners "
                         "(drop two Google Maps pins and record their pixel positions).")
    out = []
    for cp in cps[:2]:
        if cp.get("lonlat") is None:
            raise SystemExit("a control point is missing 'lonlat' — fill both corner pins "
                             "([lon, lat]) so the trace can be georeferenced for elevation.")
        out.append((_to_px(cp["px"], w, h), (float(cp["lonlat"][0]), float(cp["lonlat"][1]))))
    return out


def _pixel_centerline(src: dict, png: Path, w: int, h: int) -> tuple[list, str]:
    """Ordered centerline in pixels: hand ``trace_px`` if present, else auto-trace the asphalt mask."""
    trace = src.get("trace_px")
    if trace:
        return [_to_px(c, w, h) for c in trace], "trace_px"
    if png.suffix.lower() != ".png":
        raise SystemExit(f"auto-trace needs a PNG ({png.name} is not) — convert the screenshot to "
                         "PNG, or supply an ordered source.trace_px polyline.")
    step = int(src.get("detect_step", 2))
    mask, gw, gh = detect.asphalt_mask(png, step=step)
    px = skeleton.trace_centerline(mask)
    if len(px) < 8:
        raise SystemExit("auto-trace found too little ribbon — tune source.detect or supply trace_px.")
    return [(x * step, y * step) for x, y in px], "auto"


def build(project_dir: str | Path, screenshot: str | Path | None = None) -> dict:
    pd = Path(project_dir)
    data = pd / "data"
    data.mkdir(exist_ok=True)
    cfg = load_config(pd)
    src = cfg.source
    if src.get("type") != "aerial":
        raise SystemExit(f"source.type is {src.get('type')!r}, expected 'aerial'.")

    png = Path(screenshot) if screenshot else (pd / src["screenshot"] if src.get("screenshot")
                                               else next((pd / "source").glob("*.png")))
    w, h = detect.image_size(png)

    px_path, trace_mode = _pixel_centerline(src, png, w, h)
    cps = _control_points(src, w, h)
    params = georef.affine_from_corners(cps[0][0], cps[0][1], cps[1][0], cps[1][1])

    ring: list[Vertex] = [georef.to_lonlat(params, x, y) for x, y in px_path]
    if cfg.loop and ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    ring = resample(ring, RESAMPLE_SPACING_M)
    widths = [cfg.default_width_m] * len(ring)

    mpp = georef.meters_per_pixel(params, cps[0][1][1])
    provenance = {
        "source": "aerial", "screenshot": png.name, "trace_mode": trace_mode,
        "track_name": cfg.name, "nearby_roads": src.get("nearby_roads", []),
        "georef": dict(zip(("lon0", "sx", "lat0", "sy"), params)),
        "meters_per_pixel": [round(mpp[0], 3), round(mpp[1], 3)],
    }
    feature = _feature("centerline", "full", ring, {
        "closed": cfg.loop, "default_width_m": cfg.default_width_m,
        "widths_m": [round(x, 2) for x in widths],
        "length_m": round(polyline_length(ring), 1), "point_count": len(ring),
        **provenance,
    })
    out = data / "centerline.geojson"
    out.write_text(json.dumps({"type": "FeatureCollection", "features": [feature]}), encoding="utf-8")
    write_preview_svg(ring, {}, data / "centerline_preview.svg")
    _write_overlay(png, w, h, params, px_path, cps, data / "aerial_overlay.svg")

    return {"length_km": round(polyline_length(ring) / 1000, 2), "points": len(ring),
            "trace_mode": trace_mode, "closed": cfg.loop,
            "meters_per_pixel": provenance["meters_per_pixel"], "nearby_roads": src.get("nearby_roads", [])}


def _write_overlay(png: Path, w: int, h: int, params, px_path, cps, path: Path) -> None:
    """Draw the traced centerline + control points back onto the screenshot to verify by eye."""
    mime = "image/jpeg" if png.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    b64 = base64.b64encode(png.read_bytes()).decode()
    d = " ".join(("M" if i == 0 else "L") + f"{x:.1f},{y:.1f}" for i, (x, y) in enumerate(px_path))
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
             f'width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
             f'<image xlink:href="data:{mime};base64,{b64}" width="{w}" height="{h}"/>',
             f'<path d="{d}" fill="none" stroke="#00e5ff" stroke-width="3" opacity="0.9"/>']
    for (px, _ll) in cps:  # control-point corners
        parts.append(f'<circle cx="{px[0]:.1f}" cy="{px[1]:.1f}" r="9" fill="none" '
                     f'stroke="#ffd60a" stroke-width="4"/>')
    parts.append('<rect x="8" y="8" width="430" height="34" fill="#000" opacity="0.55"/>'
                 '<text x="18" y="31" fill="#00e5ff" font-size="18" font-family="monospace">'
                 'cyan = traced centerline, yellow = control points</text></svg>')
    path.write_text("".join(parts), encoding="utf-8")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m scripts.aerial.build_aerial <project-dir> [screenshot]")
    stats = build(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(f"traced centerline ({stats['trace_mode']}): {stats['points']} pts, "
          f"{stats['length_km']} km, closed={stats['closed']}")
    print(f"  ground scale: {stats['meters_per_pixel'][0]} x {stats['meters_per_pixel'][1]} m/px")
    if stats["nearby_roads"]:
        print(f"  nearby roads: {', '.join(stats['nearby_roads'])}")
    print("→ data/centerline.geojson, data/centerline_preview.svg, data/aerial_overlay.svg")


if __name__ == "__main__":
    main()
