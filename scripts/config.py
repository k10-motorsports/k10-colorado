"""Load and validate ``track.config.json`` — the single source of truth for a track.

The whole pipeline reads from here and writes derived values (e.g. ``true_north_rotation_deg``)
back. No hardcoded coordinates, widths, or rotations belong in pipeline code — put them here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TrackConfig:
    """Typed view over ``track.config.json``. ``raw`` keeps the full parsed document."""

    name: str
    slug: str
    location: dict[str, Any]
    source: dict[str, Any]
    loop: bool
    default_width_m: float
    origin: str
    surfaces: dict[str, str]
    layouts: list[dict[str, Any]]
    lighting: dict[str, Any]
    width_overrides: list[dict[str, Any]] = field(default_factory=list)
    true_north_rotation_deg: float | None = None
    # Track identity for ui_track.json — versioned per track, independent of the builder
    # package version (pyproject.toml). Bump these when you re-release a track.
    version: str = "0.1"
    author: str = "prodrive-ac-builder"
    year: int = 2026
    path: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def lat(self) -> float:
        return float(self.location["lat"])

    @property
    def lon(self) -> float:
        return float(self.location["lon"])

    @property
    def timezone(self) -> str:
        return str(self.location["timezone"])

    def write_back(self, **updates: Any) -> None:
        """Update derived fields (e.g. ``true_north_rotation_deg``) and persist to disk."""
        if self.path is None:
            raise ValueError("TrackConfig has no path; load via load_config() to write back.")
        self.raw.update(updates)
        for key, value in updates.items():
            if hasattr(self, key):
                setattr(self, key, value)
        self.path.write_text(json.dumps(self.raw, indent=2) + "\n", encoding="utf-8")


# The three kinds of track we build (Kevin): each archetype is a CONSTRUCTION PHILOSOPHY —
# defaults applied under the explicit config (explicit keys always win).
#   real_road      — a public road as it exists (Lookout Lariat): civil-engineering edges (bench
#                    cut / fill / retaining structure), real signage + street lighting, NO racing
#                    furniture; fidelity to the place is the product (replication gate applies).
#   street_circuit — a racing loop made from real streets (Sand Creek): the real_road base plus
#                    race-day overlay (concrete barriers on the racing line, start/pit furniture).
#   race_circuit   — a purpose-built facility (IMI, High Plains, PPIR, Second Creek, Aspen):
#                    racing kerbs, engineered runoff, pit lane/paddock, floodlights.
ARCHETYPES: dict = {
    "real_road": {
        "kerb": {"enabled": False},
        "runoff": {"enabled": False},
        "road_markings": {"style": "lane"},
    },
    "street_circuit": {
        "kerb": {"enabled": False},
        "runoff": {"enabled": False},
        "road_markings": {"style": "lane"},
        "props": {"concrete_barriers": True},
    },
    "race_circuit": {
        "kerb": {"enabled": True},
        "runoff": {"enabled": True},
        "road_markings": {"style": "track"},
    },
}


def _deep_defaults(raw: dict, base: dict) -> dict:
    for k, v in base.items():
        if k not in raw:
            raw[k] = v
        elif isinstance(v, dict) and isinstance(raw.get(k), dict):
            _deep_defaults(raw[k], v)
    return raw


def load_config(project_dir: str | Path) -> TrackConfig:
    """Load ``<project_dir>/track.config.json`` into a :class:`TrackConfig`."""
    project_dir = Path(project_dir)
    cfg_path = project_dir / "track.config.json" if project_dir.is_dir() else project_dir
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    arch = raw.get("archetype")
    if arch in ARCHETYPES:
        _deep_defaults(raw, ARCHETYPES[arch])
    return TrackConfig(
        name=raw["name"],
        slug=raw["slug"],
        location=raw["location"],
        source=raw["source"],
        loop=raw["loop"],
        default_width_m=raw["default_width_m"],
        origin=raw["origin"],
        surfaces=raw["surfaces"],
        layouts=raw["layouts"],
        lighting=raw["lighting"],
        width_overrides=raw.get("width_overrides", []),
        true_north_rotation_deg=raw.get("true_north_rotation_deg"),
        version=str(raw.get("version", "0.1")),
        author=str(raw.get("author", "prodrive-ac-builder")),
        year=int(raw.get("year", 2026)),
        path=cfg_path,
        raw=raw,
    )
