---
name: road-grounding
description: Keep the road deck, curbs, racing kerbs and scenery flush with the real earth — never floating on a mesa or hovering with a shadow gap. Build terrain UP from the ground to the road (embankment fills / cuts), DRAPE every edge strip onto the real grass surface, and span deep gaps with a bridge instead of paving them over. Use whenever a track's road "floats", run-off is a cliff, curbs/kerbs hover at angles, or after changing any road↔terrain geometry. Verify with lateral cross-sections of the BUILT track.obj, not by eye.
---

# Road grounding

**The road, curbs, kerbs and scenery must sit ON the real ground — never float above it.** These are
the defects this guards, all of which shipped at some point:

- **Road floats on a mesa** — the terrain was pulled UP to road level as a flat plateau, so the deck
  rides a tabletop, run-off is a fall off the edge, and real valleys are buried.
- **Curbs / racing kerbs hover** at a fixed road-plane height while the ground sat lower → a 0.5–1 m
  vertical gap that casts shadows (worst on banked/embanked corners, where the kerb tilts at an angle).
- **Bridges paved over** — where the road rides high over a creek, the gap got filled into a causeway.

The kn5 export is geometrically **exact** (proven: 0.000 m AABB delta blend→kn5), so a "floating" road
is ALWAYS an authoring defect in the generators, never the exporter. Fix it in `build_mesh` / `ribbon` /
`kerbs`, not in the export.

## The three moves (all in `scripts/geometry/`)

### 1. Build UP from the ground — `ribbon.grade_embankment`
Replaces the old `conform_terrain_to_road` mesa. For each terrain grid node, referenced to the nearest
road-surface sample:
- within a gentle **band** (default 4 m) → held at `road_edge − clearance` (a short drivable shelf the
  edge strip meets),
- beyond the band → ramps toward the node's OWN bare-earth height at a **`ratio`:1 slope** (default 2:1
  ≈ 27°). **Fill** descends from the shelf down to real ground; **cut** rises up to it. Once it reaches
  natural ground it STAYS there — the valley/hillside survives.

Racetracks have tiny fills (median ~0.3–0.5 m), so the same path reads as the "smooth gradations" a
purpose-built circuit wants — no track-type special-casing needed.

### 2. DRAPE the edges onto the real surface — the key anti-float move
The terrain grid is coarse (~13 m), so it can't hug a 3 m-resolution road edge — grid-based grading
alone still leaves the edge floating between nodes. So the edge strips **drape their outer lip onto the
actual grass surface**, sampled bilinearly at road resolution:
- `ribbon.curb_sidewalk` (urban `road_edge.profile == "sidewalk"`): the 5th profile point marches out at
  `ratio`:1 until it reaches `ground(x,z)`.
- `ribbon.road_shoulder` (default racetrack edge): a 3-point verge whose ground-meet point drapes.
- `kerbs.corner_kerbs`: the outer ramp-bottom vertex drapes onto `ground(x,z)` (capped below the kerb
  top so it stays a down-ramp) — this is what un-floats the racing kerbs.

All three take a `ground=` sampler. `build_mesh` passes `grass_surf` = bilinear over the graded+clamped
`grid_xyz`. **Without a ground sampler they fall back to the old fixed lip** (byte-identical), so the
change is opt-in per call.

**`build_mesh` ordering matters:** road + runoff → `grade_embankment(grid)` → `clamp_terrain_below_road`
(anti-poke below the ribbon) → build `grass_surf` from the now-final grid → drape shoulder/curb/kerb onto
it. The grid must be FINAL before anything drapes onto it — and since the contact pin landed, the
FULL final order (including the pin as the last terrain pass) is in "The contact pin" section below.

