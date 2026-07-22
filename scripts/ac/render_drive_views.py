"""Render DRIVER'S-EYE views of the built track OBJs — look at the world like the driver does.

Every gate this project has measures vertex statistics; every regression Kevin caught, he caught by
LOOKING. This closes that gap headlessly: import track.obj + environment.obj, put a camera 1.4 m over
the deck at N stations along the finished centerline, aim it down the road, render PNGs.

Run:  blender --background --python scripts/ac/render_drive_views.py -- <project-dir> <out-dir> [n_views]
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path


def main() -> None:
    import bpy

    argv = sys.argv[sys.argv.index("--") + 1:]
    proj = Path(argv[0]).resolve()
    out = Path(argv[1]).resolve()
    n_views = int(argv[2]) if len(argv) > 2 else 10
    out.mkdir(parents=True, exist_ok=True)
    data = proj / "data"

    bpy.ops.wm.read_factory_settings(use_empty=True)
    for p in (data / "track.obj", data / "environment.obj"):
        if p.exists():
            bpy.ops.wm.obj_import(filepath=str(p), up_axis="Y", forward_axis="NEGATIVE_Z")

    # flat viewport-style shading: solid colors per name prefix so layers are readable
    PAL = {"1ROAD": (0.25, 0.25, 0.28), "1GRASS": (0.30, 0.45, 0.22), "1WALL": (0.75, 0.73, 0.70),
           "MARKINGS": (0.9, 0.9, 0.9), "YLINE": (0.85, 0.7, 0.1), "CONIFER": (0.10, 0.32, 0.12),
           "FENCEWOOD": (0.55, 0.42, 0.28), "LIGHTPOST": (0.2, 0.2, 0.22), "LIGHTS": (0.95, 0.85, 0.4),
           "HWYSTRUCT": (0.6, 0.6, 0.62), "HIGHWAY": (0.35, 0.35, 0.38), "WATER": (0.2, 0.4, 0.6),
           "EUROSIGN": (0.8, 0.1, 0.1), "GANTRY": (0.5, 0.5, 0.55), "SIGNPOST": (0.5, 0.5, 0.5)}
    for ob in bpy.data.objects:
        if ob.type != "MESH":
            continue
        col = next((c for k, c in PAL.items() if ob.name.upper().startswith(k)), (0.5, 0.4, 0.35))
        mat = bpy.data.materials.new(ob.name[:20] + "_flat")
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        bsdf.inputs["Base Color"].default_value = (*col, 1.0)
        bsdf.inputs["Roughness"].default_value = 0.9
        ob.data.materials.clear()
        ob.data.materials.append(mat)

    # sun + sky so depth reads
    sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun", "SUN"))
    sun.data.energy = 4.0
    sun.rotation_euler = (math.radians(50), 0, math.radians(30))
    bpy.context.scene.collection.objects.link(sun)
    world = bpy.data.worlds.new("w")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.55, 0.65, 0.8, 1)
    world.node_tree.nodes["Background"].inputs[1].default_value = 1.0
    bpy.context.scene.world = world

    # camera path: the finished centerline (mesh frame == OBJ frame; import maps (x,y,z)->(x,-z,y))
    fc = json.loads((data / "finished_centerline.json").read_text())
    pts = fc["points_xyz_m"] if isinstance(fc, dict) else fc
    cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
    cam.data.lens = 24
    bpy.context.scene.collection.objects.link(cam)
    bpy.context.scene.camera = cam
    sc = bpy.context.scene
    sc.render.engine = "BLENDER_EEVEE_NEXT" if hasattr(bpy.types, "SceneEEVEE") else "BLENDER_EEVEE"
    sc.render.resolution_x, sc.render.resolution_y = 1280, 720

    n = len(pts)
    for k in range(n_views):
        i = int(k * n / n_views)
        j = (i + 8) % n
        x, y, z = pts[i]
        tx, ty, tz = pts[j][0] - x, pts[j][1] - y, pts[j][2] - z
        # OBJ frame -> Blender frame: (x, -z, y)
        cx, cy, cz = x, -z, y + 1.4
        lx, ly, lz = x + tx, -(z + tz), y + ty + 1.2
        cam.location = (cx, cy, cz)
        d = (lx - cx, ly - cy, lz - cz)
        L = math.sqrt(sum(v * v for v in d)) or 1.0
        # aim -Z of camera along d
        import mathutils
        cam.rotation_euler = mathutils.Vector(d).to_track_quat("-Z", "Y").to_euler()
        sc.render.filepath = str(out / f"view_{k:02d}_st{int(i)}.png")
        bpy.ops.render.render(write_still=True)
        print(f"[render] {sc.render.filepath}")


main()
