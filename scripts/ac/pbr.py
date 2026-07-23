"""Shared PBR material setup for the Blender render/export scripts.

Maps an imported object's name prefix to a texture set under ``assets/textures/`` and builds a
Principled material with a tiling diffuse + normal map driven by the mesh's UVs (generated in the
geometry pass). Falls back to a flat colour when a texture is missing, so it degrades gracefully.

Not a Blender entry point — import it (``from scripts.ac import pbr``) and call ``setup_material``.
"""

from __future__ import annotations

import json
from pathlib import Path

# name-prefix -> (diffuse, normal, roughness, fallback_rgba, metallic, water)
TEXTURES = {
    "1ROAD":   ("asphalt_cracked_diffuse.jpg", "asphalt_cracked_normal.jpg", 0.80, (0.10, 0.10, 0.11, 1), 0.0, False),  # cracked/alligator asphalt (LA Canyons lac_tarmac_cracked)
    "1RUNOFF": ("asphalt_cracked_diffuse.jpg", "asphalt_cracked_normal.jpg", 0.85, (0.16, 0.15, 0.14, 1), 0.0, False),
    "CALIB":   (None, None, 0.4, (1.0, 0.05, 0.6, 1), 0.9, False),  # bright emissive magenta — temp orientation poles
    "1KERB":   ("kerb_diffuse.png",    None,                 0.55, (0.62, 0.10, 0.08, 1), 0.0, False),
    "MARKINGS": ("line_white.png", None, 0.45, (0.88, 0.88, 0.85, 1), 0.0, False),  # white lane lines — solid-colour TEXTURE, not a bare colour: texture-less kn5 materials render BLACK in-engine (the San Diego lines-end-up-black bug)
    "YLINE":   ("line_yellow.png", None, 0.45, (0.85, 0.68, 0.10, 1), 0.0, False),  # double-yellow centreline (two-way roads, Lake Murray style)
    "ROADTEXT": ("roadtext_atlas.png", None, 0.5, (0.92, 0.92, 0.90, 1), 0.0, False),  # painted street-name decals (alpha cutout)
    "1LAWN":   ("grass_diffuse.jpg", "grass_normal.jpg",     0.90, (0.30, 0.42, 0.20, 1), 0.0, False),  # irrigated suburban green turf (SoCal neighbourhood tiles)
    "1GRASS":  ("ground_dry_diffuse.jpg", None,              0.95, (0.42, 0.38, 0.29, 1), 0.0, False),  # dry dirt/chaparral (canyon/hill/freeway-cut tiles)
    "SHOULDERUND": ("ground_dry_diffuse.jpg", None, 0.95, (0.36, 0.32, 0.25, 1), 0.0, False),  # embankment/cut mass seen from BELOW (visual underside of the shoulder strip)
    "ROCKCUT":  ("rock_granite_red.png", None, 0.92, (0.45, 0.30, 0.24, 1), 0.0, False),  # blasted red granite cut faces (Kevin's stone-wall asset harvest) — task #17 consumer
    "RETWALL":  ("stone_tan.png", None, 0.85, (0.55, 0.48, 0.38, 1), 0.0, False),           # retaining wall body — task #17 consumer
    "1WALL_PARA": ("stone_tan.png", None, 0.85, (0.58, 0.51, 0.41, 1), 0.0, False),          # 1913 stone guard parapets (collidable; MUST precede the generic 1WALL entry — prefix match is dict-ordered)
    "BUSHES":  ("bushes_atlas.png", None, 0.9, (0.36, 0.37, 0.24, 1), 0.0, False),  # dry scrub billboards (LA Canyons bo_bushes_11)
    "HIGHWAY": ("asphalt_cracked_diffuse.jpg", "asphalt_cracked_normal.jpg", 0.80, (0.30, 0.30, 0.32, 1), 0.0, False),  # I-70 deck
    "HWYSTRUCT": ("building_diffuse.jpg", "building_normal.jpg", 0.85, (0.68, 0.68, 0.66, 1), 0.0, False),  # concrete parapets/piers
    "BUILDING": ("building_diffuse.jpg", "building_normal.jpg", 0.88, (0.60, 0.60, 0.58, 1), 0.0, False),  # tilt-up concrete (mined Hamburg)
    "WAREHOUSE": ("warehouse_diffuse.jpg", "warehouse_normal.jpg", 0.90, (0.56, 0.56, 0.54, 1), 0.0, False),  # weathered concrete warehouse walls (mined Hamburg)
    "WHMETAL":  ("warehouse_metal_diffuse.jpg", "warehouse_metal_normal.jpg", 0.65, (0.58, 0.60, 0.62, 1), 0.10, False),  # corrugated-metal warehouse (CC0 CorrugatedSteel) — warehouse variety
    "BRICK":   ("brick_diffuse.jpg",   "brick_normal.jpg",   0.90, (0.45, 0.28, 0.22, 1), 0.0, False),  # red brick (mined Hamburg) — building variety
    "STUCCO":  ("stucco_diffuse.jpg",  "stucco_normal.jpg",  0.88, (0.62, 0.32, 0.28, 1), 0.0, False),  # painted stucco (mined Hamburg) — building variety
    "BARRIER": ("building_diffuse.jpg", "building_normal.jpg", 0.85, (0.74, 0.74, 0.72, 1), 0.0, False),  # concrete jersey K-rail
    "1WALL":   ("concrete_barrier_diffuse.png", "concrete_barrier_normal.png", 0.85, (0.72, 0.72, 0.70, 1), 0.0, False),  # Kevin's 4 m concrete barrier modules (props.concrete_barriers)
    "FENCEWOOD": ("ranch_fence_diffuse.png", "ranch_fence_normal.png", 0.9, (0.45, 0.35, 0.25, 1), 0.0, False),  # split-rail ranch fence panels (scenery.fences, right-of-way line)
    "EUROSIGN": ("eurosign_atlas.png", None, 0.5, (0.85, 0.85, 0.85, 1), 0.1, False),  # circular sign faces, alpha-cut (scenery.road_signs)
    "CONTAINER": ("container_diffuse.jpg", "container_normal.jpg", 0.70, (0.55, 0.30, 0.26, 1), 0.0, False),  # mined Hamburg shipping-container stacks (warehouse yards)
    "CHAINLINK": ("chainlink_diffuse.png", None, 0.60, (0.55, 0.56, 0.58, 1), 0.30, False),  # procedural alpha-cutout chain-link (warehouse yard fences)
    "ROOF":    ("roof_diffuse.jpg",    "roof_normal.jpg",    0.55, (0.26, 0.32, 0.45, 1), 0.10, False),  # PVC membrane roof (mined Hamburg)
    "RFMETAL": ("warehouse_metal_diffuse.jpg", "warehouse_metal_normal.jpg", 0.60, (0.55, 0.57, 0.60, 1), 0.15, False),  # corrugated-metal roof (on metal warehouses)
    "WATER":   (None, None, 0.04, (0.02, 0.10, 0.20, 1), 0.0, True),
    "GUARDRAIL": ("guardrail_diffuse.png", "guardrail_normal.png", 0.45, (0.74, 0.75, 0.77, 1), 0.35, False),  # swept freeway W-beam guardrail (metal)
    "GANTRY":  ("warehouse_metal_diffuse.jpg", "warehouse_metal_normal.jpg", 0.50, (0.66, 0.68, 0.70, 1), 0.40, False),  # galvanized-steel overhead sign portal (SRP-style expressway gantry)
    "FWSIGN":  (None, None, 0.55, (0.06, 0.30, 0.16, 1), 0.0, False),  # green US freeway overhead sign panel
    "PALMS":   ("palms_atlas.png", None, 0.85, (0.20, 0.34, 0.14, 1), 0.0, False),  # California fan palm billboards (SoCal surface streets)
    "TREES":   ("trees_atlas.png", None, 0.90, (0.13, 0.30, 0.11, 1), 0.0, False),  # mined Colorado 2x2 broadleaf cutout atlas
    "CONIFER": ("conifer_atlas.png", None, 0.90, (0.09, 0.20, 0.09, 1), 0.0, False),  # pine/spruce billboards (mountain tracks, scenery.tree_style "conifer")
    "LIGHTS":  ("solid_lens_warm.png", None, 0.40, (0.95, 0.82, 0.42, 1), 0.4, False),  # cobra-head lamp lens — SOLID TEXTURE (texture-less = renders BLACK); CSP adds ksEmissive at night
    "LIGHTPOST": ("solid_metal_dark.png", None, 0.60, (0.28, 0.29, 0.31, 1), 0.5, False),  # galvanized streetlight mast + arm — SOLID TEXTURE (was texture-less -> BLACK -> invisible posts, v0.7.5)
    "LAMPHEAD": ("solid_metal_dark.png", None, 0.55, (0.32, 0.33, 0.35, 1), 0.3, False),  # luminaire housing — faint CSP night emissive so the lens reads attached
    "SIGNS":   ("signs_atlas.png", None, 0.55, (0.12, 0.40, 0.18, 1), 0.0, False),  # green street-name panels
    "MOUNTAINS": (None, None, 0.95, (0.52, 0.57, 0.66, 1), 0.0, False),  # hazy blue Front Range backdrop (flat silhouette)
    "SIGNPOST": ("solid_metal_grey.png", None, 0.60, (0.45, 0.46, 0.48, 1), 0.0, False),      # grey metal posts — SOLID TEXTURE (texture-less renders black)
    "POLE":    (None, None, 0.85, (0.34, 0.24, 0.16, 1), 0.0, False),               # wooden utility/power pole
    "WIRE":    (None, None, 0.50, (0.06, 0.06, 0.07, 1), 0.0, False),               # overhead power cable
}

