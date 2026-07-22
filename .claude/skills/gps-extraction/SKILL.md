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


---

## Street fidelity (the cleanup pass — REQUIRED for street circuits)

Three defects this stage kills (observed on Sand Creek / Lookout Lariat):

### 1. Right-street lock — identity-gated OSM matching
Width/attribute analysis latched onto the nearest way and parallel freeways won: Sand Creek Drive
matched I-270; the 46th St / 45th frontage area matched I-70 — inheriting freeway widths (the
monster flares behind the raw intersection mouths). The route config NAMES every street
(`route.roads`); that identity travels with each centerline vertex and gates all matching:
- candidate way must AGREE on normalized name/ref with the vertex's identity,
- bearing within 25 deg of the centerline tangent,
- laterally within max(12 m, own width),
- `motorway/motorway_link/trunk` rejected unless the segment's own identity IS that ref.
Diagnostic + gate: `data/street_match_report.json` (per station: matched way, class, distance,
bearing). Build FAILS if >2% of stations match a differently-named way, or ANY freeway capture
on a non-freeway street.

### 2. Engineered alignment — smooth like a surveyor, honest like the map
Raw OSM node wobble (±1-2 m) swept at road scale reads hand-drawn. Denoise the horizontal
alignment in the curvature domain with a HARD deviation band vs the raw polyline (residential
0.6 m / arterial 0.8 m / highway 1.2 m): straights snap straight (|k| < 1/800), sustained
curvature becomes constant-radius arcs, blends are G2 splines. Same treatment vertically inside
the existing despike/grade-cap rails. Junction zones are EXCLUDED (pads own those). Gate: the
wobble detector — no curvature oscillation >0.02 1/m with reversals closer than 15 m; deviation
stays inside the band. The map always wins over the smoother.

### 3. Whole-intersection capture — junction pads
Where the route turns at a street junction, the real corner offers the FULL paved intersection.
Detect: heading change >25 deg within 30 m of an OSM node shared by 2+ named ways. Build: the
junction polygon (union of both streets' width-rectangles + 6-9 m curb-return fillets; aerial
trace wins when present) meshed as a deck-grade PAD fused into the ribbon with shared seam verts,
replacing the width-flare hack. Sidewalk/verge wraps AROUND the pad; pad edges grade to ground.
This deletes the raw intersection-mouth ledges at the source — then remove the flare exemption
from the drive test's excursion sweeps so mouths gate like everywhere else.

### Order of operations (it matters)
identity lock -> widths from the RIGHT ways -> junction pads reserved -> alignment smoothing
BETWEEN junctions only -> mesh/env -> full gate suite (incl. excursion sweeps).
