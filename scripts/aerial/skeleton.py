"""Reduce an asphalt ribbon mask to a 1-px medial line and order it into a centerline path.

Pure-stdlib computer vision, best-effort:

1. ``largest_component`` — keep the biggest 8-connected blob, dropping disconnected noise (parking
   lots, neighbouring buildings) the colour test also caught.
2. ``thin`` — Zhang-Suen thinning collapses the ribbon to a 1-pixel skeleton.
3. ``order_path`` — greedy nearest-neighbour walk turns the unordered skeleton pixels into a single
   ordered polyline (the centerline), in grid coordinates.

Auto-trace is inherently fragile on busy aerial shots (crossovers, infield tracks, fences read as
grey). When it misbehaves, hand the build an ordered ``source.trace_px`` instead — this module is the
convenience path, not the contract. The georef + elevation seam downstream does not depend on it.
"""

from __future__ import annotations

import math

Px = tuple[int, int]  # (x, y) in grid coordinates


def largest_component(mask: list[list[bool]]) -> list[list[bool]]:
    """Keep only the largest 8-connected component of ``mask`` (iterative flood fill)."""
    h = len(mask)
    w = len(mask[0]) if h else 0
    seen = [[False] * w for _ in range(h)]
    best: list[Px] = []
    for sy in range(h):
        for sx in range(w):
            if not mask[sy][sx] or seen[sy][sx]:
                continue
            stack = [(sx, sy)]
            seen[sy][sx] = True
            comp: list[Px] = []
            while stack:
                x, y = stack.pop()
                comp.append((x, y))
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nx, ny = x + dx, y + dy
                        if 0 <= nx < w and 0 <= ny < h and mask[ny][nx] and not seen[ny][nx]:
                            seen[ny][nx] = True
                            stack.append((nx, ny))
            if len(comp) > len(best):
                best = comp
    out = [[False] * w for _ in range(h)]
    for x, y in best:
        out[y][x] = True
    return out


# Zhang-Suen 8-neighbour order P2..P9 (clockwise from north), used by both sub-iterations.
_NB = [(0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1)]


def _transitions(vals: list[int]) -> int:
    """Number of 0→1 transitions in the circular neighbour sequence P2,P3,…,P9,P2."""
    return sum(1 for i in range(8) if vals[i] == 0 and vals[(i + 1) % 8] == 1)


def thin(mask: list[list[bool]]) -> list[Px]:
    """Zhang-Suen thinning → the set of 1-px skeleton pixels (order unspecified)."""
    h = len(mask)
    w = len(mask[0]) if h else 0
    img = [row[:] for row in mask]
    changed = True
    while changed:
        changed = False
        for step in (0, 1):
            to_clear: list[Px] = []
            for y in range(1, h - 1):
                for x in range(1, w - 1):
                    if not img[y][x]:
                        continue
                    n = [1 if img[y + dy][x + dx] else 0 for dx, dy in _NB]
                    b = sum(n)
                    if b < 2 or b > 6:
                        continue
                    if _transitions(n) != 1:
                        continue
                    # P2,P4,P6 / P4,P6,P8 (step 0) vs P2,P4,P8 / P2,P6,P8 (step 1).
                    if step == 0:
                        if n[0] and n[2] and n[4]:
                            continue
                        if n[2] and n[4] and n[6]:
                            continue
                    else:
                        if n[0] and n[2] and n[6]:
                            continue
                        if n[0] and n[4] and n[6]:
                            continue
                    to_clear.append((x, y))
            if to_clear:
                changed = True
                for x, y in to_clear:
                    img[y][x] = False
    return [(x, y) for y in range(h) for x in range(w) if img[y][x]]


def order_path(pixels: list[Px], max_gap: float = 6.0) -> list[Px]:
    """Greedy nearest-neighbour walk → one ordered polyline through the skeleton pixels.

    Starts at the lowest-then-leftmost pixel (a stable, deterministic corner) and repeatedly hops to
    the nearest unvisited pixel within ``max_gap``; stops when the trail runs out. Tracks that loop
    back come out as a near-closed ring; the caller decides whether to force-close it.
    """
    if len(pixels) < 2:
        return list(pixels)
    remaining = set(pixels)
    start = max(pixels, key=lambda p: (p[1], -p[0]))  # bottom-most, then left-most
    remaining.discard(start)
    path = [start]
    cur = start
    while remaining:
        cx, cy = cur
        nxt = min(remaining, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
        if math.hypot(nxt[0] - cx, nxt[1] - cy) > max_gap:
            break
        remaining.discard(nxt)
        path.append(nxt)
        cur = nxt
    return path


def trace_centerline(mask: list[list[bool]], max_gap: float = 6.0) -> list[Px]:
    """Mask → ordered centerline pixels: keep the main blob, thin it, walk it."""
    return order_path(thin(largest_component(mask)), max_gap=max_gap)
