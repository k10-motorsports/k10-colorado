#!/usr/bin/env bash
# Build one Colorado track end to end.
#
#   ./build.sh tracks/imi                 # full run, phases 1-5 -> installable .zip
#   ./build.sh tracks/imi mesh            # resume from a stage (skip the slow Overpass/USGS half)
#   ./build.sh tracks/imi --list          # list stages and exit
#
# Thin wrapper: the whole pipeline lives in the pinned engine (.engine/, see .engine-version and
# bootstrap.sh). This repo used to carry its OWN copy of that pipeline — 72 modules — which is why
# engine fixes never reached these tracks and why its curb-flush audit silently checked nothing for
# months (it grepped a "KERB" prefix against groups actually named "1KERB_*"). Don't re-add one.
#
# An ABSOLUTE project path is passed because the engine's build.sh cd's to its own root; build
# outputs still land back here under tracks/<slug>/build/.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="${1:?usage: build.sh <track-dir> [from-stage|--list]}"

[ -d "$ROOT/.engine" ] || "$ROOT/bootstrap.sh"

ABS="$(cd "$ROOT/$PROJ" 2>/dev/null && pwd || true)"
[ -n "$ABS" ] && [ -f "$ABS/track.config.json" ] || {
  echo "no track.config.json at $ROOT/$PROJ" >&2; exit 1; }

exec "$ROOT/.engine/scripts/build.sh" "$ABS" "${2:-gps}"
