# K10 Colorado — track project

Standalone Assetto Corsa builds of Colorado's real race circuits, done **carefully, one track at a time**.
No connecting roads, no unified "cruise" map — just the separated tracks, each accurate to its real
counterpart. First set: **Sand Creek, IMI, High Plains, Aspen (Woody Creek), PPIR (oval + infield),
Second Creek**.

---

## How we work (the pivot)

**Kevin is the designer. Claude is the Blender operator. We build live in Blender, together, before
touching kn5.** The old approach generated finished meshes headless and shipped a kn5 to test blind —
that hid problems until they were baked in. Now:

1. **Seed** a Blender scene from real data — the road ribbon + curbs pinned to real elevation (scripts do
   this deterministic part).
2. **Refine live** — Kevin looks at it in Blender and directs; Claude drives Blender over the live bridge
   (see below), shaping geometry, curbs, camber, kerbs, run-off, objects, materials.
3. **Only then export kn5** and test in-game.

### The live bridge

`blender/live_server.py` runs inside Kevin's Blender and opens a localhost socket. Claude sends Python to
it with `blender/live_client.py` and reads the result — so Claude can inspect and edit the *same* scene
Kevin is looking at.

- Kevin: in Blender, **Text Editor → open `blender/live_server.py` → Run Script** (or install it as an
  add-on). It prints `[k10-live] listening on 127.0.0.1:9761`.
- Claude: `python3 blender/live_client.py '<python>'` — `bpy` is in scope, globals persist between calls,
  set `_ = <value>` to return something. Always **read state before mutating**, and describe the change
  before making it if it's hard to undo.

---

## Non-negotiable lessons (carried over — do not relearn these the hard way)

> **The screenshot is a selection aid, not data.** Geometry comes from real sources — OpenStreetMap via
> Overpass for public roads, hand-traced **asphalt off georeferenced aerials** for the circuits. A map
> image tells us *which* asphalt to grab; it is never traced for coordinates.

> **Roads and curbs stay tight to their REAL elevation.** Sample real terrain (USGS 3DEP) along the actual
> road, then **median-despike + smooth + grade-cap by actual segment distance** so the real rolls survive
> but nothing launches or floats. The ground **conforms to the road**, not the other way around — the deck
> never hovers over the grass and the grass never buries the deck. Every past disaster (roads in the sky,
> 60 m cliffs, grass over the track) traces back to breaking one of these.

> **Curbs are tight to the road edge and to the deck height.** 1KERB hugs the 1ROAD edge at the deck's
> elevation; no floating lips, no gaps.

> **Real banking/camber, researched per track.** e.g. PPIR oval = 10° in all four turns; High Plains runs
> 1.5–4% with a deliberately off-camber Turn 1. Look it up; don't guess.

> **Conform passes never cross layers.** Any pass that seats/clamps/grades something "to the nearby
> surface" (grass clamp, embankment grading, shoulder sink, barrier/prop seating) MUST reject reference
> points more than a layer-gap (~20 m; ~4 m for a shoulder vs its own deck) from its own height. On
> stacked switchbacks XZ-proximity alone grabs the WRONG leg — that one omission, in four passes at
> once, shipped the "rainbow road" build (road/shoulder/ground on different layers, fences in the sky).
> And gate the SHIPPED kn5 with `scripts/ac/kn5_ground_check.py` (fidelity vs OBJs + double-sheet
> terrain + per-class base seating) — OBJ-stage audits that validate props against the same data that
> placed them are structurally blind.

---

## Repo layout

```
k10-colorado/
├── CLAUDE.md                 # this file
├── blender/
│   ├── live_server.py        # run in Blender → opens the operator socket
│   └── live_client.py        # Claude sends Python to Blender with this
├── scripts/                  # the deterministic SEED + export pipeline (loop/aerial only, NO network)
│   ├── gps/ trace/           # real-road mapping: KML parse, Overpass, OSM routing, resample
│   ├── aerial/               # trace asphalt off a georeferenced aerial → centerline
│   ├── elevation/            # USGS 3DEP sample + heightfield + smoothing
│   ├── geometry/             # projection, road ribbon (build_mesh), curbs, profile, dummies
│   ├── environment/          # ground + dressing seed (build_env)
│   ├── lighting/             # solar position, CSP/Sol config
│   ├── ac/                   # kn5 build/export (Blender 4.2 add-on), track folder, verify
│   └── capture/              # real-world capture (phone elevation/texture) helpers
└── tracks/<slug>/
    ├── track.config.json     # source of truth for the track
    ├── source/               # real KML / aerial / capture / textures / notes
    └── data/                 # centerline.geojson + real elevation (derived; safe to rebuild)
```

## Conventions (AC, unchanged)

- **Units: metres. Up axis: Y-up** (AC/kn5). Convert once at the projection boundary.
- **Origin** per track (centroid of its centerline); **true north** oriented so sun shadows align.
- Surface prefixes: `1ROAD_*` drivable, `1KERB_*` kerbs, unprefixed = visual-only. Dummies: `AC_START_*`,
  `AC_PIT_*`, `AC_TIME_*_L/R`, `AC_HOTLAP_START_0`. Materials via the AC Blender Tools add-on.
- **Elevation priority:** USGS 3DEP 1 m (US) → OpenTopography → SRTM. Smooth along the racing line.
- **kn5 export** needs **Blender 4.2** + the vendored AC Tools add-on (`scripts/bootstrap_blender.sh`
  pins it). The live-design Blender can be any 4.x; the kn5 pass runs in 4.2.

## Per-track status

| Track | Source | Location fidelity |
|-------|--------|-------------------|
| Sand Creek | real KML + capture + textures | **real** (Commerce City) |
| Lookout Lariat | OSM route (named + `ref:` + `id:` roads) | **real** (Golden foothills, 25 km mountain loop) |
| IMI | aerial trace | **real** (Dacono) |
| High Plains, Aspen, PPIR oval/infield, Second Creek | rough KML traces | ⚠ **fabricated compressed lat/lon** from the old unified map — **re-source to the real location + real aerial + real elevation** as we get to each one |

**No connectors.** These are separate tracks; we are deliberately not joining them.
