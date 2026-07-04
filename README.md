# K10 Colorado

Accurate, standalone **Assetto Corsa** builds of Colorado's real race circuits — **Sand Creek, IMI,
High Plains, Aspen (Woody Creek), PPIR (oval + infield), Second Creek** — built one at a time, tight to
their real geometry and elevation. No connecting roads; each track stands alone.

We build **live in Blender**: scripts seed the road + curbs at real elevation, then the track is refined
interactively before exporting a kn5. See **[CLAUDE.md](CLAUDE.md)** for the workflow and the hard-won
rules (real roads, real elevation, tight curbs, no floating/burying, researched banking).

## Live Blender bridge

In Blender: Text Editor → open `blender/live_server.py` → **Run Script** (or install it as an add-on). Then
the operator drives that same scene with `python3 blender/live_client.py '<python>'`.

## kn5 export

Needs **Blender 4.2** + the vendored AC Tools add-on. `scripts/bootstrap_blender.sh` pins Blender 4.2; the
live-design Blender can be any 4.x.
