"""Aerial-screenshot front-end: a satellite/aerial view of a real track → centerline.geojson.

Unlike ``gps-extraction`` (which pulls real road geometry from OpenStreetMap) or ``route-tracing``
(which traces a *drawn* line and map-matches it to OSM ways), this front-end is for tracks that
**have no GPS/OSM data** — private circuits, kart tracks, test loops. The drivable surface itself is
visible in the imagery, so we trace the asphalt ribbon directly and georeference it to real lon/lat
using two control points. The output is the same ``data/centerline.geojson`` the rest of the
pipeline consumes, so elevation (USGS 3DEP), projection and mesh building all run unchanged — which
is what makes an aerial-sourced track *elevation-correct* despite having no survey data.
"""
