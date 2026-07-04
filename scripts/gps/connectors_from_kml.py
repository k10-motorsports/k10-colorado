"""Phase 1b: turn config-declared side streets into drivable connector roads on the SAME kn5.

The loop is one closed centerline; "filling in the neighbourhood" means adding real streets that branch
off it. Each is declared in ``track.config.json`` under ``route.connectors_kml`` and pulled from one of the
Google My Maps exports, projected into the loop's exact local frame (shared origin, UN-mirrored — build_mesh
applies mirror_x), with elevation sampled from the SAME heightfield the grass uses so the road sits on the
real ground. Output is ``data/connectors.local.json``, which build_mesh ribbons as ``1ROAD_<name>`` (the grass
corridor conforms around them). Reproducible: re-run to regenerate byte-for-byte from config + KML + terrain.

    "route": { "connectors_kml": [
        {"name": "cowles_mountain_blvd", "kml": "../san-diego-cruise/source/San Diego 1.kmz",
         "match": "Cowles Mountain Blvd", "width_m": 10.0}
    ]}

    python -m scripts.gps.connectors_from_kml projects/san-diego-loop
"""

from __future__ import annotations

import json
import math
import re
import sys
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET


def _strip(tag: str) -> str:
    return re.sub(r"\{.*?\}", "", tag)


def _load_kml_text(p: Path) -> str:
    if p.suffix.lower() == ".kmz":
        z = zipfile.ZipFile(p)
        name = next(x for x in z.namelist() if x.endswith(".kml"))
        return z.read(name).decode("utf-8", "replace")
    return p.read_text(encoding="utf-8", errors="replace")


def _placemark_line(kml_path: Path, match: str) -> list[tuple[float, float]]:
    """The longest LineString whose Placemark name contains ``match`` (case-insensitive)."""
    root = ET.fromstring(_load_kml_text(kml_path))
    best: list[tuple[float, float]] = []
    for pm in root.iter():
        if _strip(pm.tag) != "Placemark":
            continue
        nm = ""
        for c in pm:
            if _strip(c.tag) == "name":
                nm = (c.text or "").strip()
        if match.lower() not in nm.lower():
            continue
        for el in pm.iter():
            if _strip(el.tag) == "coordinates":
                pts = [tuple(map(float, t.split(",")[:2])) for t in (el.text or "").split() if "," in t]
                if len(pts) > len(best):
                    best = pts
    return best


def _meters_per_degree(lat: float) -> tuple[float, float]:
    m_lat = 111_320.0
    m_lon = 111_320.0 * math.cos(math.radians(lat))
    return m_lon, m_lat


