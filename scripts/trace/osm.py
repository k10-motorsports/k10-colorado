"""Fetch the full drivable road network (named + unnamed) for a bbox from Overpass."""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "prodrive-ac-builder/0.1 (https://github.com/k10-motorsports/prodrive-ac-builder)"
DRIVABLE = r'^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|service)(_link)?$'


def fetch_drivable(bbox: tuple[float, float, float, float], *, retries: int = 3) -> list[dict]:
    """Return ways as ``{"name", "highway", "geom":[(lon,lat)...]}`` within bbox (s,w,n,e)."""
    s, w, n, e = bbox
    query = (f'[out:json][timeout:90];way["highway"~"{DRIVABLE}"]'
             f'({s},{w},{n},{e});out tags geom;')
    body = urllib.parse.urlencode({"data": query}).encode()
    for attempt in range(retries):
        req = urllib.request.Request(OVERPASS_URL, data=body,
                                     headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=150) as resp:
                payload = json.loads(resp.read().decode())
            break
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    ways = []
    for el in payload.get("elements", []):
        if el.get("type") != "way":
            continue
        geom = [(g["lon"], g["lat"]) for g in el.get("geometry") or []]
        if len(geom) >= 2:
            ways.append({"name": el.get("tags", {}).get("name"),
                         "highway": el.get("tags", {}).get("highway"), "geom": geom})
    return ways
