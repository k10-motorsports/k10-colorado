"""Emit CREDITS.txt + LICENSES.md into the track folder from assets/licenses.json (single source of
truth). Every bundled binary asset must be CC0-1.0 / public domain; stock AC surface sounds are
referenced by name (surfaces.ini WAV=), never redistributed. Pure stdlib.

Run:  python -m scripts.ac.credits projects/<slug>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _manifest() -> dict:
    p = Path(__file__).resolve().parents[2] / "assets" / "licenses.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"bundled_assets": [], "track": {}}


def credits_txt(name: str) -> str:
    m = _manifest()
    lines = [f"{name} — credits & licenses", "=" * (len(name) + 22), "",
             "Built with prodrive-ac-builder. All bundled binary assets are CC0-1.0 / public domain.",
             "Surface scrub & kerb-rumble sounds are the player's own stock Assetto Corsa sounds",
             "(content/sfx), referenced by name in data/surfaces.ini — none are redistributed here.", "",
             "Data:"]
    for d in m.get("track", {}).get("data_sources", []):
        lines.append(f"  - {d['name']} ({d['license']}) {d.get('url', '')}")
    lines += ["", "Bundled assets:"]
    for a in m.get("bundled_assets", []):
        lines.append(f"  - {a['name']} — {a['source']} — {a['license']} — {a.get('url', '')}")
    return "\n".join(lines) + "\n"


def licenses_md(name: str) -> str:
    m = _manifest()
    rows = ["| Asset | Files | Source | License | URL |", "|---|---|---|---|---|"]
    for a in m.get("bundled_assets", []):
        rows.append(f"| {a['name']} | {', '.join(a.get('files', []))} | {a['source']} | "
                    f"{a['license']} | {a.get('url', '')} |")
    drows = ["", "## Data sources", "", "| Source | License | Attribution | URL |", "|---|---|---|---|"]
    for d in m.get("track", {}).get("data_sources", []):
        drows.append(f"| {d['name']} | {d['license']} | "
                     f"{'required' if d.get('attribution_required') else 'courtesy'} | {d.get('url', '')} |")
    return (f"# {name} — License manifest\n\n"
            "Every bundled binary asset is CC0-1.0 (public domain). Crediting is courtesy, not required. "
            "Stock AC surface sounds are referenced by name (surfaces.ini WAV=), never redistributed.\n\n"
            "## Bundled assets\n\n" + "\n".join(rows) + "\n" + "\n".join(drows) + "\n")


def generate(project_dir: str | Path, cfg) -> None:
    out = Path(project_dir) / "build" / cfg.slug
    out.mkdir(parents=True, exist_ok=True)
    (out / "CREDITS.txt").write_text(credits_txt(cfg.name), encoding="utf-8")
    (out / "LICENSES.md").write_text(licenses_md(cfg.name), encoding="utf-8")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m scripts.ac.credits <project-dir>")
    from scripts.config import load_config
    pd = Path(sys.argv[1])
    cfg = load_config(pd)
    generate(pd, cfg)
    print(f"wrote CREDITS.txt + LICENSES.md to build/{cfg.slug}/")


if __name__ == "__main__":
    main()
