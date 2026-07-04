#!/usr/bin/env bash
# Bootstrap the pinned Blender the kn5 export add-on requires.
#
# The jwl-7 io_import_accsv add-on (vendor/io_import_accsv) registers its kn5 exporter ONLY on
# Blender 4.2 LTS — on Blender 5.x the operator registers by name but errors when called. So the
# build host pins 4.2. This installs it once into a STABLE cache (NOT /tmp, which the OS wipes),
# and is idempotent: re-running is a no-op if the app is already there.
#
# The build scripts read $BLENDER; this prints the resolved path. Usage:
#   ./scripts/bootstrap_blender.sh            # ensure installed, print path
#   export BLENDER="$(./scripts/bootstrap_blender.sh)"
set -euo pipefail

VER="4.2.9"
ARCH="$(uname -m)"                       # arm64 | x86_64
case "$ARCH" in
  arm64)  DMG_ARCH="arm64" ;;
  x86_64) DMG_ARCH="x64" ;;
  *) echo "unsupported arch: $ARCH" >&2; exit 1 ;;
esac

CACHE="${PRODRIVE_BLENDER_CACHE:-$HOME/.cache/prodrive-ac-builder}"
APP="$CACHE/Blender${VER}.app"
BIN="$APP/Contents/MacOS/Blender"

if [[ -x "$BIN" ]]; then echo "$BIN"; exit 0; fi

mkdir -p "$CACHE"
DMG="$CACHE/blender-${VER}-macos-${DMG_ARCH}.dmg"
URL="https://download.blender.org/release/Blender4.2/blender-${VER}-macos-${DMG_ARCH}.dmg"
echo "downloading $URL ..." >&2
curl -fSL --retry 3 -o "$DMG" "$URL"

echo "mounting + copying app ..." >&2
MNT="$(mktemp -d)"
hdiutil attach "$DMG" -nobrowse -mountpoint "$MNT" >/dev/null
cp -R "$MNT/Blender.app" "$APP"
hdiutil detach "$MNT" >/dev/null
rm -f "$DMG"                              # surgical: keep the .app, drop the installer

# clear the quarantine bit so headless launch doesn't get blocked
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
echo "installed -> $BIN" >&2
echo "$BIN"
