"""Assign REAL per-vertex road widths from OSM lanes — Sand Creek et al. felt like half the real road
space because every vertex was a flat ``default_width_m``. This map-matches the EXISTING centerline
(keeps the route) to OSM drivable ways per vertex, computes curb-to-curb pavement width from each way's
``lanes``/class, smooths the transitions, and rewrites ``data/centerline.local.json`` widths_m in place.

OSM has no ``width`` tags on these streets but tags ``lanes`` on the arterials; residential side streets
are untagged (→ class default). Width = ``max(lanes, class-min) × lane_w + class curb-to-curb allowance``
(parking/turn/shoulder), so a primary arterial (Colorado Blvd, Quebec) comes out ~2× a side street, matching
reality. Also detects OSM junction nodes near the centerline and writes ``data/intersections.local.json`` for
build_mesh to lay paved intersection pads.

    python -m scripts.gps.widths_from_osm tracks/sand-creek
"""
from __future__ import annotations

import json
import math
import sys
import time
import urllib.request
from pathlib import Path

Vertex = tuple[float, float]
LANE_W = 3.5
# class → (minimum lane count, curb-to-curb allowance beyond the lanes: parking/turn/shoulder/gutter, m)
CLASS = {
    "motorway": (4, 4.0), "trunk": (4, 4.0), "primary": (4, 6.0), "secondary": (3, 5.0),
    "tertiary": (2, 4.0), "unclassified": (2, 3.0), "residential": (2, 4.0), "service": (1, 1.5),
}
# The default table is tuned for URBAN US arterials (parking/turn lanes, 4-lane trunk floors). On a
# rural/mountain route those assumptions systematically over-widen: the Lariat Trail is a 6.5 m
# mountain road, not an 11 m boulevard, and 2-lane US-40 is not an 18 m runway. Select with
# route.width_profile = "mountain" in track.config.json — narrower lanes (3.0 m), no urban
# allowances, class minimums at the rural reality (2 lanes).
MOUNTAIN_LANE_W = 3.0
MOUNTAIN_CLASS = {
    "motorway": (2, 3.0), "trunk": (2, 3.0), "primary": (2, 4.0), "secondary": (2, 3.0),
    "tertiary": (2, 0.5), "unclassified": (2, 0.3), "residential": (2, 0.0), "service": (1, 1.0),
}
MIRRORS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
           "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]


def haversine_m(a: Vertex, b: Vertex) -> float:
    r = 6_371_000.0
    (lo1, la1), (lo2, la2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))
DRIVABLE = "motorway|trunk|primary|secondary|tertiary|unclassified|residential|service"


def _overpass(query: str, *, timeout: int = 120) -> dict:
    last = None
    for m in MIRRORS:
        for attempt in range(2):
            try:
                req = urllib.request.Request(m, data=query.encode(), headers={"User-Agent": "k10-colorado/1.0"})
                return json.load(urllib.request.urlopen(req, timeout=timeout))
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(2)
    raise SystemExit(f"overpass unreachable (all mirrors): {last}")


def fetch_ways(bbox: tuple[float, float, float, float]) -> list[dict]:
    """Every drivable OSM way in bbox WITH the tags we need: id, name, highway, lanes, width, node ids, geom."""
    s, w, n, e = bbox
    q = (f'[out:json][timeout:120];(way["highway"~"^({DRIVABLE})(_link)?$"]({s},{w},{n},{e}););out geom;')
    d = _overpass(q)
    ways = []
    for el in d.get("elements", []):
        if el.get("type") != "way" or "geometry" not in el:
            continue
        t = el.get("tags", {})
        ways.append({"id": el["id"], "name": t.get("name"), "ref": t.get("ref"), "highway": t.get("highway"),
                     "lanes": t.get("lanes"), "width": t.get("width"), "nodes": el.get("nodes", []),
                     "geom": [(g["lon"], g["lat"]) for g in el["geometry"]]})
    return ways