# materials drawn as alpha-cutout — wire the texture alpha + clip the transparent bg (trees as
# billboards; painted street names as flat road decals). The export addon also reads this set to set
# ALPHATEST=1 on these materials so the kn5 cuts out the transparent background in-engine.
BILLBOARD = {"TREES", "CONIFER", "ROADTEXT", "BUSHES", "CHAINLINK", "PALMS", "EUROSIGN"}

# Shader property overrides written into the kn5 material (export_kn5_addon persistence INI).
# WHY: materials shipped with NO properties -> AC falls back to shader defaults (ksDiffuse ~1.0).
# Vegetation billboards at full diffuse response turned the Lariat's roadside forest radioactive
# green under the streetlight cones while the pools on the near-black asphalt looked correct.
# Real AC vegetation runs ksAmbient-heavy / ksDiffuse-low (LA Canyons convention) so directional
# light (sun OR CSP lamps) grazes it instead of blasting it. Fence wood damped for the same reason.
KS_PROPS = {
    "CONIFER":  {"ksAmbient": 0.55, "ksDiffuse": 0.10, "ksSpecular": 0.0, "ksSpecularEXP": 1.0},
    "TREES":    {"ksAmbient": 0.55, "ksDiffuse": 0.10, "ksSpecular": 0.0, "ksSpecularEXP": 1.0},
    "BUSHES":   {"ksAmbient": 0.55, "ksDiffuse": 0.10, "ksSpecular": 0.0, "ksSpecularEXP": 1.0},
    "PALMS":    {"ksAmbient": 0.55, "ksDiffuse": 0.10, "ksSpecular": 0.0, "ksSpecularEXP": 1.0},
    "FENCEWOOD": {"ksAmbient": 0.45, "ksDiffuse": 0.35, "ksSpecular": 0.05, "ksSpecularEXP": 8.0},
    "1GRASS":   {"ksAmbient": 0.50, "ksDiffuse": 0.22, "ksSpecular": 0.0, "ksSpecularEXP": 1.0},
    "POLE":     {"ksAmbient": 0.40, "ksDiffuse": 0.15, "ksSpecular": 0.0, "ksSpecularEXP": 1.0},
    "SIGNPOST": {"ksAmbient": 0.40, "ksDiffuse": 0.25, "ksSpecular": 0.0, "ksSpecularEXP": 1.0},
}


