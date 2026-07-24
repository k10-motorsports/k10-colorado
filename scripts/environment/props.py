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
                      base_sink: float = 0.05, surface_y=None, on_pavement=None,
                      station_skip=None) -> dict:
    """Stamp the barrier module end-to-end along each warning-barrier run, on the run's OUTSIDE.

    ``surface_y(x, z) -> y|None`` seats each module on the BUILT edge surface (road+shoulder tops).
    Seating at centerline height floats the whole run wherever the verge falls away — every fill
    and hairpin on the Lariat shipped with barriers hovering in mid-air ("everything is floating")."""
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
            if station_skip is not None and station_skip(i):
                continue          # bridge spans carry their own railings — no barriers walked
                                  # onto the shoulder drape over the void (27 m floaters)
            x, y, z = pts[i]
            tx, tz = pts[min(i + 1, n - 1)][0] - pts[i - 1][0], pts[min(i + 1, n - 1)][2] - pts[i - 1][2]
            L = math.hypot(tx, tz) or 1e-9
            tx, tz = tx / L, tz / L
            nx, nz = -tz * side, tx * side              # outward on the run's side
            # width RAMPS through junction flares (0.15 m/m): a 4 m module placed off one
            # station's width ends up INSIDE the widened lane a few metres later. Use the max
            # width over the module's whole span.
            _wspan = max(widths_m[max(0, i - 2):min(n, i + 3)] or [widths_m[i]])
            off = _wspan / 2.0 + off_extra
            if on_pavement is not None:
                # a danger barrier must guard the corner, not stand on its flared apron: WALK
                # OUTBOARD until off the pavement — but ONLY onto real support (walking off a
                # BRIDGE DECK hung modules 27 m over the creek). No supported clear spot = skip.
                _ok_spot = False
                for _try in range(5):
                    _cx0, _cz0 = x + nx * off, z + nz * off
                    _hl0 = module_len / 2.0
                    if not any(on_pavement(_cx0 + tx * _q0, _cz0 + tz * _q0, y)
                               for _q0 in (-_hl0, 0.0, _hl0)):
                        if surface_y is None:
                            _ok_spot = True
                            break
                        _sy0 = surface_y(_cx0, _cz0, y)
                        # STANDABLE support only: probe the local slope — a 0.75:1 fill face
                        # (53 deg) passes the wall-cut but nothing stands on it (the Quebec
                        # approach floaters). Reject support steeper than ~24 deg.
                        if _sy0 is not None and abs(_sy0 - y) < 2.5:
                            _sy1 = surface_y(_cx0 + nx * 0.8, _cz0 + nz * 0.8, y)
                            if _sy1 is not None and abs(_sy1 - _sy0) <= 0.35:
                                _ok_spot = True
                                break
                    off += 1.0
                if not _ok_spot:
                    continue
            bx, bz = x + nx * off, z + nz * off
            if surface_y is not None:
                sy = surface_y(bx, bz, y)   # y_ref = the run's own station height (layer window)
                if sy is not None:
                    _syo = surface_y(bx + nx * 0.8, bz + nz * 0.8, y)
                    if _syo is not None and abs(_syo - sy) > 0.35:
                        sy = None           # steep face is not support — force the inboard retry
                if sy is None:
                    # retry INBOARD and MOVE the module there — taking the inboard height while
                    # leaving the module at the outer spot hung 50 modules over the shoulder edge
                    bx2, bz2 = x + nx * (off - 1.2), z + nz * (off - 1.2)
                    sy = surface_y(bx2, bz2, y)
                    if sy is not None:
                        bx, bz = bx2, bz2
                if sy is None:
                    continue                 # nothing built under this spot — skip, never float
                y = sy + base_sink           # seat ON the built surface (sink re-subtracted below)
            base = len(out["vertices"])
            # module: length along local Z, width X, up Y, base at y=0
            for mx, my, mz in module["vertices"]:
                wx = bx + tx * mz + nx * mx
                wz = bz + tz * mz + nz * mx
                out["vertices"].append((wx, y - base_sink + my, wz))
            out["uvs"].extend(module["uvs"])
            # DOUBLE-SIDED stamping: mirrored bases flip winding, module normals are mixed, and
            # "some barriers are still inside out" survived two winding fixes — end the class:
            # every face ships both ways. Barrier tri counts are tiny; correctness wins.
            out["tris"].extend((a + base, b + base, c + base) for a, b, c in module["tris"])
            out["tris"].extend((a + base, c + base, b + base) for a, b, c in module["tris"])
    return out


