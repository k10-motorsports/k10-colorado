"""ext_config TreeFX integration: data/treefx.json -> extension/trees/trees.txt + [TREES] block.

Drives the real ext_config.generate() over a synthetic project so it exercises the actual wiring
(SURFACE_MATERIALS derived from the track's real groups, the license-clean README, the stale-list
cleanup). Plain ``test_*`` for tests/run_tests.py (no pytest fixtures).
"""

from __future__ import annotations

import json
import random
import tempfile
from pathlib import Path

from scripts.ac import ext_config
from scripts.environment import treefx as tfx

GROUPS = ["1GRASS", "1ROAD_main", "1KERB_l", "BUILDINGS", "TREES", "MOUNTAINS"]


def _project(*, treefx_enabled=True, grassfx=False):
    d = Path(tempfile.mkdtemp())
    (d / "data").mkdir()
    (d / "track.config.json").write_text(json.dumps({"slug": "t", "lighting": {"grassfx": grassfx}}))
    (d / "data" / "environment.obj").write_text("\n".join(f"o {g}" for g in GROUPS) + "\n")
    cfg = {"enabled": treefx_enabled, "seed": 99,
           "zones": {"forest": {"source": "fill_terrain",
                                "species": ["MRS_pine_high_2_1.bin", "treePine1"],
                                "size_variance": [0.85, 1.15],
                                "density": {"by": "constant", "keep": 1.0}}}}
    c = tfx.TreeFXCollector(cfg)
    rng = random.Random(99)
    for i in range(6):
        c.add("fill_terrain", 100.0 + i, 1900.0, -float(i), rng=rng)
    c.write_json(d / "data" / "treefx.json")
    return d


def test_treefx_emits_trees_block_and_text_list():
    d = _project()
    txt = ext_config.generate(d).read_text()
    assert "[TREES]" in txt
    assert "LIST_0 = trees/trees.txt" in txt
    # SURFACE_MATERIALS is derived from the track's REAL groups (grass + road), not hardcoded
    for ln in txt.splitlines():
        if ln.startswith("SURFACE_MATERIALS"):
            assert "1GRASS_mat" in ln and "1ROAD_main_mat" in ln
            break
    else:
        raise AssertionError("no SURFACE_MATERIALS line")
    trees = (d / "build" / "t" / "extension" / "trees" / "trees.txt")
    assert trees.exists()
    body = trees.read_text()
    assert "tree: MRS_pine_high_2_1.bin;" in body or "tree: treePine1;" in body
    assert "configure: seed = 99" in body
    # the license-clean manifest lists the expected .bin models the user must supply
    readme = (d / "build" / "t" / "extension" / "trees" / "README").read_text()
    assert "MRS_pine_high_2_1.bin" in readme and "USER-SUPPLIED" in readme


def test_treefx_block_independent_of_grassfx():
    # TreeFX is its own effect: a [TREES] block appears even with GrassFX off (they gate separately).
    txt = ext_config.generate(_project(grassfx=False)).read_text()
    assert "[TREES]" in txt
    assert "[GRASS_FX]" not in txt


def test_no_treefx_when_sidecar_disabled():
    txt = ext_config.generate(_project(treefx_enabled=False)).read_text()
    assert "[TREES]" not in txt


def test_stale_trees_txt_removed_on_rebuild_without_treefx():
    d = _project(treefx_enabled=True)
    ext_config.generate(d)                       # writes trees.txt
    trees = d / "build" / "t" / "extension" / "trees" / "trees.txt"
    assert trees.exists()
    (d / "data" / "treefx.json").unlink()        # rebuild WITHOUT treefx (full-rebuild discipline)
    ext_config.generate(d)
    assert not trees.exists()                     # stale list cleaned so [TREES] never dangles
