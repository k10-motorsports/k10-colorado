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

## Scripts

`scripts/elevation/` — `usgs_3dep.py` (DEM fetch), `heightfield.py` (sample-along-path + grid + smooth).
