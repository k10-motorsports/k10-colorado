# Pit lanes — research + implementation plan (2026-07-20)

What each real facility actually has (researched: track guides, SCCA records, OSM/OHM surveys,
aerials — details + coordinates live in each track's `route._orientation_doc`):

| Track | Real pit lane | Side (race direction) | Length | Geometry source |
|---|---|---|---|---|
| High Plains | Yes — hot pits between track and paddock | LEFT (north of front straight) | ~690 m incl. entry/exit; exit merges at T2 exit | OSM way 289311751 "Pit Lane" (real frame — usable after HPR re-source) |
| PPIR oval | Yes — pit road inside the front stretch | LEFT (infield side), flows with CCW traffic | ~650 m | OSM way 1134037571 (on-site) |
| Pueblo | Yes — hot pit + two paddock lanes | RIGHT (east of front straight), grandstand base | ~350 m hot pit | OSM ways 1148365747–50 (on-site) |
| Second Creek | Historical "Pit Road" | LEFT (west of Buckley Straight) | ~280 m; exit at the Oval Turn | OpenHistoricalMap Ghost Tracks trace |
| Aspen | Yes — separated pit lane | LEFT (east of S/F chute) | ~100–150 m | aerial + OSM way 192313782 vicinity |
| Grand Junction | Yes — paddock-side lane, openings both ends | LEFT for CW (east of main straight) | ~150 m | aerial + OSM way 293789503 |
| IMI | **No formal pit lane** — paddock apron east of S/F straight | RIGHT (CCW) | — | aerial; model a short apron, not a walled lane |
| Sand Creek / Lariat | Street circuits — no real pits; design choice | — | — | — |

## How to build them (pipeline plan)

1. **Geometry**: fetch the real pit way as a **route connector** — `route.connectors: {"pit_lane":
   ["id:<osm-way-id>"]}` works today (the `id:` fetch form ships since the Lariat). The connector
   already resamples and sweeps as a ribbon in build_mesh (`conn_meshes`); it needs an entry/exit
   width taper into the main ribbon (same flare idea as junctions) so the mouths are seamless.
2. **Pit boxes**: `dummies.place_dummies` gains a mode that walks AC_PIT_n along the pit connector
   (staggered, at connector deck height) instead of down the start straight. AC_START/HOTLAP stay
   on the main ribbon.
3. **Surface**: pit ribbon ships as `1ROAD_pitlane` (drivable, uniquely named, collidable).
4. **What can't be done on the Mac**: the working pit-lane speed limiter and AI pit entry need the
   `ai/pit_lane.ai` spline recorded in-game — a Windows session step (see ac-track-modding skill),
   same bucket as the first-drive sun/banking checks.

Suggested first implementation: **PPIR** (on-site coords, a single straight OSM pit way, simple
merge geometry), then Pueblo and Aspen. High Plains gets real pits with its re-source pass; IMI
gets an apron only.
