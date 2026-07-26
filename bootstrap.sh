#!/usr/bin/env bash
# Fetch the shared track engine (k10-track-agents) into gitignored .engine/ at the
# tag pinned in .engine-version. Idempotent; run after clone and after bumping the pin.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VER="$(tr -d '[:space:]' < "$ROOT/.engine-version")"
REPO="${ENGINE_REPO:-git@github.com:k10-motorsports/k10-track-agents.git}"
if [ ! -d "$ROOT/.engine/.git" ]; then
  git clone --quiet "$REPO" "$ROOT/.engine"
fi
git -C "$ROOT/.engine" fetch --quiet --tags origin
git -C "$ROOT/.engine" checkout --quiet "$VER"
echo "engine: k10-track-agents @ $VER -> .engine/"
