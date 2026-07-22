# Engine migration map — consolidating into prodrive-ac-builder

Produced by read-only recon across k10-colorado (CO), k10-san-diego (SD) and prodrive-ac-builder
(PAB), 2026-07-22. Execution gated on task #14 (Kevin's lap validates the flush-world build), then
task #15 (centralize) and #16 (SD migration subagent). Do not start earlier — porting a moving
target is how SD's copies went stale twice.

## Lineage (the key finding)

One codebase, three evolutionary stages, TWO incompatible `build_kn5` pipelines:

- **SD** — oldest fork, **Blender-first**: `project/loop.blend` is source of truth; its
  `build_kn5.py` preps an existing .blend (AC_NAME rename map). Unique network + freeway-merge
  tooling. Its stable AC modules are byte-identical to PAB.
- **PAB** — the intended engine, currently behind CO. OBJ-import pipeline; has network/scs/game/
  modkit breadth CO lacks; the ONLY repo with tests (24). No git tags yet — pinning impossible
  until one is cut.
- **CO** — most evolved. Ahead of PAB on every shared engine file + native kn5 writer + the whole
  gate suite.

Consolidation direction: PAB absorbs CO's engine hardening, keeps its own breadth; SD's
Blender-first path becomes an alternate front-end (`build_kn5_loop.py`).

## Who wins per shared file

| File | Winner | Delta |
|---|---|---|
| ac/build_kn5.py | CO | over-cap FATAL, ksTree veg; SD's is a DIFFERENT PROGRAM (loop prep) — split into `build_kn5_obj.py` + `build_kn5_loop.py`, never merge |
| ac/export_kn5_addon.py | CO | KS_PROPS shader VARs into persistence INI |
| ac/kn5_write.py | CO-only | native OBJ→kn5 writer (`KN5_WRITER=native`) |
| ac/kn5_ground_check.py | CO | PAB has NO fidelity gate at all — top priority port; SD's is a stale hand-port |
| ac/verify_kn5.py | CO | terrain-poke on shipped binary; PIT_ROAD_MAX 13.5; double-sided-shoulder allowance |
| ac/ext_config.py | CO | occluders, texture_overrides, per-lamp lights, night damping (PAB even has the occluder TEST without the feature) |
| ac/pbr.py | CO | KS_PROPS, solid textures (black-material fix), texture_overrides; keep PAB's 1WALL→building for network tracks behind a flag |
| ac/track_folder.py | CO | ui_series, [build version] stamp; RE-ADD the WALL surface entry conditionally for network tracks |
| ac/{install,materials,credits,kn5_native}.py | identical ×3 | move verbatim |

CO-only engine files to add: kn5_ground_check, kn5_write, geometry/drive_test (excursion sweeps),
gps/widths_from_osm, elevation/crossfall, environment/props + make_foliage, ac/render_drive_views.

## Assets

- Engine: solid_*.png, line_*.png (procedural fixes), plus the default style library
  (conifer_atlas, eurosign_atlas, concrete_barrier_*, ranch_fence_*) and assets/models/ (Kevin's
  OBJ pack: barrier, lamp post+lens, pylon, ranch fence) — PAB needs the models dir created.
- Projects: captured real-world textures, KMLs, loop.blend, per-track data — never engine.

## Wrapper contract projects must keep working

Stages `gps→elev→project→[widths]→mesh→env→[drive]→[audit]→(blend|native)→verify→fidelity→pack`;
module entrypoints `python -m scripts.<pkg>.<mod> <PROJ>`; config keys incl. slug/version/
osm_road_widths at wrapper level and the deep set (mirror_x, route, lighting.streetlight, scenery,
props, capture.bridges, texture_overrides, ui_series...); env vars PYTHON/BLENDER/KN5_WRITER; the
PoT texture pre-flight.

## SD special cases

- Gates that drop in cleanly on the loop path: verify_kn5, kn5_ground_check (kn5-internal mode —
  no generated OBJs, so fidelity half auto-skips or gets a blend→OBJ export adapter),
  track_folder, ext_config, pbr fixes, export VARs.
- Not applicable to the loop path: the generative front half + drive_test/audit (no track.obj).
  They DO apply to SD's generative build_network.sh.
- The loop program needs its own over-cap guard (CO's lives in the OBJ program).

## Versioning

Pinned-tag checkout via committed `bootstrap.sh` + one-line `.engine-version` per project repo,
cloning the engine into gitignored `.engine/`. First actions: cut PAB `v0.1.0` baseline, port,
tag `v0.2.0`, point both project repos at it. (Wheel publishing is the fallback; tag checkout is
simpler to debug and matches the vendored-Blender pattern.)

## Skills

Single source: `prodrive-ac-builder/.claude/skills/` = union (CO: drive-test, road-grounding;
PAB: route-tracing, scs-extraction, track-grading; shared reconciled to newest: ac-track-modding,
aerial-tracing, elevation, gps-extraction incl. street-fidelity, lighting, mesh-audit). Projects
carry no skills.

## Merge order

1. Tag PAB v0.1.0 baseline. 2. Port CO deltas to the 6 shared AC files (split build_kn5 by
pipeline). 3. Add CO-only files. 4. SD build_kn5 → build_kn5_loop.py + build_loop.sh. 5. Fold
assets. 6. build.sh to CO contract (add widths/drive/audit/fidelity, KN5_WRITER; DELETE the
`rsync --delete tracks/` publish — it wipes project source when slug==dirname). 7. Union skills;
extend tests. 8. Tag v0.2.0; bootstrap.sh + .engine-version into both project repos.

## Watch-outs

- Never re-add the rsync --delete publish.
- build_kn5.py is two programs — split, don't merge.
- PAB's test_ext_config_occluders exists without the feature — must pass post-port.
- kn5_ground_check's fidelity half needs generated OBJs; loop path = adapter or kn5-only mode.
