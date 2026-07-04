"""Phase 5b: export the built .blend to a real .kn5 via the vendored AC Blender Tools add-on.

This is the SECOND headless-Blender pass. ``build_kn5.py`` builds + saves the scene (welded grass,
dummies, packed-texture materials) as ``blender/<slug>.blend``; this pass opens that .blend, installs
the add-on from ``vendor/io_import_accsv``, writes the persistence ``.ini`` the add-on reads (shader
+ alpha-cutout per material), orients the drivable surfaces face-up, and runs the kn5 exporter.

Self-contained and reproducible — NO ``/tmp`` artifacts. The add-on is zipped on the fly from the
vendored copy in the repo; nothing is downloaded. Slug/paths come from ``track.config.json``.

Run (see scripts/build.sh for the full pipeline):

    "$BLENDER" --background --python scripts/ac/export_kn5_addon.py -- <track-dir>

Validated on Blender 4.2 (the jwl-7 io_import_accsv add-on's target). Newer Blenders may register
the operator under a different name — the export step auto-detects ``exporter.kn5`` / any
``*.kn5`` / ``*assetto*`` operator, so it degrades gracefully and prints what it found.
"""

import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import addon_utils
import bpy


def _track_dir_from_argv(argv):
    if "--" not in argv:
        raise SystemExit("usage: blender --background --python export_kn5_addon.py -- <track-dir>")
    rest = argv[argv.index("--") + 1:]
    if not rest:
        raise SystemExit("missing <track-dir> after '--'")
    return Path(rest[0]).resolve()


project = _track_dir_from_argv(sys.argv)
repo = Path(__file__).resolve().parents[2]
cfg = json.loads((project / "track.config.json").read_text())
slug = cfg["slug"]
build = project / "build"
kn5 = build / f"{slug}.kn5"
blend = project / "blender" / f"{slug}.blend"

# 1. open the prebuilt scene (welded, smooth grass, dummies, materials w/ packed textures)
bpy.ops.wm.open_mainfile(filepath=str(blend))

# 2. install + enable the AC add-on — zipped on the fly from the vendored copy (no /tmp, no download)
addon_src = repo / "vendor" / "io_import_accsv"
if not (addon_src / "__init__.py").exists():
    raise SystemExit(f"vendored AC add-on missing at {addon_src}")
_zip = Path(tempfile.gettempdir()) / "_ac_addon_vendored.zip"
with zipfile.ZipFile(_zip, "w", zipfile.ZIP_DEFLATED) as z:
    for f in addon_src.rglob("*"):
        if f.is_file() and ".git" not in f.parts and "__pycache__" not in f.parts:
            z.write(f, Path("io_import_accsv") / f.relative_to(addon_src))
bpy.ops.preferences.addon_install(filepath=str(_zip), overwrite=True)
for mod in addon_utils.modules():
    if any(k in mod.__name__.lower() for k in ("assetto", "corsa", "accsv")):
        try:
            bpy.ops.preferences.addon_enable(module=mod.__name__)
        except Exception as e:
            print("enable err", e)

# 3. pbr shader/texture map (same prefix logic as the render/export pipeline)
sys.path.insert(0, str(repo / "scripts" / "ac"))
import pbr  # noqa: E402

# Real-world captured textures (realworld_capture.json) REPLACE the stock asset for their prefix. This is
# the actual ship path: the add-on binds materials + embeds textures from THIS .ini, so the override must
# be applied HERE (not only in build_kn5's .blend nodes) or the captured road/grass/kerb/barrier never
# reach the kn5. `project` is absolute (resolved above), so the override paths are absolute too.
overrides = pbr.load_overrides(project)
if overrides:
    print("[export] real-world texture overrides:", sorted(overrides))
assets_tex = repo / "assets" / "textures"


def shader_and_textures(name):
    """(shader, [(slot, filename, source_path)]). A captured override replaces the stock asset for its
    prefix so the SHIPPED kn5 binds to + embeds the real texture; source_path is where to stage it from."""
    key = next((k for k in pbr.TEXTURES if name.upper().startswith(k)), None)
    if key is None:
        return "ksPerPixel", []
    diff, norm = pbr.TEXTURES[key][0], pbr.TEXTURES[key][1]
    ov = overrides.get(key, {})
    tx = []
    if ov.get("diffuse"):
        p = Path(ov["diffuse"]); tx.append(("txDiffuse", p.name, str(p)))
    elif diff:
        tx.append(("txDiffuse", diff, str(assets_tex / diff)))
    if ov.get("normal"):
        p = Path(ov["normal"]); tx.append(("txNormal", p.name, str(p)))
    elif norm:
        tx.append(("txNormal", norm, str(assets_tex / norm)))
    has_norm = bool(norm or ov.get("normal"))
    if key in pbr.BILLBOARD:            # trees / bushes / road-text are alpha-cutout billboards
        return "ksTree", tx
    return ("ksPerPixelMultiMap" if has_norm else "ksPerPixel"), tx


# 4. write the persistence .ini the add-on reads (clear any stray .ini first)
for old in build.glob("*.ini"):
    old.unlink()
