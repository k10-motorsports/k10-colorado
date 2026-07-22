"""Measure REAL road cross-slope (camber/superelevation) from the 1 m USGS 3DEP DEM.

Samples the DEM in LATERAL PAIRS — a point near each pavement edge, every ~6 m along the lap —
and converts the height difference into a signed cross-slope. On a mountain road this recovers
the real superelevation pattern Kevin described: banked into the hillside through many curves
(outer/valley edge higher), flat elsewhere.

Safety over drama (the previous attempts "ended up super high" and launched cars):
  - DEADBAND: |slope| < 1.5% -> 0. DEM noise over a ~5 m baseline is ±2-3% per raw sample; only a
    sustained signal survives the median, and mild readings stay flat ("...but not everywhere").
  - CAP: ±6% (~3.4 deg) — the real rural superelevation maximum. Never hairpin-bowl banking.
  - RUNOFF: median (window ~54 m) then mean (~30 m) smoothing along-track, so banking rotates in
    gradually like a real superelevation transition, never snaps.

Writes data/crossfall.json: {"station_m": [...], "bank_rad": [...]} — positive = LEFT edge up
(matches the ribbon/bank_at convention). build_mesh consumes it when the track has no
hand-authored road_profile.cambers.

Run:  python -m scripts.elevation.crossfall tracks/<slug>
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from scripts.elevation.usgs_3dep import sample_points

DEADBAND = 0.015
CAP = 0.06
STRIDE = 2                # sample every 2nd centerline vertex (~6 m)
EDGE_INSET = 0.6          # sample this far inside each pavement edge


def build(project_dir: str | Path) -> dict:
    project = Path(project_dir)
    data = project / "data"
    gj = json.loads((data / "centerline.geojson").read_text())
    feat = next(f for f in gj["features"] if f["properties"].get("kind") == "full")
    cs = [(c[0], c[1]) for c in feat["geometry"]["coordinates"]]
    widths = feat["properties"].get("widths_m") or [7.0] * len(cs)
    lc = json.loads((data / "centerline.local.json").read_text())
    if len(lc["widths_m"]) == len(cs):
        widths = lc["widths_m"]                    # post-widths-pass values

    lat0 = sum(p[1] for p in cs) / len(cs)
    m_lon = 111_320.0 * math.cos(math.radians(lat0))
    m_lat = 110_540.0

    idx = list(range(1, len(cs) - 1, STRIDE))
    pairs = []
    baselines = []
    for i in idx:
        (lo0, la0), (lo1, la1) = cs[i - 1], cs[i + 1]
        tx, tz = (lo1 - lo0) * m_lon, (la1 - la0) * m_lat
        L = math.hypot(tx, tz) or 1e-9
        nx, nz = -tz / L, tx / L                   # left normal in metres
        half = max(widths[i] / 2.0 - EDGE_INSET, 1.5)
        pairs.append((cs[i][0] + nx * half / m_lon, cs[i][1] + nz * half / m_lat))    # left
        pairs.append((cs[i][0] - nx * half / m_lon, cs[i][1] - nz * half / m_lat))    # right
        baselines.append(2 * half)
    zs = sample_points(pairs)

    raw = []
    for k in range(len(idx)):
        zl, zr = zs[2 * k], zs[2 * k + 1]
        raw.append((zl - zr) / baselines[k])       # +ve = left edge up

    def med(a, w):
        h = w // 2
        return [sorted(a[max(0, i - h):i + h + 1])[len(a[max(0, i - h):i + h + 1]) // 2]
                for i in range(len(a))]

    def mean(a, w):
        h = w // 2
        return [sum(a[max(0, i - h):i + h + 1]) / len(a[max(0, i - h):i + h + 1])
                for i in range(len(a))]

    sm = mean(med(raw, 9), 5)                       # ~54 m median + ~30 m mean at 6 m stride
    banked = 0
    out_bank = []
    for v in sm:
        if abs(v) < DEADBAND:
            out_bank.append(0.0)
        else:
            s = max(-CAP, min(CAP, v))
            out_bank.append(math.atan(s))
            banked += 1

    # stations along the lap for the sampled indices
    def hav(a, b):
        r = 6_371_000.0
        (lo1, la1), (lo2, la2) = a, b
        p1, p2 = math.radians(la1), math.radians(la2)
        dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
        h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * r * math.asin(math.sqrt(h))

    st = [0.0]
    for i in range(1, len(cs)):
        st.append(st[-1] + hav(cs[i - 1], cs[i]))
    stations = [round(st[i], 1) for i in idx]

    out = {"station_m": stations, "bank_rad": [round(b, 5) for b in out_bank],
           "deadband": DEADBAND, "cap": CAP,
           "banked_pct": round(100 * banked / max(1, len(out_bank)), 1),
           "max_bank_pct": round(100 * math.tan(max(abs(b) for b in out_bank)), 2) if out_bank else 0}
    (data / "crossfall.json").write_text(json.dumps(out), encoding="utf-8")
    print(f"crossfall: {len(stations)} stations, {out['banked_pct']}% of lap banked, "
          f"max {out['max_bank_pct']}% cross-slope (cap {CAP*100:.0f}%)")
    return out


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else ".")
