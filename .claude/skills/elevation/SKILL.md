---
name: elevation
description: Sample real-world terrain elevation along the track centerline and emit a smoothed heightfield. Use after the centerline exists to add z-values to the racing line plus a surrounding terrain grid. Source priority is USGS 3DEP 1 m (US) → OpenTopography → SRTM 30 m, smoothed aggressively along the racing line to avoid stepping.
---

# Elevation

Samples and integrates real terrain data along the racing line.

## Source priority

1. **USGS 3DEP 1 m** — preferred for US locations (free, high resolution). **Primary for Commerce City.**
2. **OpenTopography** — fallback / wider coverage.
3. **SRTM 30 m** — last-resort global fallback.

## Inputs

- `data/centerline.geojson` (from `gps-extraction`).
- `track.config.json` (`location` for the region, `origin`).

## Process

1. Sample elevation **along the centerline** plus a **margin grid** for the surrounding terrain.
2. **Smooth aggressively along the racing line** to avoid stepping (the road must feel continuous).
   Keep the margin grid coarser/natural for grass.

> Commerce City is gentle terrain — expect mild grades, not big elevation.

## Output

- Centerline **z-values** (added to the centerline).
- `data/heightfield.npy` — the terrain grid used to build the grass mesh.

## Corridor resolution — the grid near the road must match road resolution

A 40 m heightfield **cannot contain an 8 m bench cut** — the Lariat's 2.3 m "floating road" traced
to this, not to any conform pass. `heightfield.fetch_corridor` samples 3DEP along the actual road
(±60 m lateral, 6 m stations, 5 m lateral steps; cached `data/corridor.elev.json`) and `build_mesh`
splices it into the grid (nearest corridor station, signed lateral, 10 m edge blend). Any mountain
track needs this before terrain-contact passes mean anything.

## Bridges: bare-earth DEMs delete the deck

Lidar bare-earth classification REMOVES bridge decks — the DEM shows the valley floor under every
real bridge, so the raw road profile dives into the creek and climbs back out (Sand Creek's
"dip + rolling hill" at the 56th double-right). Declared bridges (`capture.bridges`) are leveled by
`evidence.level_bridge_decks`, and the rules are:
- **Anchor on BANK CRESTS**: the highest ground within ~80 m OUTSIDE each span end. Fixed-offset
  anchors (old: span+30 m) land partway down the dive when the DEM valley is wider than the span
  (56th's is ~225 m wide) — the "level" deck gets built 2.5 m down inside the hole.
- **Straight line crest-to-crest, no blend back to raw**: the endpoints already sit on real ground;
  any blend zone lets the valley bleed a dip back in. Downstream centerline smoothing rounds the
  small grade breaks at the crests.
- The cut-only profile snapping must never run after leveling (it would cut the deck back into the
  valley); grade-capping is fine.

## Scripts

`scripts/elevation/` — `usgs_3dep.py` (DEM fetch), `heightfield.py` (sample-along-path + grid +
smooth + `fetch_corridor`). Bridge leveling: `scripts/capture/evidence.py level_bridge_decks`.