texdir = build / "texture"
texdir.mkdir(exist_ok=True)
lines = ["[HEADER]", "VERSION=4", ""]
needed = {}                                # basename -> source path (stock asset OR captured override)
i = 0
for mat in bpy.data.materials:
    if mat.name.startswith("__") or mat.users == 0:
        continue
    obj = mat.name[:-4] if mat.name.endswith("_mat") else mat.name
    shader, tx = shader_and_textures(obj)
    at = 1 if shader == "ksTree" else 0   # tree/bush/text billboards are alpha-cutout
    lines += [f"[MATERIAL_{i}]", f"NAME={mat.name}", f"SHADER={shader}",
              "ALPHABLEND=0", f"ALPHATEST={at}", "DEPTHMODE=0", "VARCOUNT=0", f"RESCOUNT={len(tx)}"]
    for j, (slot, fname, srcpath) in enumerate(tx):
        lines += [f"RES_{j}_NAME={slot}", f"RES_{j}_SLOT={j}", f"RES_{j}_TEXTURE={fname}"]
        needed[fname] = srcpath
    lines.append("")
    i += 1
(build / f"{slug}.ini").write_text("\n".join(lines))

# 5. stage the textures into build/texture/ (the add-on reads RES_*_TEXTURE from there)
for fn, srcpath in needed.items():
    if Path(srcpath).exists():
        shutil.copyfile(srcpath, texdir / fn)
    else:
        print(f"[export] WARNING: texture source missing, not staged: {srcpath}")
print(f"INI materials={i} textures_staged={len(needed)}")

# 5.4 drivable-surface winding: DO NOTHING. build_mesh already emits every drivable surface wound
#     face-up (+Y), the OBJ import preserves it (+Z in Blender), and the kn5 exporter preserves it
#     again — verified end-to-end: a clean build has 1GRASS/1ROAD/1KERB all face-up in the final kn5.
#
#     There used to be a "re-orient face-up" pass here (recalc_face_normals + flip the whole mesh when
#     its average normal pointed down). It was WORSE than useless: Blender's recalc_face_normals guesses
#     an "outward" side by mesh shape, and on a big/steep track (the San Diego loop climbs a 100 m+
#     Cowles foothill, with 300 m+ terrain in the margin) it guesses WRONG and flips the ENTIRE track
#     face-down — which renders fine but gives AC's one-sided physics no top surface, so the car drops
#     straight through the ground. Sand Creek's gentle terrain happened to dodge it; the loop did not.
#     A per-face variant was no better: reversing faces on the smooth-shaded terrain unshared its
#     vertex normals, blowing 1GRASS past the 65,535-vertex cap so the exporter split it into two
#     duplicate-named meshes (AC then drops one from collision — the very fall-through we started with).
#     The source winding is authoritative; leave it alone. (PRODRIVE_ORIENT=legacy restores the old
#     heuristic for A/B diagnosis only.)
import bmesh  # noqa: E402
DRIVABLE = ("1ROAD", "1RUNOFF", "1GRASS", "1LAWN", "1KERB", "MARKINGS")
if os.environ.get("PRODRIVE_ORIENT") == "legacy":
    for ob in bpy.data.objects:
        if ob.type == "MESH" and any(ob.name.upper().startswith(p) for p in DRIVABLE):
            me = ob.data
            bm = bmesh.new(); bm.from_mesh(me)
            bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:]); bm.normal_update()
            if sum(f.normal.z for f in bm.faces) / max(len(bm.faces), 1) < 0.0:
                bmesh.ops.reverse_faces(bm, faces=bm.faces[:])
            bm.to_mesh(me); bm.free(); me.update()
            print(f"[legacy-orient] {ob.name}")

# 5.5 ensure every mesh has a UV map — the add-on calls calc_tangents(), which hard-errors without one
for ob in bpy.data.objects:
    if ob.type == "MESH" and not ob.data.uv_layers:
        ob.data.uv_layers.new(name="UVMap")
        print("added UV map to", ob.name)

# 6. select everything and run the add-on's kn5 exporter (auto-detect the operator)
for ob in bpy.data.objects:
    ob.select_set(True)
try:
    kn5.unlink()
except OSError:
    pass


def _find_export_op():
    if hasattr(bpy.ops, "exporter") and hasattr(bpy.ops.exporter, "kn5"):
        return "exporter.kn5", bpy.ops.exporter.kn5
    for modname in dir(bpy.ops):
        mod = getattr(bpy.ops, modname)
        for opname in dir(mod):
            if "kn5" in opname.lower() or "assetto" in opname.lower():
                return f"{modname}.{opname}", getattr(mod, opname)
    return None, None


op_name, op = _find_export_op()
print("kn5 export operator:", op_name)
if op is not None:
    try:
        res = op("EXEC_DEFAULT", filepath=str(kn5))
        print("EXPORT RESULT:", res)
    except Exception as e:
        import traceback
        print("EXPORT EXCEPTION:", e)
        traceback.print_exc()
else:
    print("EXPORT EXCEPTION: no kn5 export operator registered (add-on failed to enable)")
print("KN5 EXISTS:", kn5.exists(), "SIZE:", kn5.stat().st_size if kn5.exists() else 0)