def pavement_width(way: dict | None, profile: str = "urban") -> float:
    """Curb-to-curb metres for an OSM way (or a sane default if unmatched)."""
    table, lane_w = (MOUNTAIN_CLASS, MOUNTAIN_LANE_W) if profile == "mountain" else (CLASS, LANE_W)
    if way is None:
        return 6.5 if profile == "mountain" else 9.0
    if way.get("width"):
        try:
            return float(str(way["width"]).split()[0])
        except ValueError:
            pass
    hw = way.get("highway", "residential")
    if hw.endswith("_link"):                   # interchange ramp: one lane + shoulder, either profile
        return round(lane_w + 2.0, 1)
    cmin, extra = table.get(hw, (2, 3.0))
    # An explicit lanes tag is ground truth — a tagged 2-lane secondary is 2 lanes, not the class
    # floor (flooring turned Sand Creek's tagged 1- and 2-lane streets into 15.5 m highways).
    # The class floor only fills in when OSM doesn't say.
    try:
        lanes = max(int(way["lanes"]), 1)
    except (TypeError, ValueError):
        lanes = cmin
    return round(lanes * lane_w + extra, 1)


def _match_per_vertex(cl: list[Vertex], ways: list[dict], *, max_snap_m: float = 26.0, min_run: int = 5):
    """Nearest OSM way index per centerline vertex (−1 = off-network), de-jittered into runs ≥ min_run."""
    lat0 = sum(p[1] for p in cl) / len(cl)
    kx = 111320.0 * math.cos(math.radians(lat0)); ky = 110540.0; lon0 = cl[0][0]

    def loc(lon, lat):
        return ((lon - lon0) * kx, (lat - lat0) * ky)

    segs = []  # (ax, ay, bx, by, way_idx)
    for wi, wv in enumerate(ways):
        xy = [loc(lo, la) for lo, la in wv["geom"]]
        for i in range(len(xy) - 1):
            segs.append((xy[i][0], xy[i][1], xy[i + 1][0], xy[i + 1][1], wi))
    idx = []
    for lon, lat in cl:
        px, py = loc(lon, lat)
        best_d2, best_wi = max_snap_m * max_snap_m, -1
        for ax, ay, bx, by, wi in segs:
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy or 1e-9
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
            cx, cy = ax + t * dx, ay + t * dy
            d2 = (px - cx) ** 2 + (py - cy) ** 2
            if d2 < best_d2:
                best_d2, best_wi = d2, wi
        idx.append(best_wi)
    # de-jitter: replace runs shorter than min_run with the previous kept way
    out = idx[:]
    i = 0
    n = len(out)
    while i < n:
        j = i
        while j < n and out[j] == out[i]:
            j += 1
        if j - i < min_run and i > 0:
            for k in range(i, j):
                out[k] = out[i - 1]
        i = j
    return out


def _smooth(vals: list[float], win: int = 9) -> list[float]:
    n = len(vals); h = win // 2
    return [sum(vals[max(0, i - h):min(n, i + h + 1)]) / (min(n, i + h + 1) - max(0, i - h)) for i in range(n)]


MAJOR = {"motorway", "trunk", "primary", "secondary", "tertiary"}   # "real" intersections cross these


def detect_junctions(ways: list[dict], cl: list[Vertex], *, near_m: float = 16.0,
                     profile: str = "urban") -> list[dict]:
    """Real intersections the circuit drives THROUGH: OSM nodes where ≥2 ARTERIAL (tertiary+) ways meet,
    within ``near_m`` of the racing line — the corners where the circuit turns street-to-street and the
    major crossings, NOT every minor residential T-junction. Returns ``[{idx, size}]`` — the centerline
    vertex index nearest the junction + the intersection's pavement size — used to FLARE the road ribbon
    width there (one continuous surface, no overlapping pad → no bumps)."""
    from collections import defaultdict
    node_ll: dict[int, Vertex] = {}
    node_ways: dict[int, list[int]] = defaultdict(list)
    for wi, wv in enumerate(ways):
        for nid, (lo, la) in zip(wv.get("nodes", []), wv["geom"]):
            node_ll[nid] = (lo, la)
            node_ways[nid].append(wi)
    lat0 = sum(p[1] for p in cl) / len(cl)
    kx = 111320.0 * math.cos(math.radians(lat0)); ky = 110540.0
    clx = [c[0] * kx for c in cl]; clz = [c[1] * ky for c in cl]
    out: list[dict] = []
    for nid, wl in node_ways.items():
        major = {wi for wi in wl if ways[wi].get("highway") in MAJOR}
        if len(major) < 2:
            continue
        lo, la = node_ll[nid]
        nx, nz = lo * kx, la * ky
        best_k, best_d2 = -1, near_m * near_m
        for k in range(len(cl)):
            d2 = (nx - clx[k]) ** 2 + (nz - clz[k]) ** 2
            if d2 < best_d2:
                best_d2, best_k = d2, k
        if best_k >= 0:
            out.append({"idx": best_k, "size": max(pavement_width(ways[wi], profile) for wi in major) + 3.0})
    return out


