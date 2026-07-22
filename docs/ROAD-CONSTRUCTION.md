# Road construction engineering reference (procedural parameters)

Researched 2026-07-22 (web, DOT manuals + historic archives). Full citations at bottom of the
research record; key sources: FHWA rock-slope design, MoDOT/WSDOT/ODOT design manuals, AASHTO
Roadside Design Guide, Oregon DOT rockfall catchment guide, IS 14458-3 + The Stone Trust (dry
stone walls), Historic Columbia River Highway archives (same-era analog), Colorado Encyclopedia /
TCLF / Golden History Museum (Lariat specifics), Commerce City engineering standards.

## THE CORE RULE (encode as the construction selector)

**A real road is never unsupported over air.** Every station's cross-section is exactly one of:
1. **BENCH CUT** into the hill,
2. **FILL** embankment reaching natural ground,
3. **RETAINING STRUCTURE** carrying the edge,
4. **BRIDGE/DECK** as explicit structure.

Procedural selector per station/side: compare deck elevation to terrain under the edge; pick the
cheapest condition that closes the gap — cut, then fill, then wall, then bridge.

## Cut slopes (H:V) by material
- solid rock 0.25:1 (near-vertical ok) · fractured rock 0.5:1 · weathered/hardpan 0.75–1:1 ·
  common earth 1–1.5:1 · loose soil 2:1.
- Round the cut top into natural ground over 2–5 m ("daylight line") — no hard crease.
- Cut-toe DITCH: 1910s mountain: V, ~0.4 m deep × 1.2 m wide against the face. Rock faces get a
  catchment ≈ 0.4–0.5 × face height wide.

## Fill
- Standard 2:1; 1910s hand-placed rock 1.5:1. Each metre of height ≈ 2 m of footprint.
- **Fill → wall trigger:** ground cross-slope steeper than ~1.5:1, OR edge fill height > ~4–8 m,
  OR toe would run > 15–20 m out. Fill on slopes steeper than 4:1 is BENCHED (keyed) into the
  hillside — the face is a clean planar wedge meeting undisturbed ground at its toe.

## Retaining structures
- **Dry stone masonry** (THE LARIAT TYPE): 0.5–4 m (practical cap 6 m), face batter 1:6 (~9.5°
  lean-back), base ≈ 0.55 × height, face 0.3–0.6 m outside the pavement edge, wall top flush with
  shoulder grade. Above a drop: a **stone guard wall/parapet ~0.5 m high × 0.4 m thick** with a
  slightly arched mortar cap (Columbia River Highway pattern, same era).
- Crib walls to 6–8 m (1930s-60s look); gabions to ~5.5 m single-stack (modern rural); MSE
  near-vertical panels (modern highway only).
- **Wall → bridge trigger:** required wall height > 6 m.

## Barriers (modern)
- Warranted at slopes >3:1 with >3 m fill; 2:1 over ~1.5–2 m usually; ≥4:1 never.
- W-beam face at shoulder edge +0.6 m; 0.6 m soil backing behind posts; rail top 0.79 m.
- 1913 equivalent: stone guard wall wherever drop > ~2 m at curves/overlooks; else nothing.

## Drainage shaping the cross-section
- Crown 2%; bench sections often slope 2% TOWARD the cut ditch (the signature asymmetry: tight
  ditch + face uphill, rounded hinge + long slope downhill).
- Culverts every ~100–250 m and at every switchback apex / sag: 450 mm min, stone headwalls
  flush with the fill face on a 1913 road.

## Urban industrial street (Commerce City / Sand Creek)
- Vertical curb 150 mm reveal; gutter pan 0.6 m at ~9%; roadway 11–12.2 m local /14.6 collector,
  2% crown; sidewalk 1.5 m attached (or absent — gravel verge is authentic here).
- **Driveway aprons** (the industrial signature + the intersection-mouth fix): curb cuts 6–12 m
  wide, 1–1.5 m flares or 7.5 m return radii, curb ROLLS 150 mm → 40 mm lip → 150 mm across the
  flare; apron rises 2–8% from gutter. Intersection curb returns 9–15 m flowline radius.

## Lookout Mountain Road (the real thing)
- "Cement Bill" Williams surveyed 1910, hand-cut a 2-ft pilot trail by 1911, road opened
  **Aug 26, 1913**. Olmsted Jr. consulted; final layout by Saco R. DeBoer. Denver Mountain Parks
  flagship (1917: stone entrance pylons; Buffalo Bill's grave at the summit).
- **Length 4.3 mi, climb ~2,000 ft, width 20 ft (6.1 m), MAX GRADE 6%** (deliberately, for early
  autos), hairpins ~15–25 m centerline radius, widened ~24–26 ft through turns. Originally
  crushed stone, now asphalt. Stone retaining walls below the outer edge; low stone guard walls
  at Sensation/Windy/Wildcat Points.

## Parameter tables

### Archetype `real_road` variant "1910s mountain scenic" (Lariat)
width 6.1 m (7.5 at hairpins) · verge 0.3–0.5 m gravel · crown 2% (or 2% to cut ditch) · max
grade 6% · rock cut 0.25:1, soil 1–1.5:1 · toe ditch 0.4×1.2 m · fill 1.5:1 · fill→wall at 4 m
edge height or >1.5:1 ground · dry-stone wall batter 1:6, cap 6 m, face +0.3–0.6 m off pavement ·
wall→bridge at 6 m · stone parapet 0.5×0.4 m at drops >2 m · culverts 450 mm @150 m + hairpin
apexes.

### Archetype `street_circuit` variant "modern industrial" (Sand Creek)
roadway 11–12.2 m · curb 150 mm/gutter 0.6 m · sidewalk 1.5 m attached or gravel verge · aprons
7.5–10.5 m wide, rolled curb to 40 mm lip, 7.5 m returns · intersection returns 9–15 m · grades
≤2%, cut/fill ≤1 m at 4:1 · inlets not ditches.
