"""Headless Blender entry point — build the mesh and export the kn5.

Run from Blender (bpy is provided by the Blender runtime, not pip):

    blender --background --python scripts/ac/build_kn5.py -- <track-dir>

Reads <track-dir>/track.config.json + data/, builds the scene (1ROAD ribbon, GRASS heightfield,
dummies), assigns materials via the AC Blender Tools addon, and exports build/<slug>.kn5.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _track_dir_from_argv(argv: list[str]) -> Path:
    """Blender passes script args after a literal ``--``.

    Resolve to an ABSOLUTE path: Blender's ``bpy.data.images.load`` resolves a relative path against its
    own base (not the python CWD), turning a relative override like ``projects/<slug>/source/textures/x.jpg``
    into ``/projects/...`` (filesystem root) — the image loads empty and the kn5 exporter then crashes
    writing a 0-byte file. Making track_dir absolute keeps every derived path (data/, overrides, build/)
    valid regardless of Blender's working directory. Stock textures already use an absolute texture_dir()."""
    if "--" not in argv:
        raise SystemExit("usage: blender --background --python build_kn5.py -- <track-dir>")
    rest = argv[argv.index("--") + 1:]
    if not rest:
        raise SystemExit("missing <track-dir> after '--'")
    return Path(rest[0]).resolve()


