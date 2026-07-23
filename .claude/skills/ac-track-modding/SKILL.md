---
name: ac-track-modding
description: Enforce Assetto Corsa conventions and run the headless Blender build to a kn5 + installable track folder. Use for AC surface naming (1ROAD_/1KERB_), required dummy objects (AC_START/AC_PIT/AC_TIME/AC_HOTLAP_START), material assignment via the AC Blender Tools addon, the headless kn5 export, and generating the track folder (models/surfaces/ui/map/ai). Documents the Windows-only steps it cannot own (AI recording + in-game QA).
---

# AC Track Modding

The build engine and the enforcer of Assetto Corsa–specific conventions.

## Surface naming (mesh object prefixes)

| Prefix | Meaning |
|--------|---------|
| `1ROAD_*` | Drivable **physical** surface, keyed to `surfaces.ini` |
| `1KERB_*` | Kerb geometry / material |
| *(no numeric prefix)* | **Visual-only** geometry (no physics) |

## Required dummy objects

- **Start / pit:** `AC_START_0..N`, `AC_PIT_0..N`
- **Timing:** `AC_TIME_0_L` / `AC_TIME_0_R` (start-finish), then `AC_TIME_1_L/R…` incremented per sector
- **Hotlap:** `AC_HOTLAP_START_0`

## Materials

`ksGrass` for grass; a road shader for `1ROAD_*` surfaces. Assigned via the **AC Blender Tools**
addon's `settings.json` (a material JSON drives the assignment).

## Headless export

```bash
blender --background --python scripts/ac/build_kn5.py -- <track-dir>
```

Requires Blender 4.x with the AC Blender Tools (kn5 export) addon.

## Track folder output

- `models_<layout>.ini` — references the shared kn5 (one model, many layouts)
- `data/surfaces.ini` — physical surface definitions (keyed to `1ROAD_*`)
- `ui/<layout>/ui_track.json` — track metadata per layout
- `map.png` + `data/map.ini` — minimap
- `ai/` — AI fast line per layout (recorded on Windows)
- `outline.png`, `preview.png` — UI imagery

> The kn5 is emitted as a **sibling** of the folder (`build/<slug>.kn5`), not inside it. The
> installer drops it into the installed folder, where `models_<layout>.ini` references it.

## Install (optional, phase 6)

`scripts/ac/install.py` copies a built track into a local AC `content/tracks`. It locates the folder
automatically — env (`AC_TRACKS_DIR`/`AC_ROOT`) → Steam (Windows registry + `libraryfolders.vdf`
across libraries, macOS, Linux/Proton/Flatpak) — and **prompts for the path if it can't find one**.
Each built track is an optional item: no argument → choose from all built tracks; an argument →
just that one. Flags: `--tracks-dir`, `--list`, `--yes`, `--force`, `--dry-run`, `--allow-missing-model`.

```bash
python -m scripts.ac.install                       # interactive: pick tracks + confirm dest
python -m scripts.ac.install projects/<slug> --yes
```

## THE ONE-SHOT NEW-ROUTE RECIPE (write the config right and the pipeline does the rest)

Goal: a NEW route (San Diego rebuilds, future tracks) reaches a drivable, dressed, gated build in
ONE pass. Everything below exists and is verified on Lookout Lariat 0.9.0 / Sand Creek 0.22.0 —
one-shotting is a CONFIG exercise, not a code exercise.

### Config template — every key that matters
```jsonc
{
  "slug": "...", "version": "0.1.0", "mirror_x": true,
  "archetype": "real_road | street_circuit | race_circuit",   // presets under explicit keys
  "route": {
    "roads": ["19th Street", "ref:US 6", "id:484287799"],      // freeway classes match ONLY if declared
    "width_profile": "mountain | urban"                        // mountain kills urban lane floors
  },
  "capture": {"bridges": [{"name": "...", "center_m": 2485, "len_m": 100}]},  // declare every real bridge;
      // bare-earth DEMs DELETE decks — undeclared crossings auto-detect but declared is exact
  "texture_overrides": {"1GRASS": {"diffuse": "assets/textures/grass_diffuse.jpg", "normal": "..."}},
      // config-level overrides BEAT capture textures (the human's word wins)
  "lighting": {"streetlight": {"intensity": 0.35, "spacing_m": 96}},   // Kevin taste: dim wide pools
  "props": {"concrete_barriers": true},
  "scenery": {
    "tree_style": "conifer", "fill_terrain": true, "tree_cap": 40000,
    "fill_per_station": 6, "fill_range_m": 220,
    "forest3d": {"module": "pine|poplar", "spacing_m": 24, "off_min_m": 7, "off_max_m": 26,
                 "cap": 700, "scale_min": 2.2, "scale_max": 4.2},
    "road_signs": {"enabled": true},                            // MUTCD diamonds at turn entries
    "fences": [ {"start_m": 0, "end_m": 999, "side": 1, "offset_m": 8} ]   // right-of-way lines
  }
}
```

### What the pipeline now guarantees (don't re-solve these)
- **Widths**: OSM lanes tag wins; undeclared freeways banned; junction + distinct-identity corner
  flares; **U-turn flare** (route folds >110° → 16 m, no identity gate) — the "impossible corner"
  class. Verify numerically (per-street table + widths diff), never from code alone.
