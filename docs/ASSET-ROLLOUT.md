# Asset rollout plan — Dropbox /Blender drop (2026-07-23)

The #17 final-polish pass implements this after Kevin validates rc9. Upload complete; every
archive unpacked and inventoried (scratchpad/drop/x/ this session).

## Unpacked findings (what's actually inside)

- **`pine_tree.zip`** → `Small_Pine.blend` + `Bark.jpg`/`PinesLeaves.png`. Blend-only →
  headless Blender OBJ export needed (we have vendored 4.2). One species; instance with
  scale/rotation jitter for variety.
- **`tree_topol.rar`** → `tree_topol.fbx` + `tree_poplar.glb` + full PBR texture set
  (bark/branches, TexturesCom). FBX→OBJ via headless Blender.
- **`wooden_fence_mt1.zip`** → THE fence standard: **5 modular OBJs** (`wood_fance_m0–m4_t1`,
  112 KB total geometry!) + texture set. Lightweight, modular, instance-ready — exactly our
  `instance_line` shape. The 215 MB is textures/renders.
- **`Wooden_Fence_Geo_Piskas`** → `wooden_fence_with/without_proxy.obj` — second style (garden
  picket); Sand Creek residential edges if wanted; otherwise spare.
- **`Fence.FBX` + `Textures.rar`** → chain-link/site fence with full PBR (Fence01/02 sets) +
  StopSign PBR sets matching `barrier_stopSign01/02.obj`. FBX→OBJ conversion.
- **`Street_Lamp.zip`** → `Street_Lamp.blend` + full PBR **including `lamp_emiss.png` and
  `lamp_opac.png`** — a real luminaire with an emissive map: replaces the mast-bridged lamp
  outright; LAMPHEAD emissive comes from the actual texture instead of a material hack.
- **`Urban_Props_Pack1.zip`** → 10 ready OBJs (small): bench, dustbin, billboard, electric
  pole, mailbox, road barrier, street light (alt lamp), traffic cone, ATM; Bus Stop as
  glb/blend. Blender-procedural materials → bake/replace with simple textures at conversion.
- **`barriers_MA.rar`** → Maya `.mb` only — **unusable** without Maya. Spare.
- **`textures.zip`** → a 3ds-MAX architecture texture library (roof tiles etc.) — reference
  archive, no direct consumer.
- **Concrete Barrier and Transition Barrier folders + `Barrier_end01/02` + `Barrier_circle` +
  `Barrier02` + `Highway_OverheadSign` + stop-sign barriers** — OBJs ready to convert.

**Conversion pipeline** (one headless Blender pass, vendored 4.2): blend/fbx/glb → OBJ →
decimate where heavy → PoT textures → `assets/models/` + `assets/textures/` + pbr entries.
`.mb` excluded.

## Inventory → placement

### Vegetation (the headline: REAL forest density)
| Asset | Where | How |
|---|---|---|
| `pine_tree.zip` (93 MB, 3D pines) | **Lariat, everywhere** | 3D instances within ~60 m of the road (the corridor the driver reads), existing conifer billboards beyond. The real mountain is ponderosa/lodgepole forest — density goes up 4-6× from today. |
| `tree_topol.rar` (229 MB, poplars) | **Sand Creek** creek corridor + street edges | Riparian band along the real Sand Creek channel (the creek the bridges cross), scattered lots/verges. Poplars/cottonwoods are exactly what lines a Commerce City drainage. |

**Density model (Lariat)**: aspect-aware — north/east faces denser (real Front Range pattern),
thinning near ridgelines; hard clear-zone at the drive-test corridor margin so trunks never
enter the obstruction band; density feathers OUT from the road so the forest reads continuous
at speed. Budget: 3D pines ~3-5k instances (near-corridor), billboards 40-60k (slopes).
**Density (Sand Creek)**: modest — industrial Commerce City is sparse; poplar rows at the creek,
occasional street trees on 46th/Dahlia verges.

