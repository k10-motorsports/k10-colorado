"""Generate a *synthetic* Prodrive Scan bundle that walks GPS along a project's centerline.

Lets the whole ingest path be exercised on the Mac with no iPhone in sight: it reads a project's
``data/centerline.local.json``, un-projects each metre position back to lon/lat through the same origin
ingest uses, and writes a schema-v1 bundle. A clean round-trip (synth → ingest) should land every
station at lateral ≈ 0 with grade matching the synthetic elevation. Deterministic unless ``gps_noise_m``
is set (and then seeded). Pure stdlib.

Run:  python -m scripts.capture.synth <out-bundle> --project projects/<slug>
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path
from typing import Any

from scripts.capture import bundle as bundle_mod
from scripts.geometry.projection import _meters_per_degree


def _interp_along(pts_xyz: list[tuple[float, float, float]], step_m: float):
    """Yield (station, x, y, z, tx, tz) sampled every ``step_m`` along the polyline (tangent = unit)."""
    seglens = [math.hypot(pts_xyz[i + 1][0] - pts_xyz[i][0], pts_xyz[i + 1][2] - pts_xyz[i][2])
               for i in range(len(pts_xyz) - 1)]
    total = sum(seglens)
    d = 0.0
    while d <= total + 1e-6:
        # locate segment containing arc length d
        acc = 0.0
        seg = 0
        while seg < len(seglens) - 1 and acc + seglens[seg] < d:
            acc += seglens[seg]
            seg += 1
        sl = seglens[seg] or 1e-9
        t = (d - acc) / sl
        ax, ay, az = pts_xyz[seg]
        bx, by, bz = pts_xyz[seg + 1]
        x, y, z = ax + t * (bx - ax), ay + t * (by - ay), az + t * (bz - az)
        tx, tz = (bx - ax) / sl, (bz - az) / sl
        yield d, x, y, z, tx, tz
        d += step_m


def write_bundle(out_dir: str | Path, project_dir: str | Path, *, session_id: str = "synth-session",
                 speed_mps: float = 15.0, step_m: float = 2.0, lateral_m: float = 0.0,
                 gps_noise_m: float = 0.0, hacc_m: float = 4.0, depth_hz: float = 4.0,
                 with_lidar: bool = True) -> dict:
    out_dir = Path(out_dir)
    project_dir = Path(project_dir)
    data = project_dir / "data"
    local = json.loads((data / "centerline.local.json").read_text(encoding="utf-8"))
    origin = local["origin"]
    lon0, lat0, elev0 = origin["lon"], origin["lat"], origin["elev_m"]
    m_lon, m_lat = _meters_per_degree(lat0)

    # NB: centerline.local.json is un-mirrored ENU (mirror_x is applied later, at mesh build), so we
    # un-project straight through the origin — no mirror — exactly the frame ingest matches against.
    pts_xyz = [(p[0], p[1], p[2]) for p in local["points_xyz_m"]]
    rng = random.Random(1234)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / bundle_mod.DEPTH_DIR).mkdir(exist_ok=True)

    samples: list[dict[str, Any]] = []
    y0 = pts_xyz[0][1]
    next_depth_d = 0.0
    depth_period_m = speed_mps / max(depth_hz, 1e-6)
    i = 0
    for d, x, y, z, tx, tz in _interp_along(pts_xyz, step_m):
        # left-normal offset in mesh space (matches ingest's lateral sign convention)
        nx, nz = -tz, tx
        mx, mz = x + nx * lateral_m, z + nz * lateral_m
        if gps_noise_m:
            mx += rng.gauss(0, gps_noise_m)
            mz += rng.gauss(0, gps_noise_m)
        lon = lon0 + mx / m_lon
        lat = lat0 + mz / m_lat
        heading = (math.degrees(math.atan2(tx, tz)) + 360.0) % 360.0  # true-north bearing of travel
        depth_ref = None
        if d >= next_depth_d:
            depth_ref = f"{bundle_mod.DEPTH_DIR}/{i:06d}.depthf32"
            (out_dir / depth_ref).write_bytes(b"\x00" * 16)  # placeholder; ingest doesn't read depth
            next_depth_d += depth_period_m
        samples.append({
            "i": i, "t": round(d / speed_mps, 3),
            "cam": _camera_transform(x, y + 1.2, z, heading),  # ~windshield height
            "intr": [1450.0, 1450.0, 960.0, 720.0],
            "gps": {"lat": lat, "lon": lon, "alt": round(elev0 + y, 3), "hacc": hacc_m,
                    "vacc": hacc_m * 1.5, "head_true": round(heading, 1), "speed": speed_mps},
            "baro": {"rel_alt_m": round(y - y0, 3), "pressure_kpa": round(101.325 - (elev0 + y) * 0.012, 4)},
            "depth": depth_ref,
        })
        i += 1

    with open(out_dir / bundle_mod.SAMPLES_NAME, "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s) + "\n")

    (out_dir / "video.mov").write_bytes(b"SYNTH-HEVC-PLACEHOLDER")  # real app writes HEVC here
    mesh = None
    if with_lidar:
        (out_dir / bundle_mod.MESH_NAME).write_text("o synth_mesh\nv 0 0 0\nv 1 0 0\nv 0 0 1\nf 1 2 3\n")
        mesh = {"file": bundle_mod.MESH_NAME, "vertex_count": 3, "face_count": 1, "frame": "arkit_world"}

    n_depth = sum(1 for s in samples if s["depth"])
    manifest = {
        "schema_version": bundle_mod.SCHEMA_VERSION,
        "producer": bundle_mod.PRODUCER,
        "app_version": "synthetic",
        "session_id": session_id,
        "started_at": "2026-01-01T00:00:00Z",
        "ended_at": "2026-01-01T00:05:00Z",
        "device": {"model": "synthetic", "os": "n/a", "name": "synth", "has_lidar": with_lidar},
        "video": {"file": "video.mov", "codec": "hevc", "width": 1920, "height": 1440,
                  "fps": round(speed_mps / step_m, 1), "frame_count": len(samples)},
        "depth": ({"dir": bundle_mod.DEPTH_DIR, "format": "f32_raw", "width": 256, "height": 192,
                   "count": n_depth, "hz": depth_hz} if with_lidar else None),
        "mesh": mesh,
        "samples": {"file": bundle_mod.SAMPLES_NAME, "count": len(samples)},
        "coordinate_notes": ("ARKit world is gravity-aligned with arbitrary yaw at session start; GPS is "
                             "WGS84; fuse to a track via GPS+trueHeading at ingest. (synthetic bundle)"),
    }
    (out_dir / bundle_mod.MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"bundle": str(out_dir), "frames": len(samples), "depth_maps": n_depth}


def _camera_transform(x: float, y: float, z: float, heading_deg: float) -> list[float]:
    """A plausible column-major 4x4 (yaw about Y at the given world position). Not used by ingest;
    present so the synthetic samples exercise the full record shape."""
    a = math.radians(heading_deg)
    c, s = math.cos(a), math.sin(a)
    return [c, 0.0, -s, 0.0,  0.0, 1.0, 0.0, 0.0,  s, 0.0, c, 0.0,  x, y, z, 1.0]


def main() -> None:
    ap = argparse.ArgumentParser(description="Write a synthetic Prodrive Scan bundle.")
    ap.add_argument("out_dir")
    ap.add_argument("--project", required=True, help="project dir with data/centerline.local.json")
    ap.add_argument("--session-id", default="synth-session")
    ap.add_argument("--lateral-m", type=float, default=0.0)
    ap.add_argument("--gps-noise-m", type=float, default=0.0)
    ap.add_argument("--no-lidar", action="store_true")
    args = ap.parse_args()
    stats = write_bundle(args.out_dir, args.project, session_id=args.session_id,
                         lateral_m=args.lateral_m, gps_noise_m=args.gps_noise_m,
                         with_lidar=not args.no_lidar)
    print(f"wrote synthetic bundle {stats['bundle']}")
    print(f"  frames: {stats['frames']}  depth_maps: {stats['depth_maps']}")


if __name__ == "__main__":
    main()
