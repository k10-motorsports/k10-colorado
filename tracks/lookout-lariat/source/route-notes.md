# The Lookout Mountain Lariat — Denver's 12.9-Mile Ring

A closed-loop driving route in the Golden, CO foothills, sized to match the Nürburgring
Nordschleife's 12.9-mile lap length as closely as a real public road allows.

**Total length:** ~13.0 miles
**Elevation change:** ~1,300 ft (Ring: ~1,000 ft)
**Character:** technical switchback climb → single mid-lap summit → flowing descent → flat-out return straight

## Route

| # | Point | Notes |
|---|-------|-------|
| 1 | **Start/Finish — Lariat Trail gates, 19th St, Golden** | Stone pillars mark the start/finish gantry. Climb begins immediately: 4.3 mi and ~1,300 ft of switchbacks — the Wehrseifen/Karussell sector, compressed. |
| 2 | **Buffalo Bill Museum & Grave (summit)** | High point of the lap at ~7,375 ft — the Hohe Acht. The switchback "M-curves" below are the most photographed corners in Colorado. |
| 3 | **Lookout Mtn Rd → US-40 (Mount Vernon)** | Cross the top past the ranches, drop to US-40. Turn left (east). |
| 4 | **US-40 descent → Heritage Square** | Flowing downhill sector, open sweepers down Mt Vernon Canyon — the Fuchsröhre-to-Brünnchen run. |
| 5 | **US-6 north → 19th St → Finish** | Flat-out straight back into Golden (the Döttinger Höhe), left on 19th St, back through the gates. Lap complete. |

## OSM mapping notes (2026-07-20)

- The loop is entirely on the OSM road network — no KML needed. `route.roads` in
  track.config.json drives the fetch.
- **19th Street**: split into many ways at the US-6 signal (dual-carriageway fan-out); handled by
  the graph-diameter `road_path` in scripts/gps/centerline.py.
- **Lookout Mountain Road** (CR 68, tertiary): one clean 9.9 km chain, gates → summit → US-40.
- **US-40 descent**: the Mt Vernon Canyon frontage is *unnamed* in OSM (`ref="US 40"` secondary),
  becomes "Lariat Loop Scenic Byway"/"West Colfax Avenue" (`ref="I 70 BUS;US 40"` primary) below
  exit 259 — fetched with the `ref:US 40` route entry. I-70 itself carries no US 40 ref here, so
  ref matching cannot grab the freeway (and motorways are excluded from ref matches anyway).
- **US-6 return**: partially unnamed (`ref="US 6"` trunk) → fetched as `ref:US 6`. The ring is
  trimmed at the Colfax and 19th St junctions, so the Clear Creek Canyon reach of US-6 drops out.

## Build character targets

- Elevation: real 3DEP. Lariat Trail averages ~6% with hairpins stacked on a steep face — the
  grade-cap/despike must NOT flatten the switchbacks (check the M-curves survive).
- Non-road terrain: dense **pine/conifer forest** fill (scenery.tree_style = "conifer"),
  no urban dressing — no buildings, streetlights, powerlines, or street signs.
- Honest caveat from the route doc: in reality it's a 20–25 mph cyclist-saturated road; in AC
  it gets to be what it looks like it should be.
