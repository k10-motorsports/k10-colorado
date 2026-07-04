"""Material assignment for the AC Blender Tools addon.

Maps mesh-name prefixes to AC shaders and writes the material JSON the kn5 exporter consumes:
  1ROAD_* -> road shader (ksPerPixelMultiMap), GRASS -> ksGrass, 1KERB_* -> kerb (ksPerPixel).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Sensible AC shader defaults per surface role.
_SHADERS = {
    "road": {"shader": "ksPerPixelMultiMap", "ksAmbient": 0.4, "ksDiffuse": 0.5,
             "ksSpecular": 0.1, "ksSpecularEXP": 12, "isTransparent": False},
    "grass": {"shader": "ksGrass", "ksAmbient": 0.5, "ksDiffuse": 0.6,
              "ksSpecular": 0.0, "ksSpecularEXP": 1, "isTransparent": False},
    "kerb": {"shader": "ksPerPixel", "ksAmbient": 0.4, "ksDiffuse": 0.5,
             "ksSpecular": 0.2, "ksSpecularEXP": 30, "isTransparent": False},
}


def material_map(surfaces: dict[str, str]) -> dict[str, Any]:
    """Map mesh-name prefixes (from config.surfaces) to AC shader assignments."""
    return {
        surfaces["road"]: _SHADERS["road"],    # e.g. "1ROAD"
        surfaces["grass"]: _SHADERS["grass"],   # e.g. "GRASS"
        surfaces["kerb"]: _SHADERS["kerb"],     # e.g. "1KERB"
    }


def write_addon_settings(mmap: dict[str, Any], out_path: str | Path) -> Path:
    """Write the material JSON consumed by the AC Blender Tools addon during kn5 export."""
    out_path = Path(out_path)
    out_path.write_text(json.dumps({"materials": mmap}, indent=2), encoding="utf-8")
    return out_path
