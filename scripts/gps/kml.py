"""Parse a KML/KMZ route (e.g. exported from Google My Maps) into named geometry.

The drawn line IS the route intent, so this is the precise source when the user hand-draws in My Maps.
Returns every Placemark across all Folders/layers as lines or points, in document order:
  [{"name": str|None, "folder": str|None, "type": "line"|"point", "coords": [(lon, lat), ...]}]
Handles plain .kml and zipped .kmz, with or without XML namespaces. Stdlib only.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any


def _tag(el) -> str:
    return el.tag.rsplit("}", 1)[-1]  # strip {namespace}


def _read_bytes(path: str | Path) -> bytes:
    path = Path(path)
    if path.suffix.lower() == ".kmz":
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".kml")), None)
            return z.read(name or "doc.kml")
    return path.read_bytes()


def _coords(text: str) -> list[tuple[float, float]]:
    out = []
    for tok in (text or "").replace("\n", " ").split():
        parts = tok.split(",")
        if len(parts) >= 2:
            try:
                out.append((float(parts[0]), float(parts[1])))  # lon, lat (KML order)
            except ValueError:
                pass
    return out


def parse_kml(path: str | Path) -> list[dict[str, Any]]:
    """Parse a .kml/.kmz into a list of placemark features (lines + points), in document order."""
    root = ET.fromstring(_read_bytes(path))
    feats: list[dict[str, Any]] = []

    def walk(el, folder: str | None):
        for child in el:
            t = _tag(child)
            if t == "Folder":
                fname = next((_text(c) for c in child if _tag(c) == "name"), folder)
                walk(child, fname)
            elif t == "Document":
                walk(child, folder)
            elif t == "Placemark":
                name = next((_text(c) for c in child if _tag(c) == "name"), None)
                for geo in child.iter():
                    gt = _tag(geo)
                    if gt in ("LineString", "LinearRing", "Point"):
                        ct = next((c for c in geo.iter() if _tag(c) == "coordinates"), None)
                        coords = _coords(ct.text) if ct is not None else []
                        if coords:
                            feats.append({"name": name, "folder": folder,
                                          "type": "point" if gt == "Point" else "line",
                                          "coords": coords})

    def _text(el):
        return (el.text or "").strip() or None

    walk(root, None)
    return feats
