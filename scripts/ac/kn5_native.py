"""Mac-native kn5 export — no Windows, no FBX round-trip.

Uses the pure-Python Hagnhofer kn5 exporter bundled in leBluem's ``io_import_accsv`` Blender addon.
That addon is GPL, so we don't vendor it: it's auto-cloned into ``vendor/io_import_accsv`` (gitignored)
on first run. This script builds the Blender scene from the project's ``track.obj`` + ``environment.obj``,
emits the AC dummies as tiny named nodes, generates the AC ``settings.json`` (shader + texture mappings
keyed off pbr.py), and writes ``build/<slug>.kn5``.

Run inside Blender:
    blender --background --python scripts/ac/kn5_native.py -- <project-dir>

Validate the result in Assetto Corsa / Content Manager (it can't be tested on the Mac). Known follow-ups:
AC_START/AC_PIT orientation (exported as positions only — facing may need a tweak in-game), and shader
choices are sensible defaults (tune per material in CM if desired).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

EXPORTER_REPO = "https://github.com/leBluem/io_import_accsv"


def _ensure_exporter(repo_root: Path) -> Path:
    vendor = repo_root / "vendor" / "io_import_accsv"
    if not (vendor / "export_kn5.py").exists():
        vendor.parent.mkdir(parents=True, exist_ok=True)
        print(f"[kn5_native] fetching GPL exporter -> {vendor}")
        subprocess.run(["git", "clone", "--depth", "1", EXPORTER_REPO, str(vendor)], check=True)
    return vendor


def _shader_and_textures(pbr, obj_name: str):
    """AC shader name + texture-slot mapping for an object, from pbr's prefix table."""
    key = next((k for k in pbr.TEXTURES if obj_name.upper().startswith(k)), None)
    if key is None:
        return "ksPerPixel", {}
    diff, norm = pbr.TEXTURES[key][0], pbr.TEXTURES[key][1]
    tx = {}
    if diff:
        tx["txDiffuse"] = {"textureName": diff}
    if norm:
        tx["txNormal"] = {"textureName": norm}
    if key in pbr.BILLBOARD and diff:   # TREES/PALMS/BUSHES — crossed cutout cards use the tree shader
        return "ksTree", tx
    return ("ksPerPixelMultiMap" if norm else "ksPerPixel"), tx