def detect_turn_corners(ways: list[dict], cl: list[Vertex], *, profile: str = "urban",
                        kink_deg: float = 45.0, near_m: float = 16.0) -> list[dict]:
    """Corners where the LAP ITSELF turns street-to-street hard — junction-grade pavement
    regardless of road class. detect_junctions gates on >=2 ARTERIAL ways, which misses a
    minor-street corner mouth entirely (Kevin: "a corner at the bottom of the hill that is
    impossible to make ... treat it like a wide intersection"). A heading kink > kink_deg
    concentrated within +-12 m, at a vertex near an OSM node shared by >=2 drivable ways,
    is a paved corner — flare it like an intersection. Mountain hairpins never trigger:
    they are one way bending back on itself, not two ways sharing a node."""
    from collections import defaultdict
    lat0 = sum(p[1] for p in cl) / len(cl)
    kx = 111320.0 * math.cos(math.radians(lat0)); ky = 110540.0
    xs = [c[0] * kx for c in cl]; zs = [c[1] * ky for c in cl]
    st = [0.0]
    for i in range(1, len(cl)):
        st.append(st[-1] + math.hypot(xs[i] - xs[i - 1], zs[i] - zs[i - 1]))
    node_ll: dict = {}
    node_ways: dict = defaultdict(set)
    for wi, wv in enumerate(ways):
        for nid, (lo, la) in zip(wv.get("nodes", []), wv["geom"]):
            node_ll[nid] = (lo * kx, la * ky)
            node_ways[nid].add(wi)

    def _ident(wi):
        wv = ways[wi]
        return (wv.get("name") or wv.get("ref") or f"way{wv.get('id', wi)}").strip().lower()

    # >=2 DISTINCT ROADS at the node — OSM splits one road into multiple ways at every junction
    # node, so counting ways flags every hairpin of a split mountain road as a 'corner' (the
    # Lariat's switchbacks all got 14 m junction pads). Same name/ref continuing = one road.
    shared = [(node_ll[nid], ws) for nid, ws in node_ways.items()
              if len({_ident(w) for w in ws}) >= 2]

    def hd(i):
        a, b = max(0, i - 2), min(len(cl) - 1, i + 2)
        return math.atan2(zs[b] - zs[a], xs[b] - xs[a])

    out: list[dict] = []
    i, n = 0, len(cl)
    while i < n:
        j0 = i
        while j0 > 0 and st[i] - st[j0] < 12.0:
            j0 -= 1
        j1 = i
        while j1 < n - 1 and st[j1] - st[i] < 12.0:
            j1 += 1
        d = abs((math.degrees(hd(j1) - hd(j0)) + 180.0) % 360.0 - 180.0)
        if d > kink_deg:
            px, pz = xs[i], zs[i]
            near = [ws for (qx, qz), ws in shared if (qx - px) ** 2 + (qz - pz) ** 2 <= near_m * near_m]
            if near:
                wset = set().union(*near)
                two = sorted((pavement_width(ways[w], profile) for w in wset), reverse=True)[:2]
                out.append({"idx": i, "size": round(max(14.0, sum(two) * 0.9), 1)})
                i = j1 + 5      # one corner, one flare
                continue
        i += 1
    return out


def flare_widths(widths: list[float], cl: list[Vertex], junctions: list[dict], *, blend_m: float = 7.0) -> list[float]:
    """Widen the road at each junction to the intersection's pavement size, tapering back to the street
    width over ``blend_m`` — a smooth flare baked INTO the ribbon width (no overlapping pad, so no bump).
    Half the pad extends ahead/behind the junction along the road; the taper reaches ``blend_m`` past that."""
    lat0 = sum(p[1] for p in cl) / len(cl)
    kx = 111320.0 * math.cos(math.radians(lat0)); ky = 110540.0
    arc = [0.0]
    for i in range(1, len(cl)):
        arc.append(arc[-1] + math.hypot((cl[i][0] - cl[i - 1][0]) * kx, (cl[i][1] - cl[i - 1][1]) * ky))
    out = widths[:]
    n = len(widths)
    for j in junctions:
        k = min(j["idx"], n - 1)
        size = j["size"]
        half = size / 2.0                                 # full flare within ±half of the junction
        reach = half + blend_m
        for i in range(n):
            d = abs(arc[i] - arc[k])
            if d > reach:
                continue
            f = 1.0 if d <= half else max(0.0, 1.0 - (d - half) / blend_m)   # 1 at the box, taper to 0
            out[i] = max(out[i], out[i] + (size - out[i]) * f)              # only ever widens
    return out


