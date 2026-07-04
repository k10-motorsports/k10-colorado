"""Label every centerline vertex with the REAL OSM street it lies on — the robust replacement for
loop-order corner counting (which mislabelled the old signs: a corner's position in the lap is not a
reliable proxy for which named road you just turned onto).

Method: fetch every *named* ``highway`` way in the route's bbox from Overpass, project them into the
SAME local ENU frame as ``centerline.local.json`` (so distances are in metres), then assign each
centerline vertex the nearest way's name (within ``match_m``). A light majority filter removes the
flicker at intersections, and runs shorter than ``min_run`` vertices are merged away. The output is

  data/street_labels.json = {
    "labels":   [display_name per centerline index],     # e.g. "E 52ND AVE"
    "raw":      [osm_name per index or null],
    "segments": [{name, start_idx, end_idx, start_station_m, entry_xz:[x,z], length_m}, ...],
  }

so downstream passes (painted street-name decals, the Sand Creek camber/dip) can ask "where does the
driver turn onto X" and "which indices are Sand Creek Drive" by NAME, not by guessing corners.

Pure stdlib + the existing ``scripts.gps.overpass`` fetcher. Run:
    python -m scripts.gps.street_labels projects/sand-creek-raceway
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from scripts.geometry.projection import _meters_per_degree
from scripts.gps.overpass import OVERPASS_URL, USER_AGENT  # noqa: F401  (kept for parity / discoverability)

Vertex = tuple[float, float]


def _fetch_named_highways(bbox: tuple[float, float, float, float], *, retries: int = 3):
    """All named highway ways within bbox -> [(name, [(lon,lat)...]), ...]. One Overpass call so we
    discover the ACTUAL OSM names along the route instead of guessing 'East 52nd Avenue' vs 'E 52nd'."""
    import time
    import urllib.parse
    import urllib.request
    s, w, n, e = bbox
    q = (f"[out:json][timeout:90];\n"
         f'(way["highway"]["name"]({s},{w},{n},{e}););\n'
         f"out geom;")
    body = urllib.parse.urlencode({"data": q}).encode()
    payload: dict = {}
    for attempt in range(retries):
        req = urllib.request.Request(OVERPASS_URL, data=body,
                                     headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
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
        name = el.get("tags", {}).get("name")
        geom = [(g["lon"], g["lat"]) for g in el.get("geometry") or []]
        if name and len(geom) >= 2:
            ways.append((name, geom))
    return ways


def _abbrev(name: str) -> str:
    """OSM full name -> compact pavement label. 'East 52nd Avenue' -> 'E 52ND AVE'."""
    repl = {
        "North": "N", "South": "S", "East": "E", "West": "W",
        "Avenue": "AVE", "Street": "ST", "Boulevard": "BLVD", "Drive": "DR",
        "Road": "RD", "Lane": "LN", "Court": "CT", "Place": "PL", "Parkway": "PKWY",
        "Highway": "HWY", "Frontage": "FRONTAGE", "Service": "SVC", "Circle": "CIR",
    }
    out = []
    for tok in name.split():
        out.append(repl.get(tok, tok).upper())
    return " ".join(out)


def _seg_dist2(px: float, pz: float, ax: float, az: float, bx: float, bz: float) -> float:
    """Squared distance from point P to segment AB in the X-Z plane."""
    dx, dz = bx - ax, bz - az
    L2 = dx * dx + dz * dz
    if L2 <= 1e-12:
        return (px - ax) ** 2 + (pz - az) ** 2
    t = ((px - ax) * dx + (pz - az) * dz) / L2
    t = max(0.0, min(1.0, t))
    cx, cz = ax + t * dx, az + t * dz
    return (px - cx) ** 2 + (pz - cz) ** 2


def label(project_dir: str | Path, *, match_m: float = 28.0, smooth_win: int = 9,
          min_run: int = 18) -> dict:
    project_dir = Path(project_dir)
    data = project_dir / "data"
    local = json.loads((data / "centerline.local.json").read_text())
    pts = local["points_xyz_m"]                       # [x, y, z] local metres, UNMIRRORED
    origin = (local["origin"]["lon"], local["origin"]["lat"])
    geo = json.loads((data / "centerline.geojson").read_text())
    coords = next(f for f in geo["features"]
                  if f["properties"].get("kind") == "full")["geometry"]["coordinates"]

    # bbox from the real centerline lon/lat + a margin (config bbox can be tighter than the loop).
    lons = [c[0] for c in coords]; lats = [c[1] for c in coords]
    mlon, mlat = _meters_per_degree(origin[1])
    pad = 300.0
    bbox = (min(lats) - pad / mlat, min(lons) - pad / mlon,
            max(lats) + pad / mlat, max(lons) + pad / mlon)
    print(f"[street_labels] bbox(s,w,n,e)={tuple(round(b,5) for b in bbox)}  fetching named highways…")
    ways = _fetch_named_highways(bbox)
    print(f"[street_labels] fetched {len(ways)} named highway ways "
          f"({len({nm for nm,_ in ways})} distinct names)")

    lon0, lat0 = origin

    def to_local(lon: float, lat: float) -> Vertex:
        return ((lon - lon0) * mlon, (lat - lat0) * mlat)

    # project ways to local; bucket segments on a coarse grid for fast nearest lookup
    CELL = 60.0
    from collections import defaultdict
    grid: dict[tuple[int, int], list] = defaultdict(list)
    for name, geom in ways:
        lp = [to_local(lo, la) for lo, la in geom]
        for k in range(len(lp) - 1):
            (ax, az), (bx, bz) = lp[k], lp[k + 1]
            ci0, cj0 = int(min(ax, bx) // CELL), int(min(az, bz) // CELL)
            ci1, cj1 = int(max(ax, bx) // CELL), int(max(az, bz) // CELL)
            for ci in range(ci0 - 1, ci1 + 2):
                for cj in range(cj0 - 1, cj1 + 2):
                    grid[(ci, cj)].append((name, ax, az, bx, bz))

    match2 = match_m * match_m
    raw: list[str | None] = []
    for x, _y, z in pts:
        ci, cj = int(x // CELL), int(z // CELL)
        best_d2, best_name = match2, None
        seen = grid.get((ci, cj), [])
        # widen search a ring if nothing close (segments may bucket into a neighbour)
        if not seen:
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    seen = seen + grid.get((ci + di, cj + dj), [])
        for name, ax, az, bx, bz in seen:
            d2 = _seg_dist2(x, z, ax, az, bx, bz)
            if d2 < best_d2:
                best_d2, best_name = d2, name
        raw.append(best_name)

    n = len(raw)

    # majority filter (mode within a window) to kill single-vertex flips at junctions
    def mode_window(i: int) -> str | None:
        from collections import Counter
        c: Counter = Counter()
        for k in range(max(0, i - smooth_win), min(n, i + smooth_win + 1)):
            if raw[k] is not None:
                c[raw[k]] += 1
        return c.most_common(1)[0][0] if c else None

    smoothed = [mode_window(i) for i in range(n)]

    # forward/back fill any remaining None (gaps shorter than the window with no nearby way)
    for i in range(n):
        if smoothed[i] is None:
            j = i
            while j < n and smoothed[j] is None:
                j += 1
            fill = smoothed[j] if j < n else None
            if fill is None:
                k = i
                while k >= 0 and smoothed[k] is None:
                    k -= 1
                fill = smoothed[k] if k >= 0 else "?"
            for t in range(i, min(j, n)):
                smoothed[t] = fill

    # merge runs shorter than min_run into the previous run (spurious cross-street touches)
    def runs_of(seq):
        out, i = [], 0
        while i < len(seq):
            j = i
            while j < len(seq) and seq[j] == seq[i]:
                j += 1
            out.append([seq[i], i, j])  # name, start, end(excl)
            i = j
        return out

    rr = runs_of(smoothed)
    changed = True
    while changed and len(rr) > 1:
        changed = False
        for idx, (nm, a, b) in enumerate(rr):
            if b - a < min_run:
                # absorb into the longer neighbour
                prev_len = (rr[idx - 1][2] - rr[idx - 1][1]) if idx > 0 else -1
                next_len = (rr[idx + 1][2] - rr[idx + 1][1]) if idx + 1 < len(rr) else -1
                tgt = idx - 1 if prev_len >= next_len else idx + 1
                if 0 <= tgt < len(rr):
                    rr[tgt][1] = min(rr[tgt][1], a)
                    rr[tgt][2] = max(rr[tgt][2], b)
                    rr.pop(idx)
                    changed = True
                    break
    # rebuild a clean per-index label from merged runs
    labels = [None] * n
    for nm, a, b in rr:
        for i in range(a, b):
            labels[i] = nm

    # station (arc length) per index
    st = [0.0]
    for i in range(1, n):
        st.append(st[-1] + math.hypot(pts[i][0] - pts[i - 1][0], pts[i][2] - pts[i - 1][2]))

    segments = []
    for nm, a, b in rr:
        segments.append({
            "name": _abbrev(nm), "osm_name": nm,
            "start_idx": a, "end_idx": b - 1,
            "start_station_m": round(st[a], 1),
            "length_m": round(st[b - 1] - st[a], 1),
            "entry_xz": [round(pts[a][0], 1), round(pts[a][2], 1)],
        })

    out = {
        "labels": [_abbrev(x) if x else "?" for x in labels],
        "raw": labels,
        "segments": segments,
    }
    (data / "street_labels.json").write_text(json.dumps(out, indent=1))
    print(f"[street_labels] wrote data/street_labels.json — {len(segments)} street legs:")
    for s in segments:
        print(f"   idx {s['start_idx']:4d}-{s['end_idx']:<4d} sta {s['start_station_m']:7.0f}m "
              f"len {s['length_m']:6.0f}m  {s['name']:<16s}  (osm: {s['osm_name']})")
    return out


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m scripts.gps.street_labels <project-dir>")
    label(sys.argv[1])


if __name__ == "__main__":
    main()
