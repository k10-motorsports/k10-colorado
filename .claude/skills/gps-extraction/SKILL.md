---
name: gps-extraction
description: Turn a region + rough route into a clean, ordered track centerline. Use when converting OpenStreetMap/Overpass road data (and optional hand-drawn KML for off-network segments) into data/centerline.geojson — ordered, deduped, resampled to ~2–5 m spacing, width-tagged, loop closed, with named connectors for layout variants.
---

# GPS Extraction

Converts a region and a rough route sketch into a clean, ordered **centerline**.

> Remember the first principle: **the screenshot is a selection aid, not data.** It tells you
> *which* roads to grab. Geometry comes from OSM; KML only fills gaps where the route deliberately
> leaves the real network.

## Inputs

- Bounding box / location and the names of the roads on the route (from the map + route notes).
- Optional hand-drawn **KML** for segments where the marked route leaves the OSM road network
  (shortcuts, private land, connectors).
- `track.config.json` (`source.type`, `source.kml_overrides`, `default_width_m`, `width_overrides`).

## Process

1. **Overpass query** — fetch ways filtered by bbox and road-name parameters.
2. **Parse KML** — bring in hand-drawn segments for off-network portions.
3. **Order, dedupe, resample** — to even **~2–5 m** spacing along the route.
4. **Width tagging** — tag width per segment (`default_width_m` + `width_overrides`).
5. **Close the loop** — stitch the centerline closed when `loop: true`.
6. **Connectors** — merge optional connectors as **named sub-paths** (e.g. `connector_a`) so layout
   variants in `layouts[]` can reference them.

## Output

`data/centerline.geojson` — ordered lat/lon coordinates with per-vertex **width** values and
**segment tags** (including connector names).

## Reference

Overpass QL syntax · loop-closure mechanics · connector tagging for layout variants.

## Scripts

`scripts/gps/` — `overpass.py` (query), `kml.py` (parse), `centerline.py` (order/dedupe/resample/tag/stitch).
