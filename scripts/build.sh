#!/usr/bin/env bash
# End-to-end Mac build for ONE track — Phases 1-5, from track.config.json to an installable .zip.
#
# This is the single entrypoint the pipeline was missing. Given a project that has its inputs in
# place (track.config.json + source/ KML/notes; see CLAUDE.md Phase 0), it runs every phase in order
# and drops projects/<slug>/build/<slug>_v<version>.zip ready for Content Manager.
#
#   ./scripts/build.sh projects/sand-creek-raceway              # full run, Phases 1-5
#   ./scripts/build.sh projects/sand-creek-raceway mesh         # resume from the mesh stage
#   ./scripts/build.sh projects/<slug> --list                   # list stages and exit
#
# Stages (in order). Earlier stages hit the network (Overpass/USGS) and only need re-running when the
# route or region changes; the geometry->package back half is offline + deterministic. Resume from any
# stage by name to skip the slow front half once the route is settled.
#   gps     centerline + street labels       (Overpass / OSM)        -> data/centerline.geojson, street_labels.json
#   elev    sample + smooth terrain          (USGS 3DEP)             -> data/heightfield.npy, dem
#   project lat/lon/elev -> local metres, origin + true north        -> data/*.local.json, writes config
#   mesh    road ribbon + grass + dummies + decals                   -> data/track.obj
#   env     buildings / trees / highways / mountains                 -> data/environment.obj
#   blend   import OBJs -> welded, materialed .blend  (Blender 4.2)   -> blender/<slug>.blend
#   kn5     export .blend -> .kn5 via vendored add-on (Blender 4.2)   -> build/<slug>.kn5
#   pack    AC track folder (configs/ui/ai) + zip                     -> build/<slug>_v<version>.zip
set -euo pipefail

PROJ="${1:?usage: build.sh <project-dir> [from-stage|--list]}"
FROM="${2:-gps}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"
STAGES=(gps elev project mesh env blend kn5 pack)

# Pre-flight: AC/CSP can hard-CRASH THE GPU (whole-PC freeze) on load when a kn5 carries a
# non-power-of-two texture (mipmap/DXT upload hangs the driver). Fail loudly before building.
"$PY" - <<'POT' || exit 1
import sys
from pathlib import Path
try:
    from PIL import Image
except Exception:
    print("  [pot-check skipped: Pillow not available]"); sys.exit(0)
pot = lambda n: (n & (n - 1)) == 0
bad = []
for f in Path("assets/textures").glob("*"):
    if f.suffix.lower() in (".png", ".jpg", ".jpeg"):
        w, h = Image.open(f).size
        if not (pot(w) and pot(h)):
            bad.append(f"{f.name} {w}x{h}")
if bad:
    print("NON-PoT textures (resize to power-of-two or AC hard-crashes on load):")
    for b in bad: print("   ", b)
    sys.exit(1)
print("  pot-check: all textures power-of-two")
POT

if [[ "$FROM" == "--list" ]]; then printf '%s\n' "${STAGES[@]}"; exit 0; fi
# index of the start stage
START=-1; for i in "${!STAGES[@]}"; do [[ "${STAGES[$i]}" == "$FROM" ]] && START=$i; done
[[ $START -lt 0 ]] && { echo "unknown stage '$FROM' (one of: ${STAGES[*]})" >&2; exit 1; }

run() { echo; echo "━━━━ [$1] ${*:2}"; "${@:2}"; }
active() { local s; for s in "${STAGES[@]:$START}"; do [[ "$s" == "$1" ]] && return 0; done; return 1; }

SLUG="$($PY -c "import json,sys;print(json.load(open('$PROJ/track.config.json'))['slug'])")"
VER="$($PY -c "import json,sys;print(json.load(open('$PROJ/track.config.json'))['version'])")"

active gps     && { run gps     "$PY" -m scripts.gps.centerline "$PROJ"
                    run gps     "$PY" -m scripts.gps.street_labels "$PROJ"; }
