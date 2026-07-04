"""Fetch OSM environment zones (water, highways, green/wetland) and cache data/environment.geojson —
the input build_env.py reads to place the creek, the raised I-70 / overpass decks, and tree areas.

This is the companion to buildings.py: same Overpass plumbing, but for the non-building dressing zones.
Re-run it whenever the route bbox changes (the old file goes stale on the old streets).

Run (caches to data/environment.geojson):
    python -m scripts.environment.zones projects/<slug>
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from scripts.config import load_config

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "prodrive-ac-builder/0.1 (https://github.com/k10-motorsports/prodrive-ac-builder)"


def _query(bbox: tuple[float, float, float, float]) -> str:
    s, w, n, e = bbox
    b = f"({s},{w},{n},{e})"
    return ("[out:json][timeout:120];(" +
            # water: the creek (lines) + open-water polys
            f'way["waterway"~"river|stream|canal|drain"]{b};' +
            f'way["natural"="water"]{b};' +
            f'way["landuse"="reservoir"]{b};' +
            # highways: the I-70 mainline + ramps + trunk (raised decks)
            f'way["highway"~"motorway|trunk|motorway_link|trunk_link"]{b};' +
            # green + wetland (tree scatter areas)
            f'way["leisure"="park"]{b};' +
            f'way["landuse"~"grass|meadow|recreation_ground|forest"]{b};' +
            f'way["natural"~"grassland|scrub|wood"]{b};' +
            f'way["natural"="wetland"]{b};' +
            ");out geom tags;")


def _classify(tags: dict) -> tuple[str, str] | None:
    """Map OSM tags -> (cls, kind) used by build_env.py, or None to skip."""
    if "waterway" in tags:
        return ("water", "line")
    if tags.get("natural") == "water" or tags.get("landuse") == "reservoir":
        return ("water", "poly")
    if "highway" in tags:
        return ("highway", "line")
    if tags.get("natural") == "wetland":
        return ("wetland", "poly")
    if (tags.get("leisure") == "park" or tags.get("natural") in ("grassland", "scrub", "wood")
            or tags.get("landuse") in ("grass", "meadow", "recreation_ground", "forest")):
        return ("green", "poly")
    return None


def fetch_zones(bbox: tuple[float, float, float, float], *, retries: int = 3) -> list[dict]:
    """Return ``[{cls, kind, coords:[(lon,lat)...]}]`` for environment zones in bbox (S,W,N,E)."""
    body = urllib.parse.urlencode({"data": _query(bbox)}).encode()
    payload: dict = {}
    for attempt in range(retries):
        req = urllib.request.Request(OVERPASS_URL, data=body,
                                     headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=200) as resp:
                payload = json.loads(resp.read().decode())
            break
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    out: list[dict] = []
    for el in payload.get("elements", []):
        if el.get("type") != "way" or not el.get("geometry"):
            continue
        ck = _classify(el.get("tags", {}))
        if not ck:
            continue
        coords = [(g["lon"], g["lat"]) for g in el["geometry"]]
        if len(coords) >= 2:
            out.append({"cls": ck[0], "kind": ck[1], "coords": coords})
    return out


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m scripts.environment.zones <project-dir>")
    project_dir = Path(sys.argv[1])
    cfg = load_config(project_dir)
    route = cfg.raw.get("route", {})
    bbox = tuple(route.get("bbox") or [cfg.lat - 0.01, cfg.lon - 0.013, cfg.lat + 0.01, cfg.lon + 0.013])
    feats = fetch_zones(bbox)
    out = project_dir / "data" / "environment.geojson"
    out.write_text(json.dumps({"features": feats}), encoding="utf-8")
    from collections import Counter
    c = Counter(f["cls"] for f in feats)
    print(f"wrote {out} — {len(feats)} zones: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))


if __name__ == "__main__":
    main()
