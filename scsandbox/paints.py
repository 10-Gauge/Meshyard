"""Ship paints (liveries): the game's tint palettes, applied to an exported model.

A ship's record lists its paints: geometry entries tagged with a paint item's name
("Paint_Stingray_Black_Black_Orange") and a tint palette record. A palette holds three
paint colors (entryA, B, C: the primary, secondary and tertiary paint) plus glass and
decal colors. The export marks the hull's paintable materials with the palette entry
they take (extras "palette_tint": 1, 2 or 3) and bakes the ship's default palette into
their color, so painting a model is swapping those colors for the paint's. Item
materials (guns, components) carry their own palettes and keep them.

Most paints are liveries: besides the palette they swap the hull's material file. A
livery can move a material to another palette entry (the Gladius Nightbreak paints its
tertiary panels in the secondary color), lay a stencil over the hull (camo, stripes,
logos) or bring its own textures. StarBreaker's decomposed export of the ship lists
every paint with its material sidecar; painting reads those and rebuilds the hull's
materials from the livery's.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import catalog, models, tools
from .installs import Install

PALETTE_DIR = "libs/foundry/records/tintpalettes"
CHANNELS = ("primary", "secondary", "tertiary")
# a paint whose spec color is this bright (linear) is a metallic finish: chrome, gold
METAL_SPEC = 0.2
# how dark a stencil's marked areas make the paint under them (1 = no change)
STENCIL_TONE = 0.45
LIVERY_VERSION = 1   # bump to repaint cached painted models
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


def palette_lin(install: Install, rel: str) -> dict | None:
    """A palette record in linear color: {"primary", "secondary", "tertiary", "glass",
    "spec": {channel: rgb}} (the spec color is what a metallic paint shows)."""
    p = palette(install, rel)
    if p is None:
        return None
    try:
        root = json.loads((_records(install) / rel).read_text(encoding="utf-8"))["_RecordValue_"]["root"]
    except (OSError, ValueError, KeyError, TypeError):
        root = {}
    out = {"spec": {}}
    for ch, k, rgb in zip(CHANNELS, "ABC", p["tint"]):
        out[ch] = [_linear(v) for v in rgb]
        sc = ((root.get(f"entry{k}") or {}).get("specColor")) or {}
        out["spec"][ch] = [_linear(int(sc.get(c, 0))) for c in "rgb"]
    out["glass"] = [_linear(v) for v in p["glass"]]
    return out


def _linear(c: int) -> float:
    s = c / 255.0
    return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4


def paint_package(install: Install, settings: dict, asset: dict, log=print) -> Path | None:
    """The folder of the ship's StarBreaker package with its paints.json, liveries.json
    and the livery material sidecars and textures (exported once, seconds). None if the
    export fails: painting then falls back to the palette colors alone."""
    out = models.models_dir(install) / f"{models._safe(asset['record'])}.paints"
    with models._lock(str(out)):
        found = sorted(out.glob("Packages/*/liveries.json"))
        if not found:
            log("reading the ship's liveries")
            try:
                tools.run(settings, "export_entity_paints", install, name=asset["record"], out=str(out))
            except tools.ToolError as e:
                log(f"livery materials unavailable, painting colors only: {str(e)[-200:]}")
                return None
            found = sorted(out.glob("Packages/*/liveries.json"))
    return found[0].parent if found else None


def _json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _submaterials(path: Path) -> dict[str, dict]:
    """A material sidecar's submaterials by name."""
    out = {}
    for sm in _json(path).get("submaterials") or []:
        attrs = {a.get("name"): a.get("value") for a in sm.get("authored_attributes") or []}
        if attrs.get("Name"):
            out[attrs["Name"]] = sm
    return out


def _channel(sm: dict | None) -> str | None:
    """The palette entry a submaterial takes: primary, secondary, tertiary, glass or None."""
    ch = (((sm or {}).get("palette_routing") or {}).get("material_channel") or {}).get("name")
    return ch.lower() if isinstance(ch, str) else None


def _texture(sm: dict | None, role: str, slot: str) -> str | None:
    for t in (sm or {}).get("texture_slots") or []:
        if t.get("role") == role and t.get("slot") == slot and t.get("export_path"):
            return t["export_path"]
    return None