active elev    &&   run elev    "$PY" -m scripts.elevation.heightfield "$PROJ"
active project &&   run project "$PY" -m scripts.geometry.projection "$PROJ"
# Real street circuits (osm_road_widths=true): replace the flat default width with per-vertex OSM
# lane-derived curb-to-curb widths + intersection pads. Runs after project (which writes the widths it
# overrides) and hits Overpass. Racetracks keep their configured widths.
active project && [ "$($PY -c "import json;print(json.load(open('$PROJ/track.config.json')).get('osm_road_widths',False))")" = "True" ] \
               &&  run widths  "$PY" -m scripts.gps.widths_from_osm "$PROJ"
active mesh    &&   run mesh    "$PY" -m scripts.geometry.build_mesh "$PROJ"
active env     &&   run env     "$PY" -m scripts.environment.build_env "$PROJ"
# Gates: the virtual DRIVE TEST (wheel paths over the built triangles) AND the mesh audit
# (geometry classes incl. E: walls floating over ground — the check that would have caught the
# hovering barrier runs v0.7.4 shipped with). A FAIL in either aborts before Blender/kn5.
active env     &&   run drive   "$PY" -m scripts.geometry.drive_test "$PROJ"
active env     &&   run audit   "$PY" -m scripts.geometry.audit_mesh "$PROJ"

if active blend || active kn5; then
  if [[ "${KN5_WRITER:-blender}" == "native" ]]; then
    # NATIVE PATH (KN5_WRITER=native): write the kn5 straight from the audited OBJs with
    # scripts/ac/kn5_write.py — no Blender import/weld/yaw/exporter between the geometry the gates
    # measure and the bytes the driver loads. Fidelity reads 0.00 m by construction. No .blend is
    # produced (use the default path when a hand-editable scene is needed).
    active kn5 && run kn5 "$PY" -m scripts.ac.kn5_write "$PROJ"
  else
    BLENDER="${BLENDER:-$("$ROOT/scripts/bootstrap_blender.sh")}"   # pin/download Blender 4.2 if needed
    active blend && run blend "$BLENDER" --background --python scripts/ac/build_kn5.py -- "$PROJ"
    active kn5   && run kn5   "$BLENDER" --background --python scripts/ac/export_kn5_addon.py -- "$PROJ"
  fi
  # Gate: assert the exported kn5 is actually drivable (no dup meshes, drivable surfaces face-up, spawns
  # on the road) — every past fall-through, encoded as a check. A failure aborts BEFORE packaging/release.
  active kn5   && run verify "$PY" -m scripts.ac.verify_kn5 "$PROJ"
  # Gate: the SHIPPED kn5 must equal the audited OBJs up to the configured yaw (kn5 fidelity).
  # Catches assembly bugs (dropped/displaced groups, stale meshes) that no OBJ-stage gate can see.
  active kn5   && run fidelity "$PY" -m scripts.ac.kn5_ground_check "$PROJ"
fi

if active pack; then
  run pack "$PY" -m scripts.ac.track_folder "$PROJ"
  cp -f "$PROJ/build/$SLUG.kn5" "$PROJ/build/$SLUG/$SLUG.kn5"
  run pack "$PY" - "$PROJ" "$SLUG" "$VER" <<'PY'
import sys, zipfile
from pathlib import Path
proj, slug, ver = sys.argv[1], sys.argv[2], sys.argv[3]
root = Path(proj) / "build"; folder = root / slug
zp = root / f"{slug}_v{ver}.zip"
with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, 6) as z:
    for f in sorted(folder.rglob("*")):
        if f.is_file():
            z.write(f, f.relative_to(root))
print(f"  -> {zp}  ({zp.stat().st_size/1e6:.1f} MB)")
PY
  # NOTE: the old prodrive layout published the content folder to tracks/<slug>/ via
  # `rsync -a --delete`. In K10, tracks/<slug>/ IS the source project dir, and when the AC slug equals
  # the track dir name (e.g. "pueblo"), that --delete rsync WIPES the track's own config/source/data.
  # The installable deliverable is build/<slug>_v<ver>.zip (+ build/<slug>/), so that publish step is
  # removed. Do NOT re-add an rsync that writes into tracks/<slug>/.
  echo "  installable: $PROJ/build/${SLUG}_v${VER}.zip  (+ folder $PROJ/build/$SLUG/)"
fi
echo; echo "✓ build complete: $PROJ ($SLUG v$VER)"