def instance_line(centerline_m, module: dict, *, ranges: list[dict], widths_m=None,
                  ground=None, module_len: float = 1.7, default_offset: float = 8.0,
                  max_dy: float = 45.0, reject=None) -> dict:
    """Instance a module continuously along config-declared lap ranges — ranch fences at the
    right-of-way line, pylon runs across the plains. Each instance drapes to ``ground(x,z)`` so
    runs follow the terrain. ranges: [{start_m, end_m, side (+1 left/-1 right/0 both), offset_m}]."""
    pts = centerline_m
    n = len(pts)
    st = [0.0]
    for i in range(1, n):
        st.append(st[-1] + math.hypot(pts[i][0] - pts[i - 1][0], pts[i][2] - pts[i - 1][2]))
    out = {"vertices": [], "uvs": [], "tris": []}
    _last_panel: dict = {}
    for rg in ranges:
        s0, s1 = float(rg["start_m"]), float(rg["end_m"])
        off = float(rg.get("offset_m", default_offset))
        sides = (1.0, -1.0) if rg.get("side", 0) == 0 else (float(rg["side"]),)
        for side in sides:
            next_at = s0
            for i in range(1, n):
                if st[i] < next_at:
                    continue
                if st[i] > s1:
                    break
                next_at += module_len
                x, y, z = pts[i]
                tx = pts[min(i + 1, n - 1)][0] - pts[i - 1][0]
                tz = pts[min(i + 1, n - 1)][2] - pts[i - 1][2]
                L = math.hypot(tx, tz) or 1e-9
                tx, tz = tx / L, tz / L
                nx, nz = -tz * side, tx * side
                w_half = (max(widths_m[max(0, i - 3):min(len(widths_m), i + 4)]) / 2.0 if widths_m else 0.0)
                bx, bz = x + nx * (w_half + off), z + nz * (w_half + off)
                # seat on the MIN ground over the module's footprint (center + both ends): a
                # center-point seat on a bank flies the downhill end of the panel (+1.6 m fence
                # floats). Burying the uphill end slightly is what real fence posts do.
                if ground and module_len <= 10.0:      # short modules (fences); pylons keep center-seat
                    _hl = module_len / 2.0
                    _cands = [ground(bx, bz), ground(bx + tx * _hl, bz + tz * _hl),
                              ground(bx - tx * _hl, bz - tz * _hl)]
                    _cands = [c for c in _cands if c is not None]
                    by = min(_cands) if _cands else None
                elif ground:
                    by = ground(bx, bz)
                else:
                    by = y
                # LAYER WINDOW safety net: a ground sample tens of metres off the run's own station
                # height is corrupt or another terrain layer — skip the instance, never fly/bury it.
                if by is None or abs(by - y) > max_dy:
                    continue
                # footing sink (per-range): tall towers on rough terrain bury their footings — a
                # 10 m-grid drape leaves a corner up to ~1 m proud of the true surface otherwise.
                by -= float(rg.get("sink_m", 0.0))
                if reject is not None:
                    _hl2 = module_len / 2.0
                    # probe the module's CORNERS too — a diagonal apron edge can clip a panel's
                    # side while its axis line stays clear (one panel, 14 audit verts)
                    _probe2 = [(bx + tx * _q, bz + tz * _q) for _q in (-_hl2, 0.0, _hl2)]
                    _probe2 += [(bx + tx * _q + nx * _w2, bz + tz * _q + nz * _w2)
                                for _q in (-_hl2, _hl2) for _w2 in (-0.6, 0.6)]
                    if any(reject(_px2, _pz2, by) for _px2, _pz2 in _probe2):
                        continue        # module would stand on pavement (corner mouths, aprons)
                # PILE GUARD: on tight corners consecutive stations' tangents fan — stamping a
                # panel per station piles them into a woodstack. Skip until the run has moved
                # a real module-length AND turned less than 30 deg since the last stamp.
                _yaw2 = math.atan2(tz, tx)
                _lp = _last_panel.get(id(rg))
                if _lp is not None:
                    _dyaw = abs((_yaw2 - _lp[2] + math.pi) % (2 * math.pi) - math.pi)
                    if math.hypot(bx - _lp[0], bz - _lp[1]) < module_len * 0.8 or \
                       (_dyaw > math.radians(30.0) and math.hypot(bx - _lp[0], bz - _lp[1]) < module_len * 2.0):
                        continue
                _last_panel[id(rg)] = (bx, bz, _yaw2)
                base = len(out["vertices"])
                for mx, my, mz in module["vertices"]:
                    out["vertices"].append((bx + tx * mz + nx * mx, by + my, bz + tz * mz + nz * mx))
                out["uvs"].extend(module["uvs"])
                out["tris"].extend((a + base, b + base, c + base) for a, b, c in module["tris"])
                if module_len <= 10.0:      # panels are seen from both sides; pylons are closed solids
                    out["tris"].extend((a + base, c + base, b + base) for a, b, c in module["tris"])
    return out