def texture_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "assets" / "textures"


def load_overrides(project_dir: str | Path) -> dict[str, dict]:
    """Real-world captured textures to use in place of the stock ``assets/textures`` set, keyed by mesh
    prefix. Read from ``<project>/source/realworld_capture.json`` (texture_overrides[], written by
    scripts/capture/textures.py); paths resolve relative to the project. Empty when no capture exists →
    the build uses the stock textures unchanged. Pure (no bpy) so it's unit-testable."""
    project_dir = Path(project_dir)
    out: dict[str, dict] = {}
    # 1) config-level overrides (track.config.json texture_overrides) — swap a stock texture per
    #    track without renaming meshes (the Lariat's terrain renders alpine grass instead of dry
    #    dirt while staying 1GRASS for physics/audits/GrassFX). Paths resolve vs the REPO root
    #    (assets/textures/...) or the project dir.
    cfgp = project_dir / "track.config.json"
    if cfgp.exists():
        repo = Path(__file__).resolve().parents[2]
        for mat, o in (json.loads(cfgp.read_text()).get("texture_overrides", {}) or {}).items():
            ent = {}
            for k in ("diffuse", "normal"):
                if o.get(k):
                    p = repo / o[k] if (repo / o[k]).exists() else project_dir / o[k]
                    ent[k] = str(p)
            if ent:
                out[str(mat).upper()] = ent
    # 2) capture-level overrides (real-world textures) win over config
    cap = project_dir / "source" / "realworld_capture.json"
    if not cap.exists():
        return out
    for o in json.loads(cap.read_text()).get("texture_overrides", []):
        ent = {}
        if o.get("diffuse"):
            ent["diffuse"] = str(project_dir / o["diffuse"])
        if o.get("normal"):
            ent["normal"] = str(project_dir / o["normal"])
        if ent:
            out[str(o["material"]).upper()] = ent
    return out


