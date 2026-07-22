"""Instance real 3D prop modules (Kevin's model pack) along the track — starting with the 4 m
concrete barrier replacing the procedural swept jersey at warning-barrier runs.

The module OBJ is loaded once (pure stdlib) and stamped along each run: position at the verge
offset, yawed to the local tangent, base seated just under the deck edge. Output is a normal
mesh dict (verts/uvs/tris) that build_mesh ships as physical ``1WALL_*`` (collidable — AC treats
1-prefixed meshes as physics surfaces/walls) and splits under the kn5 vertex cap like everything
else. The module's mesh ships in the kn5 as GEOMETRY we instanced ourselves (renders baked from
licensed models are imagery; these OBJs are Kevin-provided assets for his own track).
"""

from __future__ import annotations

import math
from pathlib import Path

Vertex = tuple[float, float, float]


def load_module(path: str | Path) -> dict:
    """Minimal OBJ loader: v/vt/f (triangulates fans). Returns {vertices, uvs, tris} with uvs
    parallel to vertices (last-seen vt per vertex — fine for these unwrapped props)."""
    vs: list[Vertex] = []
    vts: list[tuple[float, float]] = []
    uvs_per_v: dict[int, tuple[float, float]] = {}
    tris: list[tuple[int, int, int]] = []
    for ln in open(path, errors="replace"):
        if ln.startswith("v "):
            p = ln.split()
            vs.append((float(p[1]), float(p[2]), float(p[3])))
        elif ln.startswith("vt "):
            p = ln.split()
            vts.append((float(p[1]), float(p[2])))
        elif ln.startswith("f "):
            idx = []
            for tok in ln.split()[1:]:
                parts = tok.split("/")
                vi = int(parts[0]) - 1
                if len(parts) > 1 and parts[1]:
                    uvs_per_v[vi] = vts[int(parts[1]) - 1]
                idx.append(vi)
            for k in range(1, len(idx) - 1):
                tris.append((idx[0], idx[k], idx[k + 1]))
    uvs = [uvs_per_v.get(i, (0.0, 0.0)) for i in range(len(vs))]
    return {"vertices": vs, "uvs": uvs, "tris": tris}


def instance_barriers(centerline_m: list[Vertex], widths_m: list[float], placements: list[dict],
                      module: dict, *, module_len: float = 4.04, off_extra: float = 1.6,
                      base_sink: float = 0.05) -> dict:
    """Stamp the barrier module end-to-end along each warning-barrier run, on the run's OUTSIDE."""
    pts = centerline_m
    n = len(pts)
    out = {"vertices": [], "uvs": [], "tris": []}
    for pl in placements:
        a0, b0, side = pl["start_idx"], pl.get("end_idx", pl["start_idx"] + 8), pl["side"]
        arc = 0.0
        next_at = 0.0
        for i in range(max(1, a0), min(b0, n - 1)):
            seg = math.hypot(pts[i][0] - pts[i - 1][0], pts[i][2] - pts[i - 1][2])
            arc += seg
            if arc < next_at:
                continue
            next_at += module_len
            x, y, z = pts[i]
            tx, tz = pts[min(i + 1, n - 1)][0] - pts[i - 1][0], pts[min(i + 1, n - 1)][2] - pts[i - 1][2]
            L = math.hypot(tx, tz) or 1e-9
            tx, tz = tx / L, tz / L
            nx, nz = -tz * side, tx * side              # outward on the run's side
            off = widths_m[i] / 2.0 + off_extra
            bx, bz = x + nx * off, z + nz * off
            base = len(out["vertices"])
            # module: length along local Z, width X, up Y, base at y=0
            for mx, my, mz in module["vertices"]:
                wx = bx + tx * mz + nx * mx
                wz = bz + tz * mz + nz * mx
                out["vertices"].append((wx, y - base_sink + my, wz))
            out["uvs"].extend(module["uvs"])
            out["tris"].extend((a + base, b + base, c + base) for a, b, c in module["tris"])
    return out