def build(project_dir: str | Path) -> dict:
    project = Path(project_dir)
    data = project / "data"
    cfg = json.loads((project / "track.config.json").read_text())
    specs = cfg.get("route", {}).get("connectors_kml", [])
    if not specs:
        print("[connectors] no route.connectors_kml in config — nothing to do")
        return {"connectors": 0}

    local = json.loads((data / "centerline.local.json").read_text())
    lon0, lat0 = local["origin"]["lon"], local["origin"]["lat"]
    elev0 = local["origin"]["elev_m"]
    m_lon, m_lat = _meters_per_degree(lat0)

    # Heightfield sampler — index the SAME grid the grass is built from (bbox_swne, nx x ny), bilinear.
    grid = json.loads((data / "heightfield.npy.json").read_text()) if (data / "heightfield.npy.json").exists() else None
    if grid is None:
        import numpy as np  # noqa
        grid = np.load(data / "heightfield.npy").tolist()
    meta = json.loads((data / "heightfield.meta.json").read_text())
    s, w, n, e = meta["bbox_swne"]
    ny, nx = len(grid), len(grid[0])
    gx = (e - w) / (nx - 1)
    gy = (n - s) / (ny - 1)

    def sample_elev(lon: float, lat: float) -> float:
        fi = min(max((lon - w) / gx, 0.0), nx - 1.0001)
        fj = min(max((lat - s) / gy, 0.0), ny - 1.0001)
        i0, j0 = int(fi), int(fj)
        ti, tj = fi - i0, fj - j0
        g = grid
        top = g[j0][i0] * (1 - ti) + g[j0][i0 + 1] * ti
        bot = g[j0 + 1][i0] * (1 - ti) + g[j0 + 1][i0 + 1] * ti
        return top * (1 - tj) + bot * tj

    loop = local["points_xyz_m"]   # loop centreline in UN-mirrored local metres (x, y, z)

    def nearest_loop(pt):
        b = min(loop, key=lambda q: (q[0] - pt[0]) ** 2 + (q[2] - pt[2]) ** 2)
        return math.hypot(b[0] - pt[0], b[2] - pt[2]), b

    out_connectors = []
    for spec in specs:
        kml_path = (project / spec["kml"]).resolve()
        line = _placemark_line(kml_path, spec["match"])
        if not line:
            print(f"[connectors] WARNING: no placemark matching {spec['match']!r} in {kml_path.name}")
            continue
        # clip to the terrain bbox (drop any tail that leaves the sampled ground)
        line = [(lon, lat) for lon, lat in line if s <= lat <= n and w <= lon <= e]
        # project to UN-mirrored local metres (x east, z north); y from the shared heightfield
        pts = [[(lon - lon0) * m_lon, sample_elev(lon, lat) - elev0, (lat - lat0) * m_lat]
               for lon, lat in line]
        # Orient so the end that JOINS the loop is LAST, then snap+blend it flush. A Google-directions
        # line ends at an address, not a junction, so without this the branch floats ~tens of m off the
        # loop with an elevation step (unreachable / a wall). Bridge the small gap to the nearest loop
        # point and taper the branch's terrain-sampled height to the loop road height over ``blend_m``,
        # so you can actually turn onto it and there's no bump at the mouth. The far end stays a dead-end
        # spur (drive up + back).
        # Find where the connector comes CLOSEST to the loop — anywhere along it, not just an endpoint.
        # A "Directions" round-trip (e.g. Fletcher Pkwy, 8285 -> 8285) has both ENDS at the same address
        # far from the loop, but its MIDDLE crosses right by it; an out-and-back spur (Cowles) is closest
        # at an end. Handling both: pick the min-gap vertex, blend the connector's terrain height toward
        # the loop road height in a window around it (kills the step at the crossing), and only WELD a
        # vertex onto the loop centreline when the closest point is an END with a real gap (a spur mouth).
        blend_m = 90.0
        gi = min(range(len(pts)), key=lambda i: nearest_loop(pts[i])[0])
        gap, join = nearest_loop(pts[gi])
        is_end = gi <= 1 or gi >= len(pts) - 2
        RIBBON_OVERLAP_M = 12.0    # within this, the two road ribbons already overlap — no weld needed
        if gap <= 120.0:
            if is_end and gap > RIBBON_OVERLAP_M:
                if gi <= 1:
                    pts.insert(0, [join[0], join[1], join[2]]); gi = 0
                else:
                    pts.append([join[0], join[1], join[2]]); gi = len(pts) - 1
                gap = 0.0
            # blend Y toward the loop height outward from the junction vertex in BOTH directions
            for step in (1, -1):
                acc = 0.0; k = gi
                while 0 <= k + step < len(pts):
                    acc += math.dist(pts[k + step][::2], pts[k][::2])
                    if acc >= blend_m:
                        break
                    t = acc / blend_m
                    pts[k + step][1] = pts[k + step][1] * t + join[1] * (1 - t)
                    k += step
            pts[gi][1] = join[1]
            note = f"joined loop at {'end' if is_end else 'mid-street'} (gap {gap:.0f} m, blended {blend_m:.0f} m)"
        else:
            note = f"NOT joined (nearest loop {gap:.0f} m) — floating spur, SKIPPED"
            continue
        width = float(spec.get("width_m", 10.0))
        out_connectors.append({"name": spec["name"], "points_xyz_m": [[round(c, 3) for c in p] for p in pts],
                               "widths_m": [width] * len(pts)})
        length = sum(math.dist(pts[i][::2], pts[i - 1][::2]) for i in range(1, len(pts)))
        print(f"[connectors] {spec['name']}: {len(pts)} pts, {length/1000:.2f} km, width {width} m — {note}")

    (data / "connectors.local.json").write_text(json.dumps({"connectors": out_connectors}), encoding="utf-8")
    print(f"[connectors] wrote data/connectors.local.json ({len(out_connectors)} connectors)")
    return {"connectors": len(out_connectors)}


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m scripts.gps.connectors_from_kml <project-dir>")
    build(sys.argv[1])


if __name__ == "__main__":
    main()