def _param(sm: dict, name: str, default: float) -> float:
    try:
        return float((sm.get("public_params") or {}).get(name, default))
    except (TypeError, ValueError):
        return default


def livery_sources(pkg: Path, record: str, paint: str) -> tuple[str, dict, dict | None] | None:
    """(hull material stem, base submaterials, livery submaterials or None when the
    paint keeps the base material) from a paint package; None if it can't be read."""
    root = pkg.parent.parent
    base_rel = None
    for lv in _json(pkg / "liveries.json").get("liveries") or []:
        if any(e.lower() == f"entityclassdefinition.{record.lower()}" for e in lv.get("entity_names") or []):
            base_rel = (lv.get("material_sidecars") or [None])[0]
            break
    if not base_rel:
        return None
    base_doc = _json(root / base_rel)
    stem = Path(base_doc.get("source_material_path") or base_rel).stem
    stem = re.sub(r"_TEX\d+\.materials$", "", stem)
    base = _submaterials(root / base_rel)
    liv = None
    for v in _json(pkg / "paints.json").get("paint_variants") or []:
        if (v.get("subgeometry_tag") or "").lower() == paint.lower():
            if v.get("exterior_material_sidecar"):
                liv = _submaterials(root / v["exterior_material_sidecar"])
            break
    return stem, base, liv