def build(project_dir: str | Path) -> dict:
    project = Path(project_dir)
    data = project / "data"
    gj = json.loads((data / "centerline.geojson").read_text())
    feats = gj.get("features", [gj])
    line = next((f for f in feats if f.get("geometry", {}).get("type") == "LineString"), None)
    cl = [(c[0], c[1]) for c in line["geometry"]["coordinates"]]
    local = json.loads((data / "centerline.local.json").read_text())
    n_local = len(local["widths_m"])
    if len(cl) != n_local:
        print(f"  [widths_from_osm] NOTE centerline.geojson has {len(cl)} pts, local has {n_local}; "
              f"matching on the shorter and nearest-resampling widths")
    lons = [c[0] for c in cl]; lats = [c[1] for c in cl]
    pad = 0.0015
    bbox = (min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad)
    route = json.loads((project / "track.config.json").read_text()).get("route", {}) or {}
    profile = route.get("width_profile", "urban")
    ways = fetch_ways(bbox)
    # IDENTITY LOCK: freeway-class ways may donate widths ONLY when the route declares them
    # (by name, "ref:NAME" or "id:N,N"). A street circuit beside I-270/US-85 was inheriting
    # freeway widths from nearest-way matching — "sand creek is fucking highways".
    _decl_names, _decl_refs, _decl_ids = set(), set(), set()
    for _r in route.get("roads") or []:
        if _r.startswith("ref:"):
            _decl_refs.add(_r[4:].strip().lower())
        elif _r.startswith("id:"):
            _decl_ids.update(int(t) for t in _r[3:].split(",") if t.strip())
        else:
            _decl_names.add(_r.strip().lower())
    _FREEWAY = {"motorway", "trunk", "motorway_link", "trunk_link"}
    _banned = 0
    _kept = []
    for _w in ways:
        if _w.get("highway") in _FREEWAY:
            _nm = (_w.get("name") or "").strip().lower()
            _rf = (_w.get("ref") or "").strip().lower()
            if _w["id"] not in _decl_ids and _nm not in _decl_names \
                    and not (_rf and any(_rf == d or d in _rf.split(";") for d in _decl_refs)):
                _banned += 1
                continue
        _kept.append(_w)
    if _banned:
        print(f"  [widths_from_osm] identity lock: {_banned} undeclared freeway-class ways excluded from matching")
    ways = _kept
    # cache street geometry for the pack stage's OSM-style preview (no network at pack time)
    (data / "osm.ways.json").write_text(json.dumps(
        [{"highway": w.get("highway"), "geom": w["geom"]} for w in ways]), encoding="utf-8")
    idx = _match_per_vertex(cl, ways)
    widths_cl = [pavement_width(ways[wi] if wi >= 0 else None, profile) for wi in idx]
    widths_cl = _smooth(widths_cl)
    # FLARE the ribbon at the real intersections the circuit drives through (widen the road itself, one
    # continuous surface — NOT an overlapping pad, which poked above the sloped road and read as bumps).
    junctions = detect_junctions(ways, cl, profile=profile)
    corners = detect_turn_corners(ways, cl, profile=profile)
    if corners:
        print(f"  [widths_from_osm] {len(corners)} hard route corners flared to junction grade "
              f"(sizes {[c['size'] for c in corners]})")
    widths_cl = flare_widths(widths_cl, cl, junctions + corners)
    # U-TURN FLARE, no identity gate: where the ROUTE FOLDS BACK on itself (>110 deg within
    # ±15 m) the corner is undrivable at street width no matter whose node it is — Kevin's
    # bottom-of-the-hill corner is 165 deg at 9 m wide with a fast downhill approach. Mountain
    # switchbacks stay under this threshold (a 20 m-radius hairpin turns ~86 deg per ±15 m).
    _lat0u = sum(p[1] for p in cl) / len(cl)
    _kxu = 111320.0 * math.cos(math.radians(_lat0u)); _kyu = 110540.0
    _stu = [0.0]
    for _i in range(1, len(cl)):
        _stu.append(_stu[-1] + math.hypot((cl[_i][0] - cl[_i - 1][0]) * _kxu,
                                          (cl[_i][1] - cl[_i - 1][1]) * _kyu))

    def _hdu(i):
        a, b = max(0, i - 3), min(len(cl) - 1, i + 3)
        return math.atan2((cl[b][1] - cl[a][1]) * _kyu, (cl[b][0] - cl[a][0]) * _kxu)

    _uturns = 0
    for _i in range(len(cl)):
        _j0 = _i
        while _j0 > 0 and _stu[_i] - _stu[_j0] < 15.0:
            _j0 -= 1
        _j1 = _i
        while _j1 < len(cl) - 1 and _stu[_j1] - _stu[_i] < 15.0:
            _j1 += 1
        _du = abs((math.degrees(_hdu(_j1) - _hdu(_j0)) + 180.0) % 360.0 - 180.0)
        if _du > 110.0 and widths_cl[_i] < 16.0:
            widths_cl[_i] = 16.0
            _uturns += 1
    if _uturns:
        print(f"  [widths_from_osm] u-turn flare: {_uturns} verts widened to 16 m (route folds >110 deg)")
    # TAPER RATE LIMIT: real lane adds/gores open at ~1:7 or shallower. Map-matched widths step
    # hard when the matched way changes (mainline vs turn pocket vs ramp) — up to 6 m per 3 m
    # vertex on the US-6 corridor — and each step ships as a sawtooth edge / a 1-2 m shoulder
    # cliff at the gore. Forward+backward passes cap |dW/ds| at 0.15 m/m.
    st = [0.0]
    for i in range(1, len(cl)):
        st.append(st[-1] + haversine_m(cl[i - 1], cl[i]))
    for rng in (range(1, len(widths_cl)), range(len(widths_cl) - 2, -1, -1)):
        for i in rng:
            j = i - 1 if rng.step == 1 else i + 1
            ds = abs(st[i] - st[j])
            if widths_cl[i] > widths_cl[j] + 0.15 * ds:
                widths_cl[i] = widths_cl[j] + 0.15 * ds
    # resample widths to the LOCAL vertex count if they differ (nearest by fractional index)
    if len(widths_cl) != n_local:
        widths = [widths_cl[min(len(widths_cl) - 1, round(i * (len(widths_cl) - 1) / max(n_local - 1, 1)))]
                  for i in range(n_local)]
    else:
        widths = widths_cl
    widths = [round(w, 2) for w in widths]
    local["widths_m"] = widths
    local["default_width_m"] = round(sum(widths) / len(widths), 2)
    (data / "centerline.local.json").write_text(json.dumps(local), encoding="utf-8")
    # the overlapping-pad approach is gone; remove any stale pad file so build_mesh lays none.
    try:
        (data / "intersections.local.json").unlink()
    except OSError:
        pass

    # per-street summary
    from collections import defaultdict
    seg = defaultdict(int)
    for wi in idx:
        seg[wi] += 1
    named = []
    for wi, cnt in sorted(seg.items(), key=lambda x: -x[1]):
        wv = ways[wi] if wi >= 0 else None
        named.append((cnt, (wv or {}).get("name") or ("off-network" if wi < 0 else f"way{wi}"),
                      (wv or {}).get("highway", "-"), (wv or {}).get("lanes", "-"), pavement_width(wv, profile)))
    import statistics
    stats = {"vertices": n_local, "min_w": min(widths), "median_w": statistics.median(widths),
             "max_w": max(widths), "matched_pct": round(100 * sum(1 for w in idx if w >= 0) / len(idx), 1),
             "junctions": len(junctions), "streets": named[:16]}
    return stats


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m scripts.gps.widths_from_osm <project-dir>")
    st = build(sys.argv[1])
    print(f"widths_from_osm: {st['vertices']} verts  matched {st['matched_pct']}%  "
          f"width min/median/max = {st['min_w']}/{st['median_w']}/{st['max_w']} m  "
          f"junction flares = {st['junctions']}")
    for cnt, nm, hw, ln, w in st["streets"]:
        print(f"  {cnt:5d}v  {nm:32s} {hw:12s} lanes={ln!s:>3}  -> {w} m")


if __name__ == "__main__":
    main()