def main() -> None:
    import bpy
    argv = sys.argv
    project_dir = Path(argv[argv.index("--") + 1])
    repo = project_dir.parents[1]
    vendor = _ensure_exporter(repo)
    sys.path.insert(0, str(vendor))
    sys.path.insert(0, str(vendor / "kn5"))   # the kn5 modules use sibling imports
    sys.path.insert(0, str(repo / "scripts" / "ac"))
    import pbr

    data = project_dir / "data"
    slug = json.loads((project_dir / "track.config.json").read_text())["slug"]
    out = project_dir / "build"
    out.mkdir(parents=True, exist_ok=True)
    kn5_path = out / f"{slug}.kn5"

    bpy.ops.wm.read_factory_settings(use_empty=True)
    for obj_file in ("track.obj", "environment.obj"):
        p = data / obj_file
        if p.exists():
            bpy.ops.wm.obj_import(filepath=str(p), up_axis="Y", forward_axis="NEGATIVE_Z")

    # AC dummies (AC_START/AC_PIT/AC_TIME/AC_HOTLAP) as tiny named nodes: the exporter only writes
    # meshes, not empties, and AC reads the node position from its geometry, so a 5 cm tri works.
    #
    # The position MUST live in the vertices, not in ob.location: the exporter bakes geometry with
    # `obj.matrix_world @ co`, but in `--background` the depsgraph isn't evaluated after we set
    # ob.location, so matrix_world stays identity and every marker collapsed to the world origin
    # (car spawned at the track centroid, metres underground -> fell through, no ground visible).
    # Bake the world position into the tri directly, like the road ribbon does.
    #
    # dummies.json is AC Y-up (x, y=up, z). The exporter applies convert_vector3 (x,y,z)->(x,z,-y)
    # to every vertex, so pre-rotate into Blender's frame with the inverse (x,-z,y) and leave the
    # object at the origin — the road reaches AC unchanged the same way (Y-up OBJ import + that one
    # convert == identity), so this lands the marker exactly on the road.
    dummies = json.loads((data / "dummies.json").read_text()) if (data / "dummies.json").exists() else {}
    for name, (x, y, z) in dummies.items():
        bx, by, bz = x, -z, y  # inverse of the exporter's convert_vector3, so it lands at AC (x,y,z)
        m = bpy.data.meshes.new(name)
        m.from_pydata([(bx, by, bz), (bx + 0.05, by, bz), (bx, by + 0.05, bz)], [], [(0, 1, 2)])
        ob = bpy.data.objects.new(name, m)
        bpy.context.scene.collection.objects.link(ob)

    # materials (pbr image nodes -> textures embed) + AC settings.json (shaders + texture slot mappings)
    settings = {"materials": {}}
    for ob in [o for o in bpy.data.objects if o.type == "MESH"]:
        mat = pbr.setup_material(bpy, ob)
        shader, tx = _shader_and_textures(pbr, ob.name)
        is_billboard = any(ob.name.upper().startswith(k) for k in pbr.BILLBOARD)
        settings["materials"][mat.name] = {
            "shaderName": shader,
            "alphaBlendMode": "AlphaToCoverage" if is_billboard else "Opaque",
            "alphaTested": is_billboard,
            "depthMode": "DepthNormal",
            "properties": {},
            "textures": tx,
        }
    (out / "settings.json").write_text(json.dumps(settings, indent=1))

    # Stage referenced textures into build/texture/ — the kn5 export add-on resolves each material's
    # textureName to <kn5_dir>/texture/<name> and embeds it from there. Without this it embeds 0
    # textures and every surface renders flat (the road/grass look untextured). Copy each unique
    # texture named in settings.json from assets/textures/ into the staging dir.
    import shutil
    texdir = out / "texture"
    texdir.mkdir(exist_ok=True)
    staged = 0
    for mat_def in settings["materials"].values():
        for slot in mat_def.get("textures", {}).values():
            fn = slot.get("textureName")
            if not fn:
                continue
            src = pbr.texture_dir() / fn
            if src.exists() and not (texdir / fn).exists():
                shutil.copyfile(src, texdir / fn)
                staged += 1
            elif not src.exists():
                print(f"[kn5_native] WARNING: texture missing, not staged: {src}")
    print(f"[kn5_native] staged {staged} textures into {texdir}")

    # Save the assembled scene as blender/<slug>.blend so the PROVEN 2-pass exporter
    # (scripts/ac/export_kn5_addon.py — the path that builds the working Sand Creek) can re-export it
    # with the add-on operator (recalc_face_normals + face-up orient + proper physics). kn5_native's own
    # direct export_kn5.save() below is a fallback; the pipeline prefers export_kn5_addon on this .blend.
    blend_path = project_dir / "blender" / f"{slug}.blend"
    blend_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        bpy.ops.file.pack_all()   # PACK images into the .blend — the blender-assetto-corsa-tools kn5
        #                           exporter (used by export_kn5_addon, the working path) hard-errors
        #                           "Image not packed" otherwise.
    except Exception as e:
        print(f"[kn5_native] WARNING: pack_all failed: {e}")
    try:
        bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
        print(f"[kn5_native] saved packed scene -> {blend_path}")
    except Exception as e:
        print(f"[kn5_native] WARNING: could not save .blend: {e}")

    import export_kn5
    for ob in bpy.data.objects:
        ob.select_set(ob.type == "MESH")
    try:
        kn5_path.unlink()
    except OSError:
        pass
    export_kn5.save(bpy.context, str(kn5_path), 1.0)

    ok = kn5_path.exists() and kn5_path.stat().st_size > 64
    if ok:
        with open(kn5_path, "rb") as f:
            header = f.read(6)
        print(f"[kn5_native] {'OK' if header == b'sc6969' else 'BAD HEADER'} — "
              f"{kn5_path} ({kn5_path.stat().st_size} bytes, header {header!r})")
    else:
        print("[kn5_native] FAILED — no kn5 written")


main()
