"""Ship paints (liveries): the game's tint palettes, applied to an exported model.

A ship's record lists its paints: geometry entries tagged with a paint item's name
("Paint_Stingray_Black_Black_Orange") and a tint palette record. A palette holds three
paint colors (entryA, B, C: the primary, secondary and tertiary paint) plus glass and
decal colors. The export marks the hull's paintable materials with the palette entry
they take (extras "palette_tint": 1, 2 or 3) and bakes the ship's default palette into
their color, so painting a model is swapping those colors for the paint's. Item
materials (guns, components) carry their own palettes and keep them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import catalog, models, tools
from .installs import Install

PALETTE_DIR = "libs/foundry/records/tintpalettes"
DEFAULT = "default"
# an exported color counts as the ship's default paint when every channel is this close
MATCH = 0.02


def _records(install: Install) -> Path:
    return catalog.build_dir(install) / "records"


def ensure_palettes(install: Install, settings: dict, log=print) -> Path:
    """The tint palette records, extracted from the game data the first time (seconds)."""
    out = _records(install) / PALETTE_DIR
    if not out.is_dir() or not any(out.rglob("*.json")):
        log("reading the game's paint colors")
        tools.run(settings, "extract_records", install, out=str(_records(install)), filter="**/tintpalettes/**")
    return out


def _rel(path: str) -> str:
    """'file://./../../libs/foundry/records/tintpalettes/x.json' -> 'libs/foundry/.../x.json'"""
    p = path.replace("\\", "/")
    i = p.lower().find("libs/foundry/records/")
    return p[i:].lower() if i >= 0 else ""


def _record_json(install: Install, asset: dict) -> dict | None:
    rel = asset.get("record_path") or ""
    p = _records(install) / rel
    if not rel or not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _palettes_in(record: dict) -> tuple[str, dict[str, str]]:
    """(default palette, {paint tag: palette}) from a ship record's geometry entries."""
    default, paints = "", {}

    def walk(o, depth=0):
        nonlocal default
        if isinstance(o, dict):
            if o.get("_Type_") == "SGeometryNodeParams":
                pal = (((o.get("Geometry") or {}).get("Palette") or {}).get("RootRecord")) or ""
                rel = _rel(pal) if pal else ""
                tags = [t for t in re.split(r"[\s,]+", o.get("Tags") or "") if t]
                ptags = [t for t in tags if t.lower().startswith("paint_")]
                if rel:
                    if ptags:
                        for t in ptags:
                            paints.setdefault(t, rel)
                    elif not tags and not default:
                        default = rel
            for v in o.values():
                walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, depth + 1)
    walk(record)
    return default, paints


def _pretty(tag: str) -> str:
    """'Paint_Stingray_Black_Black_Orange' -> 'Black Black Orange' (when no item name)."""
    parts = tag.split("_")[2:] or tag.split("_")[1:]
    return " ".join(p.capitalize() for p in parts) or tag


def ship_paints(install: Install, settings: dict, asset: dict, log=print) -> list[dict]:
    """The paints this ship can wear, first its default: [{"id", "name"}]. Empty for
    things without paints (guns, props)."""
    rec = _record_json(install, asset)
    if rec is None:
        return []
    default, paints = _palettes_in(rec)
    if not paints:
        return []
    ensure_palettes(install, settings, log)
    con = catalog.connect(install)
    out = [{"id": DEFAULT, "name": "Default"}]
    for tag in sorted(paints):
        if not (_records(install) / paints[tag]).is_file():
            continue
        row = con.execute("SELECT name FROM assets WHERE record = ? COLLATE NOCASE", (tag,)).fetchone()
        name = (row[0] if row and row[0] else "") or _pretty(tag)
        out.append({"id": tag, "name": name})
    return out


def palette(install: Install, rel: str) -> dict | None:
    """{"tint": [A, B, C] sRGB 0-255, "glass": rgb, "decal": [R, G, B]} of a palette record."""
    p = _records(install) / rel
    try:
        root = json.loads(p.read_text(encoding="utf-8"))["_RecordValue_"]["root"]
    except (OSError, ValueError, KeyError, TypeError):
        return None

    def rgb(c):
        c = c or {}
        return [int(c.get("r", 0)), int(c.get("g", 0)), int(c.get("b", 0))]
    return {"tint": [rgb((root.get(f"entry{k}") or {}).get("tintColor")) for k in "ABC"],
            "glass": rgb(root.get("glassColor")),
            "decal": [rgb(root.get(f"decalColor{k}")) for k in "RGB"]}


def _linear(c: int) -> float:
    s = c / 255.0
    return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4


def recolor(doc: dict, old: dict, new: dict) -> int:
    """Swap the default paint colors for a paint's in a glTF document's materials.
    Returns materials changed."""
    old_lin = [[_linear(v) for v in c] for c in old["tint"]]
    new_lin = [[_linear(v) for v in c] for c in new["tint"]]
    changed = 0
    for m in doc.get("materials", []):
        ex = m.get("extras") or {}
        k = ex.get("palette_tint")
        if k not in (1, 2, 3):
            continue
        pbr = m.setdefault("pbrMetallicRoughness", {})
        base = pbr.get("baseColorFactor") or [1.0, 1.0, 1.0, 1.0]
        if all(abs(base[i] - old_lin[k - 1][i]) <= MATCH for i in range(3)):
            pbr["baseColorFactor"] = [*new_lin[k - 1], base[3] if len(base) > 3 else 1.0]
            changed += 1
    return changed


def painted_glb(install: Install, settings: dict, asset: dict, glb: Path, paint: str, log=print) -> Path:
    """The model file in a paint (cached beside it); the file itself for the default."""
    if not paint or paint == DEFAULT:
        return glb
    rec = _record_json(install, asset)
    default, paints = _palettes_in(rec or {})
    if paint not in paints:
        raise ValueError(f"{asset.get('record')} has no paint {paint}")
    ensure_palettes(install, settings, log)
    old, new = palette(install, default) if default else None, palette(install, paints[paint])
    if not old or not new:
        raise ValueError(f"couldn't read the colors of {paint}")
    out = glb.with_name(glb.name[:-4] + f".{models._safe(paint).lower()}.glb")
    if out.exists() and out.stat().st_mtime >= glb.stat().st_mtime:
        return out
    doc, bin_, version = models._read_glb(glb)
    n = recolor(doc, old, new)
    models._write_glb(out, doc, bin_, version)
    log(f"painted {n} hull material(s) in {paint}")
    return out
