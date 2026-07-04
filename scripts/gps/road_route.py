"""Route ordered waypoints along the real OSM road network.

Snap each waypoint to the nearest road node, then Dijkstra the shortest on-road path between
consecutive waypoints — so a sequence of junction pins becomes a path that follows pavement instead
of cutting straight across. Used to turn KML interior-junction points into a road-true connector.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict

from scripts.trace import osm

Vertex = tuple[float, float]


def _haversine(a: Vertex, b: Vertex) -> float:
    r = 6_371_000.0
    (lo1, la1), (lo2, la2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _build_graph(ways) -> dict:
    g: dict = defaultdict(list)
    def snap(p):
        return (round(p[0], 6), round(p[1], 6))
    for w in ways:
        geom = w["geom"]
        for i in range(len(geom) - 1):
            a, b = snap(geom[i]), snap(geom[i + 1])
            if a == b:
                continue
            d = _haversine(geom[i], geom[i + 1])
            g[a].append((b, d))
            g[b].append((a, d))
    return g


def _dijkstra(g: dict, s: Vertex, t: Vertex) -> list[Vertex]:
    dist = {s: 0.0}; prev: dict = {}; pq = [(0.0, s)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == t:
            break
        if d > dist.get(u, 9e18):
            continue
        for v, w in g[u]:
            nd = d + w
            if nd < dist.get(v, 9e18):
                dist[v] = nd; prev[v] = u; heapq.heappush(pq, (nd, v))
    if t not in dist:
        return [s, t]  # disconnected → straight bridge
    path = [t]
    while path[-1] != s:
        path.append(prev[path[-1]])
    path.reverse()
    return path


def route_waypoints(waypoints: list[Vertex], *, margin_m: float = 350.0) -> list[Vertex]:
    """Return an on-road path through ``waypoints`` (lon, lat), in order. Fetches OSM for their bbox."""
    if len(waypoints) < 2:
        return list(waypoints)
    lons = [p[0] for p in waypoints]; lats = [p[1] for p in waypoints]
    midlat = (min(lats) + max(lats)) / 2
    dlat = margin_m / 111_132.0
    dlon = margin_m / (111_132.0 * math.cos(math.radians(midlat)))
    bbox = (min(lats) - dlat, min(lons) - dlon, max(lats) + dlat, max(lons) + dlon)
    g = _build_graph(osm.fetch_drivable(bbox))
    nodes = list(g)
    if not nodes:
        return list(waypoints)
    snapped = [min(nodes, key=lambda n: _haversine(n, p)) for p in waypoints]
    out: list[Vertex] = []
    for i in range(len(snapped) - 1):
        seg = _dijkstra(g, snapped[i], snapped[i + 1])
        out += seg if not out else seg[1:]
    return out
