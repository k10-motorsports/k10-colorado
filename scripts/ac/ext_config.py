"""Generate a CSP (Custom Shaders Patch) ext_config.ini for the track.

Adds the in-engine effects layer on top of the kn5 geometry:
  - GrassFX  : 3D procedural grass on the GRASS mesh (the road/kerbs/etc. occlude it),
  - Water    : the smWaterSurface shader on Sand Creek (a material the kn5 already carries),
  - Lights   : a [LIGHT_SERIES] that drops one warm light on each streetlight post, plus an emissive
               lamp material so the posts glow at night.

CSP reads ``<track>/extension/ext_config.ini`` automatically when the track loads. Mesh/material names
match the kn5 exported from the FBX (pbr.py names materials ``<object>_mat``). Syntax follows the
acc-extension-config wiki — VERIFY IN-GAME, the keys shift between CSP versions; this is a strong
starting point, not a guaranteed-final file.

Run:  python -m scripts.ac.ext_config projects/<slug>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _obj_groups(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln[2:].strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("o ")]


def generate(project_dir: str | Path) -> Path:
    project_dir = Path(project_dir)
    data = project_dir / "data"
    cfg_raw = json.loads((project_dir / "track.config.json").read_text())
    slug = cfg_raw["slug"]
    # Light pollution is town-specific — read it from config (lighting.light_pollution) so each track
    # gets its own night sky glow instead of a hardcoded one. Defaults below = a big coastal metro
    # (warm-white, broad, faint) which suits San Diego; a small industrial loop can override to a tight
    # amber sodium glow. COLOR is r,g,b (0..1) only.
    lp = (cfg_raw.get("lighting", {}) or {}).get("light_pollution", {}) or {}
    lp_color = lp.get("color", [1.0, 0.86, 0.66])
    lp_density = lp.get("density", 0.22)
    lp_radius_km = lp.get("radius_km", 6.0)
    lp_rel = lp.get("relative_position", [-1.5, 0, -2.0])    # bias toward the brightest sky (downtown)
    lp_note = lp.get("_note", "broad warm-white metro glow")
    groups = _obj_groups(data / "track.obj") + _obj_groups(data / "environment.obj")

    grass_meshes = [g for g in groups if g.upper().startswith(("1GRASS", "1LAWN"))] or ["1GRASS"]
    # GrassFX OCCLUDING_MESHES = ONLY ground-level solid surfaces that sit on the grass plane (so grass
    # doesn't grow through them). This is a deliberate ALLOW-LIST. Anything else MUST NOT be an occluder:
    # foliage billboards (TREES/BUSHES), the far-field MOUNTAINS backdrop, elevated HIGHWAY/HWYSTRUCT decks,
    # thin LIGHTS posts, barriers, decorative props. WHY it's an allow-list not a blacklist: GrassFX builds
    # its occlusion field over the COMBINED bounding volume of these meshes at SESSION load — a far-field
    # mesh wrongly enrolled (e.g. a 24 km x 1.45 km mountain backdrop) explodes that volume and the GPU
    # pass exceeds the Windows TDR watchdog -> whole-PC freeze. That exact bug shipped v0.12.1-v0.12.6 when
    # MOUNTAINS/HIGHWAY got auto-swept in by the old blacklist. A missing prefix here only lets grass grow
    # through something (cosmetic); an over-broad list crashes the PC, so fail safe toward FEWER occluders.
    # Building WALLS + roofs all occlude (they sit on / rise from the grass plane and are near-field —
    # within ~330 m of the lap, so no far-field TDR risk). The variety pass split the single BUILDINGS/
    # WAREHOUSE/ROOFS groups into façade variants (BRICK/STUCCO, WHMETAL, RFMETAL); list every variant so
    # the split stays occlusion-transparent (grass still doesn't grow through any building wall). CHAINLINK
    # fences are deliberately NOT occluders (see-through + thin), nor are POLE/WIRE/SIGNS.
    OCCLUDE_PREFIXES = ("1ROAD", "1KERB", "1RUNOFF", "MARKINGS", "ROADTEXT",
                        "BUILDINGS", "BRICK", "STUCCO", "WAREHOUSE", "WHMETAL",
                        "ROOFS", "RFMETAL", "WATER")
    occluders = [g for g in groups if g.upper().startswith(OCCLUDE_PREFIXES)]
    has_water = any(g.upper() == "WATER" for g in groups)
    has_lights = any(g.upper() == "LIGHTS" for g in groups)
    has_windows = any(g.upper() in ("WINDOWS", "BUILDING", "BUILDINGS") for g in groups)
    has_signs = any(g.upper() == "SIGNS" for g in groups)

    # RainFX material lists (CSP RainFX self-enables from these lists — there is no master on/off key).
    # PUDDLES/SOAKING are road-only per the CSP docs (kerbs deliberately excluded); MARKINGS get the
    # wet-paint physics; grass is ROUGH (darkens, no reflections); WATER keeps its own shader.
    paved = [f"{g}_mat" for g in groups if g.upper().startswith(("1ROAD", "1RUNOFF"))] \
        or ["1ROAD_main_mat", "1ROAD_shoulder_mat", "1RUNOFF_corners_mat"]
    lines_mats = [f"{g}_mat" for g in groups if g.upper().startswith("MARKINGS")] or ["MARKINGS_mat"]
    rough_mats = [f"{g}_mat" for g in grass_meshes]

    # GrassFX builds a 3D-grass occlusion field over EVERY listed grass mesh at session load. Over a
    # huge area (a 27 km network with 100+ grass tiles) that field is enormous and can exceed the GPU
    # watchdog -> whole-PC freeze (TDR). So it's OPT-IN per track via lighting.grassfx (default off for
    # safety on big tracks; small loops like Sand Creek can turn it on).
    grassfx_on = bool((cfg_raw.get("lighting", {}) or {}).get("grassfx", False))
    out = ["; ============================================================================",
           f"; {slug} — CSP ext_config (generated by scripts/ac/ext_config.py)",
           "; Custom Shaders Patch reads this automatically. Names match the kn5 (pbr.py -> <obj>_mat).",
           "; VERIFY IN-GAME — CSP config keys evolve between versions.",
           "; Ref: github.com/ac-custom-shaders-patch/acc-extension-config/wiki",
           "; ============================================================================", ""]
    if grassfx_on:
        out += ["; --- GrassFX: 3D grass on the terrain -----------------------------------",
                "[GRASS_FX]",
                f"GRASS_MESHES = {', '.join(grass_meshes)}",
                f"OCCLUDING_MESHES = {', '.join(occluders)}",
                "MASK_MAIN_THRESHOLD = 0.5",
                "MASK_RED_THRESHOLD = 0.05",
                "MASK_MIN_LUMINANCE = 0.02",
                "MASK_MAX_LUMINANCE = 0.35", ""]
    else:
        out += ["; GrassFX DISABLED for this track (lighting.grassfx=false): 100+ grass meshes over a",
                "; 27 km map = a GPU-watchdog-busting occlusion field -> whole-PC freeze. The ground keeps",
                "; its flat grass texture; only the 3D procedural grass blades are off.", ""]
    out += ["; --- RainFX: wet surfaces, puddles, reflections, spray ----------------------",
           "; No master enable key — listing materials IS the enable. PUDDLES/SOAKING are road-only",
           "; per CSP docs (kerbs excluded on purpose). WATER keeps its own shader (not listed here).",
           "; Ref: acc-extension-config wiki -> Tracks - RainFX",
           "[RAIN_FX]",
           f"PUDDLES_MATERIALS = {', '.join(paved)}",
           f"SOAKING_MATERIALS = {', '.join(paved)}",
           f"LINES_MATERIALS = {', '.join(lines_mats)}",
           f"ROUGH_MATERIALS = {', '.join(rough_mats)}", "",
           "; --- Light pollution: warm sodium sky/horizon glow over the loop at night ---",
           "; COLOR is r,g,b (0..1) ONLY — no 4th term. CSP auto-gates it to night.",
           "[LIGHT_POLLUTION]",
           "ACTIVE = 1",
           f"COLOR = {', '.join(str(c) for c in lp_color)}      ; {lp_note}",
           f"DENSITY = {lp_density}",
           f"RADIUS_KM = {lp_radius_km}",
           f"RELATIVE_POSITION = {', '.join(str(p) for p in lp_rel)}  ; bias toward the brightest sky",
           "",
           "; --- Global night lighting: lift emissive response a hair -------------------",
           "[LIGHTING]",
           "LIT_MULT = 1.05",
           "BOUNCED_LIGHT_MULT = 1, 1, 1, 0.12", ""]

    if has_water:
        out += ["; --- Water: real water shader on Sand Creek ---------------------------------",
                "[INCLUDE: common/materials_track.ini]", "",
                "[Material_Water]",
                "Materials = WATER_mat",
                "Type = POND", ""]

    if has_lights:
        out += ["; --- Streetlights: one light per post + glowing lamp at night ---------------",
                "[LIGHT_SERIES_STREETLIGHTS]",
                "MESHES = LIGHTS",
                "OFFSET = 0, 3.5, 0          ; lift the light toward the top of the ~9 m post",
                "COLOR = 1.0, 0.82, 0.55, 30 ; warm sodium-ish, last value = intensity",
                "COLOR_OFF = 0, 0, 0, 0      ; fully off by day",
                "CONDITION = NIGHT_SMOOTH    ; ramp on at dusk (CSP pre-shipped condition)",
                "RANGE = 38",
                "SPOT = 124",
                "SPOT_SHARPNESS = 0.3",
                "DIRECTION = 0, -1, 0        ; shine downward",
                "CLUSTER_THRESHOLD = 8       ; posts are ~48 m apart -> one light each", "",
                "[MATERIAL_ADJUSTMENT_STREETLIGHTS]",
                "MATERIALS = LIGHTS_mat",
                "KEY_0 = ksEmissive",
                "VALUE_0 = 255, 209, 166, 0.5  ; ksEmissive is 0-255 RGB + brightness (NOT 0..1!)",
                "VALUE_0_OFF = 0, 0, 0, 0",
                "CONDITION = NIGHT_SMOOTH", ""]

    # Night-only emissive windows/signs — emitted when the scenery pass adds those meshes (v0.6.0).
    if has_windows:
        win_mat = "WINDOWS_mat" if any(g.upper() == "WINDOWS" for g in groups) else "BUILDING_mat"
        out += ["; --- Lit windows at night --------------------------------------------------",
                "[MATERIAL_ADJUSTMENT_LIT_WINDOWS]",
                f"MATERIALS = {win_mat}",
                "KEY_0 = ksEmissive",
                "VALUE_0 = 255, 200, 112, 0.5",
                "VALUE_0_OFF = 0, 0, 0, 0",
                "CONDITION = NIGHT_SMOOTH", ""]
    if has_signs:
        out += ["; --- Illuminated signage ---------------------------------------------------",
                "[MATERIAL_ADJUSTMENT_LIT_SIGNS]",
                "MATERIALS = SIGNS_mat",
                "KEY_0 = ksEmissive",
                "VALUE_0 = 255, 217, 153, 0.6",
                "VALUE_0_OFF = 13, 13, 13, 0",
                "CONDITION = NIGHT_SMOOTH", ""]

    ext = project_dir / "build" / slug / "extension"
    ext.mkdir(parents=True, exist_ok=True)
    cfg = ext / "ext_config.ini"
    cfg.write_text("\n".join(out) + "\n", encoding="utf-8")
    return cfg


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m scripts.ac.ext_config <project-dir>")
    cfg = generate(sys.argv[1])
    print(f"wrote {cfg}")


if __name__ == "__main__":
    main()