- **Contact**: corridor 3DEP splice → contact bend → clamps → sink → **grid contact pin** (two-
  sided, window-min, bridge-exempt) → welded edge rings sharing the shoulder's meet vertices →
  fill bench + 2:1 faces. Flat tracks measure 0.000/0.000/0.000; mountains ~0.5 m median hidden
  under-slab. Construction selector: cut 0.75:1 / fill 2:1 / wall 1:6 batter + RETWALL/ROCKCUT
  skins / stone parapets (hysteresis + gap-merge runs — never per-station warrants: tombstones).
- **Furniture doctrine**: concrete barriers ONLY where a fast approach (last 300 m near-straight)
  meets a ≥70° corner or a blind-crest corner — danger showcases. Everything else warning-worthy
  gets WOODEN FENCES ~3 m off the shoulder (instance_line, max_dy 3.5 so panels never hang over
  banks). MUTCD warning diamonds at every warning-turn entry, direction+severity cell.
- **Starts**: the start complex auto-relocates to the straightest flattest 260 m window; spawn
  yaw derives at the dummy. Never assume pts[0].
- **Packaging**: single-layout tracks ship suffix-FREE (models.ini, root ui/ai/map) or AC hides
  the minimap; map.png/outline.png are PIL-drawn TRUE-alpha (qlmanage flattens SVG alpha to
  opaque white); preview.png = cached-OSM street map with the route drawn (widths stage caches
  ways to data/osm.ways.json).
- **Gates before any zip**: drive test (excursions, soft-top, severe counters-first), audit
  (footprint-exact, steep-tri-aware), kn5 verify (footprint-exact poke), fidelity (weld, double-
  sheet 0, per-class 0% hover, deck-vs-mountain), hash-verified zips, new tag per build.

### The corner doctrine (a full day of builds — do NOT relearn)
- A swept ribbon CANNOT carry junction-width pavement through a fold: every auto-widened fold
  ships pleated fan steps. Fold corners are geometrically IDENTICAL to switchbacks (both ~12 m
  apex radius) — no heuristic separates them; the difference is driver judgment.
- Therefore: auto treatment only for EXTREME folds (>110 deg over ±15 m): a MODEST 10 m floor
  (taper-aware, 18 m hold) + a local ±40 m plateau. Driver-flagged corners are DECLARED in
  `route.wide_corners: [{station_m, width_m}]` and additionally get a paved DISC.
- Discs only on FLAT fold zones (<2.5% across ±60 m): rims step against camber on graded folds.
- Complexes: adjacent kinks <150 m apart cluster; near-coplanar crossing legs (Δy<=2.5 m,
  valley junctions) level to ONE plane over their full adjacency span; stacked stairs keep
  per-kink local bowls (flattening a stair dumps its climb into the blends).
- Gates: fold >110 deg needs CORE width >= 9 (max within ±20 m — the line uses the core, the
  taper edge is not the corner). Severe/bump steps at |off|>3.5 on >=9.5 m pavement are APRON
  texture (reported separately, not gated) — real aprons have rolled crossfall breaks; mid-lane
  gating stays absolute. Furniture near flares: span-max widths, pavement-reject (fences skip,
  barriers WALK OUTBOARD), 4.2 m fence offsets.

### Hard-won placement rules (each cost a build cycle — bake into any new pass)
- **Trees**: clearance is canopy-aware (`off += canopy_half x scale`) and checked against
  EVERY nearby leg within the height layer, not just the leg that placed the tree
  (switchback stacks). 3D near-corridor, billboards for mass.
- **U-turn folds** (>110° within ±15 m): plateau the profile ±40 m (smoothstep) — the swept
  ribbon self-overlaps there and grade x arc across the fold is a REAL mid-corner step
  (~0.5-1 m) that gates as severe steps and eats cars.
- **Fence/rail audits**: a rail member between posts has no base verts in its own audit
  column — checks need the grounded-post-nearby (arm exclusion) rule or healthy fences
  read as floating walls.
- **Panel seating**: short line-instanced modules seat on the footprint MIN (center+ends),
  never the center point (banks fly the downhill end); long modules (pylons) keep center.
- **Sidewalks follow the street's line**: rolling-median width (~30 m), max'd with the real
  width — never trace flare jitter (zigzag walks).

### Asset conversion pattern (Dropbox drops → engine)
Headless Blender 4.2: import (.blend/.fbx/.glb) → drop render-scene planes (huge flat meshes) →
join → separate by MATERIAL → decimate to instancer weight (trees ≤2.5k, lamps ≤2.5k, props
≤1k) → **delete loose verts** (Decimate leaves the collapsed originals as points — one pine
shipped 91k orphan verts x 649 instances) → selected-only OBJ export → **verify the FILE's
`grep -c '^v '` count before staging** → PoT textures (bake opacity into diffuse alpha; fold emissive
in) → assets/models + assets/textures + a pbr TEXTURES entry. Specific prefixes (1WALL_WOODF,
1WALL_PARA) must precede generic ones (1WALL) — the prefix match is dict-ordered. Maya .mb is
unusable — ask for FBX/OBJ.

## Scope boundary

Documents what it **can't own**: **AI line recording and in-game testing require the Windows GUI**
(AC + Content Manager + CSP). Phases 1–5 are code-driven here; phase 6 is the manual loop.

## Scripts

`scripts/ac/` — `build_kn5.py` (headless Blender entry), `materials.py` (material JSON / addon settings),
`track_folder.py` (emit models/surfaces/ui/map/ai layout files), `install.py` (optional install into
a local AC `content/tracks`, with auto-detect + prompt).