### 3. Bridge deep gaps instead of filling — `build_mesh._bridge_detector`
Where the road rides `> BRIDGE_MIN_H_M` (3.5 m) above bare earth over `≥ BRIDGE_MIN_SPAN_M` (12 m) — a
real gap to span — `grade_embankment` **suppresses fill** within `BRIDGE_CLEAR` (14 m) of the span, so
the natural creek/valley stays open. `build_env._creek_bridge` then spans it with a deck, parapets,
underside slab and piers (keyed off the OSM water polylines; piers reach `ground_y − 0.6`, sampled from
`ground.local.json`).

## The contact pin — "they don't need to be the same, they NEED TO TOUCH"

Everything above grades and drapes, but on a real mountainside it still left the deck ~2 m off the
ground: **"real ground" under the outer half of a bench-cut road is legitimately metres low** (that
half rides fill or a wall in real construction), and every pass we had either pushed grass DOWN
(clamps) or pulled it partway up (the bend). Clamp-down can never close that gap. The pros conform
the terrain to the road (rt_california: 0.0 mm median vertical). The pin does that:

1. **Two-sided pin under the footprint**: every terrain grid node under the pavement is SET to
   `deck − clearance` — up or down. Not a clamp, a pin.
2. **Sag-proof window-min**: pin to the MINIMUM deck height within half a grid cell in all 8
   directions (axis + diagonals — grass triangulates with an a-c diagonal). Pinning to the deck at
   the exact point lets 6 m grass chords bridge ABOVE the deck through every sag and across camber
   (shipped 1,618 "ground on top mid-lane" contacts; axis-only window still left 28).
3. **Feather is a continuous field**: outside the footprint, blend from deck height to natural
   ground over ~6 m using distance to the closest point ON the road triangles with smoothstep
   falloff — nearest-VERTEX distance oscillates with vertex spacing and prints a 0.2 m washboard
   into the fill face (drive test reads it as severe steps). Cap the feather target by the same
   window-min: a cell can span the whole road and feather both verts to deck-EDGE height, chording
   above the cambered low side.
4. **Bridges keep their valley**: under a declared span the pin may only push down, never lift
   (station lookup vs `bridge_of`).
5. **Layer window ≤ 20 m** on every deck sample — stacked switchbacks, as always.

**Ordering is load-bearing (cost: 3 failed builds):** the pin runs AFTER every pavement mutation —
including the shoulder hem SINK pass, which otherwise lowers pavement under already-pinned grass and
re-opens a poke — and BEFORE `write_ground_local` + `grass_terrain`, so props, audit G and the
rendered grass all see ONE surface. Pinning the grass mesh after the ground snapshot ships phantom
"floating plants" (props seated on a surface that no longer exists). Full order:
grade → clamp 1 → grass_surf → drape → clamp 2 → contact bend → road/shoulder clamps → shoulder
sink → **contact pin (+ shoulder/runoff/kerb down-clamp)** → write ground.local → grass_terrain.

**The sampling-consistency law**: every consumer of the ground — prop seating (`build_env.ground_y`),
audit checks, gates — must sample **triangle-exact with the same (a,b,c)+(a,c,d) diagonal split the
grass renders**. Bilinear sits up to ~1 m off the rendered triangle on steep terrain: it shipped
hovering fences and 19 phantom floating plants in one build.

**Corridor elevation is a precondition**: a 40 m heightfield cannot contain an 8 m bench cut — the
splice (`heightfield.fetch_corridor`: 3DEP ±60 m, 6 m stations, cached `data/corridor.elev.json`)
must feed the grid near the road or the pin is pinning to fantasy ground.

