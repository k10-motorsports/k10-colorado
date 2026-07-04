"""Render a flythrough of the dressed track — a chase camera flying the racing line (Blender/Eevee).

Run from Blender:
    blender --background --python scripts/ac/flythrough.py -- <project-dir> [seconds] [height_m]

Writes <project-dir>/build/flythrough.mp4. Imports track.obj + environment.obj, lights it like the
still preview, then drives a camera along centerline.local.json (one keyframe per frame, so there's
no interpolation overshoot) looking a little ahead down the road.
"""

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so Blender's python finds pbr.py (same dir)
import pbr  # noqa: E402  (must follow the sys.path tweak)


def _args():
    a = sys.argv
    rest = a[a.index("--") + 1:] if "--" in a else []
    pd = Path(rest[0]) if rest else None
    seconds = float(rest[1]) if len(rest) > 1 else 16.0
    height = float(rest[2]) if len(rest) > 2 else 12.0
    return pd, seconds, height


def _look_rot(eye, target):
    """Euler for a camera at ``eye`` looking at ``target``, world-up locked to +Z (never rolls).
    Built explicitly because ``Vector.to_track_quat(...).to_euler()`` flips the camera upside-down at
    some headings when run headless."""
    import mathutils
    fwd = (target - eye)
    fwd = fwd.normalized() if fwd.length > 1e-6 else mathutils.Vector((0, -1, 0))
    up = mathutils.Vector((0, 0, 1))
    right = fwd.cross(up)
    right = right.normalized() if right.length > 1e-6 else mathutils.Vector((1, 0, 0))
    tup = right.cross(fwd).normalized()
    return mathutils.Matrix((right, tup, -fwd)).transposed().to_euler()  # cols X=right Y=up Z=-fwd


def _assign_material(obj):
    import bpy
    pbr.setup_material(bpy, obj)


def main():
    import bpy
    import mathutils
    pd, seconds, height = _args()
    fps = 30
    frames = max(2, int(seconds * fps))
    look_ahead, look_back, look_up = 22.0, 12.0, 0.0  # metres — aim at road level, fairly close

    bpy.ops.wm.read_factory_settings(use_empty=True)
    for obj_file in ("track.obj", "environment.obj"):
        p = pd / "data" / obj_file
        if p.exists():
            bpy.ops.wm.obj_import(filepath=str(p), up_axis="Y", forward_axis="NEGATIVE_Z")
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    for o in meshes:  # recalc normals outward so faces are lit
        bpy.context.view_layer.objects.active = o
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.normals_make_consistent(inside=False)
        bpy.ops.object.mode_set(mode="OBJECT")
    for o in meshes:
        _assign_material(o)

    # --- racing line (OBJ coords x, y-up, z) + horizontal arc-length param ---
    pts = json.loads((pd / "data" / "centerline.local.json").read_text())["points_xyz_m"]
    arc = [0.0]
    for i in range(1, len(pts)):
        arc.append(arc[-1] + math.hypot(pts[i][0] - pts[i - 1][0], pts[i][2] - pts[i - 1][2]))
    total = arc[-1]

    def sample(s):
        s %= total
        lo, hi = 0, len(arc) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if arc[mid] < s:
                lo = mid + 1
            else:
                hi = mid
        i = max(1, lo)
        t = (s - arc[i - 1]) / max(1e-6, arc[i] - arc[i - 1])
        a, b = pts[i - 1], pts[i]
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)

    def to_bl(p):  # OBJ (x, y-up, z-north) -> Blender (x, -z, y); up_axis='Y'+forward='-Z' sends +Z(north)->-Y
        return mathutils.Vector((p[0], -p[2], p[1]))

    cam_data = bpy.data.cameras.new("Cam")
    cam_data.lens = 28
    cam_data.clip_end = max(total, 4000)
    cam = bpy.data.objects.new("Cam", cam_data)
    bpy.context.scene.collection.objects.link(cam)
    bpy.context.scene.camera = cam

    for f in range(frames):
        s = (f / frames) * total
        cam_pos = to_bl(sample(s - look_back)) + mathutils.Vector((0, 0, height))
        target = to_bl(sample(s + look_ahead)) + mathutils.Vector((0, 0, look_up))
        cam.location = cam_pos
        cam.rotation_euler = _look_rot(cam_pos, target)
        cam.keyframe_insert("location", frame=f + 1)
        cam.keyframe_insert("rotation_euler", frame=f + 1)

    # --- light + sky (match the still preview) ---
    sun_data = bpy.data.lights.new("Sun", "SUN")
    sun_data.energy = 2.2
    sun = bpy.data.objects.new("Sun", sun_data)
    bpy.context.scene.collection.objects.link(sun)
    sun.rotation_euler = (math.radians(52), math.radians(18), math.radians(35))

    world = bpy.data.worlds.new("Sky")
    world.use_nodes = True
    bpy.context.scene.world = world
    sky = world.node_tree.nodes.new("ShaderNodeTexSky")
    try:
        sky.sky_type = "NISHITA"
    except Exception:
        pass
    bg = world.node_tree.nodes.get("Background")
    world.node_tree.links.new(sky.outputs[0], bg.inputs[0])
    bg.inputs[1].default_value = 0.45

    sc = bpy.context.scene
    engines = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
    sc.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in engines else "BLENDER_EEVEE"
    try:
        sc.eevee.taa_render_samples = 16
    except Exception:
        pass
    sc.view_settings.view_transform = "Standard"
    sc.view_settings.exposure = -0.7
    sc.render.resolution_x = 1280
    sc.render.resolution_y = 720
    sc.frame_start, sc.frame_end = 1, frames
    sc.render.fps = fps
    # This Blender build ships without FFmpeg, so render a PNG sequence and let ffmpeg encode it after.
    sc.render.image_settings.file_format = "PNG"
    frames_dir = pd / "build" / "_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("f_*.png"):
        old.unlink()
    sc.render.filepath = str(frames_dir / "f_")
    bpy.ops.render.render(animation=True)
    print("FLYTHROUGH_FRAMES", frames_dir, frames, fps)


main()
