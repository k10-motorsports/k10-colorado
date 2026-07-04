# Sand Creek Raceway — route notes

> Per the project's first principle, **this screenshot is a selection aid, not data.** The
> coordinates below pick the location and mark intent; the precise centerline comes from
> OpenStreetMap via Overpass (see the `gps-extraction` skill).

## Location

- **Anchor / pitlane marker:** `39.7839155, -104.9398905`
- **Timezone:** America/Denver (UTC−7 / MDT in summer)
- **Area:** Commerce City / Central Park (Denver), CO — bounded roughly by **E 56th Ave** (north),
  **Quebec St** (east), **E 48th / E 49th Ave** (south), with a western return leg.
- **Google Maps:** https://maps.google.com?q=39.7839155,-104.9398905&entry=gps&shh=CAE&lucs=,94297699,94231188,94280568,47071704,94218641,94282134,100813469,94286869&g_st=ic

The dropped pin (`39.7839155, -104.9398905`) marks **the side road where the pitlane should be** —
a spur off the main loop near the southwest corner (by *Quality Restaurant Equipment* / E 48th Ave).
It is *inside* the loop, so it also serves as the config `location` anchor for now; the mesh origin
is still computed as the centerline **centroid** (see `track.config.json`).

## Layout (from the marked map)

A single outer loop with one internal connector:

- **North leg** — sweeps along / above **E 56th Ave**, curving down toward Quebec St.
- **East leg** — runs south past **TA Travel Center** down the east side near **Quebec St** to E 49th Ave.
- **South leg** — runs west along **E 49th Ave → E 48th Ave**.
- **West leg** — returns north up the western edge back to the north leg.
- **Internal connector** — a spur around **Ivy St** (mid-map) cuts across the infield. This is the
  candidate **`connector_a`** used by the `short` layout (see `layouts[]` in `track.config.json`).

**Landmarks on/near the route:** Sand Creek Landfill (infield W), TA Travel Center (NE),
Elemental Studios (infield S), Quality Restaurant Equipment (SW, near the pit spur).

## Layouts

| Layout | Route | Connectors |
|--------|-------|------------|
| `full`  | Complete outer loop | — |
| `short` | Outer loop cut short via the Ivy St infield link | `connector_a` |

## Assets

- `track-layout.png` — the marked Google Maps screenshot showing the red route + pitlane pin. ✓ committed.

## TODO

- [x] Commit `track-layout.png` into this folder.
- [ ] Run `gps-extraction` to derive `data/centerline.geojson` from OSM/Overpass for this bbox.
- [ ] Confirm the Ivy St spur as `connector_a`; tag it in the centerline.
- [ ] Decide exact pit-spur road and place `AC_PIT_*` / pit lane there.