def setup_material(bpy, obj, tex_dir: Path | None = None, overrides: dict | None = None):
    """Assign a Principled material (diffuse + normal from ``tex_dir``) to ``obj`` by name prefix.
    ``overrides`` (from :func:`load_overrides`) swaps in real captured textures for matching prefixes.
    Returns the material so callers can stamp AC metadata (e.g. ``mat['shaderName']``) on it."""
    tex_dir = Path(tex_dir) if tex_dir else texture_dir()
    name = obj.name.upper()
    key = next((k for k in TEXTURES if name.startswith(k)), None)
    diff, norm, rough, color, metal, water = TEXTURES.get(
        key, (None, None, 0.8, (0.5, 0.5, 0.5, 1), 0.0, False))
    ov = (overrides or {}).get(key or "", {})

    mat = bpy.data.materials.new(obj.name + "_mat")
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")

    dpath = Path(ov["diffuse"]) if ov.get("diffuse") else (tex_dir / diff if diff else None)
    if dpath and dpath.exists():
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = bpy.data.images.load(str(dpath), check_existing=True)
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        if key in BILLBOARD:  # cutout billboard: wire alpha + clip the transparent background
            nt.links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
            for attr, val in (("blend_method", "CLIP"), ("shadow_method", "CLIP"),
                              ("surface_render_method", "DITHERED")):
                try:
                    setattr(mat, attr, val)
                except Exception:
                    pass
    else:
        bsdf.inputs["Base Color"].default_value = color
    bsdf.inputs["Roughness"].default_value = 0.05 if water else rough
    bsdf.inputs["Metallic"].default_value = metal
    if water and "Transmission Weight" in bsdf.inputs:
        bsdf.inputs["Transmission Weight"].default_value = 0.4

    npath = Path(ov["normal"]) if ov.get("normal") else (tex_dir / norm if norm else None)
    if npath and npath.exists():
        ntex = nt.nodes.new("ShaderNodeTexImage")
        nimg = bpy.data.images.load(str(npath), check_existing=True)
        nimg.colorspace_settings.name = "Non-Color"
        ntex.image = nimg
        nmap = nt.nodes.new("ShaderNodeNormalMap")
        nt.links.new(ntex.outputs["Color"], nmap.inputs["Color"])
        nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    obj.data.materials.clear()
    obj.data.materials.append(mat)
    return mat
