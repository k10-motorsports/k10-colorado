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


def load_config(project_dir: str | Path) -> TrackConfig:
    """Load ``<project_dir>/track.config.json`` into a :class:`TrackConfig`."""
    project_dir = Path(project_dir)
    cfg_path = project_dir / "track.config.json" if project_dir.is_dir() else project_dir
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
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
