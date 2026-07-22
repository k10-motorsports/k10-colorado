"""Direct OBJ -> kn5 writer — no Blender, no addon, no import/weld/export pipeline between the
audited geometry and the shipped binary.

WHY: every "gates green, game broken" cycle this project suffered lived in the gap between
data/track.obj + environment.obj (what the drive test / audits measure) and the kn5 (what the
driver loads). The Blender path crosses that gap through OBJ import axis conversion, remove_doubles,
holes_fill, smooth-shading, transform_apply, material INI/JSON layering and an exporter addon — six
places for the artifact to diverge from the audited geometry. This writer crosses it with struct.pack:
the OBJ vertices ARE the kn5 vertices (modulo the configured true-north yaw, applied here in one
place). kn5_ground_check's fidelity pass should read 0.00 m worst-case by construction.

Format knowledge mirrors scripts/ac/verify_kn5._parse (the battle-tested reader):
  header 'sc6969' + int32 version(5)
  textures:  int32 count { int32 active(1), str name, uint32 size, bytes }
  materials: uint32 count { str name, str shader, u8 alphaBlendMode, u8 alphaTested, int32 depthMode,
                            uint32 nprops { str name, f32 valueA, f32[2] B, f32[3] C, f32[4] D },
                            uint32 nsamples { str name, int32 slot, str texture } }
  nodes (recursive): int32 class, str name, uint32 children, u8 active
    class 1: f32[16] row-major matrix (translation in [12..14])
    class 2: u8 castShadows, u8 visible, u8 transparent, uint32 vc,
             vc * { f32[3] pos, f32[3] normal, f32[2] uv, f32[3] tangent },
             uint32 ic, ic * uint16, uint32 materialID, uint32 layer,
             f32 lodIn, f32 lodOut, f32[4] bounding sphere, u8 isRenderable

Run:  python3 -m scripts.ac.kn5_write <project-dir>
"""

from __future__ import annotations

import json
import math
import struct
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from scripts.ac import pbr  # noqa: E402

VERT_CAP = 65535


# ---------------------------------------------------------------- OBJ reader
def read_obj(path: Path) -> list[dict]:
    """Groups with parallel verts/uvs (our write_obj emits per-vertex vt with matching indices)."""
    groups: list[dict] = []
    cur = None
    verts: list = []
    vts: list = []
    v_base = vt_base = 0
    if not path.exists():
        return groups

    def flush():
        nonlocal v_base, vt_base
        if cur is not None:
            groups.append(cur)

    with open(path) as f:
        for ln in f:
            if ln.startswith("o "):
                flush()
                cur = {"name": ln[2:].strip(), "v0": len(verts), "vt0": len(vts), "tris": []}
            elif ln.startswith("v "):
                p = ln.split()
                verts.append((float(p[1]), float(p[2]), float(p[3])))
            elif ln.startswith("vt "):
                p = ln.split()
                vts.append((float(p[1]), float(p[2])))
            elif ln.startswith("f ") and cur is not None:
                idx = []
                for tok in ln.split()[1:]:
                    a = tok.split("/")
                    idx.append(int(a[0]) - 1)
                for k in range(1, len(idx) - 1):
                    cur["tris"].append((idx[0], idx[k], idx[k + 1]))
    flush()
    out = []
    for g in groups:
        lo = g["v0"]
        hi = max((max(t) for t in g["tris"]), default=lo - 1) + 1
        gv = verts[lo:hi]
        # per-vertex uv: our writer emits vt parallel to v within the group (same count/order)
        guv = vts[g["vt0"]:g["vt0"] + len(gv)] if g["vt0"] + len(gv) <= len(vts) else []
        if len(guv) != len(gv):
            guv = [(v[0] / 8.0, v[2] / 8.0) for v in gv]      # planar fallback
        out.append({"name": g["name"], "verts": gv, "uvs": guv,
                    "tris": [(a - lo, b - lo, c - lo) for a, b, c in g["tris"]]})
    return [g for g in out if g["tris"]]


# ------------------------------------------------------------- mesh building
def smooth_normals(verts, tris) -> list:
    """Area-weighted normals, welded by position (1e-4) so shared edges shade continuously —
    the same visual effect the Blender path got from remove_doubles + smooth shading."""
    key = lambda v: (round(v[0], 4), round(v[1], 4), round(v[2], 4))
    acc: dict = defaultdict(lambda: [0.0, 0.0, 0.0])
    for a, b, c in tris:
        va, vb, vc = verts[a], verts[b], verts[c]
        ux, uy, uz = vb[0] - va[0], vb[1] - va[1], vb[2] - va[2]
        wx, wy, wz = vc[0] - va[0], vc[1] - va[1], vc[2] - va[2]
        n = (uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx)   # area-weighted
        for vi in (a, b, c):
            s = acc[key(verts[vi])]
            s[0] += n[0]; s[1] += n[1]; s[2] += n[2]
    out = []
    for v in verts:
        nx, ny, nz = acc[key(v)]
        L = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        out.append((nx / L, ny / L, nz / L))
    return out