def build(track_dir: Path) -> Path:
    """Build the Blender scene from the project data and export the kn5. Returns the kn5 path."""
    import json

    import bpy  # only available inside Blender

    data = track_dir / "data"
    slug = json.loads((track_dir / "track.config.json").read_text())["slug"]
    out_kn5 = track_dir / "build" / f"{slug}.kn5"
    out_kn5.parent.mkdir(parents=True, exist_ok=True)
    (track_dir / "blender").mkdir(parents=True, exist_ok=True)

    # 1. empty scene
    bpy.ops.wm.read_factory_settings(use_empty=True)

    # 2. import ALL pre-built meshes (Y-up): the network track+environment PLUS any merged detailed
    #    tracks (track_<slug>.obj / env_<slug>.obj, e.g. the full-detail Sand Creek + IMI built in the
    #    same shared frame). They all combine into one kn5. Group names are pre-uniquified per track
    #    (1ROAD_<slug>_*) so no duplicate-name collision drops a mesh from collision.
    obj_files = sorted(set(list(data.glob("track*.obj")) + list(data.glob("environment*.obj"))
                           + list(data.glob("env_*.obj"))), key=lambda p: p.name)
    for p in obj_files:
        bpy.ops.wm.obj_import(filepath=str(p), up_axis="Y", forward_axis="NEGATIVE_Z")
    print(f"[build_kn5] imported {len(obj_files)} OBJ(s): {[p.name for p in obj_files]}")

    # 2b. Weld coincident vertices. The geometry generators emit UNWELDED triangles (3 verts each),
    #     so 1GRASS lands at ~283k verts — over AC/ksEditor's hard 65,535-per-mesh limit (16-bit
    #     indices). Merging by a sub-mm distance collapses the terrain grid back to its ~48k shared
    #     grid points (and shrinks every mesh) without changing the surface, then warns if anything
    #     is somehow still over the limit.
    import bmesh
    for ob in [o for o in bpy.data.objects if o.type == "MESH"]:
        bm = bmesh.new()
        bm.from_mesh(ob.data)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-4)
        # Terrain must be WATERTIGHT or the car falls through. The source grass IS watertight, but
        # Blender's 1e-4 weld doesn't fully re-merge the sparse terrain grid (tears ~500 holes near the
        # racing line). The grid is ~4 m, so a 5 cm weld is safe and merges the stragglers; then fill
        # any residual small holes (never the huge outer boundary).
        if ob.name.upper().startswith("1GRASS"):
            before = sum(1 for e in bm.edges if len(e.link_faces) == 1)
            bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.05)
            try:
                bmesh.ops.holes_fill(bm, edges=bm.edges, sides=16)
            except Exception as e:
                print(f"[build_kn5] holes_fill skipped on {ob.name}: {e}")
            # Do NOT recalc/flip normals here. build_mesh authors the grass 100% face-up already, and
            # the OBJ import + kn5 export both preserve winding — so the ONLY thing that ever corrupts
            # it is a normal-recalc pass. recalc_face_normals guesses "outward" from mesh SHAPE and on
            # big/steep terrain guesses DOWN, flipping the whole grass face-down -> AC has no top-side
            # collision -> the car falls through the ground (this exact bug hit the San Diego loop with
            # the old code, then Sand Creek's pits when the compensating flip was removed). A per-face
            # geometric flip is no better: on spiky terrain it unshares smooth normals and splits 1GRASS
            # past the 65,535 cap into duplicate meshes. Weld + fill holes, then leave the winding alone.
            after = sum(1 for e in bm.edges if len(e.link_faces) == 1)
            print(f"[build_kn5] 1GRASS open edges {before} -> {after} (weld 5 cm + holes_fill)")
        bm.to_mesh(ob.data)
        bm.free()
        ob.data.update()
        # ksEditor/AC count RENDER vertices (unique position+normal+uv), NOT welded positions. A
        # FLAT-shaded mesh keeps 3 verts per triangle on export, so 1GRASS exported at ~246k even
        # after welding to ~48k positions. Smooth-shading shares normals across the continuous
        # terrain UV, collapsing render verts back to the position count. Only smooth meshes big
        # enough to risk the 65,535 limit (len(loops) is the flat-shaded upper bound) — buildings
        # etc. stay flat so their hard edges don't round off.
        if len(ob.data.loops) > 65535:
            for poly in ob.data.polygons:
                poly.use_smooth = True
            ob.data.update()
            print(f"[build_kn5] smooth-shaded '{ob.name}' ({len(ob.data.vertices)} positions) to fit "
                  f"the 65,535 render-vertex limit")
        if len(ob.data.vertices) > 65535:
            print(f"[build_kn5] WARNING: '{ob.name}' has {len(ob.data.vertices)} positions (> 65535) "
                  f"— needs splitting or decimation before re-export.")

    # 3. AC dummy empties from dummies.json (AC Y-up metres). The meshes import Y-up -> Blender Z-up,
    #    so place each empty in that SAME Blender frame (x, -z, y) or it lands swapped vs the road.
    import math

    import mathutils

    # Spawn/pit FACING: the empty's forward is Blender -Y, and the mesh import puts the centerline
    # travel direction (centreline pts[0]->pts[1]) along Blender -Y for a due-north start. So an
    # un-rotated empty already faces the travel direction; the yaw that aligns local -Y with the
    # Blender travel vector (tx, -tz) is atan2(tx, tz) (= 0 for north).
    # mirror_x: the dummy POSITIONS already come mirrored via dummies.json (build_mesh placed them on the
    # mirrored centerline). The facing here reads the raw centerline, so mirror its east axis to match.
    cfg_json = json.loads((track_dir / "track.config.json").read_text())
    mirror_x = bool(cfg_json.get("mirror_x", False))
    sx = -1.0 if mirror_x else 1.0
    # SUN FIX yaw (deg, about vertical): re-aim the model so AC's world-fixed sun lands correctly
    # (mirror_x tracks are spun 180deg vs the sun -> sunset went EAST; AC has no sun/north key, so we
    # yaw the model). Applied to BOTH the meshes AND the dummy positions so spawns stay on the road.
    # yc/ys map a world (x,z) to the yawed Blender (x,-z) frame consistently with the mesh rotation R.
    # See scripts/lighting/csp_config.resolve_true_north.
    yaw_deg = float(cfg_json.get("true_north_rotation_deg", 0.0))
    yr = math.radians(yaw_deg); yc, ys = math.cos(yr), math.sin(yr)
    start_yaw = 0.0
    cl_path = data / "centerline.local.json"
    if cl_path.exists():
        pts = json.loads(cl_path.read_text()).get("points_xyz_m", [])
        if len(pts) >= 2:
            tx, tz = sx * (pts[1][0] - pts[0][0]), pts[1][2] - pts[0][2]
            L = math.hypot(tx, tz) or 1.0
            start_yaw = math.atan2(tx / L, tz / L)

    FACING = ("AC_START", "AC_PIT", "AC_HOTLAP")
    dummies = json.loads((data / "dummies.json").read_text()) if (data / "dummies.json").exists() else {}
    for name, (x, y, z) in dummies.items():
        empty = bpy.data.objects.new(name, None)
        empty.empty_display_type = "ARROWS"
        empty.empty_display_size = 3.0
        # Blender frame (x, -z, y), PRE-rotated by the yaw as a clean location (not a matrix_world
        # rotation, which the kn5 exporter mis-baked -> spawns 500 m off the road).
        empty.location = (x * yc + z * ys, x * ys - z * yc, y)
        if any(name.startswith(p) for p in FACING):
            empty.rotation_euler = (0.0, 0.0, start_yaw + yr)
        bpy.context.scene.collection.objects.link(empty)

    # 3b. SUN FIX — yaw the MESHES by the same angle (dummies pre-yawed above). A proper rotation, NOT a
    #     mirror: geometry stays correct-handed, only orientation changes, so the sun sets in the true
    #     west. Minimap rotated to match in track_folder.py.
    if abs(yaw_deg) > 1e-6:
        R = mathutils.Matrix.Rotation(yr, 4, "Z")   # Blender Z = world up here
        bpy.ops.object.select_all(action="DESELECT")
        meshes = [o for o in bpy.data.objects if o.type == "MESH"]
        for o in meshes:
            o.matrix_world = R @ o.matrix_world
            o.select_set(True)
        if meshes:
            bpy.context.view_layer.objects.active = meshes[0]
            bpy.ops.object.transform_apply(location=True, rotation=True, scale=False)
        print(f"[build_kn5] applied sun-fix yaw {yaw_deg:.0f}deg to {len(meshes)} meshes (+ pre-yawed dummies)")

    # 4. textured PBR material per object (pbr.py: tiling diffuse + normal on the generated UVs) +
    #    stamp the AC shader name from materials.json — the FBX carries the textures (path_mode=COPY)
    #    AND the kn5 gets the right shader.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import pbr
    overrides = pbr.load_overrides(track_dir)   # real captured textures (scripts/capture/textures.py)
    if overrides:
        print(f"[build_kn5] real-world texture overrides: {sorted(overrides)}")
    mat_file = data / "materials.json"
    mats = json.loads(mat_file.read_text())["materials"] if mat_file.exists() else {}
    for ob in [o for o in bpy.data.objects if o.type == "MESH"]:
        mat = pbr.setup_material(bpy, ob, overrides=overrides)
        prefix = next((p for p in mats if ob.name.upper().startswith(p.upper())), None)
        if prefix:
            mat["shaderName"] = mats[prefix]["shader"]  # read by AC Blender Tools

    # Pack the textures INTO the .blend so it's self-contained and portable to another machine
    # (the image nodes point at this repo's assets/textures/, which won't exist on a Windows box).
    # A packed .blend opens with all materials/textures intact -> open it, File > Export > kn5.
    try:
        bpy.ops.file.pack_all()
    except Exception as e:
        print(f"[build_kn5] WARNING: could not pack textures into the .blend: {e}")
    bpy.ops.wm.save_as_mainfile(filepath=str(track_dir / "blender" / f"{slug}.blend"))

    # 5. export kn5. Auto-detect whichever export operator the installed AC add-on registered
    #    (AC Tools, moppius/blender-assetto-corsa-tools, io_import_accsv, …) — no hardcoded name.
    found = []
    for modname in dir(bpy.ops):
        mod = getattr(bpy.ops, modname)
        for opname in dir(mod):
            if "kn5" in opname.lower() or "assetto" in opname.lower():
                found.append((f"{modname}.{opname}", getattr(mod, opname)))
    found.sort(key=lambda x: ("export" not in x[0].lower(), x[0]))  # prefer export ops
    if found:
        op_name, op = found[0]
        print(f"[build_kn5] kn5 export operator: {op_name}")
        try:
            op(filepath=str(out_kn5))
        except TypeError:
            op()  # some exporters read the path from a panel/scene setting instead
        return out_kn5

    # No direct kn5 exporter on this Blender (AC Tools exports FBX; direct-kn5 add-ons need Blender 3.x).
    # Fall back to an AC-standard FBX — convert to kn5 with Content Manager (import) or ksEditor.
    out_fbx = track_dir / "build" / f"{slug}.fbx"
    bpy.ops.export_scene.fbx(filepath=str(out_fbx), axis_forward="-Z", axis_up="Y",
                             apply_unit_scale=True, use_mesh_modifiers=True, path_mode="COPY")
    print(f"[build_kn5] no direct kn5 exporter found — wrote AC-standard FBX: {out_fbx}")
    print("           convert to .kn5 via Content Manager (track import) or ksEditor.")
    return out_fbx


def main() -> None:
    track_dir = _track_dir_from_argv(sys.argv)
    out = build(track_dir)
    print(f"[build_kn5] exported {out}")


if __name__ == "__main__":
    main()
