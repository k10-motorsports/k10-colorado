"""Harvest the real ROAD-surface texture from a Prodrive Scan capture video (OpenCV).

The road is perspective-rectified to top-down and built from a TEMPORAL MEDIAN over only the
asphalt-masked pixels: the car's lane drifts frame-to-frame, so every top-down spot sees clean asphalt
in most frames and the lane lines / cars / hood get masked out and filled from the others — no blur, no
contamination. The result is flattened (lighting removed), made seamless (roll + inpaint the centre
seam), and a normal map derived. Frames where the road mask is sparse (the phone fell → sky/dash) are
auto-skipped.

All OTHER materials (grass/building/sidewalk/fence) come from segment_textures.py (semantic segmentation
— colour heuristics only work for the road). Both share the synthesis helpers here and emit
texture_overrides in realworld_capture.json that the build consumes. Needs numpy + opencv (pip --user).
See [[companion-app-plan]].

Run:  python -m scripts.capture.textures projects/<slug> <bundle-dir>
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

RW, RH = 256, 512        # top-down road strip (px): ~3.5 m wide × ~10 m ahead
OUT = 512                # final texture size
N_FRAMES = 80            # candidate frames sampled across the drive
# Lane trapezoid in the frame (fractions of W,H) → the top-down rectangle. Calibrated to the windshield
# mount: lane ahead, below the horizon (~0.66 H), above the hood (~0.80 H).
LANE_SRC = [(0.34, 0.66), (0.66, 0.66), (0.92, 0.80), (0.08, 0.80)]


def _video(bundle: Path) -> Path:
    for n in ("video_recovered.mp4", "video.mov"):
        if (bundle / n).exists():
            return bundle / n
    raise FileNotFoundError(f"no video in {bundle}")


def _frame_count(v: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                          "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1", str(v)],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0)


def _extract(v: Path, idxs: list[int], out: Path) -> list[Path]:
    sel = "+".join(f"eq(n\\,{i})" for i in idxs)
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(v), "-vf", f"select='{sel}'",
                    "-fps_mode", "passthrough", "-q:v", "2", str(out / "f_%04d.jpg")], check=False)
    return sorted(out.glob("f_*.jpg"))


# --------------------------------------------------------------------------- segmentation


def _road_homography(w: int, h: int) -> np.ndarray:
    src = np.float32([(fx * w, fy * h) for fx, fy in LANE_SRC])
    dst = np.float32([[0, 0], [RW, 0], [RW, RH], [0, RH]])
    return cv2.getPerspectiveTransform(src, dst)


def _asphalt_mask(bgr: np.ndarray) -> np.ndarray:
    """Grey, mid-value, low-saturation → asphalt. Excludes lane paint, grass, vehicles, the hood."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    s, v = hsv[:, :, 1], hsv[:, :, 2]
    return ((s < 55) & (v > 45) & (v < 210)).astype(bool)


# --------------------------------------------------------------------------- synthesis


def _flatten(bgr: np.ndarray) -> np.ndarray:
    """Remove the low-frequency lighting gradient (divide by a heavy blur), keep grain, re-tint to mean."""
    f = bgr.astype(np.float32) + 1.0
    blur = cv2.GaussianBlur(f, (0, 0), sigmaX=max(bgr.shape[:2]) / 6.0)
    mean = f.reshape(-1, 3).mean(0)
    out = f / blur * mean
    return np.clip(out, 0, 255).astype(np.uint8)


