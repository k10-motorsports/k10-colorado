"""Generate the installable Assetto Corsa track folder (everything except the kn5 + AI).

All layouts share one kn5 and differ only in AC config. Emits, under build/<slug>/:
  models_<layout>.ini, data/surfaces.ini, data/map.ini, map.png, ui/<layout>/ui_track.json,
  ui/<layout>/{preview,outline}.png, ai/<layout>/ (placeholder), README.txt.
The kn5 is produced by Blender (scripts/ac/build_kn5.py); the AI fast_lane is recorded in-game (Windows).

Run:  python -m scripts.ac.track_folder projects/<slug>
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

from scripts.config import load_config
from scripts.ac import credits, ext_config


# --- AC config files -----------------------------------------------------------

def surfaces_ini() -> str:
    """data/surfaces.ini — physical surfaces keyed to the 1ROAD/1KERB/1RUNOFF/1GRASS mesh prefixes.
    WAV= names a STOCK AC sound resolved from the player's own content/sfx (FMOD GUIDs.txt) — NOTHING
    is bundled or redistributed. ROAD is intentionally silent (tyre-roll covers tarmac); KERB rumbles,
    RUNOFF/GRASS scrub. The kerb mesh is already 3D-ridged, so SIN_HEIGHT stays 0 (no double bump)."""
    # KEY, params, stock WAV (referenced by name), WAV_PITCH
    blocks = [
        ("ROAD",   dict(FRICTION=0.99, DAMPING=0, IS_VALID_TRACK=1, DIRT_ADDITIVE=0, IS_PITLANE=0),
                   "", 0),
        ("KERB",   dict(FRICTION=0.92, DAMPING=0, IS_VALID_TRACK=1, DIRT_ADDITIVE=0, IS_PITLANE=0,
                        VIBRATION_GAIN=1.0, VIBRATION_LENGTH=1.5),
                   "kerb.wav", 1.3),
        # Paved corner run-off: drivable tarmac but slightly low grip and OFF-track (cutting it doesn't
        # count) — forgiving without turning the apron into a free track-extension.
        ("RUNOFF", dict(FRICTION=0.90, DAMPING=0.02, IS_VALID_TRACK=0, DIRT_ADDITIVE=0, IS_PITLANE=0,
                        VIBRATION_GAIN=0.4, VIBRATION_LENGTH=2.0),
                   "gravel.wav", 1.2),
        ("GRASS",  dict(FRICTION=0.60, DAMPING=0.1, IS_VALID_TRACK=0, DIRT_ADDITIVE=1, IS_PITLANE=0),
                   "grass.wav", 1.0),
        # LAWN = irrigated suburban turf (1LAWN_* tiles): a touch more grip than dry grass, same
        # off-track scrub. Keeps the green neighbourhood ground physical so you don't fall through it.
        ("LAWN",   dict(FRICTION=0.65, DAMPING=0.09, IS_VALID_TRACK=0, DIRT_ADDITIVE=1, IS_PITLANE=0),
                   "grass.wav", 1.0),
    ]
    out = []
    for i, (key, p, wav, pitch) in enumerate(blocks):
        lines = [f"[SURFACE_{i}]", f"KEY={key}", f"WAV={wav}", f"WAV_PITCH={pitch}", "FF_EFFECT=0",
                 "SIN_HEIGHT=0", "SIN_LENGTH=0", "BLACK_FLAG_TIME=0"]
        lines += [f"{k}={v}" for k, v in p.items()]
        out.append("\n".join(lines))
    return "\n\n".join(out) + "\n"


def ui_track_json(cfg, layout: str, length_m: float, n_pits: int) -> dict:
    loc = cfg.raw.get("location", {})
    city = loc.get("city", "Commerce City")
    country = loc.get("country", "United States")
    freeroam = not cfg.raw.get("loop", True)
    desc = cfg.raw.get("ui_description") or (
        f"Open-world freeroam cruise of {city} — a real street network ({round(length_m/1000)} km) "
        f"merged from map exports, with real USGS terrain. Built by prodrive-ac-builder. Just drive."
        if freeroam else
        f"Real-world street circuit built from OpenStreetMap + USGS 3DEP by prodrive-ac-builder "
        f"({city}, {country}).")
    tags = ["freeroam", "cruise", "street", "open-world", "real-roads"] if freeroam else \
        ["street", "loop", "fictional", "real-roads"]
    # per-layout label: use the layout's configured `name` (e.g. "Sand Creek Raceway") so each config
    # in the in-game dropdown reads as its actual track, not the raw id.
    lo_meta = next((lo for lo in (cfg.raw.get("layouts", []) or []) if lo.get("id") == layout), {})
    lo_label = lo_meta.get("name") or layout
    # Content Manager display name: prefix the series so all K10 Colorado circuits group together in the
    # in-game list ("K10 Colorado - Sand Creek Raceway", ...). Override per track via `ui_series`.
    series = cfg.raw.get("ui_series", "K10 Colorado")
    base_name = f"{series} - {cfg.name}" if series else cfg.name
    # Only append a layout label when the track actually HAS multiple layouts — a single-layout track
    # (e.g. IMI's one "full_course") shouldn't read "… — full_course".
    n_layouts = len(cfg.raw.get("layouts", []) or [])
    suffix = "" if (layout in ("full", "freeroam") or n_layouts <= 1) else f" — {lo_label}"
    return {
        "name": base_name + suffix,
        # build stamp UP FRONT: a full release cycle was burned diagnosing "fixed nothing" against a
        # stale install — the version must be visible at a glance in CM, not just the version field.
        "description": f"[build {cfg.version}] " + desc,
        "tags": tags,
        "geotags": [f"{cfg.lat}", f"{cfg.lon}"],
        "country": country, "city": city,
        "length": str(round(length_m)), "width": str(round(cfg.default_width_m)),
        "pitboxes": str(n_pits), "run": "clockwise",
        "author": cfg.author, "version": str(cfg.version), "year": cfg.year,
    }


def models_ini(kn5_name: str, spawn_kn5: str | None = None) -> str:
    s = f"[MODEL_0]\nFILE={kn5_name}\nPOSITION=0,0,0\nROTATION=0,0,0\n"
    if spawn_kn5:   # per-layout spawn kn5 (AC_START/AC_PIT dummies for THIS track), loaded alongside main
        s += f"\n[MODEL_1]\nFILE={spawn_kn5}\nPOSITION=0,0,0\nROTATION=0,0,0\n"
    return s


# --- minimap / images ----------------------------------------------------------

def _xz(local: dict) -> list[tuple[float, float]]:
    return [(p[0], p[2]) for p in local["points_xyz_m"]]  # (X east, Z north) metres


def _map_params(xz, size: int = 900, margin: int = 28):
    """Square, centered canvas so nothing clips and map.png matches map.ini exactly."""
    xs = [p[0] for p in xz]; zs = [p[1] for p in xz]
    minx, maxx, minz, maxz = min(xs), max(xs), min(zs), max(zs)
    rx, rz = maxx - minx, maxz - minz
    scale = (size - 2 * margin) / max(rx, rz)
    ox, oz = (size - rx * scale) / 2, (size - rz * scale) / 2  # centering offsets (px)
    params = {"WIDTH": size, "HEIGHT": size, "SCALE_FACTOR": round(scale, 6),
              "X_OFFSET": round(ox / scale - minx, 3), "Z_OFFSET": round(oz / scale - minz, 3),
              "MARGIN": margin, "DRAWING_SIZE": 10}
    geom = {"minx": minx, "minz": minz, "maxz": maxz, "scale": scale, "ox": ox, "oz": oz, "S": size}
    return params, geom


def map_ini(params: dict) -> str:
    body = "\n".join(f"{k}={v}" for k, v in params.items())
    return f"[PARAMETERS]\n{body}\n"


def _network_edges_xz(project_dir: Path):
    """For a freeroam NETWORK: return (edges_xz, length_m). Each edge is a [(x,z)...] polyline in the
    SAME mirrored local frame the mesh used, recomputed from network.geojson + network.local.json."""
    netp = project_dir / "data" / "network.geojson"
    locp = project_dir / "data" / "network.local.json"
    if not (netp.exists() and locp.exists()):
        return None
    loc = json.loads(locp.read_text())
    o = loc["origin"]; lon0, lat0 = o["lon"], o["lat"]
    sx = -1.0 if loc.get("mirror_x", True) else 1.0
    phi = math.radians(lat0)
    m_lat = 111132.954 - 559.822 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi)
    m_lon = 111412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi) + 0.118 * math.cos(5 * phi)
    # SUN FIX: the model is yawed by true_north_rotation_deg at export (build_kn5). Yaw the minimap
    # coords by the SAME angle so map.png + map.ini offsets stay aligned with the in-game world.
    tn = math.radians(float(loc.get("true_north_rotation_deg", 0.0)))
    ct, st = math.cos(tn), math.sin(tn)
    fc = json.loads(netp.read_text())
    edges, total = [], 0.0
    for f in fc["features"]:
        e = []
        for lon, lat in f["geometry"]["coordinates"]:
            x, z = sx * (lon - lon0) * m_lon, (lat - lat0) * m_lat
            e.append((x * ct - z * st, x * st + z * ct))
        edges.append(e)
        total += float(f["properties"].get("length_m") or 0.0)
    return edges, round(total, 1)


def _track_svg(xz, geom, *, stroke: str, width: float, bg: str | None, flip_y: bool = False,
               multi=None) -> str:
    minx, minz, maxz, scale, ox, oz, S = (geom[k] for k in ("minx", "minz", "maxz", "scale", "ox", "oz", "S"))

    def px(x, z):
        py = ((maxz - z) if flip_y else (z - minz)) * scale + oz  # flip_y=True → north up (UI); else AC HUD
        return round((x - minx) * scale + ox, 1), round(py, 1)

    rect = f'<rect width="100%" height="100%" fill="{bg}"/>' if bg else ""
    if multi is not None:  # freeroam network: one polyline per edge (no jumps between disjoint streets)
        paths = "".join(
            f'<polyline points="{" ".join(f"{a},{b}" for a, b in (px(x, z) for x, z in e))}" '
            f'fill="none" stroke="{stroke}" stroke-width="{width}" stroke-linejoin="round" '
            f'stroke-linecap="round"/>' for e in multi)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{S}" height="{S}" viewBox="0 0 {S} {S}">'
                f'{rect}{paths}</svg>')
    pts = " ".join(f"{a},{b}" for a, b in (px(x, z) for x, z in xz))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{S}" height="{S}" viewBox="0 0 {S} {S}">'
            f'{rect}<polyline points="{pts}" fill="none" stroke="{stroke}" stroke-width="{width}" '
            f'stroke-linejoin="round" stroke-linecap="round"/></svg>')


def _rasterize(svg: str, png: Path, size: int) -> bool:
    """qlmanage SVG→PNG (macOS build host). Falls back to writing the .svg if unavailable."""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as f:
            f.write(svg); tmp = f.name
        subprocess.run(["qlmanage", "-t", "-s", str(size), "-o", str(png.parent), tmp],
                       capture_output=True, timeout=30)
        out = png.parent / (Path(tmp).name + ".png")
        if out.exists():
            out.replace(png)
            return True
    except Exception:
        pass
    png.with_suffix(".svg").write_text(svg, encoding="utf-8")
    return False


# --- orchestration -------------------------------------------------------------

def generate(project_dir: str | Path, kn5_name: str | None = None) -> dict:
    project_dir = Path(project_dir)
    cfg = load_config(project_dir)
    dummies = json.loads((project_dir / "data" / "dummies.json").read_text(encoding="utf-8")) \
        if (project_dir / "data" / "dummies.json").exists() else {}
    n_pits = sum(1 for k in dummies if k.startswith("AC_PIT_"))
    kn5_name = kn5_name or f"{cfg.slug}.kn5"

    # Freeroam NETWORK (this project) vs single-loop (legacy): the minimap draws every edge and the
    # "length" is the total drivable road, not a lap.
    net = _network_edges_xz(project_dir)
    if net is not None:
        edges_xz, length_m = net
        xz = [p for e in edges_xz for p in e]      # all points (for bbox/centering)
        multi = edges_xz
    else:
        local = json.loads((project_dir / "data" / "centerline.local.json").read_text(encoding="utf-8"))
        xz = _xz(local)
        multi = None
        length_m = json.loads((project_dir / "data" / "centerline.geojson").read_text())["features"][0]["properties"]["length_m"]

    out = project_dir / "build" / cfg.slug
    (out / "data").mkdir(parents=True, exist_ok=True)
    (out / "data" / "surfaces.ini").write_text(surfaces_ini(), encoding="utf-8")

    params, geom = _map_params(xz)
    (out / "data" / "map.ini").write_text(map_ini(params), encoding="utf-8")
    # HUD minimap — white track, transparent, AC convention (consistent with map.ini)
    _rasterize(_track_svg(xz, geom, stroke="#ffffff", width=params["DRAWING_SIZE"], bg=None, flip_y=False,
                          multi=multi), out / "map.png", params["WIDTH"])
    # preview — reuse the Phase-4 3D render (a real view of the track) if present
    render_svg = project_dir / "data" / "track_render.svg"
    preview_src = render_svg.read_text(encoding="utf-8") if render_svg.exists() else \
        _track_svg(xz, geom, stroke="#ff3b30", width=5, bg="#0c1116", flip_y=True, multi=multi)

    import shutil
    layouts = [lo["id"] for lo in cfg.layouts]
    for layout in layouts:
        # Per-track spawn: if this layout has its own dummies_<layout>.json + a built spawn kn5, load it
        # as MODEL_1 alongside the shared main kn5, and count pits from THIS layout's dummies.
        sp_dummies_path = project_dir / "data" / f"dummies_{layout}.json"
        spawn_kn5 = f"{cfg.slug}__{layout}.kn5"
        spawn_kn5_src = project_dir / "build" / spawn_kn5
        has_spawn = sp_dummies_path.exists() and spawn_kn5_src.exists()
        layout_dummies = json.loads(sp_dummies_path.read_text(encoding="utf-8")) if sp_dummies_path.exists() else dummies
        layout_pits = sum(1 for k in layout_dummies if k.startswith("AC_PIT_")) or n_pits or 1
        (out / ("models_%s.ini" % layout)).write_text(
            models_ini(kn5_name, spawn_kn5 if has_spawn else None), encoding="utf-8")
        if has_spawn:
            shutil.copyfile(spawn_kn5_src, out / spawn_kn5)
        uid = out / "ui" / layout
        uid.mkdir(parents=True, exist_ok=True)
        (uid / "ui_track.json").write_text(json.dumps(ui_track_json(cfg, layout, length_m, layout_pits), indent=2),
                                           encoding="utf-8")
        _rasterize(_track_svg(xz, geom, stroke="#e8ecf0", width=3, bg="#0c1116", flip_y=True, multi=multi), uid / "outline.png", params["WIDTH"])
        _rasterize(preview_src, uid / "preview.png", 600)
        aid = out / "ai" / layout
        aid.mkdir(parents=True, exist_ok=True)
        (aid / "README.txt").write_text(
            "fast_lane.ai is recorded in-game (AC + Content Manager, Windows) — see ac-track-modding.\n",
            encoding="utf-8")

    (out / "README.txt").write_text(
        f"{cfg.name} — Assetto Corsa track folder (generated by prodrive-ac-builder).\n\n"
        f"GENERATED HERE: surfaces.ini, map.ini, map.png, models_<layout>.ini, ui/<layout>/*.\n"
        f"STILL NEEDED:\n"
        f"  - {kn5_name}: build with Blender 4.x + AC Blender Tools — `blender --background "
        f"--python scripts/ac/build_kn5.py -- {project_dir}`\n"
        f"  - ai/<layout>/fast_lane.ai: record in-game (Windows).\n"
        f"Install: `python -m scripts.ac.install {project_dir}` (auto-finds AC, asks if it can't), "
        f"or copy this folder to <AC>/content/tracks/{cfg.slug}/. Validate in Content Manager.\n",
        encoding="utf-8")
    credits.generate(project_dir, cfg)  # CREDITS.txt + LICENSES.md from assets/licenses.json
    ext_config.generate(project_dir)  # extension/ext_config.ini — GrassFX / RainFX / light pollution / water
    return {"out": str(out), "layouts": layouts, "length_m": length_m, "n_pits": n_pits,
            "map_px": f"{params['WIDTH']}x{params['HEIGHT']}", "kn5": kn5_name}


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="scripts.ac.track_folder",
                                 description="Generate the installable AC track folder.")
    ap.add_argument("project_dir", help="projects/<slug>")
    ap.add_argument("--install", action="store_true",
                    help="after generating, install into a local AC content/tracks folder "
                         "(auto-detects it, asks if it can't find it)")
    args = ap.parse_args()

    info = generate(args.project_dir)
    print(f"wrote track folder → {info['out']}")
    for k, v in info.items():
        if k != "out":
            print(f"  {k}: {v}")

    if args.install:
        from scripts.ac import install
        raise SystemExit(install.main([args.project_dir]))


if __name__ == "__main__":
    main()
