"""Resolve true-north rotation and emit CSP/Sol lighting config + time-of-day presets.

Writes ``true_north_rotation_deg`` back to track.config.json so in-game shadows match reality.

THE SUN FIX (mirror_x tracks). The pipeline builds geometry in a real ENU frame and then negates X
(``mirror_x``) to cancel the right-handed(Blender)->left-handed(AC) reflection the kn5 export applies.
The NET of those two reflections is a 180-degree yaw, so the in-game GEOMETRY looks correct (a proper
rotation, not a mirror -- verified in-game: the track reads right, only the sun is wrong) but the whole
world is spun 180 degrees relative to AC's world-fixed sun. AC has NO per-track sun/north key
(_doc_config_tracks.ini has none), so the only lever is the model's orientation. resolve_true_north
returns the counter-yaw that re-aims the model so AC's sun (computed from the real geotags) sets in the
true west. It is applied to the whole assembled model + dummies at export (scripts/ac/build_kn5.py) and
to the minimap (scripts/ac/track_folder.py), so geometry stays put and only the orientation changes.

Confirm on the FIRST Windows drive at BOTH sunset (sun in the WEST, behind the Front Range backdrop)
and noon (sun in the SOUTH). If sunset is fixed but noon is wrong, the error was a reflection not a
rotation -- override lighting.model_yaw_deg to dial it in.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from scripts.config import TrackConfig
from scripts.lighting.solar import solar_position, sun_world_vector


def resolve_true_north(config: TrackConfig) -> float:
    """Model yaw (deg, clockwise about vertical) that aligns the exported track with AC's sun.

    mirror_x tracks are spun 180 deg vs AC's world-fixed sun (see module docstring), so they need a
    180 deg counter-yaw. Non-mirrored tracks need none. Override with ``lighting.model_yaw_deg``.
    """
    raw = config.raw
    override = (raw.get("lighting", {}) or {}).get("model_yaw_deg")
    if override is not None:
        return float(override) % 360.0
    return 180.0 if bool(raw.get("mirror_x", True)) else 0.0


def _sun_side(config: TrackConfig) -> dict:
    """Mac-side oracle: sunset sun azimuth + its ENU east-sign, for the sun-vs-mountains check."""
    loc = config.location
    lat, lon, tz = loc["lat"], loc["lon"], loc.get("timezone", "UTC")
    # a representative summer sunset (elevation ~0); azimuth is what matters for the E/W side
    when = f"{_dt.date.today().year}-07-01T20:30:00"
    az, el = solar_position(lat, lon, when, tz)
    x, y, z = sun_world_vector(az, el)
    return {"azimuth_deg": round(az, 1), "elevation_deg": round(el, 1),
            "east_sign": "west" if x < 0 else "east", "enu_x": round(x, 3)}


def emit_lighting(config: TrackConfig, track_dir: str | Path) -> list[Path]:
    """Emit per-preset time-of-day sun angles (JSON) for the track, computed from real lat/lon/tz.

    These are informational templates (morning/noon/golden_hour) that record the true sun azimuth +
    elevation at the track location, so a driver/QA knows the real sun path. CSP itself derives the
    live sun from the ui geotags; the geometric orientation fix lives in resolve_true_north.
    """
    import json

    track_dir = Path(track_dir)
    out_dir = track_dir / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    loc = config.location
    lat, lon, tz = loc["lat"], loc["lon"], loc.get("timezone", "UTC")
    presets = (config.raw.get("lighting", {}) or {}).get("presets", ["morning", "noon", "golden_hour"])
    hours = {"morning": "08:00:00", "noon": "13:00:00", "golden_hour": "19:45:00",
             "sunset": "20:30:00", "dawn": "05:45:00"}
    year = _dt.date.today().year
    result = {"location": {"lat": lat, "lon": lon, "timezone": tz},
              "true_north_rotation_deg": resolve_true_north(config),
              "sunset_check": _sun_side(config), "presets": {}}
    for p in presets:
        hhmmss = hours.get(p, "13:00:00")
        az, el = solar_position(lat, lon, f"{year}-07-01T{hhmmss}", tz)
        result["presets"][p] = {"local_time": hhmmss, "sun_azimuth_deg": round(az, 1),
                                "sun_elevation_deg": round(el, 1)}
    out = out_dir / "lighting_presets.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return [out]
