# How professional AC modders construct tracks (measured from shipped kn5s)

Dissected 2026-07-22 with our own parsers (triangle-exact) from Kevin's Dropbox training archive:
SRP beta (physical + terrain), the 2013 Colorado hillclimb (base + dedicated physics road),
rt_california_highway. SRP's vertex payload is obfuscated (structure-only facts quoted). We lack
each track's surfaces.ini, so physics attribution is name-inferred.

## The money finding — contact IS the pro standard

- **Colorado hillclimb**: grass NEVER underlaps the deck; the seam is a vertex-welded ring chain
  road → edge/rumble strip → grass (81% of grass boundary verts coincide within 1 mm with the
  adjoining strip), with dedicated transition materials (road_edge, grass_edge). Zero grass pokes
  >2 cm over the deck in 19.5k samples.
- **rt_california**: terrain boundary CONFORMED to the road edge — 15 mm median horizontal,
  0.0 mm median vertical; open sea-cliff drops are deliberate freeroam design, not seam errors.
- **Physics road vs visual road (colorado): |offset| median 0.1 mm, p95 0.6 mm** — the visual is
  a decimation of the physics surface, never a re-derivation.

Kevin's zero doctrine is exactly what the masters ship. Millimetres, not "under 1.8 m".

## Structural practices (with numbers)

1. **Multi-kn5 separation via models.ini**: invisible dense physics road kn5 (renderable=0,
   NULL.dds textures, lodOut=500) + visual base kn5 (+ tiny far-backdrop kn5 — SRP's whole Kanto
   skyline is 3 meshes / 15k tris).
2. **Physics density**: hillclimb physics road ~0.75 m grid (3.85 tris/m², 13× denser than its
   visual road); smooth highways get away with 4.6 m segments. Density carries bump truth.
3. **Terrain density is a gradient anchored to the road**: ~0.7 m at the verge → 1 m at 30 m →
   3.4 m at 100 m → 12–17 m beyond 300 m (rt_california). THE VERGE IS FINER THAN THE ROAD.
   (Our uniform 6 m grid is too coarse at the verge and wastefully fine 300 m out — our single
   biggest structural difference.)
4. **Chunk small, name unique**: physics chunks median 7–12k verts, max ~15k (colorado)/28.5k
   p90 (SRP); overflow pre-split as `_SUB0/1/2`; zero duplicate names in 312 meshes. Digit prefix
   reused as uniquifier (`1WALL…16WALL`).
5. **Semantic surface keys** (SRP): `1OVERLAP` for stacked/crossing decks, `1BRIDGE` (expansion
   joints), `1PIT` as tall zone volumes, `1LAPCUT`.
6. **Containment is a PERIMETER, not a lip rail**: colorado's 135 walls (1.4–3 m tall) sit
   median 17 m off the edge with run-off between — 97% of edge points have containment within
   40 m. rt_california contains by TERRAIN SHAPE (cut slopes) and deliberately leaves cliffs
   open. → our car-survival gate should demand containment-within-X, not wall-at-edge.
7. **ksMultilayer_fresnel_nm is the near-terrain (and road) workhorse** — RGBA mask + 4 tiling
   detail layers kills visible tiling; ksPerPixel is for props/collision only. Zero ksTree /
   ksGrass in these kn5s (vegetation shipped separately/CSP).
8. **Conservative ks values**: terrain spec 0.0; road spec 0.05–0.1 EXP 10–24; ambient≈diffuse
   0.25–0.5; walls 0.25/0.2/0.03.
9. **Flat hierarchy**, dummies as plain depth-1 nodes. Textures 2048² standard, 60–115 MB/kn5.

## Adoption list for our engine (post-extraction, with the construction selector)

- Edge-ring WELDED seams (shared verts road↔shoulder↔terrain boundary) instead of clearance-only.
- Road-anchored terrain density gradient (fine verge, coarse far) replacing the uniform grid.
- Physics/visual kn5 split; visuals decimated from the same audited surface.
- Containment-within-X gating semantics + perimeter wall placement (pairs with the guard-wall
  warrant rules in ROAD-CONSTRUCTION.md).
- ksMultilayer terrain material pass; `1OVERLAP` at the Lariat's stacked switchbacks.
