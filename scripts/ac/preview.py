"""Render a presentation preview of the dressed track in Blender (Eevee) — camera, sun, sky, materials.

Run from Blender:  blender --background --python scripts/ac/preview.py -- <project-dir>
Writes <project-dir>/build/preview.png. Imports track.obj + environment.obj, assigns principled
materials by object name, frames the whole circuit, lights it with a sun + Nishita sky.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so Blender's python finds pbr.py (same dir)
import pbr  # noqa: E402  (must follow the sys.path tweak)


def _project_dir():
    a = sys.argv
    return Path(a[a.index("--") + 1]) if "--" in a and a.index("--") + 1 < len(a) else None


def _assign_material(obj):
    import bpy
    pbr.setup_material(bpy, obj)


def main():
    import bpy
    import mathutils
    pd = _project_dir()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    for obj_file in ("track.obj", "environment.obj"):
        p = pd / "data" / obj_file
        if p.exists():
            bpy.ops.wm.obj_import(filepath=str(p), up_axis="Y", forward_axis="NEGATIVE_Z")
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    for o in meshes:  # recalc normals outward so top-down faces are lit
        bpy.context.view_layer.objects.active = o
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.normals_make_consistent(inside=False)
        bpy.ops.object.mode_set(mode="OBJECT")
    for o in meshes:
        _assign_material(o)

    # Frame on the circuit + its terrain only — the I-70/I-270 decks sprawl far beyond it.
    frame_objs = [o for o in meshes if any(o.name.upper().startswith(k)
                                           for k in ("GRASS", "1ROAD", "1KERB"))] or meshes
    mn = [1e18] * 3; mx = [-1e18] * 3
    for o in frame_objs:
        for corner in o.bound_box:
            w = o.matrix_world @ mathutils.Vector(corner)
            for i in range(3):
                mn[i] = min(mn[i], w[i]); mx[i] = max(mx[i], w[i])
    cx, cy, cz = ((mn[i] + mx[i]) / 2 for i in range(3))
    span = max(mx[0] - mn[0], mx[1] - mn[1])  # ground plane is X-Y; Z is up after import

    cam_data = bpy.data.cameras.new("Cam")
    cam_data.lens = 36
    cam_data.clip_end = span * 20
    cam = bpy.data.objects.new("Cam", cam_data)
    bpy.context.scene.collection.objects.link(cam)
    cam.location = (cx, cy - span * 0.62, cz + span * 0.82)  # due south of centre, raised (Z up)
    direction = mathutils.Vector((cx, cy, cz)) - mathutils.Vector(cam.location)
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()  # set directly (headless-safe)
    bpy.context.scene.camera = cam

    sun_data = bpy.data.lights.new("Sun", "SUN")
    sun_data.energy = 2.0
    sun = bpy.data.objects.new("Sun", sun_data)
    bpy.context.scene.collection.objects.link(sun)
    sun.rotation_euler = (math.radians(52), math.radians(18), math.radians(35))

    world = bpy.data.worlds.new("Sky")
    world.use_nodes = True
    bpy.context.scene.world = world
    nt = world.node_tree
    sky = nt.nodes.new("ShaderNodeTexSky")
    try:
        sky.sky_type = "NISHITA"
    except Exception:
        pass
    bg = nt.nodes.get("Background")
    nt.links.new(sky.outputs[0], bg.inputs[0])
    bg.inputs[1].default_value = 0.4

    sc = bpy.context.scene
    engines = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
    sc.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in engines else "BLENDER_EEVEE"
    sc.view_settings.view_transform = "Standard"
    sc.view_settings.exposure = -0.6
    sc.render.resolution_x = 1600
    sc.render.resolution_y = 900
    out = pd / "build" / "preview.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    sc.render.filepath = str(out)
    bpy.ops.render.render(write_still=True)
    print("PREVIEW_RENDERED", out)


main()
