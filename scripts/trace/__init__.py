"""Route tracing — read a hand-drawn route off a map screenshot and snap it to exact OSM roads.

See skill: route-tracing. Pipeline: detect drawn pixels → georeference to lat/lon by aligning to the
real road network → map-match to OSM ways → emit the ordered road list for gps-extraction.
"""