def tangents_for(normals) -> list:
    out = []
    for nx, ny, nz in normals:
        # any unit vector orthogonal to n; +X preference (normal maps tile world-planar here)
        tx, ty, tz = 1.0, 0.0, 0.0
        d = tx * nx
        tx, ty, tz = tx - d * nx, ty - d * ny, tz - d * nz
        L = math.sqrt(tx * tx + ty * ty + tz * tz)
        if L < 1e-6:
            tx, ty, tz, L = 0.0, 0.0, 1.0, 1.0
        out.append((tx / L, ty / L, tz / L))
    return out


# ------------------------------------------------------------------- writer
class W:
    def __init__(self):
        self.b = bytearray(b"sc6969")

    def i32(self, v): self.b += struct.pack("<i", v)
    def u32(self, v): self.b += struct.pack("<I", v)
    def u16(self, v): self.b += struct.pack("<H", v)
    def f32(self, v): self.b += struct.pack("<f", v)
    def u8(self, v): self.b += struct.pack("<B", v)
    def s(self, t):
        e = t.encode("utf-8")
        self.u32(len(e)); self.b += e
    def raw(self, x): self.b += x


def build(project_dir: str | Path) -> Path:
    project_dir = Path(project_dir)
    data = project_dir / "data"
    cfg = json.loads((project_dir / "track.config.json").read_text())
    slug = cfg["slug"]
    yaw = math.radians(float(cfg.get("true_north_rotation_deg") or 0.0))
    c, s_ = math.cos(yaw), math.sin(yaw)

    def rot(x, y, z):     # matches the shipped frame: yaw 180 -> (-x, y, -z)
        return (x * c - z * s_, y, x * s_ + z * c)

    groups = read_obj(data / "track.obj") + read_obj(data / "environment.obj")
    print(f"[kn5_write] {len(groups)} OBJ groups")
    over = pbr.load_overrides(project_dir)
    mats_json = data / "materials.json"
    shader_over = json.loads(mats_json.read_text())["materials"] if mats_json.exists() else {}

    # material per group (same _mat naming as the Blender path so ext_config keeps matching)
    tex_dir = REPO / "assets" / "textures"
    materials: list[dict] = []
    mat_of: dict[str, int] = {}
    needed_tex: dict[str, Path] = {}
    for g in groups:
        name = g["name"]
        mname = f"{name}_mat"
        if mname in mat_of:
            continue
        key = next((k for k in pbr.TEXTURES if name.upper().startswith(k)), None)
        diff, norm = (pbr.TEXTURES[key][0], pbr.TEXTURES[key][1]) if key else (None, None)
        ov = over.get(key or "", {})
        if ov.get("diffuse"):
            diff = Path(ov["diffuse"]).name
            needed_tex[diff] = Path(ov["diffuse"])
        elif diff:
            needed_tex[diff] = tex_dir / diff
        if ov.get("normal"):
            norm = Path(ov["normal"]).name
            needed_tex[norm] = Path(ov["normal"])
        elif norm:
            needed_tex[norm] = tex_dir / norm
        if key in pbr.BILLBOARD:
            shader, atest = "ksTree", True
        elif norm:
            shader, atest = "ksPerPixelMultiMap", False
        else:
            shader, atest = "ksPerPixel", False
        sh_ov = next((v for k, v in shader_over.items() if name.upper().startswith(k.upper())), None)
        if sh_ov:
            shader = sh_ov["shader"]
        ks_key = next((k for k in pbr.KS_PROPS if name.upper().startswith(k)), None)
        props = dict(pbr.KS_PROPS.get(ks_key, {"ksAmbient": 0.4, "ksDiffuse": 0.4,
                                               "ksSpecular": 0.1, "ksSpecularEXP": 8.0}))
        samples = [("txDiffuse", 0, diff)] if diff else []
        if norm and shader == "ksPerPixelMultiMap":
            samples.append(("txNormal", 1, norm))
        mat_of[mname] = len(materials)
        materials.append({"name": mname, "shader": shader, "atest": atest,
                          "props": props, "samples": samples})
    print(f"[kn5_write] {len(materials)} materials, {len(needed_tex)} textures")

    w = W()
    w.i32(5)                                        # version
    texs = {k: v for k, v in needed_tex.items() if v.exists()}
    for k, v in needed_tex.items():
        if not v.exists():
            print(f"[kn5_write] WARNING: texture missing, skipped: {v}")
    w.i32(len(texs))
    for tname, tpath in texs.items():
        blob = tpath.read_bytes()
        w.i32(1); w.s(tname); w.u32(len(blob)); w.raw(blob)
    w.u32(len(materials))
    for m in materials:
        w.s(m["name"]); w.s(m["shader"])
        w.u8(0); w.u8(1 if m["atest"] else 0)       # alphaBlendMode, alphaTested
        w.i32(0)                                    # depthMode
        w.u32(len(m["props"]))
        for pname, pval in m["props"].items():
            w.s(pname); w.f32(float(pval))
            for _ in range(9):                      # valueB[2] + valueC[3] + valueD[4]
                w.f32(0.0)
        w.u32(len(m["samples"]))
        for sname, slot, tname in m["samples"]:
            w.s(sname); w.i32(slot); w.s(tname or "")

    # nodes: root -> [meshes..., dummies...]
    dummies = json.loads((data / "dummies.json").read_text()) if (data / "dummies.json").exists() else {}
    # spawn facing: same derivation as build_kn5 (raw centerline travel direction, mirrored, + yaw)
    start_yaw = 0.0
    clp = data / "centerline.local.json"
    if clp.exists():
        pts = json.loads(clp.read_text()).get("points_xyz_m", [])
        if len(pts) >= 2:
            sx = -1.0 if cfg.get("mirror_x") else 1.0
            tx, tz = sx * (pts[1][0] - pts[0][0]), pts[1][2] - pts[0][2]
            L = math.hypot(tx, tz) or 1.0
            start_yaw = math.atan2(tx / L, tz / L)
    FACING = ("AC_START", "AC_PIT", "AC_HOTLAP")

    w.i32(1); w.s(slug); w.u32(len(groups) + len(dummies)); w.u8(1)
    ident = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]
    for v in ident:
        w.f32(v)
    for g in groups:
        verts, tris = g["verts"], g["tris"]
        if len(verts) > VERT_CAP:
            # weld identical (position, uv) pairs — the generators emit unwelded triangle soup for
            # some visual strips (MARKINGS ~75k) and the Blender path only survived the cap via
            # remove_doubles. Same effect, deterministic, no Blender.
            remap: dict = {}
            keep: list[int] = []
            new_idx = [0] * len(verts)
            for i, v in enumerate(verts):
                u, vv = g["uvs"][i]
                k = (round(v[0], 4), round(v[1], 4), round(v[2], 4), round(u, 3), round(vv, 3))
                j = remap.get(k)
                if j is None:
                    j = remap[k] = len(keep)
                    keep.append(i)
                new_idx[i] = j
            print(f"[kn5_write] welded {g['name']}: {len(verts)} -> {len(keep)} verts")
            verts = [verts[i] for i in keep]
            g["uvs"] = [g["uvs"][i] for i in keep]
            tris = [(new_idx[a], new_idx[b], new_idx[c]) for a, b, c in tris
                    if len({new_idx[a], new_idx[b], new_idx[c]}) == 3]
            g["verts"], g["tris"] = verts, tris
        if len(verts) > VERT_CAP:
            raise SystemExit(f"[kn5_write] FATAL: {g['name']} {len(verts)} verts > {VERT_CAP} — "
                             f"route through split_mesh_under_cap")
        rv = [rot(*v) for v in verts]
        rn = [rot(*n) for n in smooth_normals(verts, tris)]
        rt = tangents_for(rn)
        cx = sum(v[0] for v in rv) / len(rv)
        cy = sum(v[1] for v in rv) / len(rv)
        cz = sum(v[2] for v in rv) / len(rv)
        rad = math.sqrt(max((v[0] - cx) ** 2 + (v[1] - cy) ** 2 + (v[2] - cz) ** 2 for v in rv))
        w.i32(2); w.s(g["name"]); w.u32(0); w.u8(1)
        w.u8(1); w.u8(1); w.u8(0)                   # castShadows, visible, transparent
        w.u32(len(rv))
        uvs = g["uvs"]
        for i in range(len(rv)):
            x, y, z = rv[i]; nx, ny, nz = rn[i]; tx2, ty2, tz2 = rt[i]
            u, vv = uvs[i]
            w.raw(struct.pack("<11f", x, y, z, nx, ny, nz, u, 1.0 - vv, tx2, ty2, tz2))
        w.u32(len(tris) * 3)
        for a, b, c2 in tris:
            w.u16(a); w.u16(b); w.u16(c2)
        w.u32(mat_of[f"{g['name']}_mat"]); w.u32(0)  # material, layer
        w.f32(0.0); w.f32(0.0)                       # lodIn/out
        w.raw(struct.pack("<4f", cx, cy, cz, rad))
        w.u8(1)                                      # renderable
    for name, (x, y, z) in dummies.items():
        px, py, pz = rot(x, y, z)
        phi = (start_yaw + yaw) if any(name.startswith(p) for p in FACING) else 0.0
        cph, sph = math.cos(phi), math.sin(phi)
        m = [cph, 0, -sph, 0,  0, 1, 0, 0,  sph, 0, cph, 0,  px, py, pz, 1]
        w.i32(1); w.s(name); w.u32(0); w.u8(1)
        for v in m:
            w.f32(v)

    out = project_dir / "build" / f"{slug}.kn5"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(w.b))
    print(f"[kn5_write] wrote {out}  ({out.stat().st_size/1e6:.1f} MB, "
          f"{len(groups)} meshes, {len(dummies)} dummies)")
    return out


if __name__ == "__main__":
    build(sys.argv[1])
