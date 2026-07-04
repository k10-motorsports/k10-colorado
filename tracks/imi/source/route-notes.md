# IMI Motorsports Complex — route notes

**Source:** aerial screenshot (`imi-aerial.jpeg`), Google Maps satellite view.
**Why aerial:** this is a private road course — no OpenStreetMap drivable geometry, no GPS trace.
The asphalt ribbon is visible in the imagery, so it's traced directly (`aerial-tracing` skill).

## Read off the image
- **Track name:** "IMI Motorsports Complex" (bottom label) → `name`.
- **Nearby road:** "Summit Blvd" (top label) → `source.nearby_roads`. With the name this geolocates to
  5074 Summit Blvd, Dacono, CO 80514 (~40.0423, -104.9482) → `location` (sun/lighting only).
- The grey serpentine **asphalt road course** is the target surface. The surrounding tan/dirt loops
  (motocross, off-road, quarter-midget) and the truck/RV lots are NOT part of this track.

## Georeference (must be done for elevation accuracy)
`source.control_points` currently hold **estimated** NW/SE corner lon/lat (published address at an
assumed ~0.42 m/px). For a survey-accurate, elevation-correct build, replace each `lonlat` with a
**dropped Google Maps pin** (`?q=lat,lon`) on the matching corner of the track.

## Trace
`source.trace_px` is an approximate hand-trace of the road-course centerline (normalised coords) so
the front-end builds end to end. To improve it: convert the screenshot to PNG and let
`build_aerial` auto-trace the asphalt mask, or click the centerline more densely.

## Build
```bash
python -m scripts.aerial.build_aerial projects/imi-motorsports   # → data/centerline.geojson
# then the standard back-half:
python -m scripts.elevation.heightfield projects/imi-motorsports
python -m scripts.geometry.projection  projects/imi-motorsports
# … geometry / lighting / ac as usual
```
