"""Harvest a real texture per material via semantic segmentation (SegFormer / Cityscapes).

The colour heuristics in textures.py only nail the road; the rest of the scene (grass, buildings,
fences) needs real per-pixel labels. This segments each frame with a Cityscapes-trained SegFormer, masks
out the fixed windshield furniture (dashboard reads as "car", mirror as "person") and any real vehicles/
people, then for each target material finds the LARGEST clean single-class patch (distance transform),
keeps the best across frames, and flattens + tiles + derives a normal — reusing textures.py's synthesis.

Cityscapes classes → track materials:  vegetation+terrain → 1GRASS,  building → BUILDING.
Outputs feed the same texture_overrides the build already consumes (scripts/ac/pbr.load_overrides).
Needs torch + transformers (offline harvest only). See [[companion-app-plan]].

Run:  python -m scripts.capture.segment_textures projects/<slug> <bundle-dir>
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

from scripts.capture import textures as tx

MODEL_ID = "nvidia/segformer-b0-finetuned-cityscapes-1024-1024"
N_FRAMES = 40
# track material prefix → (Cityscapes class ids, prefer_plain). prefer_plain biases toward a PLAIN patch
# (façade/concrete — avoid door/window/sign edges that tile badly); now safe because the brightness gate
# rejects the dark degenerate patches that broke this before. Grain-rich ground keeps prefer_plain=False.
# (Vegetation/trees = class 8 are alpha-cutout BILLBOARDS, a separate atlas — not tiling overrides.)
MATERIALS = {
    "1GRASS":   ({9}, False),   # terrain → the dry verge / ground (grain wanted)
    "BUILDING": ({2}, True),    # building façades → plain warehouse wall, not the door/window
    "1KERB":    ({1}, True),    # sidewalk → plain concrete
    "BARRIER":  ({4}, False),   # fence → keep the rail/mesh structure
}
TRANSIENT = {11, 12, 13, 14, 15, 16, 17, 18}   # person/rider/car/truck/bus/train/motorcycle/bicycle


def _load_model():
    import torch
    from transformers import SegformerForSemanticSegmentation, SegformerImageProcessor
    proc = SegformerImageProcessor.from_pretrained(MODEL_ID)
    model = SegformerForSemanticSegmentation.from_pretrained(MODEL_ID).eval()
    return torch, proc, model


def _segment(torch, proc, model, bgr: np.ndarray) -> np.ndarray:
    """Per-pixel Cityscapes label map at the frame's resolution."""
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    inp = proc(images=rgb, return_tensors="pt")
    with torch.no_grad():
        logits = model(**inp).logits
    up = torch.nn.functional.interpolate(logits, size=bgr.shape[:2], mode="bilinear", align_corners=False)
    return up.argmax(1)[0].numpy().astype(np.int32)


def _windshield_valid(h: int, w: int) -> np.ndarray:
    """Exclude the fixed dashboard (bottom band) and rear-view mirror (top-right) from harvesting."""
    valid = np.ones((h, w), bool)
    valid[int(0.78 * h):, :] = False              # dashboard / hood
    valid[:int(0.40 * h), int(0.74 * w):] = False  # mirror corner
    return valid


def _largest_square(mask: np.ndarray) -> tuple[int, int, int] | None:
    """Centre + half-size of the largest square fully inside the mask (via distance transform)."""
    dt = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 5)
    _, mx, _, loc = cv2.minMaxLoc(dt)
    half = int(mx)
    if half < 24:
        return None
    return loc[0], loc[1], half


def _harvest(frames, class_ids, torch, proc, model, prefer_plain=False):
    """Best clean single-class patch across frames → flattened, seamless texture + frames used.
    Brightness-gated (rejects shadow/blown patches); scored by size and either grain (ground) or
    plainness (façades — prefer_plain=True, to dodge door/window/sign edges that tile badly)."""
    best, best_score, used = None, -1.0, 0
    for p in frames:
        im = cv2.imread(str(p))
        if im is None:
            continue
        seg = _segment(torch, proc, model, im)
        h, w = seg.shape
        want = np.isin(seg, list(class_ids)) & _windshield_valid(h, w)
        bad = np.isin(seg, list(TRANSIENT))
        want &= ~cv2.dilate(bad.astype(np.uint8), np.ones((9, 9), np.uint8)).astype(bool)
        if want.mean() < 0.02:
            continue
        used += 1
        sq = _largest_square(want)
        if not sq:
            continue
        cx, cy, half = sq
        patch = im[cy - half:cy + half, cx - half:cx + half]
        if patch.shape[0] < 48 or patch.shape[1] < 48:
            continue
        bright = float(patch.mean())
        if bright < 50 or bright > 225:        # reject shadow / blown-out degenerate patches
            continue
        detail = cv2.Laplacian(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
        # façades: big + PLAIN (low edge content → no door/window). ground: big + grainy.
        score = (half * half) / (detail + 1.0) if prefer_plain else (half * half) * (detail + 1.0)
        if score > best_score:
            best_score, best = score, patch
    if best is None:
        raise ValueError("no clean single-class patch found")
    sq = cv2.resize(best, (tx.OUT, tx.OUT), interpolation=cv2.INTER_LANCZOS4)
    return tx._make_seamless(tx._flatten(sq)), used


def build(project_dir, bundle_dir, materials: dict | None = None) -> dict:
    project_dir, bundle_dir = Path(project_dir), Path(bundle_dir)
    materials = materials or MATERIALS
    v = tx._video(bundle_dir)
    total = tx._frame_count(v)
    lo, hi = int(total * 0.04), int(total * 0.96)
    idxs = [lo + (hi - lo) * k // (N_FRAMES - 1) for k in range(N_FRAMES)]

    out_dir = project_dir / "source" / "textures"
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = json.loads((project_dir / "track.config.json").read_text())["slug"]
    torch, proc, model = _load_model()
    stats: dict = {}

    with tempfile.TemporaryDirectory() as td:
        frames = tx._extract(v, idxs, Path(td))
        stats["frames"] = len(frames)
        for prefix, (class_ids, plain) in materials.items():
            mat = prefix.lower().lstrip("1")
            try:
                tex, used = _harvest(frames, class_ids, torch, proc, model, prefer_plain=plain)
            except ValueError as e:
                stats[prefix] = f"skipped: {e}"
                continue
            diff = out_dir / f"{slug}_{mat}_diffuse.jpg"
            norm = out_dir / f"{slug}_{mat}_normal.jpg"
            cv2.imwrite(str(diff), tex, [cv2.IMWRITE_JPEG_QUALITY, 92])
            cv2.imwrite(str(norm), tx._normal_map(tex), [cv2.IMWRITE_JPEG_QUALITY, 92])
            tx._record_override(project_dir, prefix, f"source/textures/{diff.name}",
                                f"source/textures/{norm.name}", bundle_dir.name)
            stats[prefix] = f"{used} frames -> {diff.name} + normal"
    return stats


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: python -m scripts.capture.segment_textures <project-dir> <bundle-dir>")
    for k, v in build(sys.argv[1], sys.argv[2]).items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
