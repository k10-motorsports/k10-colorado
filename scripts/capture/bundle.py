"""The Prodrive Scan **session bundle** contract (schema v1) — the spine shared by both halves.

A bundle is a *directory* the iOS app writes and this Mac pipeline reads:

    prodrive-scan-<ISO8601>-<shortid>/
      manifest.json   # session + device + counts + sample rate + file refs + schema_version
      samples.jsonl   # one JSON record per ARFrame (pose/GPS/baro/depth-ref), time-synced
      video.mov       # HEVC, from ARFrame.capturedImage
      depth/          # throttled sceneDepth maps: <i>.depthf32 (raw float32, dims in manifest)
      mesh.obj        # final fused ARMeshAnchor scene mesh (ARKit world coords)

The phone logs in **ARKit world** (gravity-aligned, arbitrary yaw at session start) + **WGS84 GPS**
and has zero track knowledge. Fusion to a track happens at ingest via GPS → mesh XZ.

``SCHEMA_VERSION`` here MUST match ``SessionBundle.schemaVersion`` in the Swift app
(apps/prodrive-scan/Sources/SessionBundle.swift). Bump both together. Pure stdlib.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
PRODUCER = "prodrive-scan"

MANIFEST_NAME = "manifest.json"
SAMPLES_NAME = "samples.jsonl"
DEPTH_DIR = "depth"
MESH_NAME = "mesh.obj"


# --------------------------------------------------------------------------- samples


@dataclass
class GPS:
    """A WGS84 fix. ``head_true`` is degrees from true north (CoreLocation trueHeading)."""

    lat: float
    lon: float
    alt: float | None = None          # ellipsoidal/MSL altitude (m), as CoreLocation reports it
    hacc: float | None = None         # horizontal accuracy (m); <0 means invalid
    vacc: float | None = None         # vertical accuracy (m)
    head_true: float | None = None    # true heading (deg), or None if not yet converged
    speed: float | None = None        # ground speed (m/s)

    @classmethod
    def from_json(cls, d: dict[str, Any] | None) -> "GPS | None":
        if not d:
            return None
        return cls(lat=float(d["lat"]), lon=float(d["lon"]),
                   alt=_optf(d.get("alt")), hacc=_optf(d.get("hacc")), vacc=_optf(d.get("vacc")),
                   head_true=_optf(d.get("head_true")), speed=_optf(d.get("speed")))

    @property
    def valid(self) -> bool:
        # CoreLocation reports hacc < 0 for an invalid fix; treat missing accuracy as usable.
        return self.hacc is None or self.hacc >= 0


@dataclass
class Baro:
    """Barometer reading. ``rel_alt_m`` is relative to the CMAltimeter session start, not MSL."""

    rel_alt_m: float | None = None
    pressure_kpa: float | None = None

    @classmethod
    def from_json(cls, d: dict[str, Any] | None) -> "Baro | None":
        if not d:
            return None
        return cls(rel_alt_m=_optf(d.get("rel_alt_m")), pressure_kpa=_optf(d.get("pressure_kpa")))


@dataclass
class Sample:
    """One ARFrame's worth of synchronized log. ``cam`` is a column-major 4x4 in ARKit world."""

    i: int                            # frame index
    t: float                          # seconds since session start
    cam: list[float] | None = None    # 16 floats, column-major (ARFrame.camera.transform)
    intr: list[float] | None = None   # [fx, fy, cx, cy] at capture resolution
    gps: GPS | None = None
    baro: Baro | None = None
    depth: str | None = None          # relative path to a depth map, or None on non-depth frames

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Sample":
        return cls(
            i=int(d["i"]), t=float(d["t"]),
            cam=[float(x) for x in d["cam"]] if d.get("cam") else None,
            intr=[float(x) for x in d["intr"]] if d.get("intr") else None,
            gps=GPS.from_json(d.get("gps")), baro=Baro.from_json(d.get("baro")),
            depth=d.get("depth"),
        )


# --------------------------------------------------------------------------- manifest


@dataclass
class Manifest:
    schema_version: int
    producer: str
    session_id: str
    app_version: str = ""
    started_at: str = ""
    ended_at: str = ""
    device: dict[str, Any] = field(default_factory=dict)
    video: dict[str, Any] = field(default_factory=dict)
    depth: dict[str, Any] | None = None
    mesh: dict[str, Any] | None = None
    samples: dict[str, Any] = field(default_factory=dict)
    coordinate_notes: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def sample_count(self) -> int:
        return int(self.samples.get("count", 0))

    @property
    def has_lidar(self) -> bool:
        return bool(self.device.get("has_lidar"))


def load_manifest(bundle_dir: str | Path) -> Manifest:
    """Read + version-check ``manifest.json``. Raises on a missing file or schema mismatch."""
    bundle_dir = Path(bundle_dir)
    path = bundle_dir / MANIFEST_NAME
    if not path.exists():
        raise FileNotFoundError(f"not a Prodrive Scan bundle: missing {MANIFEST_NAME} in {bundle_dir}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    ver = int(raw.get("schema_version", -1))
    if ver != SCHEMA_VERSION:
        raise ValueError(
            f"bundle schema_version {ver} != supported {SCHEMA_VERSION} "
            f"(upgrade scripts/capture or re-export from a matching app build)")
    return Manifest(
        schema_version=ver,
        producer=str(raw.get("producer", "")),
        session_id=str(raw.get("session_id", "")),
        app_version=str(raw.get("app_version", "")),
        started_at=str(raw.get("started_at", "")),
        ended_at=str(raw.get("ended_at", "")),
        device=raw.get("device") or {},
        video=raw.get("video") or {},
        depth=raw.get("depth"),
        mesh=raw.get("mesh"),
        samples=raw.get("samples") or {},
        coordinate_notes=str(raw.get("coordinate_notes", "")),
        raw=raw,
    )


def iter_samples(bundle_dir: str | Path) -> Iterator[Sample]:
    """Stream ``samples.jsonl`` so a multi-GB lap never loads into memory at once."""
    path = Path(bundle_dir) / SAMPLES_NAME
    if not path.exists():
        raise FileNotFoundError(f"missing {SAMPLES_NAME} in {bundle_dir}")
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield Sample.from_json(json.loads(line))


def validate(bundle_dir: str | Path) -> list[str]:
    """Return a list of human-readable problems (empty == OK). Cheap, file-existence level."""
    bundle_dir = Path(bundle_dir)
    problems: list[str] = []
    try:
        man = load_manifest(bundle_dir)
    except (FileNotFoundError, ValueError) as e:
        return [str(e)]
    if man.producer != PRODUCER:
        problems.append(f"unexpected producer {man.producer!r} (want {PRODUCER!r})")
    if not (bundle_dir / SAMPLES_NAME).exists():
        problems.append(f"missing {SAMPLES_NAME}")
    if man.video.get("file") and not (bundle_dir / man.video["file"]).exists():
        problems.append(f"manifest references video {man.video['file']} but it is absent")
    if man.mesh and man.mesh.get("file") and not (bundle_dir / man.mesh["file"]).exists():
        problems.append(f"manifest references mesh {man.mesh['file']} but it is absent")
    return problems


def _optf(v: Any) -> float | None:
    return None if v is None else float(v)
