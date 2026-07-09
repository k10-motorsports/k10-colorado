---
name: mesh-audit
description: Audit a built track mesh for the geometry defects that repeatedly break generated networks — support columns standing in roads, terrain poking up through the road surface, junctions crossing over the road they connect to, plus a wall/curb CRUFT pass (walls on the road, walls crossing each other, walls floating, curbs not flush). Run at the END of every generative pass (after build_network_mesh, before Blender/kn5). Reports counts + worst offenders and exits nonzero so a build can gate on it.
---

# Mesh audit

**Run this at the end of every generative pass.** Procedurally-built road networks keep re-growing the
same defects; this catches them on the *actual built geometry* before they ship into a kn5.

```bash
python -m scripts.geometry.audit_mesh projects/<slug>
```

Reads `data/track.obj` (+ `data/network.piers.json`, `data/network.geojson`), writes `data/audit.json`,
prints a report, and exits `1` if any defect remains so `build_network.sh` (or CI) can fail the build.

## The checks

| Check | Defect | How it's measured |
|-------|--------|-------------------|
| **A. supports-in-road** | a viaduct pier/column standing in a road that passes under its deck (car hits a column) | each emitted pier vs the real `1ROAD` verts: a road surface `SUPPORT_BELOW` m under the deck within `SUPPORT_R` = speared |
| **B. terrain-poke** | a `1GRASS` vertex sitting above the road surface next to it (launches the car) | every `1GRASS` vert vs nearby `1ROAD` verts: grass `> road_y + POKE_ABOVE_M` within `POKE_R` = poke |
| **C. junction-crossing** | a ramp crossing OVER the road it connects to at grade instead of merging into it | per-edge decks (trimmed like the build): two edges within `CROSS_R`, `|Δy| < CROSS_DY`, tangents `> CROSS_ANGLE_DEG` apart |
| **D. wall-on-road** | a `1WALL` vert sitting ON another road — a median wall landing on the opposing carriageway, or walls tangling across a road at an interchange (covers "walls crossing over others" + "walls passing onto the road") | a wall vert within 2.5 m of a `1ROAD` vert at `|Δy| < 1.2`. A wall is placed >off past its OWN edge, so this only catches a DIFFERENT road |
| **E. wall-floating** | a wall base hovering over the ground/road beneath it | min-y per 0.6 m wall column vs nearest ground within 3 m: base `> ground + 0.6` = floating |
| **F. curb-not-flush** | a `KERB` vert meeting neither the road nor the ground | curb vert with no `1ROAD` and no `1GRASS` vert within 2 m at `|Δy| < 0.5` |
| **G. prop-floating** | a scatter billboard (bush/tree/palm) hovering above the ground | needs `environment.obj` (post-env). Base (min-y per column) vs the conformed-ground SURFACE (`ground.local.json`) — measure against the SAME surface the props are placed on, NOT nearest verts (that phantoms 7 m floats on Mt Soledad's slope) |

**Gating:** A, B, D, E, F, G are hard drivability/cruft failures — the CLI exits nonzero on any. C is
reported but non-gating (residual at-grade crossings have their walls gapped, so they're drivable). G
needs the env stage, so run the audit AGAIN after `build_network_env` to cover it.

## First principle — measure the BUILT geometry, never a reconstruction

A + B read `track.obj` vertices directly. **Do not** re-derive the road surface from the source data to
audit it: a reconstruction that drifts even slightly from the real mesh invents phantom defects. (This
was learned the hard way — an early version reconstructed the deck and reported a **31 m "poke" that did
not exist**; the real mesh had zero.) C needs per-edge identity so it *does* reconstruct — but it applies
the *same* ramp merge-trim the build does, so it reflects what was actually built, and it's a topology
check (angle + proximity), which is robust to small absolute-height drift.

## The fixes these checks guard (all in `build_network_mesh.py`, config-gated)

- **A** — viaduct structs are built AFTER the edge loop; `_col_blocks` drops any column whose footprint
  sits under another road (`gy-2 < road_y < deck-1.5`). Piers are written to `network.piers.json`.
- **B** — `_clamp_terrain_poke` one-sided-clamps any terrain node within a road footprint (+margin) down
  below the road surface. One-sided: natural dips survive.
- **C** — `_trim_ramp_merge` drops the ramp deck vertices that fall inside ANOTHER road's footprint at a
  similar height (a merge / at-grade crossing), keeping grade-separated flyovers intact. Walls are also
  *gapped* at any residual at-grade crossing so the car is never trapped.
- **D** — the wall-gap in the wall pass ALSO gaps a wall panel wherever its line lands within `rhalf+~3` m
  of another road at similar height (any angle) — this removes median walls that land on the opposing
  carriageway and untangles crossing walls at interchanges. `_wall` then PRUNES the orphan verts a gapped
  panel leaves behind (else the audit still counts them). Outer walls facing dirt are untouched.
- **E** — the shoulder verge under the wall keeps the base flush; audit measures 0. (If a future lifted
  ramp has no verge, extend the wall base to the terrain.)
- **F** — root cause, not a curb patch: for an `osm-network` (freeway-only), `classify` votes ONLY on
  freeway OSM ways, so a ramp beside a frontage road is never mislabelled a surface street — no spurious
  curbs get built at all.
- **G** — `build_network_mesh` writes the FINAL conformed+clamped ground as `data/ground.local.json` (a
  regular local-XZ height grid); `build_network_env` samples THAT for prop heights instead of the raw
  heightfield, so bushes/trees sit on the real grass mesh, not float where terrain was conformed to a road.
- **Junctions "join at angles"** (not a check, a fix): `_merge_taper` tapers a ramp's width to a near-point
  at the end where it merges into another road, so it joins that road's EDGE as a lane instead of a
  full-width ribbon crossing the through-lanes. Threaded into the ribbon + shoulder width; markings are
  suppressed on ramps. Roads do NOT need to be welded into one object — coplanar + tapered reads as merged.
- **Median fill** (not a check, a fix): once median walls are gapped (D), the strip between two close
  carriageways is open bumpy grass + verge dips that bounce the car. `_median_fill` spans the gap between
  two close, parallel carriageway edges with a FLAT drivable `1RUNOFF` surface (heights ramp linearly, no
  step); only narrow medians (< `max_gap` edge-to-edge) fill, real wide grass medians stay grass. Config:
  `scenery.median_fill.enabled`.

## Interpreting the result

- **A and B must be 0** before shipping — they are hard drivability failures (hit a column / launch).
- **C** should be small and confined to interchange tangles (report `C_by_kind`). Residual crossings are
  drivable (walls are gapped there); driving them to zero needs grade-separating OSM-untagged crossings,
  a bigger enhancement. Note the count rather than blocking on it.

Tune tolerances at the top of `scripts/geometry/audit_mesh.py`.
