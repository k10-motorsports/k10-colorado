# Second Creek Raceway — sourcing notes

The circuit closed in the 2000s and the site was redeveloped, so it is **not in OpenStreetMap**
(verified against Overpass 2026-07-26: only "Second Creek Trail", a footpath, and the creek itself).
Its geometry therefore has to come from the two sources in this directory, not from OSM.

## What we have

| File | What it gives |
|---|---|
| `second_creek_layout_map.gif` (804×500) | The circuit SHAPE, with real corner names — Owl Turn, The Pipeline, Spectator Hook, Bonzai Straight, "Airborne", 88th Dog Leg, Kamakazi Turn, The Shoot — over contour lines. States **Length – 1.7 miles** (2736 m) and shows Buckley Road, the paddock, staging and parking. |
| `second_creek_site_aerial_2026.png` (1206×2622) | The site TODAY at Buckley Rd & E 96th Ave. The old track's scar is still visible in the ground. Anchored by streets that ARE in OSM. |

## Georeferencing anchors (from Overpass, tight bbox 39.848–39.866 / −104.800–−104.778)

| Feature | Real position |
|---|---|
| North Buckley Road | lon **−104.7909** (N–S), lat 39.84147–39.87060 |
| East 88th Avenue | lat **39.85640** (E–W), lon −104.79085–−104.77795 |
| Pitkin Street | lon **−104.7866** (N–S), lat 39.86035–39.87025 |

Buckley → Pitkin is 0.0043° lon ≈ **367 m**, which sets the aerial's scale at roughly **0.35 m/px**
(Buckley at x≈60, Pitkin at x≈1100), so the visible field is about **425 m E–W × 925 m N–S**.

## Progress so far

The layout map traces cleanly. Thresholding at <100 grey gives a binary image; the track line is
thick where the contour lines are 1–2 px, so **two erosions isolate it** — the largest connected
component is 24,883 px spanning 721×312, and every other component is under 50 px (label
fragments). Zhang-Suen thinning yields 2,612 skeleton pixels.

Two things still to solve:

1. **The skeleton is a graph, not a loop.** `order_path` walked only 786 of the 2,612 pixels,
   because the map draws the alternate configurations (the inner loops sharing sections with the
   full course). This needs the same cycle-selection used for High Plains: enumerate closed cycles
   and pick the one matching the stated length. **1.7 miles = 2736 m is the validator** — a huge
   advantage over guessing.

2. **The orientation conflicts and must be resolved by fitting, not by reading the map.**
   - The schematic puts Buckley Road *vertical on its right edge*, with the track to its **left**.
   - Reality has the track **east** of Buckley Road: OSM shows the Rocky Mountain Arsenal refuge
     perimeter immediately **west** of it (lon −104.808 to −104.791), so there is no racetrack on
     that side.
   - The traced shape is **wide and short** (387×189 px, 2:1) while the real envelope is **tall and
     narrow** (~425 × 925 m), so the map is rotated roughly 90° from north-up.
   - Encouragingly, fitting the long axis to N–S gives 800/387 ≈ **2.07 m/px** and the short axis to
     E–W gives 400/189 ≈ **2.1 m/px** — consistent, which is a good sign the two sources describe
     the same place at the same scale.

   **Kevin confirms the track was EAST of Buckley Road** (2026-07-27), which settles the conflict:
   the schematic's left/right is wrong, OSM's refuge-to-the-west reasoning is right. This removes
   the mirroring ambiguity — any candidate transform placing the circuit west of lon −104.7909 is
   rejected outright, leaving orientation (4 rotations) as the only free parameter.

   So: trace the scar in the aerial (that is the ground truth for position and orientation), then fit
   the schematic shape to it over the four rotations, and accept the transform that puts the circuit
   east of Buckley, matches the scar, and reproduces 2736 m. Do **not** pick an orientation by reading
   the map's labels — they disagree with OSM and with Kevin.

## Why this matters

Every metre of this track's geometry will come from a hand-fitted trace rather than a survey, so the
fit has to be validated against something independent. There are two independent checks available —
the stated 1.7-mile length and the visible scar's position between real streets — and both must pass
before this ships as a real port (L1).


---

## Re-source attempt, 2026-07-27 — what was actually done, and what was NOT

### The aerial scar is not traceable

Examined at full resolution, autocontrast+unsharp, and in two 1.5x crops. The site has been
regraded: what is visible is drainage swales, graded pads, stockpiles and active construction
(the area immediately north of 88th is aconstruction  site). **No closed 2.7 km loop can be distinguished
from grading contours.** The plan above — "trace the scar in the aerial (that is the ground truth
for position and orientation)" — cannot be executed against this image. Do not spend more time on
it without a better/older aerial (pre-redevelopment imagery would work).

### A correction to the reasoning above

The claim that "the real envelope is tall and narrow (~425 x 925 m)" and therefore "the map is
rotated roughly 90 degrees from north-up" is **wrong**. 425 x 925 m is the aerial IMAGE at
0.353 m/px (1206 x 2622 px) — it is a phone screenshot's viewport at its zoom level, not a
property boundary. The screenshot is titled *Buckley Rd & E 96th Ave*; the land east of Buckley
runs toward 96th, about a mile of open ground. Nothing constrains the site to 425 m wide, so the
traced shape being 2:1 wide (1032 x 415 m) is not evidence of a rotation.

**The rotation question is therefore open, not settled** — in either direction.

### An orientation clue the notes missed

The layout map names a corner **"88TH DOG LEG"**. Corners are named for what they sit on, so the
circuit ran along or touched E 88th Avenue (lat 39.85640). That is an independent anchor from the
source material itself, and it agrees with a west-east layout hugging 88th on its south side.

### What was actually changed

A **translation only** — no rotation, no rescaling, so the traced shape and its length are
untouched (2741 m before and after, against the map's stated 1.7 mi = 2736 m).

| | before | after |
|---|---|---|
| lon | -104.79689 .. -104.78483 (straddling Buckley) | -104.79043 .. -104.77838 |
| lat | 39.85491 .. 39.85865 (straddling 88th) | 39.85640 .. 39.86014 |

+552 m east, +165 m north: west edge to Buckley + 40 m (the paddock/access strip the layout map
draws along the road), south edge onto 88th Ave (the 88th Dog Leg).

### Fidelity — read this before calling it a port

Position is **fitted to two OSM street anchors plus Kevin's confirmation that the circuit was east
of Buckley**. It is not surveyed, and it is not traced from the ground. The shape and length come
from the layout map and are good; where that shape sits along 88th, and its rotation, carry real
uncertainty. Registry fidelity must say so.
