"""Mac-native CSP **TreeFX** + **GrassFX** vegetation emitter.

The kn5 exporter is Blender-on-Windows; this module is the Mac-native alternative for *foliage*:
instead of baking billboard/OBJ tree meshes into the kn5, we emit the CSP (Custom Shaders Patch)
config that makes AC render real **TreeFX** 3D trees at runtime, plus a **GrassFX** block.

**Licensing (critical).** The actual ``.bin`` tree MODELS are third-party, licensed, and
USER-SUPPLIED — we never download or commit them. Everything here only *references* a species by
its ``.bin`` filename; the config we generate (``trees.txt`` + the ext_config ``[TREES]`` block) is
ours and license-clean. The user drops the licensed models into ``extension/trees/`` locally; a
build logs exactly which filenames it expects so nothing is guessed.

**The verified format** (validated against docs and a real pack — see ``docs/TREEFX-FORMAT.md``):

Per-track ``extension/trees/trees.txt``, one directive per line::

    ; comment
    configure: size variance = 0.9, 1.1        ; defaults for the trees that FOLLOW
    tree: MRS_pine_high_2_1.bin; 12.5, 2103.4, -55.2                        ; simple
    tree: pine.bin; pos=12.5, 2103.4, -55.2; angle = 137; size = 3.2        ; extended

The ``.bin`` filename comes first (terminated by ``;``) and *is* the species id; then position
``X, Y, Z`` (metres, AC/kn5 world space, **Y up**); then optional ``key = value`` params (``angle``
in degrees 0-360, ``size``/``width`` unitless multipliers where 1 = native, ``color`` an RGB
multiplier). ``configure:`` lines set defaults for all following trees so per-zone variance is
declared once instead of hand-jittered per line.

This module is pure stdlib (no bpy / numpy) so it runs under the dependency-free test harness and on
a Mac with system python. ``build_env`` computes real positions + ground Y + the same
pavement/clearance rejection it uses for baked foliage, hands them to a :class:`TreeFXCollector`,
and serialises ``data/treefx.json``; ``scripts/ac/ext_config`` renders the text list + the
``[TREES]`` / ``[GRASS_FX]`` sections into the track's ``extension/``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence


# --------------------------------------------------------------------------------------------------
# Number formatting — trim trailing zeros so lines read like a real pack ("size = 1.05", not
# "1.050000"), stay deterministic (tests assert exact strings), and parse as plain floats in-engine.
# --------------------------------------------------------------------------------------------------
def _fmt(v: float, nd: int = 3) -> str:
    s = f"{float(v):.{nd}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _fmt_list(vals: Sequence[float], nd: int = 3) -> str:
    return ", ".join(_fmt(v, nd) for v in vals)


# --------------------------------------------------------------------------------------------------
# trees.txt directives
# --------------------------------------------------------------------------------------------------
@dataclass
class Tree:
    """One ``tree:`` directive. ``x, y, z`` are metres in AC world space (Y up). ``angle`` is yaw in
    degrees; ``size``/``width`` are unitless multipliers (1 = the model's native size); ``color`` is
    an RGB multiplier (``(1, 1, 1)`` neutral). Any of angle/size/width/color set → the *extended*
    line form (``pos=X, Y, Z; key = value; ...``); all unset → the *simple* form (``X, Y, Z``)."""

    species: str
    x: float
    y: float
    z: float
    angle: Optional[float] = None
    size: Optional[float] = None
    width: Optional[float] = None
    color: Optional[tuple] = None

    def line(self) -> str:
        # X/Y/Z at cm precision (nd=2): trunks land where we sampled; angle whole-ish (nd=1);
        # size/width/color fine (nd=3) — the multipliers matter to the eye at 2 decimals.
        pos = f"{_fmt(self.x, 2)}, {_fmt(self.y, 2)}, {_fmt(self.z, 2)}"
        extended = any(v is not None for v in (self.angle, self.size, self.width, self.color))
        if not extended:
            return f"tree: {self.species}; {pos}"
        parts = [f"tree: {self.species}; pos={pos}"]
        if self.angle is not None:
            parts.append(f"angle = {_fmt(self.angle % 360.0, 1)}")
        if self.size is not None:
            parts.append(f"size = {_fmt(self.size, 3)}")
        if self.width is not None:
            parts.append(f"width = {_fmt(self.width, 3)}")
        if self.color is not None:
            parts.append(f"color = {_fmt_list(self.color, 3)}")
        return "; ".join(parts)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"species": self.species, "x": self.x, "y": self.y, "z": self.z}
        if self.angle is not None:
            d["angle"] = self.angle
        if self.size is not None:
            d["size"] = self.size
        if self.width is not None:
            d["width"] = self.width
        if self.color is not None:
            d["color"] = list(self.color)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Tree":
        color = d.get("color")
        return cls(species=d["species"], x=d["x"], y=d["y"], z=d["z"],
                   angle=d.get("angle"), size=d.get("size"), width=d.get("width"),
                   color=tuple(color) if color is not None else None)


@dataclass
class Configure:
    """A ``configure:`` directive — sets a default for every tree that FOLLOWS it (until the next
    ``configure`` for the same key). Corroborated keys: ``size variance = lo, hi``,
    ``angle variance = lo, hi``, ``seed``."""

    key: str
    values: Any  # scalar or sequence

    def line(self) -> str:
        if isinstance(self.values, (list, tuple)):
            body = _fmt_list(self.values, 3)
        elif isinstance(self.values, float):
            body = _fmt(self.values, 3)
        else:
            body = str(self.values)
        return f"configure: {self.key} = {body}"

    def to_dict(self) -> dict:
        vals = list(self.values) if isinstance(self.values, (list, tuple)) else self.values
        return {"key": self.key, "values": vals}

    @classmethod
    def from_dict(cls, d: dict) -> "Configure":
        return cls(key=d["key"], values=d["values"])


DEFAULT_HEADER = [
    "; ============================================================================",
    "; CSP TreeFX tree list (generated by prodrive-ac-builder / scripts/environment/treefx.py)",
    "; Loaded via extension/ext_config.ini -> [TREES] LIST_0. Each 'tree:' line references a .bin",
    "; MODEL by filename; the licensed models are USER-SUPPLIED under extension/trees/ (see README,",
    "; never committed). Format: tree: <model>.bin; X, Y, Z  (metres, AC world space, Y up).",
    "; Spec + GrassFX keys: docs/TREEFX-FORMAT.md",
    "; ============================================================================",
    "",
]


def render_trees_txt(directives: Iterable, *, header: Optional[Sequence[str]] = None) -> str:
    """Render an ordered list of directives (``str`` comments, :class:`Configure`, :class:`Tree`) to
    the exact ``trees.txt`` text. Order is load-bearing: a ``configure`` applies to trees after it."""
    lines: list[str] = list(DEFAULT_HEADER if header is None else header)
    for d in directives:
        if isinstance(d, str):
            lines.append(d)
        else:
            lines.append(d.line())
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------------------
# Density model
# --------------------------------------------------------------------------------------------------
def elevation_keep_prob(y: float, *, y_lo: float, y_hi: float,
                        keep_lo: float = 0.2, keep_hi: float = 1.0, curve: float = 1.0) -> float:
    """Keep-probability that rises MONOTONICALLY with elevation ``y`` — the mountain-forest model
    (sparse at the low end, thickening toward the summit). ``t`` = normalised elevation clamped to
    ``[0, 1]``, shaped by ``t ** curve`` (curve > 1 keeps the low slopes barer), lerped
    ``keep_lo -> keep_hi``. ``y_hi == y_lo`` degenerates to ``keep_hi``."""
    span = y_hi - y_lo
    t = 0.0 if span == 0 else (y - y_lo) / span
    t = max(0.0, min(1.0, t))
    if curve != 1.0:
        t = t ** curve
    return keep_lo + (keep_hi - keep_lo) * t


# Aspect (compass direction a slope FACES downhill) from a horizontal downhill vector. Convention:
# +X = East, and NORTH = -Z (documented in TREEFX-FORMAT.md). N/E-facing slopes hold more moisture
# in the northern hemisphere -> denser conifer forest, which is the effect we want on the Lariat.
def aspect_label(nx: float, nz: float) -> Optional[str]:
    """Dominant compass label ('N'/'S'/'E'/'W') of a horizontal aspect vector, or None if flat."""
    if abs(nx) < 1e-9 and abs(nz) < 1e-9:
        return None
    if abs(nx) >= abs(nz):
        return "E" if nx > 0 else "W"
    return "S" if nz > 0 else "N"


def aspect_keep_bonus(nx: float, nz: float, *, favored: Sequence[str] = ("N", "E"),
                      bonus: float = 0.0) -> float:
    """Extra keep-probability when the slope faces a favored compass direction (0 on flat ground or
    an unfavored aspect). Scaled by how strongly the aspect points that way, so a gentle slope gets a
    fraction of the full bonus and a dead-flat spot gets none."""
    label = aspect_label(nx, nz)
    if label is None or label not in favored:
        return 0.0
    mag = math.hypot(nx, nz)
    strength = min(1.0, mag / 0.15)   # ~0.15 m of relief across the sample step = full bonus
    return bonus * strength


# --------------------------------------------------------------------------------------------------
# Creek / riparian band selection
# --------------------------------------------------------------------------------------------------
def _point_segment_distance(px: float, pz: float,
                            ax: float, az: float, bx: float, bz: float) -> float:
    dx, dz = bx - ax, bz - az
    seg2 = dx * dx + dz * dz
    if seg2 <= 1e-12:
        return math.hypot(px - ax, pz - az)
    t = ((px - ax) * dx + (pz - az) * dz) / seg2
    t = max(0.0, min(1.0, t))
    cx, cz = ax + t * dx, az + t * dz
    return math.hypot(px - cx, pz - cz)


def min_distance_to_polylines(px: float, pz: float, lines: Iterable[Sequence[tuple]]) -> float:
    """Shortest distance from (px, pz) to any segment of any polyline (each ``[(x, z), ...]``)."""
    best = math.inf
    for line in lines:
        for i in range(1, len(line)):
            ax, az = line[i - 1][0], line[i - 1][1]
            bx, bz = line[i][0], line[i][1]
            d = _point_segment_distance(px, pz, ax, az, bx, bz)
            if d < best:
                best = d
    return best


def near_polylines(px: float, pz: float, lines: Iterable[Sequence[tuple]], band_m: float) -> bool:
    """True when (px, pz) lies within ``band_m`` of any polyline — the riparian-band test."""
    return min_distance_to_polylines(px, pz, lines) <= band_m


def select_creek_band(positions: Iterable[Sequence[float]], creek_lines: Iterable[Sequence[tuple]],
                      band_m: float) -> list:
    """Filter ``positions`` (``(x, z)`` or ``(x, y, z)``) down to those within ``band_m`` of a creek
    polyline. Keeps only near-water positions — the dense riparian planting stays on the banks."""
    lines = [list(l) for l in creek_lines]
    kept = []
    for p in positions:
        x = p[0]
        z = p[-1]
        if near_polylines(x, z, lines, band_m):
            kept.append(p)
    return kept


# --------------------------------------------------------------------------------------------------
# Zones
# --------------------------------------------------------------------------------------------------
@dataclass
class Zone:
    """A vegetation zone (config-driven, keyed by name). ``source`` names which build_env placement
    loop feeds it (``poly_scatter`` / ``fill_terrain`` / ``forest3d`` / ``creek`` / ``bush``).
    ``species`` is the list of ``.bin`` filenames mixed within the zone. Dropping a new filename into
    a zone's ``species`` is all it takes to fill that zone with a new model."""

    name: str
    source: str = "fill_terrain"
    species: list = field(default_factory=list)
    weights: Optional[list] = None
    size_variance: Optional[tuple] = None
    angle_variance: Optional[tuple] = (0.0, 360.0)
    color: Optional[tuple] = None
    band_m: Optional[float] = None
    density: dict = field(default_factory=dict)

    @classmethod
    def from_config(cls, name: str, d: dict) -> "Zone":
        d = d or {}
        sv = d.get("size_variance")
        av = d.get("angle_variance", [0.0, 360.0])
        color = d.get("color")
        return cls(
            name=name,
            source=str(d.get("source", "fill_terrain")),
            species=list(d.get("species", [])),
            weights=list(d["weights"]) if d.get("weights") else None,
            size_variance=tuple(sv) if sv else None,
            angle_variance=tuple(av) if av else None,
            color=tuple(color) if color else None,
            band_m=float(d["band_m"]) if d.get("band_m") is not None else None,
            density=dict(d.get("density", {}) or {}),
        )

    def keep_prob(self, y: float, aspect: Optional[Sequence[float]] = None) -> float:
        """Probability of KEEPING a candidate at height ``y`` (with an optional horizontal aspect
        vector). ``density.by`` selects the model: ``elevation`` (mountain forest, rises with y,
        optional N/E aspect bonus) or ``constant`` (a flat ``keep`` — used by creek/urban zones,
        which are already spatially gated by their placement loop)."""
        dd = self.density
        by = dd.get("by", "constant")
        if by == "elevation":
            p = elevation_keep_prob(
                y,
                y_lo=float(dd.get("elev_lo_m", 0.0)),
                y_hi=float(dd.get("elev_hi_m", 1.0)),
                keep_lo=float(dd.get("keep_lo", 0.2)),
                keep_hi=float(dd.get("keep_hi", 1.0)),
                curve=float(dd.get("curve", 1.0)),
            )
            if aspect is not None and dd.get("aspect_bonus"):
                p += aspect_keep_bonus(
                    aspect[0], aspect[1],
                    favored=tuple(dd.get("aspect_favored", ("N", "E"))),
                    bonus=float(dd.get("aspect_bonus", 0.0)),
                )
            return max(0.0, min(1.0, p))
        return max(0.0, min(1.0, float(dd.get("keep", 1.0))))

    def pick_species(self, rng) -> str:
        if not self.species:
            return "tree.bin"
        if self.weights and len(self.weights) == len(self.species):
            return rng.choices(self.species, weights=self.weights, k=1)[0]
        return self.species[rng.randrange(len(self.species))]

    def configure_directives(self, seed: int) -> list:
        """The per-zone ``configure:`` lines emitted before its trees — declaring variance ONCE so
        CSP jitters size/angle instead of us hand-jittering every line."""
        out: list = [Configure("seed", int(seed))]
        if self.size_variance:
            out.append(Configure("size variance", list(self.size_variance)))
        if self.angle_variance:
            out.append(Configure("angle variance", list(self.angle_variance)))
        return out


# --------------------------------------------------------------------------------------------------
# Collector — build_env populates this from real placements, then serialises it.
# --------------------------------------------------------------------------------------------------
class TreeFXCollector:
    """Accumulates real tree placements from build_env's foliage loops, applies each zone's density
    model, and renders ``trees.txt`` + the ``[TREES]`` block. One source maps to at most one zone
    (first declared wins); a source with no zone places nothing (build_env skips that loop)."""

    def __init__(self, cfg: Optional[dict] = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.seed = int(cfg.get("seed", 1))
        self.surface_materials = list(cfg.get("surface_materials") or []) or None
        self.surface_meshes = list(cfg.get("surface_meshes") or [])
        self.grassfx = cfg.get("grassfx")  # optional per-track GrassFX tuning (dict) or None
        self.compiled_list = cfg.get("compiled_list")  # optional baked VAO filename (not produced here)
        self.header = cfg.get("header")  # optional custom trees.txt header (list of lines)
        self.zones: dict[str, Zone] = {
            name: Zone.from_config(name, z) for name, z in (cfg.get("zones") or {}).items()
        }
        self._source_index: dict[str, str] = {}
        for name, zone in self.zones.items():
            self._source_index.setdefault(zone.source, name)
        self._by_zone: dict[str, list] = {name: [] for name in self.zones}
        self._expected: set[str] = set()

    # ---- queries build_env uses to gate each loop ----
    def has_source(self, source: str) -> bool:
        return source in self._source_index

    def zone_for(self, source: str) -> Optional[Zone]:
        name = self._source_index.get(source)
        return self.zones.get(name) if name else None

    # ---- placement ----
    def add(self, source: str, x: float, y: float, z: float, *, rng,
            aspect: Optional[Sequence[float]] = None, angle: Optional[float] = None,
            size: Optional[float] = None, width: Optional[float] = None) -> bool:
        """Offer a candidate (already ground-sampled + pavement/clearance-rejected by build_env) to
        the zone that owns ``source``. Applies the zone's density keep-test with ``rng``; on keep,
        picks a species and records the tree. Returns True iff a tree was placed."""
        zone = self.zone_for(source)
        if zone is None:
            return False
        if rng.random() > zone.keep_prob(y, aspect):
            return False
        species = zone.pick_species(rng)
        self._expected.add(species)
        self._by_zone[zone.name].append(
            Tree(species, x, y, z, angle=angle, size=size, width=width, color=zone.color)
        )
        return True

    # ---- outputs ----
    def counts(self) -> dict:
        return {name: len(trees) for name, trees in self._by_zone.items()}

    def total(self) -> int:
        return sum(len(t) for t in self._by_zone.values())

    def expected_species(self) -> list:
        """Every ``.bin`` filename this build references — the models the user must supply. Includes
        each zone's declared species even if density happened to place none, so the manifest is
        stable across seeds."""
        declared = {sp for zone in self.zones.values() for sp in zone.species}
        return sorted(self._expected | declared)

    def directives(self) -> list:
        """Ordered directive list: per zone, a header comment, its ``configure`` lines, then its
        trees. Zones with no placed trees are skipped."""
        out: list = []
        for name, zone in self.zones.items():
            trees = self._by_zone[name]
            if not trees:
                continue
            sp = "/".join(zone.species) if zone.species else "(unspecified)"
            out.append(f"; --- zone '{name}' ({zone.source}): {len(trees)} trees, species={sp} ---")
            out.extend(zone.configure_directives(self.seed))
            out.extend(trees)
        return out

    def trees_txt(self) -> str:
        return render_trees_txt(self.directives(), header=self.header)

    def to_json(self) -> dict:
        zones_json = {}
        for name, zone in self.zones.items():
            zones_json[name] = {"source": zone.source, "species": zone.species,
                                "size_variance": list(zone.size_variance) if zone.size_variance else None,
                                "angle_variance": list(zone.angle_variance) if zone.angle_variance else None,
                                "color": list(zone.color) if zone.color else None,
                                "trees": [t.to_dict() for t in self._by_zone[name]]}
        return {
            "enabled": self.enabled,
            "seed": self.seed,
            "surface_materials": self.surface_materials,
            "surface_meshes": self.surface_meshes,
            "grassfx": self.grassfx,
            "compiled_list": self.compiled_list,
            "header": self.header,
            "expected_species": self.expected_species(),
            "zones": zones_json,
        }

    def write_json(self, path) -> None:
        import json
        Path(path).write_text(json.dumps(self.to_json(), indent=1) + "\n", encoding="utf-8")

    @classmethod
    def from_json(cls, obj: dict) -> "TreeFXCollector":
        """Reconstruct a collector from ``data/treefx.json`` — used by ext_config to render the
        text list + [TREES] block without re-running placement."""
        self = cls.__new__(cls)
        self.enabled = bool(obj.get("enabled", False))
        self.seed = int(obj.get("seed", 1))
        self.surface_materials = obj.get("surface_materials")
        self.surface_meshes = list(obj.get("surface_meshes") or [])
        self.grassfx = obj.get("grassfx")
        self.compiled_list = obj.get("compiled_list")
        self.header = obj.get("header")
        self.zones = {}
        self._source_index = {}
        self._by_zone = {}
        self._expected = set(obj.get("expected_species") or [])
        for name, zj in (obj.get("zones") or {}).items():
            zone = Zone(name=name, source=zj.get("source", "fill_terrain"),
                        species=list(zj.get("species") or []),
                        size_variance=tuple(zj["size_variance"]) if zj.get("size_variance") else None,
                        angle_variance=tuple(zj["angle_variance"]) if zj.get("angle_variance") else None,
                        color=tuple(zj["color"]) if zj.get("color") else None)
            self.zones[name] = zone
            self._source_index.setdefault(zone.source, name)
            self._by_zone[name] = [Tree.from_dict(t) for t in (zj.get("trees") or [])]
        return self


# --------------------------------------------------------------------------------------------------
# ext_config sections
# --------------------------------------------------------------------------------------------------
def trees_ext_section(list_files: Sequence[str], *, surface_materials: Sequence[str],
                      surface_meshes: Optional[Sequence[str]] = None,
                      compiled_list: Optional[str] = None,
                      expected_species: Optional[Sequence[str]] = None,
                      autumn_conditions: Optional[Sequence[str]] = None,
                      winter_conditions: Optional[Sequence[str]] = None) -> list:
    """The ``[TREES]`` ext_config block. ``LIST_0..N`` point at text lists (relative to
    ``extension/``); ``SURFACE_MATERIALS`` are THIS track's real grass/road materials the trees snap
    vertically onto (and align lighting to). ``COMPILED_LIST`` (the optional baked VAO from the
    external bakery — not produced here) is emitted commented-out."""
    out = ["; --- TreeFX: 3D trees rendered by CSP from a text list (models are user-supplied .bin) -",
           "; SURFACE_MATERIALS = this track's real grass/road materials; trees snap onto them.",
           "[TREES]"]
    for i, lf in enumerate(list_files):
        out.append(f"LIST_{i} = {lf}")
    out.append(f"SURFACE_MATERIALS = {', '.join(surface_materials)}")
    if surface_meshes:
        out.append(f"SURFACE_MESHES = {', '.join(surface_meshes)}")
    for i, cond in enumerate(autumn_conditions or []):
        out.append(f"SEASON_AUTUMN_{i} = {cond}")
    for i, cond in enumerate(winter_conditions or []):
        out.append(f"SEASON_WINTER_{i} = {cond}")
    if compiled_list:
        out.append(f"; COMPILED_LIST = {compiled_list}   ; optional baked VAO (external bakery runs "
                   "AC headless; not required — the text list above loads directly)")
    if expected_species:
        out.append("; Expects these user-supplied .bin models under extension/trees/ (see README):")
        out.append("; " + ", ".join(expected_species))
    out.append("")
    return out


def grassfx_block(*, grass_meshes: Sequence[str], occluding_meshes: Sequence[str],
                  grass_materials: Optional[Sequence[str]] = None, cfg: Optional[dict] = None) -> list:
    """The ``[GRASS_FX]`` block, modeled on the Nordschleife template: ``GRASS_MESHES`` /
    ``OCCLUDING_MESHES`` bound to OUR mesh names, ``SHAPE_SIZE``, one ``[GRASS_FX_TEXTURE_0]`` group,
    and one ``[GRASS_FX_CONFIGURATION_0]`` block bound to our grass material. Conservative defaults;
    every key is documented in docs/TREEFX-FORMAT.md and must be VERIFIED IN-GAME (CSP keys shift)."""
    cfg = cfg or {}
    grass_materials = list(grass_materials or [f"{g}_mat" for g in grass_meshes])
    shape_size = cfg.get("shape_size", 0.9)
    mask_main = cfg.get("mask_main_threshold", 0.5)
    mask_red = cfg.get("mask_red_threshold", 0.05)
    lum_min = cfg.get("mask_min_luminance", 0.02)
    lum_max = cfg.get("mask_max_luminance", 0.35)
    texture = cfg.get("texture", "grass_fx/grass_ks.dds")
    brightness = cfg.get("brightness", 1.0)
    health = cfg.get("health", 1.0)
    out = [
        "; --- GrassFX: 3D procedural grass on the terrain (Nordschleife-style block) --",
        "; GRASS_MESHES / OCCLUDING_MESHES are OUR mesh names; the occluder list is an ALLOW-LIST",
        "; (ground-level solids only) — a far-field/oversized mesh here busts the GPU watchdog.",
        "[GRASS_FX]",
        f"GRASS_MESHES = {', '.join(grass_meshes)}",
        f"OCCLUDING_MESHES = {', '.join(occluding_meshes)}",
        f"MASK_MAIN_THRESHOLD = {_fmt(mask_main, 3)}",
        f"MASK_RED_THRESHOLD = {_fmt(mask_red, 3)}",
        f"MASK_MIN_LUMINANCE = {_fmt(lum_min, 3)}",
        f"MASK_MAX_LUMINANCE = {_fmt(lum_max, 3)}",
        f"SHAPE_SIZE = {_fmt(shape_size, 3)}          ; blade size multiplier (1 = CSP default)",
        "",
        "; Blade atlas group — the texture CSP samples for blade shapes/tint. CSP ships a default;",
        "; override TEXTURE only with your own power-of-two atlas under extension/grass_fx/.",
        "[GRASS_FX_TEXTURE_0]",
        f"TEXTURE = {texture}",
        "",
        "; One configuration block binding OUR grass material to the blade look (density/tint).",
        "[GRASS_FX_CONFIGURATION_0]",
        f"MATERIALS = {', '.join(grass_materials)}",
        f"BRIGHTNESS = {_fmt(brightness, 3)}",
        f"HEALTH = {_fmt(health, 3)}",
        "",
    ]
    return out


# --------------------------------------------------------------------------------------------------
# extension/trees/README — the license-clean "reference, don't bundle" manifest.
# --------------------------------------------------------------------------------------------------
def trees_readme(slug: str, expected_species: Sequence[str]) -> str:
    lines = [
        f"{slug} — TreeFX models (user-supplied, NOT committed)",
        "=" * 56,
        "",
        "This track renders 3D trees via CSP TreeFX. The tree MODELS are third-party, licensed,",
        "and USER-SUPPLIED — prodrive-ac-builder never downloads or commits them. We only generate",
        "the config that REFERENCES each species by its .bin filename (trees.txt + ext_config",
        "[TREES]); those config files are ours and license-clean.",
        "",
        "PLACE the licensed .bin models listed below in THIS folder (extension/trees/). They are",
        "gitignored (*.bin) so they never end up in a committed/published track. A missing model is",
        "harmless — its 'tree:' lines simply render nothing until you drop the file in.",
        "",
        "Expected .bin models for this build:",
    ]
    if expected_species:
        lines += [f"  - {sp}" for sp in expected_species]
    else:
        lines.append("  (none — no TreeFX zones placed any trees)")
    lines += ["", "Format + GrassFX reference: docs/TREEFX-FORMAT.md", ""]
    return "\n".join(lines)
