"""Overpass API queries: fetch named route roads within a bounding box.

The map screenshot is only a selection aid — this is where real geometry enters the pipeline.
Uses the standard library (urllib) so Phase 1 runs with no third-party deps. Overpass rejects
requests that lack a ``User-Agent`` (HTTP 406), so we always send one, and we retry on the
transient 504s the public instance occasionally returns.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "prodrive-ac-builder/0.1 (https://github.com/k10-motorsports/prodrive-ac-builder)"

# (lon, lat) vertex; a way is a list of them; bbox is (south, west, north, east)
Vertex = tuple[float, float]


def build_query(bbox: tuple[float, float, float, float], road_names: list[str], timeout: int = 90) -> str:
    """Build an Overpass QL query: every named way (clipped to bbox), returned with geometry."""
    south, west, north, east = bbox
    bb = f"({south},{west},{north},{east})"
    clauses = "".join(f'  way["highway"]["name"={json.dumps(n)}]{bb};\n' for n in road_names)
    return f"[out:json][timeout:{timeout}];\n(\n{clauses});\nout geom;"


def fetch_ways(
    bbox: tuple[float, float, float, float],
    road_names: list[str],
    *,
    retries: int = 3,
) -> dict[str, list[list[Vertex]]]:
    """Run the query and return ``{name: [way, ...]}``; each way is a list of (lon, lat)."""
    body = urllib.parse.urlencode({"data": build_query(bbox, road_names)}).encode()
    payload: dict = {}
    for attempt in range(retries):
        req = urllib.request.Request(
            OVERPASS_URL,
            data=body,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=150) as resp:
                payload = json.loads(resp.read().decode())
            break
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))  # back off on transient 504/timeout

    out: dict[str, list[list[Vertex]]] = {n: [] for n in road_names}
    for el in payload.get("elements", []):
        if el.get("type") != "way":
            continue
        name = el.get("tags", {}).get("name")
        geom = [(g["lon"], g["lat"]) for g in el.get("geometry") or []]
        if name in out and len(geom) >= 2:
            out[name].append(geom)
    return out


# Drivable highway classes to map-match a GPS drive against (excludes footways/cycleways/paths).
DRIVABLE = ("motorway|motorway_link|trunk|trunk_link|primary|primary_link|secondary|secondary_link|"
            "tertiary|tertiary_link|residential|unclassified|service|living_street|road")


def _post(query: str, *, retries: int = 3) -> dict:
    """POST an Overpass QL query and return the parsed JSON (shared HTTP + retry plumbing)."""
    body = urllib.parse.urlencode({"data": query}).encode()
    for attempt in range(retries):
        req = urllib.request.Request(
            OVERPASS_URL, data=body,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    return {}


def fetch_drivable_ways(bbox: tuple[float, float, float, float], *, timeout: int = 120) -> list[dict]:
    """Every drivable OSM way in the bbox, for map-matching: ``[{id, name, highway, geom:[(lon,lat)]}]``.
    Unlike :func:`fetch_ways` (named route roads only), this pulls the whole road network so a recorded
    drive can be snapped onto it regardless of what the streets are called."""
    s, w, n, e = bbox
    query = (f'[out:json][timeout:{timeout}];\n'
             f'way["highway"~"^({DRIVABLE})$"]({s},{w},{n},{e});\nout geom;')
    payload = _post(query)
    out: list[dict] = []
    for el in payload.get("elements", []):
        if el.get("type") != "way":
            continue
        geom = [(g["lon"], g["lat"]) for g in el.get("geometry") or []]
        if len(geom) >= 2:
            out.append({"id": el.get("id"), "name": el.get("tags", {}).get("name"),
                        "highway": el.get("tags", {}).get("highway"), "geom": geom})
    return out
