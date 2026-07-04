---
name: aerial-tracing
description: Build a track centerline from an aerial/satellite screenshot of a real circuit that has no GPS or OpenStreetMap data — private road courses, kart tracks, test loops. Use when the drivable surface itself is visible in the imagery (not a drawn line, not OSM roads). Traces the asphalt ribbon, georeferences it from two corner control points so it is elevation-correct, names it from the on-image labels, and emits the same data/centerline.geojson the GPS pipeline consumes.
---

# Aerial Tracing

Turns an **aerial photo of a track** into the same `data/centerline.geojson` that `gps-extraction`
produces — so elevation, projection and mesh building all run downstream unchanged. Reach for this
when the circuit **isn't in OpenStreetMap and has no GPS trace**: the asphalt is right there in the
satellite view, so we trace *it* directly instead of looking up roads.

How it differs from its siblings:

| Front-end | Source | What it traces |
|-----------|--------|----------------|
| `gps-extraction` | OSM/Overpass + KML | Real public road geometry by name |
| `route-tracing` | Map screenshot with a **drawn** line | The drawn line → matched OSM ways |
| **`aerial-tracing`** | **Aerial/satellite screenshot** | **The visible asphalt ribbon itself** |

## First principle (still holds)

> The screenshot is a selection aid, not data — *except* when there is no other data.

For a private circuit there is no Overpass geometry to defer to, so the imagery becomes the geometry.
We keep it honest by **georeferencing to real lon/lat** (two control points) rather than inventing a
coordinate frame: that real-world tie is exactly what lets the existing USGS 3DEP elevation phase
sample real ground under the line, making an aerial-sourced track **elevation-correct** with no survey.

## Pipeline

1. **Read** the screenshot dimensions — PNG (full stdlib decode) or JPEG (SOF header only). (`detect.py`)
2. **Trace** an ordered centerline in pixels, two ways:
   - **Auto (PNG):** isolate the asphalt by colour → largest connected blob → Zhang-Suen thinning to
     a 1-px skeleton → greedy nearest-neighbour walk into a polyline. (`detect.py`, `skeleton.py`)
   - **Manual (any image):** an ordered `source.trace_px` polyline (clicked along the centerline).
     Required for JPEG (no stdlib JPEG decoder) and the reliable fallback when auto-trace struggles
     on busy shots (crossovers, infield tracks, fences that read as grey).
3. **Georeference** pixels → lon/lat with a north-up affine pinned by **two corner control points**
   (`georef.py`). This is the step that makes elevation correct.
4. **Emit** `data/centerline.geojson` (resampled ~3 m, width-tagged, loop closed) with provenance —
   track name + nearby road names off the image — plus `aerial_overlay.svg` (trace drawn back onto
   the screenshot) and `centerline_preview.svg`. (`build_aerial.py`)

## Control points (the georeference)

Drop two Google Maps pins (`?q=lat,lon`) on recognisable **opposite corners** of the track and record
each one's pixel position in the screenshot. In `track.config.json`:

```json
"source": {
  "type": "aerial",
  "screenshot": "source/aerial.png",
  "nearby_roads": ["Summit Blvd"],
  "control_points": [
    { "px": [0.18, 0.14], "lonlat": [-104.9501, 40.04498], "_corner": "NW" },
    { "px": [0.86, 0.84], "lonlat": [-104.94607, 40.03976], "_corner": "SE" }
  ],
  "trace_px": [[0.84, 0.5], [0.80, 0.42], "... ordered centerline ..."]
}
```

`px` and `trace_px` accept **normalised [0,1] image fractions** (resolution-independent) or absolute
pixels. Corners must differ in both x and y. Omit `trace_px` to auto-trace a PNG.

## Naming "from the data on the image"

The on-image text *is* the metadata: the place label (e.g. "IMI Motorsports Complex") becomes
`name`, and a labelled street ("Summit Blvd") goes in `source.nearby_roads` and pins down the real
location (→ geocoded `location` for sun/lighting). These are recorded into the centerline's
provenance so the build is traceable back to the source picture.

## Inputs / outputs

- **In:** `source/<aerial>.{png,jpeg}` + two control points (+ `trace_px` for JPEG/manual).
- **Out:** `data/centerline.geojson` (the pipeline contract), `data/aerial_overlay.svg`,
  `data/centerline_preview.svg`.
- **Then:** run `elevation` → `geometry`/`lighting` → `ac` exactly as for a GPS-sourced track.

## Run

```bash
python -m scripts.aerial.build_aerial projects/<slug> [screenshot]
```

## Scripts

`scripts/aerial/` — `detect.py` (PNG/JPEG sizing + asphalt mask), `skeleton.py` (Zhang-Suen thinning +
loop ordering), `georef.py` (two-corner north-up affine), `build_aerial.py` (orchestrator).

## Status & limits

The **georeference → centerline.geojson seam is implemented and tested** end-to-end (both trace paths),
so the elevation/mesh back-half runs unchanged. **Auto-trace is best-effort** and PNG-only: tune the
`is_asphalt` colour test per source, and prefer a hand `trace_px` on cluttered aerials or when the
track self-crosses. Control-point `lonlat` are the only real-world input — get them from dropped pins;
estimated corners give a self-consistent but un-surveyed track (elevation will sample the wrong ground
until they're real). The screenshot is assumed **north-up** (Google Maps default); rotated captures
need de-rotating first. Verify `aerial_overlay.svg` before committing the trace.
