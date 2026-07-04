"""Sample USGS 3DEP elevations (meters). Primary source for US locations at 1 m resolution.

Uses the 3DEP ImageServer ``getSamples`` batch endpoint (stdlib urllib, JSON) — one request returns
an elevation per input point, so we don't need a DEM raster or GDAL for Phase 2. Source priority per
the spec is 3DEP -> OpenTopography -> SRTM 30 m; only 3DEP is wired up so far (full Commerce City
coverage). Points outside 3DEP coverage come back as None and are filled by the caller.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

USGS_3DEP_IMAGE_SERVER = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer"
GET_SAMPLES = USGS_3DEP_IMAGE_SERVER + "/getSamples"
USER_AGENT = "prodrive-ac-builder/0.1 (https://github.com/k10-motorsports/prodrive-ac-builder)"

Vertex = tuple[float, float]  # (lon, lat)


def _sample_batch(batch: list[Vertex], *, retries: int) -> list[float | None]:
    geometry = json.dumps({"points": [[lon, lat] for lon, lat in batch], "spatialReference": {"wkid": 4326}})
    body = urllib.parse.urlencode({
        "geometry": geometry,
        "geometryType": "esriGeometryMultipoint",
        "returnFirstValueOnly": "true",
        "f": "json",
    }).encode()
    for attempt in range(retries):
        req = urllib.request.Request(
            GET_SAMPLES, data=body,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=150) as resp:
                payload = json.loads(resp.read().decode())
            # Map by echoed location so we're robust to dropped no-data points / reordering.
            by_loc = {}
            for s in payload.get("samples", []):
                loc = s.get("location", {})
                by_loc[(round(loc.get("x"), 6), round(loc.get("y"), 6))] = float(s["value"])
            return [by_loc.get((round(lon, 6), round(lat, 6))) for lon, lat in batch]
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    return [None] * len(batch)


def _fill_none(values: list[float | None]) -> list[float]:
    """Linear-interpolate missing samples (no-data points) from their nearest valid neighbors."""
    n = len(values)
    if all(v is None for v in values):
        raise SystemExit("3DEP returned no elevation data — check bbox/coverage.")
    out: list[float] = [v if v is not None else float("nan") for v in values]
    i = 0
    while i < n:
        if out[i] != out[i]:  # NaN
            j = i
            while j < n and out[j] != out[j]:
                j += 1
            left = out[i - 1] if i > 0 else out[j]
            right = out[j] if j < n else out[i - 1]
            for k in range(i, j):
                t = (k - i + 1) / (j - i + 1)
                out[k] = left + (right - left) * t
            i = j
        else:
            i += 1
    return out


def sample_points(points: list[Vertex], *, chunk: int = 450, retries: int = 3) -> list[float]:
    """Return an elevation (m) for each (lon, lat) point, in order. Batches via 3DEP getSamples."""
    raw: list[float | None] = []
    for i in range(0, len(points), chunk):
        raw += _sample_batch(points[i:i + chunk], retries=retries)
    return _fill_none(raw)