Result on the Lariat (25 km mountain loop): deck-vs-mountain 2.23/3.59/15.1 m → **0.48/1.21/3.76 m**
with the residual hidden UNDER the slab as chord-safety margin; drive test 0 mid-lane contacts;
audit 0 pokes. Flat tracks (Sand Creek) measure 0.000/0.000/0.000. Getting to true 0.0 mm on
mountains needs welded edge rings (the #17 pro-construction pass), not a deeper pin.

## Verify on the BUILT mesh — cross-sections, never by eye

Rebuild only the offline stages (fast, no Blender) and slice the actual `track.obj`:

```bash
python -m scripts.geometry.build_mesh tracks/<slug>          # writes track.obj + ground.local.json
python -m scripts.environment.build_env tracks/<slug>        # scenery + bridge (needs ground.local.json)
python -m scripts.geometry.audit_mesh   tracks/<slug>        # B/F/G must be 0 (see mesh-audit skill)
```

Then a lateral cross-section at suspect stations (embankment, cut, normal, bridge) confirming that at
every offset the **draped edge lands on the grass surface** (`|edge − grass| < ~0.1 m`) and the grass
slopes to natural rather than sitting as a flat mesa. Keep a scratch slicer that, per arc station, prints
each surface's Y vs lateral offset alongside `grass_surf` and the raw bare-earth. What "fixed" looks like:
sidewalk float `0.8 m → flush`; creek `grass +9.7 m mesa → slope to real ground`.

## Tuning (per `track.config.json` where exposed / constants otherwise)
- Fill/cut steepness: `ratio` in `grade_embankment` / the drape calls (2:1 default; gentler = more
  survivable run-off, steeper = more realistic embankment).
- Gentle band width: `band` (grade_embankment) / `verge_w` (shoulder) / sidewalk `grade_w`.
- Bridge trigger: `BRIDGE_MIN_H_M`, `BRIDGE_MIN_SPAN_M`, `BRIDGE_CLEAR` in `build_mesh`.

## Known limits / follow-ups
- **Coarse grid smooths narrow gorges.** A 10 m-deep creek only ~1 grid cell wide reads as a ~5 m dip,
  so bridge piers look short. Fix (unimplemented): locally refine the grid near the water line, or carve
  the creek channel as explicit geometry before grading. Not blocking — the deck-on-piers is correct.

## Gotcha: draped-edge seam pokes (banked ovals, roval connectors)

Applying grounding to the rest surfaced small terrain pokes (grass through road, +0.1–0.55 m) — worst on
the **PPIR banked oval + roval connector**. Two causes, both fixed in `build_mesh`:

1. **The drape lands on the bilinear `grass_surf`, but the grass MESH is triangulated grid nodes** — a
   node near the seam can sit a touch ABOVE the draped shoulder/kerb edge. Fix: an **anti-poke pass 2** —
   `clamp_terrain_below_road(grid, shoulder + kerb verts, clear=0.05, reach=6.0)` AFTER the drape, BEFORE
   `grass_terrain`. Tiny 5 cm clearance so it kills the poke without re-opening a visible gap.
2. **Connectors were built AFTER `grass_terrain`**, so the grid was never graded/clamped below them and the
   grass poked up through the roval. Fix: build `conn_meshes` BEFORE the clamp and include their verts in
   anti-poke **pass 1** (`road + runoff + connectors`).

Order that works: grade → road + runoff + connectors → clamp pass 1 → grass_surf → drape shoulder + kerb →
clamp pass 2 (below draped edges, 5 cm) → write ground.local → grass_terrain. After this, all 8 circuits
(flat road courses, banked ovals, the roval) audit **B (poke) = 0**.

## Per-track notes (append as you apply grounding to each circuit)
- **Lookout Lariat** (real_road, 25 km mountain loop, stacked switchbacks): the track that forced
  the contact pin, the corridor splice and the sampling-consistency law. v0.6.0 = first fully-green
  contact build.
- **Sand Creek** (road, `sidewalk` edge, mirror_x): reference case. 4 elevation-detected bridge spans
  (creek at 45th/Quebec). Draped sidewalk + embankments + creek bridge. audit CLEAN, kn5 verified. v0.6.1.
- **PPIR** (banked 10° oval + infield roval connector): triggered both seam-poke fixes above. Clean after.
- **Pueblo / Grand Junction / Aspen / High Plains / Second Creek / IMI**: flat-to-rolling road courses;
  small fills → smooth gradations, no bridges. All audit CLEAN with the two-pass clamp.
