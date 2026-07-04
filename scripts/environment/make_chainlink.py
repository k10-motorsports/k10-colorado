"""Generate a tileable chain-link fence alpha-cutout texture — procedural, CC0 (nothing mined).

Industrial Commerce City is wall-to-wall chain-link around the warehouse yards (it's in nearly every
frame of the real-world capture). This authors that texture once: galvanised-grey diagonal wires on a
transparent background, so the diamond holes cut out in-engine (the FENCE/CHAINLINK material is in
pbr.BILLBOARD → ALPHATEST=1). The wire families are ``x+y`` and ``x-y`` mod ``spacing`` (45° diagonals);
``S`` a multiple of ``spacing`` keeps it seamlessly tileable.

Build-time tool only (like the qlmanage sign atlases) — run once, commit the PNG, build_env just
references it via pbr. Pure stdlib (reuses road_text's PNG writer); no PIL/numpy so it runs under the
no-numpy system python.

Run:  python -m scripts.environment.make_chainlink
"""

from __future__ import annotations

from pathlib import Path

from scripts.geometry.road_text import _write_rgba_png


def make(out_png: Path, *, S: int = 512, spacing: int = 32, wire: float = 1.6,
         rgb: tuple[int, int, int] = (150, 152, 156)) -> Path:
    """Write an ``S``×``S`` RGBA chain-link texture: opaque grey wire, transparent diamonds."""
    assert S % spacing == 0, "S must be a multiple of spacing to tile seamlessly"
    r, g, b = rgb
    pix = bytearray(S * S * 4)
    for y in range(S):
        for x in range(S):
            d1 = (x + y) % spacing; d1 = min(d1, spacing - d1)   # dist to nearest '/' wire
            d2 = (x - y) % spacing; d2 = min(d2, spacing - d2)   # dist to nearest '\' wire
            o = (y * S + x) * 4
            pix[o], pix[o + 1], pix[o + 2] = r, g, b
            pix[o + 3] = 255 if (d1 <= wire or d2 <= wire) else 0
    out_png.parent.mkdir(parents=True, exist_ok=True)
    _write_rgba_png(out_png, S, S, bytes(pix))
    return out_png


def main() -> None:
    out = Path(__file__).resolve().parents[2] / "assets" / "textures" / "chainlink_diffuse.png"
    make(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
