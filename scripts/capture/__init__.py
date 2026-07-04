"""Capture phase — ingest a real-world windshield run from the Prodrive Scan iOS app and distil it
into committed ``projects/<slug>/source/realworld_capture.json`` evidence.

The phone is a track-agnostic logger (ARKit 6DoF pose + WGS84 GPS + barometer + LiDAR depth/mesh);
all track association happens here on the Mac, by projecting captured GPS into the *same* local-metre
mesh frame the geometry pipeline built (see ``scripts.geometry.projection``). Pure stdlib so it runs
under the system ``python3`` like the geometry/environment back-half. See docs/companion-app-plan.
"""