def _add_texture(doc: dict, bin_: bytes, png: bytes) -> tuple[bytes, int]:
    """Append a PNG image to the glTF and return (binary, texture index); repeat-wrapped."""
    bin_ += b"\0" * (-len(bin_) % 4)
    views = doc.setdefault("bufferViews", [])
    views.append({"buffer": 0, "byteOffset": len(bin_), "byteLength": len(png)})
    bin_ += png
    doc.setdefault("buffers", [{}])[0]["byteLength"] = len(bin_)
    images = doc.setdefault("images", [])
    images.append({"bufferView": len(views) - 1, "mimeType": "image/png"})
    samplers = doc.setdefault("samplers", [])
    samplers.append({"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497})
    textures = doc.setdefault("textures", [])
    textures.append({"source": len(images) - 1, "sampler": len(samplers) - 1})
    return bin_, len(textures) - 1


def stencil_tone(mask_png: Path, override: float) -> bytes | None:
    """The stencil as a grey multiplier image (sRGB-encoded so it reads back linear):
    the marked areas of the stencil's first `override` channels darken the paint under
    them, the way StarBreaker's add-on reads the game's stencils. None if unreadable."""
    import io

    import numpy as np
    from PIL import Image
    try:
        a = np.asarray(Image.open(mask_png).convert("RGBA"), float) / 255.0
    except (OSError, ValueError):
        return None
    tone = np.ones(a.shape[:2])
    used = max(1, min(3, int(round(override)) or 1))
    for c in range(used):
        ch = a[..., c]
        if ch.max() - ch.min() < 0.02:
            continue  # a flat channel marks nothing
        tone *= STENCIL_TONE + (1.0 - STENCIL_TONE) * ch
    srgb = np.where(tone <= 0.0031308, tone * 12.92, 1.055 * np.power(tone, 1 / 2.4) - 0.055)
    img = Image.fromarray((np.clip(srgb, 0, 1) * 255).round().astype("uint8"), "L").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _set_paint(pbr: dict, color: list[float], spec: list[float] | None) -> None:
    base = pbr.get("baseColorFactor") or [1.0, 1.0, 1.0, 1.0]
    alpha = base[3] if len(base) > 3 else 1.0
    if spec and max(spec) >= METAL_SPEC:
        pbr["baseColorFactor"] = [*spec, alpha]   # metallic finish: its color is its spec
        pbr["metallicFactor"] = 1.0
    else:
        pbr["baseColorFactor"] = [*color, alpha]


def repaint(doc: dict, bin_: bytes, old: dict, new: dict, sources=None, root: Path | None = None) -> tuple[bytes, int]:
    """Paint a glTF document: hull materials follow the livery's material (its palette
    routing, stencil and textures) when sources are known; other paintable materials
    (gear, attachments in the ship's palette) swap the default colors for the paint's.
    Returns (binary, materials changed)."""
    stem, base, liv = sources if sources else (None, {}, None)
    prefix = f"{stem.lower()}_mtl_" if stem else None
    changed = 0
    for m in doc.get("materials", []):
        ex = m.get("extras") or {}
        pbr = m.setdefault("pbrMetallicRoughness", {})
        sub = ex.get("source_name")
        hull = bool(prefix) and (m.get("name") or "").lower().startswith(prefix) \
            and (sub in base or (liv is not None and sub in liv))
        if not hull:
            k = ex.get("palette_tint")
            if k not in (1, 2, 3):
                continue
            ch = CHANNELS[k - 1]
            cur = pbr.get("baseColorFactor") or [1.0, 1.0, 1.0, 1.0]
            if all(abs(cur[i] - old[ch][i]) <= MATCH for i in range(3)):
                _set_paint(pbr, new[ch], new["spec"][ch])
                changed += 1
            continue
        b = base.get(sub)
        sm = liv.get(sub) if liv is not None else b
        if sm is None:
            continue
        ch = _channel(sm)
        touched = False
        if ch in CHANNELS:
            _set_paint(pbr, new[ch], new["spec"][ch])
            touched = True
        elif ch == "glass":
            cur = pbr.get("baseColorFactor") or [1.0, 1.0, 1.0, 1.0]
            pbr["baseColorFactor"] = [*new["glass"], cur[3] if len(cur) > 3 else 1.0]
            touched = True
        if liv is not None and root is not None:
            # the livery's own color texture, where it differs from the base material's
            tex = _texture(sm, "base_color", "TexSlot1")
            if tex and tex != _texture(b, "base_color", "TexSlot1") and (root / tex).is_file():
                bin_, ti = _add_texture(doc, bin_, (root / tex).read_bytes())
                pbr["baseColorTexture"] = {"index": ti, "texCoord": 0}
                touched = True
            st = _texture(sm, "stencil", "TexSlot7")
            if st and (root / st).is_file():
                png = stencil_tone(root / st, _param(sm, "StencilTintOverride", 1))
                if png:
                    bin_, ti = _add_texture(doc, bin_, png)
                    tiling = _param(sm, "StencilTiling", 1.0) or 1.0
                    pbr["baseColorTexture"] = {
                        "index": ti, "texCoord": 1 if _param(sm, "UseUV2ForStencil", 0) > 0.5 else 0,
                        "extensions": {"KHR_texture_transform": {"scale": [tiling, tiling]}}}
                    used = doc.setdefault("extensionsUsed", [])
                    if "KHR_texture_transform" not in used:
                        used.append("KHR_texture_transform")
                    touched = True
        changed += touched
    return bin_, changed


def painted_glb(install: Install, settings: dict, asset: dict, glb: Path, paint: str, log=print) -> Path:
    """The model file in a paint (cached beside it); the file itself for the default."""
    if not paint or paint == DEFAULT:
        return glb
    rec = _record_json(install, asset)
    default, paints = _palettes_in(rec or {})
    if paint not in paints:
        raise ValueError(f"{asset.get('record')} has no paint {paint}")
    ensure_palettes(install, settings, log)
    old, new = palette_lin(install, default) if default else None, palette_lin(install, paints[paint])
    if not old or not new:
        raise ValueError(f"couldn't read the colors of {paint}")
    out = glb.with_name(glb.name[:-4] + f".{models._safe(paint).lower()}.l{LIVERY_VERSION}.glb")
    pkg = paint_package(install, settings, asset, log)
    with models._lock(str(out)):   # the viewer and a print may paint the same file at once
        if out.exists() and out.stat().st_mtime >= glb.stat().st_mtime:
            return out
        sources = livery_sources(pkg, asset["record"], paint) if pkg else None
        doc, bin_, version = models._read_glb(glb)
        bin_, n = repaint(doc, bin_, old, new, sources, pkg.parent.parent if pkg else None)
        models._write_glb(out, doc, bin_, version)
    log(f"painted {n} material(s) in {paint}" + ("" if sources else " (colors only)"))
    return out
