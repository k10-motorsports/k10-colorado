"""Generate CC0 foliage billboard atlases — procedural, nothing mined.

Replaces the previously-kitbashed trees_atlas.png / bushes_atlas.png (which were extracted from other
AC mods and were NOT redistributable) with fully original, procedurally-authored alpha-cutout atlases
so the built kn5 carries only distributable assets.

  - trees_atlas.png  : 3x2 grid — Front-Range mix (broadleaf cottonwood/aspen canopies + a couple of
                       conifer spruces), green canopy + brown trunk on transparent background.
  - bushes_atlas.png : 2x2 grid — dry sage/rabbitbrush shrub mounds.

Both are alpha-cutout (pbr.BILLBOARD -> ALPHATEST=1 in-engine). Build-time tool: run once, commit the
PNGs; build_env references them via pbr. Needs PIL + numpy.

Run:  python -m scripts.environment.make_foliage
"""
from __future__ import annotations
import math
from pathlib import Path
import numpy as np
from PIL import Image, ImageFilter


def _noise(h, w, scale, seed):
    rng = np.random.default_rng(seed)
    small = rng.random((max(2, h // scale), max(2, w // scale)))
    img = np.array(Image.fromarray((small * 255).astype("uint8")).resize((w, h), Image.BILINEAR)) / 255.0
    return img


def _tree_cell(W, H, seed, conifer=False):
    rng = np.random.default_rng(seed)
    rgba = np.zeros((H, W, 4), dtype=np.float32)
    cx = W / 2
    # trunk
    tw = W * 0.055
    for y in range(int(H * 0.62), H):
        t = (y - H * 0.62) / (H * 0.38)
        ww = tw * (0.6 + 0.8 * t)
        col = np.array([0.32, 0.22, 0.13]) * (0.8 + 0.2 * rng.random())
        rgba[y, int(cx - ww):int(cx + ww)] = [*col, 1.0]
    # canopy
    canopy_seed = _noise(H, W, 7, seed + 1)
    base_green = np.array([0.16 + 0.12 * rng.random(), 0.34 + 0.14 * rng.random(), 0.12 + 0.08 * rng.random()])
    if conifer:
        # stacked triangles
        tiers = 5
        for i in range(tiers):
            ty = H * (0.10 + 0.52 * i / tiers)
            tb = H * (0.10 + 0.52 * (i + 1.4) / tiers)
            hw = W * (0.42 - 0.30 * i / tiers)
            for y in range(int(ty), int(tb)):
                fr = (y - ty) / max(1, (tb - ty))
                ww = hw * fr
                x0, x1 = int(cx - ww), int(cx + ww)
                shade = 0.7 + 0.5 * canopy_seed[min(H - 1, y), cx.__int__()]
                rgba[y, x0:x1, :3] = base_green * shade
                rgba[y, x0:x1, 3] = 1.0
    else:
        # blob of overlapping ellipses
        blobs = 9
        for _ in range(blobs):
            bx = cx + (rng.random() - 0.5) * W * 0.5
            by = H * (0.30 + rng.random() * 0.28)
            br = W * (0.16 + rng.random() * 0.14)
            yy, xx = np.mgrid[0:H, 0:W]
            d = ((xx - bx) / br) ** 2 + ((yy - by) / (br * 1.05)) ** 2
            m = d < 1.0
            shade = 0.65 + 0.6 * canopy_seed
            rgba[m, :3] = (base_green[None, None, :] * shade[..., None])[m]
            rgba[m, 3] = 1.0
    a = Image.fromarray((rgba[..., 3] * 255).astype("uint8")).filter(ImageFilter.GaussianBlur(1.2))
    out = (rgba[..., :3] * 255).astype("uint8")
    im = Image.fromarray(out, "RGB").convert("RGBA")
    im.putalpha(a)
    return im


def _bush_cell(W, H, seed):
    rng = np.random.default_rng(seed)
    rgba = np.zeros((H, W, 4), dtype=np.float32)
    tex = _noise(H, W, 5, seed + 3)
    base = np.array([0.30 + 0.12 * rng.random(), 0.34 + 0.10 * rng.random(), 0.16 + 0.06 * rng.random()])
    mounds = 6
    for _ in range(mounds):
        bx = W * (0.25 + rng.random() * 0.5)
        by = H * (0.55 + rng.random() * 0.25)
        brx = W * (0.16 + rng.random() * 0.14)
        bry = brx * (0.7 + rng.random() * 0.3)
        yy, xx = np.mgrid[0:H, 0:W]
        d = ((xx - bx) / brx) ** 2 + ((yy - by) / bry) ** 2
        m = d < 1.0
        shade = 0.6 + 0.7 * tex
        rgba[m, :3] = (base[None, None, :] * shade[..., None])[m]
        rgba[m, 3] = 1.0
    a = Image.fromarray((rgba[..., 3] * 255).astype("uint8")).filter(ImageFilter.GaussianBlur(1.0))
    im = Image.fromarray((rgba[..., :3] * 255).astype("uint8"), "RGB").convert("RGBA")
    im.putalpha(a)
    return im


def build(out_dir: str | Path = "assets/textures"):
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    # trees: 3 cols x 2 rows, 1024 atlas
    S = 1024
    tc, tr = 3, 2
    cw, ch = S // tc, S // tr
    atlas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    for i in range(tc * tr):
        conifer = i in (2, 5)
        cell = _tree_cell(cw, ch, seed=100 + i, conifer=conifer)
        atlas.paste(cell, ((i % tc) * cw, (i // tc) * ch), cell)
    atlas.save(out / "trees_atlas.png")
    # bushes: 2 x 2
    bc, br = 2, 2
    cw, ch = S // bc, S // br
    batlas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    for i in range(bc * br):
        cell = _bush_cell(cw, ch, seed=200 + i)
        batlas.paste(cell, ((i % bc) * cw, (i // bc) * ch), cell)
    batlas.save(out / "bushes_atlas.png")
    print(f"wrote {out/'trees_atlas.png'} (3x2) and {out/'bushes_atlas.png'} (2x2), {S}x{S} CC0")


if __name__ == "__main__":
    build()