### Fences (Kevin: "I'd like fences added too")
| Asset | Where |
|---|---|
| `wooden_fence_mt1.zip` / `Wooden_Fence_Geo_Piskas_new_obj.rar` / `FENCE.rar` / `Fence.FBX` | **Lariat**: right-of-way lines along the lower meadows and Windy Saddle pastures (replaces/augments ranch_fence ranges); short guard runs at overlook parking mouths. **Sand Creek**: industrial yard perimeters (upgrade from procedural chain-link where the aerial shows wood/post fencing). |

Pick ONE wooden fence family as the standard (evaluate mesh weight first; the 215 MB pack is
likely the textured hero — decimate for AC), keep the others as spares. Instance with the
existing `instance_line` rails: draped to final ground, layer-window guarded.

### Barriers (fixes "runs just stop" + street-circuit dressing)
| Asset | Where |
|---|---|
| `Concrete Barrier and Transition Barrier` (+OBJ folder) | Both tracks: REPLACE the plain module at run ENDS with the transition piece — real runs taper into the ground, ours currently just stop. |
| `Barrier_end01/02.obj` | End caps for every 1WALL run (Sand Creek racing line, Lariat guard runs). |
| `Barrier_circle.obj` | Sand Creek paddock/start compound dressing. |
| `barrier_stopSign01/02.obj`, `RoadBlockade_02.blend` | **Sand Creek**: street-circuit closure furniture at every side-street mouth the circuit seals off (the Long-Beach look: the city closed the road for race day). |
| `barriers_MA.rar`, `Road_Barrier.blend` + texture | Evaluate; likely spare variants. |

### Street furniture / highway
| Asset | Where |
|---|---|
| `Street_Lamp.zip` | **Evaluate as replacement for our mast-bridged lamp** on both tracks — a purpose-modeled lamp beats a procedurally repaired one. Keep alternating-sides spacing + halved intensity + LAMPHEAD split pipeline. |
| `black_lamp_spotII_01.blend` | **Lariat**: Buffalo Bill summit area + overlook points — the 1913 Denver Mountain Parks aesthetic wants ornamental fixtures, not highway cobras. |
| `Highway_OverheadSign.obj` | **Lariat**: US-6 canyon + I-70/US-40 junction area gantries. Also feeds the San Diego network tracks after #16. |
| `Highway_modular_set.blend` | Mostly a **San Diego** asset (post-#16); on CO maybe US-6 median/rail segments. |
| `Pylone.blend` | Replaces/augments the existing pylon run module (plains crossings). |
| `Urban_Props_Pack1.zip` (1.1 GB!) | **Sand Creek** industrial dressing: loading docks, dumpsters, pallets, drums, hydrants along 45th/46th/Dahlia frontages. Cherry-pick ~10-15 props, decimate, atlas — never ship the whole gigabyte. |
| `textures.zip` / `Textures.rar` | Support sets for the above — extract on demand. |

## The other polish-pass items (same build)

1. **Stone walls read as tombstones** — the parapet warrant flickers station-to-station, so
   ≥5-station runs come out as short separated boxes. Fix: hysteresis on the drop warrant
   (enter >2.0 m, exit <1.5 m), merge runs separated by <10 m, raise min run to ~10 stations,
   smooth the cap line along the run, tile the stone UV by arc. Same treatment for RETWALL
   skins (60 verts today = fragments; they should read as continuous masonry under the parapet).
2. **Sand Creek gets the Lariat's road + grass textures** — texture_overrides in SC config
   pointing 1ROAD/1GRASS at the Lariat set (kn5-stage change only, no geometry).
3. **Trees**: density model above; verify drive-test obstruction stays 0 and the corridor
   margin holds.
4. Remaining #17 tail: physics/visual kn5 split + ksMultilayer terrain material (decide after
   the asset pass — the split matters more once tree density raises draw cost).

## Sequencing (Kevin's words)
rc9 validation lap → **this polish pass finishes #17** → #15 (centralize engine into
prodrive-ac-builder) → #16 (San Diego migration, subagent). The Dropbox assets migrate into
`assets/models/` + `assets/textures/` via the usual decimate → PoT-texture → pbr-entry path,
and the selection lands in the central engine during #15 so San Diego inherits it.
