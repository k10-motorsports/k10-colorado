---
name: lighting
description: Compute solar position, orient the track to true north, and emit CSP/Sol lighting config plus time-of-day presets. Use to resolve true_north_rotation_deg (written back to track.config.json) so in-game shadows align with the real world, and to generate lighting templates (morning/noon/golden_hour) for the track folder.
---

# Lighting

Uses real geographic coordinates to make sun position and shadows correct.

## Inputs

- `track.config.json` (`location.lat/lon/timezone`, `lighting.presets`).
- The built mesh orientation (so it can be aligned to true north).

## Process

1. **Solar position** — compute solar azimuth/elevation for the track latitude (~39.8 °N for
   Commerce City) across the relevant dates/times.
2. **True north** — orient the mesh to true north so computed shadows align correctly.
3. **Write back** — store the resolved rotation in `true_north_rotation_deg` in `track.config.json`.
4. **Presets & config** — emit time-of-day presets (`morning`, `noon`, `golden_hour`) and
   CSP / Sol / Pure lighting config templates for the track folder.

## Output

- `true_north_rotation_deg` (written back to config).
- CSP/Sol lighting config files for the track folder.

## Notes

Solar math uses `astral` (or `pvlib`) — latitude/longitude/timezone come straight from config.

## Scripts

`scripts/lighting/` — `solar.py` (azimuth/elevation), `csp_config.py` (orientation + preset/CSP emit).
