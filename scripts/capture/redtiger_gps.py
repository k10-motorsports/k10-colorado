"""Extract the GPS track from a REDTIGER (YOUQING/Novatek) dashcam MP4 — straight off the SD card.

REDTIGER markets the GPS as "proprietary encrypted" and locks it behind a Windows-only online player.
It isn't encrypted: the raw files on the card are textbook Novatek **freeGPS** — one block per second
embedded in the stream, `A/N/S/E/W` status flags, float32 little-endian `DDDmm.mmmm` coordinates. (Only
the app's *exported* clips drop the block, which is where the "encryption" reputation comes from — export
straight off the card, not through the player.)

Block layout (offsets from the `freeGPS` marker), reverse-engineered + validated against the burned-in
on-screen stamp (39.7400 N matched the decode to 4 dp):
    +0   'freeGPS '              marker
    +8   uint32 payload size (0x90)
    +12  'YOUQING' 'GPS'         OEM id
    +36  float32 latitude   (DDDmm.mmmm)
    +40  float32 longitude  (DDDmm.mmmm)
    ~+68 'A' <N|S> <E|W>         fix-valid + hemispheres
    near a uint32 0x000007EA (year 2026): int32 hour,min,sec  then  month,day

Pure stdlib (no numpy / no exiftool), so it runs under the no-numpy system python like the rest of
scripts/capture. Speed is derived from consecutive fixes (the device leaves the stored speed field 0).

    python -m scripts.capture.redtiger_gps <dashcam.mp4> [--gpx out.gpx] [--json out.json]

See [[companion-app-plan]] — this is a track-agnostic dashcam GPS source feeding the texture/position
ingest (rear-camera façades keyed to the building they belong to).
"""

from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path
from typing import Any

MARKER = b"freeGPS"
BLOCK = 0xE0           # bytes to read per block (payload is 0x90 + header slack)
LAT_OFF, LON_OFF = 36, 40
YEAR_LE = b"\xea\x07\x00\x00"   # 2026; the time int32s bracket this anchor


def _ddmm(v: float) -> float:
    """DDDmm.mmmm (degrees + decimal minutes packed) → decimal degrees."""
    d = int(v / 100.0)
    return d + (v - d * 100.0) / 60.0


def _datetime(b: bytes) -> str | None:
    """Find the 6 consecutive LE int32 (hour,min,sec,year,month,day) in a freeGPS block and return an
    ISO-8601 string. Located by structural validation rather than a fixed year anchor, so it works across
    years (the card spans 2025→2026). The 6-field datetime pattern is specific enough to be unambiguous."""
    for off in range(8, len(b) - 24):
        hh, mm, ss, yr, mo, dd = struct.unpack_from("<IIIIII", b, off)
        if 0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60 and 2020 <= yr <= 2035 and 1 <= mo <= 12 and 1 <= dd <= 31:
            return f"{yr:04d}-{mo:02d}-{dd:02d}T{hh:02d}:{mm:02d}:{ss:02d}"
    return None


def _find_all(data: bytes, sub: bytes) -> list[int]:
    out, i = [], data.find(sub)
    while i != -1:
        out.append(i)
        i = data.find(sub, i + 1)
    return out


def parse_fixes(path: str | Path) -> list[dict[str, Any]]:
    """Decode every freeGPS block in the MP4 → list of fixes sorted by time, each:
    ``{t, lat, lon, speed_kmh}`` (t = ISO-8601, speed derived from motion)."""
    data = Path(path).read_bytes()
    fixes: list[dict[str, Any]] = []
    for o in _find_all(data, MARKER):
        b = data[o:o + BLOCK]
        if len(b) < 64:
            continue
        try:
            lat_r, lon_r = struct.unpack_from("<ff", b, LAT_OFF)
        except struct.error:
            continue
        lat, lon = _ddmm(lat_r), _ddmm(lon_r)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            continue
        si = b.find(b"A", 60)                                   # 'A' fix-valid, then hemispheres
        ns = chr(b[si + 1]) if 0 <= si < len(b) - 2 and b[si + 1] in b"NS" else "N"
        ew = chr(b[si + 2]) if 0 <= si < len(b) - 2 and b[si + 2] in b"EW" else "W"
        if ns == "S":
            lat = -lat
        if ew == "W":
            lon = -lon
        t = _datetime(b)        # 6 LE int32 (h,m,s,year,month,day) located structurally (year varies)
        if t is None:
            continue
        fixes.append({"t": t, "lat": round(lat, 6), "lon": round(lon, 6)})

    fixes.sort(key=lambda f: f["t"])
    # de-dupe identical timestamps (block boundaries can repeat a second) + derive speed from motion
    dedup: list[dict[str, Any]] = []
    for f in fixes:
        if not dedup or dedup[-1]["t"] != f["t"]:
            dedup.append(f)
    for i, f in enumerate(dedup):
        if i == 0:
            f["speed_kmh"] = 0.0
            continue
        p = dedup[i - 1]
        dt = _sec(f["t"]) - _sec(p["t"]) or 1
        dx = (f["lon"] - p["lon"]) * 111320 * math.cos(math.radians(f["lat"]))
        dy = (f["lat"] - p["lat"]) * 110540
        f["speed_kmh"] = round(math.hypot(dx, dy) / dt * 3.6, 1)
    return dedup


def _sec(iso: str) -> int:
    hh, mm, ss = iso[11:].split(":")
    return int(hh) * 3600 + int(mm) * 60 + int(ss)


def to_gpx(fixes: list[dict[str, Any]]) -> str:
    pts = "\n".join(
        f'    <trkpt lat="{f["lat"]}" lon="{f["lon"]}"><time>{f["t"]}Z</time>'
        f'<speed>{f["speed_kmh"] / 3.6:.2f}</speed></trkpt>'
        for f in fixes)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="prodrive-ac-builder/redtiger_gps">\n'
            f'  <trk><name>REDTIGER dashcam track</name><trkseg>\n{pts}\n'
            '  </trkseg></trk>\n</gpx>\n')


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit("usage: python -m scripts.capture.redtiger_gps <mp4> [--gpx out] [--json out]")
    src = args[0]
    fixes = parse_fixes(src)
    if not fixes:
        raise SystemExit(f"no freeGPS blocks found in {src} (an EXPORTED clip drops them — copy raw off the card)")
    lat0, lat1 = min(f["lat"] for f in fixes), max(f["lat"] for f in fixes)
    lon0, lon1 = min(f["lon"] for f in fixes), max(f["lon"] for f in fixes)
    print(f"{len(fixes)} fixes  {fixes[0]['t']} → {fixes[-1]['t']}")
    print(f"  bbox lat {lat0:.5f}..{lat1:.5f}  lon {lon0:.5f}..{lon1:.5f}  "
          f"speed max {max(f['speed_kmh'] for f in fixes):.0f} km/h")
    if "--gpx" in args:
        out = Path(args[args.index("--gpx") + 1])
        out.write_text(to_gpx(fixes), encoding="utf-8")
        print(f"  wrote {out}")
    if "--json" in args:
        out = Path(args[args.index("--json") + 1])
        out.write_text(json.dumps(fixes, indent=1), encoding="utf-8")
        print(f"  wrote {out}")


if __name__ == "__main__":
    main()
