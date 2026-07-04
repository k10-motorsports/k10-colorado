"""Phase: ingest a Prodrive Scan session bundle → committed real-world evidence.

Projects every captured GPS fix into the ``centerline.local.json`` frame the geometry pipeline built
(reusing ``scripts.geometry.projection._meters_per_degree`` + the origin in that file), matches each to
the centerline to get an arc-length **station** + a **signed lateral offset** in that un-mirrored ENU
frame (NOT driver-POV L/R). NB: ``centerline.local.json`` is always un-mirrored — the config
``mirror_x`` is applied later by build_mesh/build_env, so ingest must not apply it. The run is then
distilled into per-station evidence and merged into ``projects/<slug>/source/realworld_capture.json``.

Output is deterministic (no wall-clock fields) so re-ingesting the same bundle is a no-op diff, and
each source carries its own distilled per-station summary so the committed file survives even after the
raw multi-GB bundle is deleted (bundles live outside git, like the mods). Multiple laps accumulate in
``sources[]`` and strengthen the merged ``stations[]``.

Only the evidence layer is produced here; ``placements[]`` / ``texture_overrides[]`` (consumed later
by build_env) are left empty. Pure stdlib — runs under the system ``python3``.

Run:  python -m scripts.capture.ingest projects/<slug> <bundle-dir>
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

from scripts.capture import bundle as bundle_mod
from scripts.config import load_config
from scripts.geometry.projection import _meters_per_degree

OUTPUT_SCHEMA_VERSION = 1
STATION_BIN_M = 5.0          # distil to one evidence point every 5 m of centerline
MAX_FRAMES_PER_STATION = 3   # keep a few representative frame refs per station (lowest GPS error)
GRADE_WINDOW_BINS = 6        # ±6 bins (~60 m) least-squares slope — wide enough to smooth GPS-alt noise
FRAME = "ENU local meters (X=east, Y=up, Z=north)"


# --------------------------------------------------------------------------- geometry helpers


def _cumulative_stations(pts_xz: list[tuple[float, float]]) -> list[float]:
    """Arc length to each centerline vertex, in mesh metres (the build_env station convention)."""
    s = [0.0]
    for i in range(1, len(pts_xz)):
        (x0, z0), (x1, z1) = pts_xz[i - 1], pts_xz[i]
        s.append(s[-1] + math.hypot(x1 - x0, z1 - z0))
    return s


def match_to_centerline(px: float, pz: float, pts_xz: list[tuple[float, float]],
                        stations: list[float]) -> tuple[float, float]:
    """Nearest point on the centerline polyline → (station_m, signed_lateral_m).

    Lateral sign is the cross product of the centerline tangent with (point - segment-start) in mesh
    XZ — a frame-space quantity, stable under whatever lateral-flip the mesh used.
    """
    best_d2 = float("inf")
    best_station = 0.0
    best_lateral = 0.0
    for i in range(len(pts_xz) - 1):
        ax, az = pts_xz[i]
        bx, bz = pts_xz[i + 1]
        dx, dz = bx - ax, bz - az
        seg2 = dx * dx + dz * dz
        if seg2 < 1e-9:
            continue
        t = ((px - ax) * dx + (pz - az) * dz) / seg2
        t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
        cx, cz = ax + t * dx, az + t * dz
        d2 = (px - cx) ** 2 + (pz - cz) ** 2
        if d2 < best_d2:
            best_d2 = d2
            seg_len = math.sqrt(seg2)
            best_station = stations[i] + t * seg_len
            # signed perpendicular: cross(unit_tangent, point - segment_start)
            best_lateral = (dx * (pz - az) - dz * (px - ax)) / seg_len
    return best_station, best_lateral


# --------------------------------------------------------------------------- per-source distillation


def _distil_source(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bin matched samples (one per GPS fix) into ~5 m stations with averaged evidence + grade.

    Each record: {station, lateral, gps_alt|None, baro_rel|None, hacc|None, i, t}.
    """
    bins: dict[int, dict[str, Any]] = {}
    for r in records:
        idx = int(r["station"] // STATION_BIN_M)
        b = bins.setdefault(idx, {"lat": [], "alt": [], "hacc": [], "frames": []})
        b["lat"].append(r["lateral"])
        if r["gps_alt"] is not None:
            b["alt"].append(r["gps_alt"])
        if r["hacc"] is not None:
            b["hacc"].append(r["hacc"])
        b["frames"].append({"i": r["i"], "t": round(r["t"], 3),
                            "hacc": r["hacc"] if r["hacc"] is not None else 1e9})

    stations: list[dict[str, Any]] = []
    for idx in sorted(bins):
        b = bins[idx]
        n = len(b["lat"])
        frames = sorted(b["frames"], key=lambda f: f["hacc"])[:MAX_FRAMES_PER_STATION]
        # medians, not means: a station is visited on every lap, so pooling all passes and taking the
        # median is robust to the odd off-line GPS fix or vertical spike.
        stations.append({
            "station_m": round(idx * STATION_BIN_M, 2),
            "lat_offset_m": round(_median(b["lat"]), 3),
            "alt_m": round(_median(b["alt"]), 3) if b["alt"] else None,
            "gps_hacc_m": round(_mean(b["hacc"]), 2) if b["hacc"] else None,
            "n": n,
            "frames": [{"i": f["i"], "t": f["t"]} for f in frames],
        })

    _fill_grade(stations)
    return stations


def _fill_grade(stations: list[dict[str, Any]]) -> None:
    """Grade (%) as an n-weighted least-squares slope of GPS altitude over a ±``GRADE_WINDOW_BINS``
    window. GPS altitude is absolute and drift-free; the barometer's relative-altitude reference drifts
    over a long multi-lap session (weather + sensor) and yields wild grades (±80%+ on real data). The
    median-per-station alt + wide window + n-weighting smooth the residual GPS vertical noise to a sane
    ±3% for gentle terrain. Mutates each station, adding ``grade_pct`` (None where alt is too sparse)."""
    n = len(stations)
    for k in range(n):
        xs, ys, ws = [], [], []
        for s in stations[max(0, k - GRADE_WINDOW_BINS):k + GRADE_WINDOW_BINS + 1]:
            if s["alt_m"] is not None:
                xs.append(s["station_m"]); ys.append(s["alt_m"]); ws.append(s["n"])
        stations[k]["grade_pct"] = round(_wls_slope(xs, ys, ws) * 100.0, 2) if len(xs) >= 2 else None


def _wls_slope(xs: list[float], ys: list[float], ws: list[float]) -> float:
    """Weighted least-squares slope dy/dx (0.0 if degenerate)."""
    wsum = sum(ws)
    xm = sum(w * x for w, x in zip(ws, xs)) / wsum
    ym = sum(w * y for w, y in zip(ws, ys)) / wsum
    den = sum(w * (x - xm) ** 2 for w, x in zip(ws, xs))
    if den < 1e-9:
        return 0.0
    return sum(w * (x - xm) * (y - ym) for w, x, y in zip(ws, xs, ys)) / den


# --------------------------------------------------------------------------- cross-source merge


def _aggregate(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-build the top-level ``stations[]`` as an n-weighted merge over every source's distilled
    stations, keyed on the shared 5 m grid. Frame refs carry their owning bundle."""
    groups: dict[float, dict[str, Any]] = {}
    for src in sources:
        for s in src["stations"]:
            g = groups.setdefault(s["station_m"],
                                  {"lat": 0.0, "alt": 0.0, "altw": 0, "hacc": 0.0, "haccw": 0,
                                   "grade": 0.0, "gradew": 0, "n": 0, "sources": set(), "frames": []})
            n = s["n"]
            g["n"] += n
            g["lat"] += s["lat_offset_m"] * n
            g["sources"].add(src["bundle"])
            if s["alt_m"] is not None:
                g["alt"] += s["alt_m"] * n; g["altw"] += n
            if s["gps_hacc_m"] is not None:
                g["hacc"] += s["gps_hacc_m"] * n; g["haccw"] += n
            if s["grade_pct"] is not None:
                g["grade"] += s["grade_pct"] * n; g["gradew"] += n
            for f in s["frames"]:
                g["frames"].append({"bundle": src["bundle"], "i": f["i"], "t": f["t"]})

    out = []
    for station_m in sorted(groups):
        g = groups[station_m]
        out.append({
            "station_m": station_m,
            "lat_offset_m": round(g["lat"] / g["n"], 3),
            "grade_pct": round(g["grade"] / g["gradew"], 2) if g["gradew"] else None,
            "alt_m": round(g["alt"] / g["altw"], 3) if g["altw"] else None,
            "gps_hacc_m": round(g["hacc"] / g["haccw"], 2) if g["haccw"] else None,
            "n": g["n"],
            "sources": sorted(g["sources"]),
            "frames": g["frames"][:MAX_FRAMES_PER_STATION],
        })
    return out


# --------------------------------------------------------------------------- driver


def build(project_dir: str | Path, bundle_dir: str | Path) -> dict:
    project_dir = Path(project_dir)
    bundle_dir = Path(bundle_dir)
    data = project_dir / "data"

    problems = bundle_mod.validate(bundle_dir)
    if problems:
        raise ValueError("bad bundle:\n  - " + "\n  - ".join(problems))
    man = bundle_mod.load_manifest(bundle_dir)

    cfg = load_config(project_dir)
    local = json.loads((data / "centerline.local.json").read_text(encoding="utf-8"))
    origin = local["origin"]
    lon0, lat0, elev0 = origin["lon"], origin["lat"], origin["elev_m"]
    m_lon, m_lat = _meters_per_degree(lat0)

    # centerline.local.json is UN-mirrored ENU: projection.py never mirrors — the config `mirror_x` is
    # applied LATER, when build_mesh/build_env/build_kn5 construct the actual mesh. So we match GPS in
    # this same un-mirrored frame and must NOT apply mirror_x here. The lateral is therefore in the
    # centerline frame; the consumer (build_env) applies mirror_x consistently when it places things.
    pts_xz = [(p[0], p[2]) for p in local["points_xyz_m"]]
    stations_cum = _cumulative_stations(pts_xz)

    # --- project + match every valid GPS fix ---
    records: list[dict[str, Any]] = []
    gps_fixes = 0
    for s in bundle_mod.iter_samples(bundle_dir):
        if s.gps is None or not s.gps.valid:
            continue
        gps_fixes += 1
        x = (s.gps.lon - lon0) * m_lon
        z = (s.gps.lat - lat0) * m_lat
        station, lateral = match_to_centerline(x, z, pts_xz, stations_cum)
        records.append({
            "station": station, "lateral": lateral,
            "gps_alt": (s.gps.alt - elev0) if s.gps.alt is not None else None,  # mesh-relative, like the mesh Y
            "baro_rel": s.baro.rel_alt_m if s.baro else None,
            "hacc": s.gps.hacc, "i": s.i, "t": s.t,
        })

    if not records:
        raise ValueError("no valid GPS fixes in bundle — nothing to ingest (was Location permission granted?)")

    src_stations = _distil_source(records)
    source = {
        "bundle": bundle_dir.name,
        "session_id": man.session_id,
        "app_version": man.app_version,
        "device": man.device,
        "started_at": man.started_at,
        "ended_at": man.ended_at,
        "frames": man.sample_count,
        "gps_fixes": gps_fixes,
        "stations": src_stations,
    }

    # --- merge into the committed evidence file (idempotent per bundle+session) ---
    out_path = project_dir / "source" / "realworld_capture.json"
    doc = _load_existing(out_path, cfg.slug)
    doc["sources"] = [s for s in doc["sources"]
                      if not (s.get("bundle") == source["bundle"] and s.get("session_id") == source["session_id"])]
    doc["sources"].append(source)
    doc["sources"].sort(key=lambda s: (s.get("started_at", ""), s.get("bundle", "")))
    doc["origin"] = {"lon": lon0, "lat": lat0, "elev_m": elev0}
    doc["frame"] = FRAME
    doc["centerline_length_m"] = round(stations_cum[-1], 1)
    doc["stations"] = _aggregate(doc["sources"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    covered = len(doc["stations"]) * STATION_BIN_M
    return {
        "bundle": source["bundle"], "gps_fixes": gps_fixes, "source_stations": len(src_stations),
        "merged_stations": len(doc["stations"]), "sources_total": len(doc["sources"]),
        "coverage_m": round(covered, 0), "centerline_m": round(stations_cum[-1], 0),
        "coverage_pct": round(100.0 * covered / max(1e-6, stations_cum[-1]), 1),
        "out": str(out_path),
    }


def _load_existing(out_path: Path, slug: str) -> dict[str, Any]:
    if out_path.exists():
        doc = json.loads(out_path.read_text(encoding="utf-8"))
        doc.setdefault("sources", [])
        doc.setdefault("placements", [])
        doc.setdefault("texture_overrides", [])
        return doc
    return {"schema_version": OUTPUT_SCHEMA_VERSION, "track_slug": slug,
            "sources": [], "stations": [], "placements": [], "texture_overrides": []}


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    m = len(s)
    if m == 0:
        return 0.0
    return s[m // 2] if m % 2 else (s[m // 2 - 1] + s[m // 2]) / 2.0


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: python -m scripts.capture.ingest <project-dir> <bundle-dir>")
    stats = build(sys.argv[1], sys.argv[2])
    print(f"ingested {stats['bundle']} → {stats['out']}")
    for k, v in stats.items():
        if k not in ("bundle", "out"):
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
