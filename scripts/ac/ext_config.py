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
import shutil
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
    # PREFIX matching everywhere, never exact: split_mesh_under_cap renames every over-cap mesh to
    # <name>_a.._n (LIGHTS -> LIGHTS_a..). v0.7.5 shipped BOTH street tracks with completely dead
    # streetlights because `== "LIGHTS"` matched nothing after the split.
    has_water = any(g.upper().startswith("WATER") for g in groups)
    lights_groups = [g for g in groups if g.upper().startswith("LIGHTS")]
    has_lights = bool(lights_groups)
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
    _gfx_raw = (cfg_raw.get("lighting", {}) or {}).get("grassfx", False)
    grassfx_on = bool(_gfx_raw)
    gfx_cfg = _gfx_raw if isinstance(_gfx_raw, dict) else {}

    # TreeFX: build_env (with scenery.treefx.enabled) writes data/treefx.json — real tree positions
    # grouped by config zone, ground-sampled + pavement/clearance-rejected the SAME way baked foliage
    # is. If present we render extension/trees/trees.txt and a [TREES] block that references the
    # user-supplied .bin MODELS by filename only (never bundled — see the license note in treefx.py).
    from scripts.environment import treefx as _tfxmod
    _tfx = None
    _tfx_json = data / "treefx.json"
    if _tfx_json.exists():
        try:
            _tfx = _tfxmod.TreeFXCollector.from_json(json.loads(_tfx_json.read_text()))
            if not _tfx.enabled:
                _tfx = None
        except Exception as e:   # unreadable/stale sidecar -> skip TreeFX, don't fail the whole config
            print(f"  [ext_config] treefx sidecar unreadable ({e}); skipping [TREES]")
            _tfx = None

    out = ["; ============================================================================",
           f"; {slug} — CSP ext_config (generated by scripts/ac/ext_config.py)",
           "; Custom Shaders Patch reads this automatically. Names match the kn5 (pbr.py -> <obj>_mat).",
           "; VERIFY IN-GAME — CSP config keys evolve between versions.",
           "; Ref: github.com/ac-custom-shaders-patch/acc-extension-config/wiki",
           "; ============================================================================", ""]
    if grassfx_on and _tfx is not None:
        # TreeFX path only: the richer Nordschleife-style block (GRASS_MESHES/OCCLUDING_MESHES +
        # SHAPE_SIZE + a texture group + one configuration block), bound to OUR mesh/material names.
        # The occluder list stays the same crash-safe ALLOW-LIST computed above (a far-field/oversized
        # mesh here freezes the PC). GATED ON TreeFX so treefx-OFF tracks keep their existing
        # [GRASS_FX] byte-for-byte — this local port intentionally leaves the off path untouched; the
        # global grassfx_block upgrade lands with the .engine switchover.
        out += _tfxmod.grassfx_block(grass_meshes=grass_meshes, occluding_meshes=occluders,
                                     grass_materials=[f"{g}_mat" for g in grass_meshes], cfg=gfx_cfg)
    elif grassfx_on:
        # LEGACY inline block — unchanged for treefx-off tracks (byte-identical to pre-TreeFX output).
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

    # --- TreeFX: 3D trees from a text list (models user-supplied) -------------------------------
    if _tfx is not None:
        # SURFACE_MATERIALS = THIS track's real grass + road materials, so trees snap vertically onto
        # them and align lighting. Prefer an explicit config override; else read the kn5 material
        # names (pbr.py names materials <obj>_mat) from the groups we already have.
        surf_mats = _tfx.surface_materials or ([f"{g}_mat" for g in grass_meshes] + paved)
        _tfx_surf_meshes = _tfx.surface_meshes or grass_meshes
        out += _tfxmod.trees_ext_section(
            ["trees/trees.txt"], surface_materials=surf_mats, surface_meshes=_tfx_surf_meshes,
            compiled_list=_tfx.compiled_list or "compiled_trees.bin",
            expected_species=_tfx.expected_species())

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
                f"Materials = {', '.join(f'{g}_mat' for g in groups if g.upper().startswith('WATER')) or 'WATER_mat'}",
                "Type = POND", ""]

    if has_lights:
        # THE TWO DEAD-LIGHTS BUGS (found on the Lariat, same as San Diego's history through their
        # v0.5.1): (1) NIGHT_SMOOTH is NOT a built-in CSP condition — without including
        # common/conditions.ini it evaluates OFF forever; (2) a [LIGHT_SERIES] over ONE merged
        # LIGHTS mesh emits a single light at the blob's centroid, not one per lamp. Fix per the
        # working Lake Murray recipe: include the conditions file and emit an explicit [LIGHT_N]
        # per lamp head — positions CLUSTERED FROM THE BUILT KN5 (frame-proof: whatever yaw/mirror
        # the export applied, these are the shipped world coordinates).
        sl = (cfg_raw.get("lighting", {}) or {}).get("streetlight", {}) or {}
        sl_i = sl.get("intensity", 5.0)
        sl_r = sl.get("range_m", 24.0)
        sl_e = sl.get("emissive", 0.35)
        # pool shaping — CSP RANGE scales deposited energy, not just reach: widening range without
        # cutting intensity saturates the pool core white (v0.7.4 lesson). Wide+dim needs all four.
        sl_spot = sl.get("spot_deg", 124)
        sl_sharp = sl.get("spot_sharpness", 0.3)
        sl_grad = sl.get("range_gradient_offset", 0.2)
        sl_spec = sl.get("specular_mult", 0.6)
        sl_c = sl.get("color", [1.0, 0.82, 0.55])
        sl_ec = sl.get("emissive_color", [255, 209, 166])
        out += ["; --- Streetlights (per-lamp; see dead-lights notes in ext_config.py) ---------",
                "[INCLUDE]",
                "INCLUDE = common/conditions.ini   ; defines NIGHT_SMOOTH — NOT built-in", ""]
        heads: list[tuple[float, float, float]] = []
        kn5p = project_dir / "build" / f"{slug}.kn5"
        if kn5p.exists():
            try:
                from scripts.ac.verify_kn5 import _parse
                _nodes, _meshes = _parse(kn5p)
                pts = [v for m in _meshes if m["name"].upper().startswith("LIGHTS") for v in m["P"]]
                buckets: dict[tuple[int, int], list] = {}
                for x, y, z in pts:
                    buckets.setdefault((int(x // 5), int(z // 5)), []).append((x, y, z))
                for vs in buckets.values():
                    heads.append(tuple(sum(c) / len(vs) for c in zip(*vs)))
            except Exception as e:  # kn5 unreadable -> fall through to series
                print(f"  [ext_config] LIGHTS clustering failed ({e}); falling back to LIGHT_SERIES")
        if heads:
            c255 = [round(255 * float(v)) for v in sl_c]
            for i, (hx, hy, hz) in enumerate(sorted(heads)):
                out += [f"[LIGHT_{i}]", "ACTIVE = 1",
                        f"POSITION = {hx:.2f}, {hy - 0.15:.2f}, {hz:.2f}",
                        "DIRECTION = 0, -1, 0",
                        f"COLOR = {c255[0]}, {c255[1]}, {c255[2]}, {sl_i}",
                        "COLOR_OFF = 0, 0, 0, 0",
                        f"SPOT = {sl_spot}", f"SPOT_SHARPNESS = {sl_sharp}",
                        f"RANGE = {sl_r}", f"RANGE_GRADIENT_OFFSET = {sl_grad}",
                        f"SPECULAR_MULT = {sl_spec}",
                        "FADE_AT = 1200", "FADE_SMOOTH = 200",
                        "CONDITION = NIGHT_SMOOTH", ""]
            print(f"  [ext_config] {len(heads)} per-lamp [LIGHT_N] entries clustered from the kn5")
            # --- DANGER LIGHTS: real red light sources at every warning board, FLASHING, and
            #     visible from far (Kevin: "just red boxes not lights at all... flashing
            #     preferably"). Clustered from the kn5 like the lamps. Alternating two-phase
            #     flash via CSP's flashing keys; if a runtime lacks them the light stays solid.
            dpts = [v for m in _meshes if m["name"].upper().startswith("DANGERLITE") for v in m["P"]]
            dbuckets: dict[tuple[int, int], list] = {}
            for x, y, z in dpts:
                dbuckets.setdefault((int(x // 6), int(z // 6)), []).append((x, y, z))
            dboards = [tuple(sum(c) / len(vs) for c in zip(*vs)) for vs in dbuckets.values()]
            for j, (dx, dy, dz) in enumerate(sorted(dboards)):
                out += [f"[LIGHT_DANGER_{j}]", "ACTIVE = 1",
                        f"POSITION = {dx:.2f}, {dy + 0.4:.2f}, {dz:.2f}",
                        "DIRECTION = 0, -0.3, 0",
                        "COLOR = 255, 24, 16, 14",              # hot red, strong
                        "COLOR_OFF = 0, 0, 0, 0",
                        "RANGE = 34", "RANGE_GRADIENT_OFFSET = 0.25",
                        "SPECULAR_MULT = 0.4",
                        "FADE_AT = 3000", "FADE_SMOOTH = 400",  # readable from the far straight
                        "FLASHING_PERIOD = 0.8",                # CSP blink keys (no-op if absent)
                        f"FLASHING_SKIP = {0.5 if j % 2 else 0.0}",
                        ""]
            if dboards:
                print(f"  [ext_config] {len(dboards)} flashing danger lights clustered from the kn5")
        else:
            out += ["[LIGHT_SERIES_STREETLIGHTS]", "MESHES = LIGHTS", "OFFSET = 0, -0.1, 0",
                    f"COLOR = {sl_c[0]}, {sl_c[1]}, {sl_c[2]}, {sl_i}", "COLOR_OFF = 0, 0, 0, 0",
                    "CONDITION = NIGHT_SMOOTH", f"RANGE = {sl_r}", "SPOT = 124", "SPOT_SHARPNESS = 0.3",
                    "DIRECTION = 0, -1, 0", "CLUSTER_THRESHOLD = 8", ""]
        # Night damping ENFORCED IN-ENGINE: the kn5's baked ksDiffuse (pbr.KS_PROPS) is the first
        # line, but CSP dynamic-light response paths vary by shader/version — this pins vegetation
        # and grass response down at night regardless, so lamp cones read as pools on the ground
        # instead of floodlit neon foliage ("Floodlights", v0.7.8).
        veg_mats = ", ".join(f"{g}_mat" for g in groups
                             if g.upper().startswith(("CONIFER", "TREES", "BUSHES", "PALMS")))
        grass_mats2 = ", ".join(f"{g}_mat" for g in groups if g.upper().startswith(("1GRASS", "1LAWN")))
        if veg_mats:
            out += ["[MATERIAL_ADJUSTMENT_VEG_NIGHT]",
                    f"MATERIALS = {veg_mats}",
                    "KEY_0 = ksDiffuse", "VALUE_0 = 0.04", "VALUE_0_OFF = 0.10",
                    "CONDITION = NIGHT_SMOOTH", ""]
        if grass_mats2:
            out += ["[MATERIAL_ADJUSTMENT_GRASS_NIGHT]",
                    f"MATERIALS = {grass_mats2}",
                    "KEY_0 = ksDiffuse", "VALUE_0 = 0.14", "VALUE_0_OFF = 0.22",
                    "CONDITION = NIGHT_SMOOTH", ""]
        head_mats = ", ".join(f"{g}_mat" for g in groups if g.upper().startswith("LAMPHEAD"))
        if head_mats:
            _ussign = [g for g in groups if g.upper().startswith("USSIGN")]
            if _ussign:
                out += ["[MATERIAL_ADJUSTMENT_USSIGNS]",
                        "ACTIVE = 1",
                        "MATERIALS = " + ", ".join(f"{g}_mat" for g in _ussign),
                        "CONDITION = NIGHT_SMOOTH",
                        "KEY_0 = ksEmissive",
                        "VALUE_0 = 5.5, 4.6, 1.6",    # retroreflective sheeting POPS at night (Kevin: not lit)
                        ""]
            _danger = [g for g in groups if g.upper().startswith("DANGERLITE")]
            if _danger:
                out += ["[MATERIAL_ADJUSTMENT_DANGER]",
                        "ACTIVE = 1",
                        "MATERIALS = " + ", ".join(f"{g}_mat" for g in _danger),
                        "KEY_0 = ksEmissive",
                        "VALUE_0 = 80, 1.1, 0.85",    # hot red, blooms at distance
                        ""]
            out += ["[MATERIAL_ADJUSTMENT_LAMPHEADS]",
                    f"MATERIALS = {head_mats}",
                    "KEY_0 = ksEmissive",
                    "VALUE_0 = 90, 82, 66, 0.10   ; faintly lit luminaire housing — the lens reads ATTACHED",
                    "VALUE_0_OFF = 0, 0, 0, 0",
                    "CONDITION = NIGHT_SMOOTH", ""]
        lights_mats = ", ".join(f"{g}_mat" for g in lights_groups) or "LIGHTS_mat"
        out += ["[MATERIAL_ADJUSTMENT_STREETLIGHTS]",
                f"MATERIALS = {lights_mats}",
                "KEY_0 = ksEmissive",
                f"VALUE_0 = {sl_ec[0]}, {sl_ec[1]}, {sl_ec[2]}, {sl_e}  ; lamp-lens glow (lighting.streetlight.emissive_color/.emissive)",
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

    # TreeFX text list + the .bin MODELS. A stale trees.txt from a previous treefx-on build is removed
    # on a rebuild without TreeFX so the [TREES] block above (absent now) never dangles.
    #
    # BUNDLING (scenery.treefx.bundle=true): these are PERSONAL, non-distributed builds, so we COPY the
    # species .bin models straight from a gitignored per-track bundle dir (scenery.treefx.bundle_dir,
    # default source/trees/) into extension/trees/ next to trees.txt — the packaged track has the forest
    # baked in, no manual "drop the models in" step. The .bin files stay gitignored (never committed);
    # they only ride along in the build output. With bundle off, we fall back to the reference-only
    # manifest (models user-supplied). See docs/TREEFX-FORMAT.md.
    _tfx_scn = (cfg_raw.get("scenery", {}) or {}).get("treefx", {}) or {}
    _bundle = bool(_tfx_scn.get("bundle"))
    _bundle_dir = project_dir / str(_tfx_scn.get("bundle_dir", "source/trees"))
    trees_dir = ext / "trees"
    if _tfx is not None:
        trees_dir.mkdir(parents=True, exist_ok=True)
        (trees_dir / "trees.txt").write_text(_tfx.trees_txt(), encoding="utf-8")
        _exp = _tfx.expected_species()
        bundled, missing = [], []
        if _bundle:
            for sp in _exp:
                src = _bundle_dir / sp
                if src.exists():
                    shutil.copy2(src, trees_dir / sp)
                    bundled.append(sp)
                else:
                    missing.append(sp)
            for _old in trees_dir.glob("*.bin"):          # prune models no longer referenced
                if _old.name not in _exp:
                    _old.unlink()
        (trees_dir / "README").write_text(
            _trees_readme(slug, _exp, bundled, missing, _bundle, _bundle_dir), encoding="utf-8")
        print(f"  [ext_config] TREES: {_tfx.total()} trees across {len(_tfx.zones)} zone(s) "
              f"-> extension/trees/trees.txt")
        if _bundle:
            print(f"  [ext_config] BUNDLED {len(bundled)} .bin model(s) into extension/trees/: "
                  f"{', '.join(bundled) if bundled else '(none)'}")
            if missing:
                print(f"  [ext_config] WARNING missing from {_bundle_dir}/ (forest incomplete): "
                      f"{', '.join(missing)}")
        else:
            print(f"  [ext_config] TreeFX expects user-supplied .bin models in extension/trees/: "
                  f"{', '.join(_exp) if _exp else '(none)'}")
    elif trees_dir.exists():
        for _stale in list(trees_dir.glob("*.bin")) + [trees_dir / "trees.txt"]:
            if _stale.exists():
                _stale.unlink()                            # stale list/models from a prior treefx-on build
    return cfg


def _trees_readme(slug, expected, bundled, missing, bundle_on, bundle_dir):
    """extension/trees/README manifest. Bundling mode (personal builds) documents the models that
    ride along IN this folder; reference mode falls back to the license-clean user-supplied note."""
    if not bundle_on:
        from scripts.environment import treefx as _tfxmod
        return _tfxmod.trees_readme(slug, expected)
    lines = [
        f"{slug} — TreeFX models (BUNDLED — personal, non-distributed build)",
        "=" * 60,
        "",
        "This track renders 3D trees via CSP TreeFX. Because these are PERSONAL builds (not shared),",
        "the .bin tree MODELS are bundled straight into this folder next to trees.txt — the forest is",
        "baked into the package, nothing to drop in. The .bin files stay gitignored (never committed);",
        f"the build copies them from {bundle_dir} at pack time.",
        "",
        "Bundled .bin models in this folder:",
    ]
    lines += ([f"  - {sp}" for sp in bundled] or ["  (none)"])
    if missing:
        lines += ["", f"MISSING from {bundle_dir}/ (these species render nothing until added):"]
        lines += [f"  - {sp}" for sp in missing]
    lines += ["", "Format + GrassFX reference: docs/TREEFX-FORMAT.md", ""]
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m scripts.ac.ext_config <project-dir>")
    cfg = generate(sys.argv[1])
    print(f"wrote {cfg}")


if __name__ == "__main__":
    main()
