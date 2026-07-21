---
name: drive-test
description: Virtual drive test — simulate what a CAR experiences over the built track before any kn5 or release. Sweeps six wheel paths along the actual built triangles asking, at every metre, the three things a driver feels — what surface is under the tire (ground poking through the road), what's standing in the corridor (walls, decks, posts, trunks), and whether the surface is smooth at speed (steps, slope kinks). Run after EVERY mesh/env build and on any track a driver calls rough; gates the build (exit 1 on FAIL). This is the seat-time proxy — audits check geometry classes, this checks the drive.
---

# Drive test

**The problem this solves:** three releases shipped "audit CLEAN" and drove terribly. Vertex-level
audits pass while the triangle faces between vertices knife through the deck; centerline metrics
look smooth while the lane edges ride 5 cm kerb lips; nothing checked whether a wall stands in the
driving line. The only truth is the built triangle soup the physics engine collides with — so this
test drives it.

```bash
python -m scripts.geometry.drive_test tracks/<slug>     # after mesh + env, BEFORE blend/kn5
```

Reads `data/track.obj` + `data/environment.obj` + `centerline.local.json`, writes
`data/drive_test.json`, prints worst offenders with lap stations, exits 1 on FAIL.

## What it measures (and the physics of each threshold)

Six wheel paths — a wheel pair (±0.75 m) around the lane centre and both third-lines — sampled
every 1 m along the whole lap, always against the HIGHEST physical surface at each point, because
that is what the tire lands on.

| Check | FAIL means | Threshold + why |
|---|---|---|
| **Ground on top mid-lane** | a grass/terrain triangle is the top surface inside the lane — the car launches off it | any occurrence; this is the "ground sticking through the road" defect, measured at the faces, not the vertices |
| **Severe steps** | the top surface jumps vertically between metres beyond slope continuation | > 6 cm ≈ a full curb face at speed — suspension crash |
| **Bumps /km** | smaller sharp steps (mesh seams, pad edges, kerb lips in the lane) | > 2.5 cm ≈ lane-reflector hit; report rate, judge trend vs a track the driver approved |
| **Slope kinks /km** | grade changes faster than real roads allow | > 3 %-pt across 1 m ≈ a driveway lip. Real geometric design rounds EVERY grade change through vertical curves and keeps cross-slope breaks ≤ 4–8%; a healthy build's carriageway shows near-zero |
| **Obstructed stations** | anything solid rising > 0.25 m above deck inside lane + 0.3 m | walls, highway-deck parapets, posts, tree trunks — reported BY MESH NAME so the generator that put it there is identifiable |

## How to read a failure (defect → fix map)

- `soft_top_in_lane` → terrain conform resolution or clamp margin: raise the upsample budget /
  check `grade_embankment` covers the flare width; verify with the audit's H check AND this test
  (H samples cross-sections; this samples wheel paths — both must be green).
- `severe_steps` at mesh-name boundaries → seams between split meshes or pad/apron edges: the
  offending station's top-owner changes name; blend heights at the seam.
- `steps` clustered at junctions → flare/pad geometry; on street tracks check widths_from_osm
  flare blending.
- `kinks` clustered where elevation is real → despike/smoothing windows too narrow for the DEM
  noise, or the grade cap fighting real grades (see road_profile.max_grade_pct).
- `obstructions` named `BARRIER_*` / `HWYSTRUCT*` / `HIGHWAY*` → a scenery generator crossed the
  drivable corridor (zones-sourced highway decks are the usual offender where the route parallels
  a real freeway); gate or clip that generator against the road corridor.
- `obstructions` named `CONIFER*`/`TREES*`/`BUSHES*` → scatter corridor margin too small.

## Build roads like real roads (the constructive half)

When a failure needs geometry redesign rather than a knob, apply real road-construction practice:

1. **Vertical curves, never grade breaks.** Real profiles are tangents joined by parabolic
   vertical curves sized so the grade change per metre stays imperceptible. If a profile has a
   kink, round it over 20–60 m (crest/sag), don't clamp it.
2. **Cross-section is a system**: travel lane (1.5–2% crown or superelevated), then shoulder
   (same plane or ~2% steeper — NEVER a lip), then a rounded hinge into the fore-slope (4:1–6:1
   drivable if possible, 2:1 max), then ditch/ground. Every strip shares seam vertices with its
   neighbour — no independent overlapping surfaces (pads on roads read as bumps).
3. **Superelevation transitions rotate gradually** (runoff over tens of metres), so banked corners
   enter smoothly — never step a banked section against a flat one.
4. **Edge furniture belongs off the roadway**: kerbs/rumbles only where the real road has them
   (a mountain road has NONE — `kerb.enabled: false`); barriers outside the shoulder, parallel,
   with end treatments — never angled into the corridor.
5. **The clear zone**: keep solid objects (trees, posts, walls) a consistent margin off the edge;
   in-pipeline that's `scenery.corridor_margin_m` and the obstruction check enforces it.

## Gate criteria (what FAILs)

Absolute zeroes — no driver tolerates these, ever:
- `soft_top_in_lane > 0` (ground through the lane)
- `obstruction_stations > 0` (anything solid in the corridor)

Rates — measured against the driver-approved baseline (Sand Creek v0.15.x: ~11 severe/km,
~19 bumps/km, concentrated at junction crotches where edge strips meet):
- `severe_per_km > 12` or `steps_per_km > 75` FAILs

`kinks_per_km` is REPORTED but does not gate: on a real mountain profile, 3 %-pt/m grade events
are genuine road texture. Watch its trend between builds of the same track instead.

## Process rule

A build ships only after BOTH gates: `audit_mesh` (geometry classes) AND `drive_test` (the drive),
then a human lap for anything the driver previously flagged. The test exists so the release/test
cycle isn't the first drive — never ship a FAIL, and never dismiss a warning line in a build log
as cosmetic (the invisible-forest bug lived in a warning for three releases).