def _make_seamless(bgr: np.ndarray) -> np.ndarray:
    """Roll by half — that makes the TILE BORDER continuous (its new edges are adjacent original rows/
    cols) — then INPAINT away the centre cross (the only remaining discontinuity). Cleaner than feather:
    no visible seam, full grain preserved away from the thin healed lines."""
    h, w = bgr.shape[:2]
    off = np.roll(np.roll(bgr, h // 2, 0), w // 2, 1)
    mask = np.zeros((h, w), np.uint8)
    t = max(2, w // 100)
    mask[h // 2 - t:h // 2 + t, :] = 255
    mask[:, w // 2 - t:w // 2 + t] = 255
    return cv2.inpaint(off, mask, 3, cv2.INPAINT_TELEA)


def _normal_map(bgr: np.ndarray, strength: float = 2.0) -> np.ndarray:
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3) * strength
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3) * strength
    nz = np.ones_like(g)
    n = np.dstack([-gx, -gy, nz])
    n /= np.linalg.norm(n, axis=2, keepdims=True)
    return ((n * 0.5 + 0.5) * 255).astype(np.uint8)[:, :, ::-1]   # to BGR for imwrite


# --------------------------------------------------------------------------- harvest


def _harvest_road(frames: list[Path]) -> tuple[np.ndarray, int]:
    """Temporal median of the top-down asphalt over masked-valid pixels. Returns (texture_bgr, n_used)."""
    H = None
    tops, masks = [], []
    for p in frames:
        im = cv2.imread(str(p))
        if im is None:
            continue
        if H is None:
            H = _road_homography(im.shape[1], im.shape[0])
        top = cv2.warpPerspective(im, H, (RW, RH))
        m = _asphalt_mask(top)
        if m.mean() < 0.45:                # sparse asphalt → fell/blocked frame; skip
            continue
        tops.append(top.astype(np.float32))
        masks.append(m)
    if len(tops) < 5:
        raise ValueError(f"only {len(tops)} usable road frames")
    stack = np.stack(tops)                  # N,H,W,3
    msk = np.stack(masks)                   # N,H,W
    stack[~msk] = np.nan
    med = np.nanmedian(stack, axis=0)       # H,W,3  (NaN where never asphalt)
    hole = np.isnan(med).any(2).astype(np.uint8)
    med = np.nan_to_num(med).astype(np.uint8)
    if hole.any():
        med = cv2.inpaint(med, hole, 5, cv2.INPAINT_TELEA)
    crop = med[RH // 4:RH // 4 + RW, :, :]  # square from the well-covered middle band
    tex = _make_seamless(_flatten(crop))
    return cv2.resize(tex, (OUT, OUT), interpolation=cv2.INTER_LANCZOS4), len(tops)


# --------------------------------------------------------------------------- driver


def build(project_dir: str | Path, bundle_dir: str | Path) -> dict:
    project_dir, bundle_dir = Path(project_dir), Path(bundle_dir)
    v = _video(bundle_dir)
    total = _frame_count(v)
    lo, hi = int(total * 0.04), int(total * 0.96)
    idxs = [lo + (hi - lo) * k // (N_FRAMES - 1) for k in range(N_FRAMES)]

    out_dir = project_dir / "source" / "textures"
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = json.loads((project_dir / "track.config.json").read_text())["slug"]
    stats: dict = {}

    with tempfile.TemporaryDirectory() as td:
        frames = _extract(v, idxs, Path(td))
        stats["frames"] = len(frames)
        for mat, prefix, harvest in (("road", "1ROAD", _harvest_road),):  # grass+ via segment_textures.py
            try:
                tex, used = harvest(frames)
            except ValueError as e:
                stats[mat] = f"skipped: {e}"
                continue
            diff = out_dir / f"{slug}_{mat}_diffuse.jpg"
            norm = out_dir / f"{slug}_{mat}_normal.jpg"
            cv2.imwrite(str(diff), tex, [cv2.IMWRITE_JPEG_QUALITY, 92])
            cv2.imwrite(str(norm), _normal_map(tex), [cv2.IMWRITE_JPEG_QUALITY, 92])
            _record_override(project_dir, prefix,
                             f"source/textures/{diff.name}", f"source/textures/{norm.name}", bundle_dir.name)
            stats[mat] = f"{used} frames -> {diff.name} + normal"
    return stats


def _record_override(project_dir: Path, material: str, diffuse: str, normal: str, bundle: str) -> None:
    cap = project_dir / "source" / "realworld_capture.json"
    doc = json.loads(cap.read_text()) if cap.exists() else {"texture_overrides": []}
    doc.setdefault("texture_overrides", [])
    doc["texture_overrides"] = [o for o in doc["texture_overrides"] if o.get("material") != material]
    doc["texture_overrides"].append({"material": material, "diffuse": diffuse, "normal": normal,
                                     "source": bundle})
    cap.write_text(json.dumps(doc, indent=2) + "\n")


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: python -m scripts.capture.textures <project-dir> <bundle-dir>")
    for k, v in build(sys.argv[1], sys.argv[2]).items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
