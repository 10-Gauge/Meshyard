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
GLASS_TINT = 0.3      # how much of the palette glass color the pane shows
LIVERY_VERSION = 26   # bump to repaint cached painted models
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


def _submaterial_list(path: Path) -> list[tuple[str, dict]]:
    """A material sidecar's (name, submaterial) pairs in slot order, repeats and all."""
    out = []
    for sm in _json(path).get("submaterials") or []:
        attrs = {a.get("name"): a.get("value") for a in sm.get("authored_attributes") or []}
        out.append((attrs.get("Name") or "", sm))
    return out


def _submaterials(path: Path) -> dict[str, dict]:
    """A material sidecar's submaterials by name (the first of a repeated name)."""
    out = {}
    for name, sm in _submaterial_list(path):
        if name:
            out.setdefault(name, sm)
    return out


def _channel(sm: dict | None) -> str | None:
    """The palette entry a submaterial's main color takes: primary, secondary, tertiary,
    glass or None. With layers that is the main (first) layer's: a panel can route only
    its wear layer to the palette and keep its own tint underneath (the Railen Grey Grey
    Gold's hull panels)."""
    layers = (sm or {}).get("layer_manifest") or []
    if layers:
        attrs = {a.get("name"): a.get("value") for a in layers[0].get("authored_attributes") or []}
        pt = str(attrs.get("PaletteTint", "")).strip()
        if pt in ("1", "2", "3"):
            return CHANNELS[int(pt) - 1]
        if pt == "0":
            return None
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
                liv = _match_livery(_submaterial_list(root / base_rel),
                                    _submaterial_list(root / v["exterior_material_sidecar"]))
            break
    return stem, base, liv


def _match_livery(base: list, liv: list) -> dict:
    """The livery's submaterials keyed by the base material's names, from both sidecars'
    (name, submaterial) lists in slot order. Most match by name; a base submaterial the
    livery doesn't name takes the livery's one in the same slot when that one is new
    (the Railen liveries renamed Painted_Panel_Dark, its biggest panel, to
    Trim_Paint_Panel_Base)."""
    out = dict(liv)
    base_names = {n for n, _ in base}
    for i, (name, _) in enumerate(base):
        if name not in out and i < len(liv) and liv[i][0] not in base_names:
            out[name] = liv[i][1]
    return out


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


def _stencil_pixels(mask_png: Path):
    """The stencil as an RGBA float array, as the image stores it: the second uv set reads
    it the right way up (flipped, the Idris Sharktooth's camo landed on its underside and
    its beige on top of the nose)."""
    import numpy as np
    from PIL import Image
    return np.asarray(Image.open(mask_png).convert("RGBA"), float) / 255.0


def stencil_tone(mask_png: Path, override: float) -> bytes | None:
    """The stencil as a grey multiplier image (sRGB-encoded so it reads back linear):
    the marked areas of the stencil's first `override` channels darken the paint under
    them, the way StarBreaker's add-on reads the game's stencils. None if unreadable."""
    import io

    import numpy as np
    from PIL import Image
    try:
        a = _stencil_pixels(mask_png)
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


def _bare_metal(sm: dict | None) -> bool:
    """Whether a submaterial's main layer is bare metal: the game shows its palette
    entry's spec color (the Railen's silver and bronze panels), not its tint."""
    layers = (sm or {}).get("layer_manifest") or []
    snap = (layers[0].get("layer_snapshot") or {}) if layers else {}
    try:
        return float(snap.get("metallic") or 0) >= 0.5
    except (TypeError, ValueError):
        return False


def _metal_paint(color: list[float], spec: list[float]) -> bool:
    """A metallic finish: a bright spec that is colored (gold, bronze, red metal) or far
    brighter than the paint under it (chrome over dark). A white paint with a white spec
    (the Railen's pearl panels) stays paint: as metal it turns into a black mirror."""
    hi = max(spec)
    if hi < METAL_SPEC:
        return False
    colored = hi - min(spec) > 0.25 * hi
    lum = lambda c: 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]  # noqa: E731
    return colored or lum(spec) > 2.0 * lum(color)


def _layer_spec(sm: dict | None) -> list[float] | None:
    """The main layer's spec color (linear rgb): what bare metal shows."""
    layers = (sm or {}).get("layer_manifest") or []
    spec = ((layers[0].get("layer_snapshot") or {}).get("specular")) if layers else None
    try:
        return [float(v) for v in spec][:3] if spec and len(spec) >= 3 else None
    except (TypeError, ValueError):
        return None


def _layer_tint(sm: dict | None) -> list[float] | None:
    """The main layer's own TintColor (linear rgb) when the layer takes no palette entry:
    a livery can recolor such panels directly (the Railen White Grey Gold whitens its
    dark hull panels this way)."""
    layers = (sm or {}).get("layer_manifest") or []
    if not layers:
        return None
    attrs = {a.get("name"): a.get("value") for a in layers[0].get("authored_attributes") or []}
    if str(attrs.get("PaletteTint", "0")).strip() not in ("0", ""):
        return None
    try:
        rgb = [float(v) for v in str(attrs.get("TintColor") or "").split(",")]
    except ValueError:
        return None
    return rgb if len(rgb) == 3 else None


def _palette_layer_tint(sm: dict | None) -> list[float]:
    """The main layer's TintColor when it takes a palette entry (the game multiplies the
    two); white when there is none."""
    layers = (sm or {}).get("layer_manifest") or []
    if layers:
        attrs = {a.get("name"): a.get("value") for a in layers[0].get("authored_attributes") or []}
        try:
            rgb = [float(v) for v in str(attrs.get("TintColor") or "").split(",")]
            if len(rgb) == 3 and all(0.0 <= v <= 1.0 for v in rgb):
                return rgb
        except ValueError:
            pass
    return [1.0, 1.0, 1.0]


def mounted_items(doc: dict) -> set[str]:
    """Names of the items mounted on hardpoints (the hardpoints' children)."""
    nodes = doc.get("nodes", [])
    return {nodes[c].get("name") or "" for nd in nodes if (nd.get("name") or "").lower().startswith("hardpoint_")
            for c in nd.get("children", [])} - {""}


def own_paint_items(install: Install, doc: dict, paint: str) -> set[str]:
    """The mounted items whose own record has a geometry entry for this paint: in the
    game they wear their own material in its colors, not the hull's livery."""
    tag = re.compile(r'"Tags"\s*:\s*"(?:[^"]*\s)?' + re.escape(paint) + r'(?:\s[^"]*)?"')
    found = set()
    con = catalog.connect(install)
    try:
        for name in mounted_items(doc):
            row = con.execute("SELECT record_path FROM assets WHERE record = ? COLLATE NOCASE LIMIT 1", (name,)).fetchone()
            p = _records(install) / (row[0] if row else "")
            try:
                if row and row[0] and tag.search(p.read_text(encoding="utf-8")):
                    found.add(name)
            except OSError:
                pass
    finally:
        con.close()
    return found


def _split_mounted_materials(doc: dict, prefix: str, items: set[str]) -> set[int]:
    """Give the parts of the named mounted items that use the hull's materials their own
    copies of them, so the hull's livery can skip them. Returns the copies' indices."""
    nodes, meshes, mats = doc.get("nodes", []), doc.get("meshes", []), doc.get("materials", [])
    parent = {c: i for i, nd in enumerate(nodes) for c in nd.get("children", [])}

    def mounted(i):
        seen = set()
        while i in parent and i not in seen:
            seen.add(i)
            p = parent[i]
            if (nodes[p].get("name") or "").lower().startswith("hardpoint_"):
                return (nodes[i].get("name") or "") in items
            i = p
        return False
    users: dict[int, set] = {}
    for i, nd in enumerate(nodes):
        if "mesh" in nd:
            users.setdefault(nd["mesh"], set()).add(mounted(i))
    copies: dict[int, int] = {}
    for mesh_i, kinds in users.items():
        if kinds != {True} or mesh_i >= len(meshes):
            continue          # hull meshes, and meshes the hull shares with a mount, stay
        for prim in meshes[mesh_i].get("primitives", []):
            k = prim.get("material")
            if k is None or not (mats[k].get("name") or "").lower().startswith(prefix):
                continue
            if k not in copies:
                copy = json.loads(json.dumps(mats[k]))
                copy["name"] = (copy.get("name") or "") + "_mounted"
                copies[k] = len(mats)
                mats.append(copy)
            prim["material"] = copies[k]
    return set(copies.values())


def _use_extension(doc: dict, name: str) -> None:
    used = doc.setdefault("extensionsUsed", [])
    if name not in used:
        used.append(name)


def _rgb_param(sm: dict, name: str) -> list[float] | None:
    v = (sm.get("public_params") or {}).get(name)
    try:
        rgb = [float(x) for x in str(v).split(",")]
    except (TypeError, ValueError):
        return None
    return rgb if len(rgb) == 3 else None


def _stencil_specs(sm: dict):
    """(colors per stencil channel, opacity) when the stencil marks colored specular
    flecks rather than darker paint (StencilTintOverride 0 with spec colors: the Railen's
    pink, teal or grey sparkle); None otherwise."""
    if _param(sm, "StencilTintOverride", 1) != 0:
        return None
    c1, c2 = _rgb_param(sm, "StencilSpecularColor"), _rgb_param(sm, "StencilSpecularColor2")
    if not c1:
        return None
    return [c1, c2 or c1], max(0.0, min(1.0, _param(sm, "StencilOpacity", 1.0)))


def _stencil_colors(sm: dict, pal: dict) -> list[list[float]]:
    """The colors a tint-override stencil paints: its red, green and blue areas take the
    palette's primary, secondary and tertiary paint (each scaled by its
    StencilDiffuseColor). The Idris Sharktooth ("beige beige black") gets a black hull with
    beige tiger stripes and outlines; the Avenger's grey black red camo three greys, its
    red left to the trims."""
    names = ("StencilDiffuseColor", "StencilDiffuseColor2", "StencilDiffuseColor3")
    return [[a * b for a, b in zip(pal[CHANNELS[c]], _rgb_param(sm, names[c]) or [1.0, 1.0, 1.0])]
            for c in range(3)]


def stencil_palette(mask_png: Path, base: list[float], colors: list[list[float]], opacity: float) -> bytes | None:
    """Base color image for a tint-override stencil.

    A camo "generator" stencil (blue marks the camo, green its pattern inside the blue:
    the Idris Sharktooth) paints the camo in the tertiary paint, black on the Sharktooth,
    and the pattern part way back toward the panel's own color: the dark grey hull with
    lighter grey tiger stripes the game shows. Its red only edges the camo and paints
    nothing. Other stencils lay each channel's areas in its color (blue first, red on top:
    the Avenger's grey black red camo in three greys). A channel flat all over marks
    nothing."""
    import io

    import numpy as np
    from PIL import Image
    try:
        a = _stencil_pixels(mask_png)
    except (OSError, ValueError):
        return None
    if _generator_stencil(a):
        a = _crisp(a, 2)
    col = np.ones(a.shape[:2] + (3,)) * np.asarray(base[:3])
    if _generator_stencil(a):
        blue, green = (a[..., 2] * opacity)[..., None], a[..., 1:2] * a[..., 2:3]
        dark = np.asarray(colors[2][:3])
        # neutral grey stripes: the panel's brightness, not its beige
        lum = float(np.dot(np.asarray(base[:3]), [0.2126, 0.7152, 0.0722]))
        light = dark + (lum - dark) * CAMO_PATTERN_LIFT
        col = col * (1 - blue) + dark * blue
        col = col * (1 - green) + light * green
        colors = []
    for c in reversed(range(len(colors))):
        ch = a[..., c]
        if ch.max() - ch.min() < 0.02:
            continue
        w = (ch * opacity)[..., None]
        col = col * (1 - w) + np.asarray(colors[c][:3]) * w
    srgb = np.where(col <= 0.0031308, col * 12.92, 1.055 * np.power(np.clip(col, 0, 1), 1 / 2.4) - 0.055)
    buf = io.BytesIO()
    Image.fromarray((np.clip(srgb, 0, 1) * 255).round().astype("uint8"), "RGB").save(buf, "PNG")
    return buf.getvalue()


# how far a generator stencil's pattern lifts the camo back toward the panel color
CAMO_PATTERN_LIFT = 0.1


def _crisp(a, scale: int):
    """The stencil enlarged `scale` times with its soft edges tightened: a camo mask is
    sharp-edged, but 1024 pixels across a 240 m hull blur into a smear up close."""
    import numpy as np
    from PIL import Image
    h, w = a.shape[:2]
    out = np.empty((h * scale, w * scale, a.shape[2]))
    for c in range(a.shape[2]):
        ch = Image.fromarray((a[..., c] * 255).astype(np.uint8)).resize((w * scale, h * scale), Image.BICUBIC)
        x = np.clip((np.asarray(ch, float) / 255.0 - 0.45) / 0.3, 0, 1)
        out[..., c] = x * x * (3 - 2 * x)          # smoothstep around the middle
    return out


def _generator_stencil(a) -> bool:
    """Whether a stencil marks its camo in blue with the pattern in green inside it (the
    Idris Sharktooth's): both channels vary and nearly all the green lies on blue."""
    import numpy as np
    b, g = a[..., 2], a[..., 1]
    if np.ptp(b) < 0.02 or np.ptp(g) < 0.02:
        return False
    green = g > 0.5
    return bool(green.any()) and float((green & (b > 0.5)).sum()) / float(green.sum()) > 0.95


def stencil_flecks(mask_png: Path, base: list[float], colors: list[list[float]], opacity: float):
    """Base color and metal/roughness images for a fleck stencil: the paint, with the
    stencil's first two channels' areas in their spec colors as polished metal."""
    import io

    import numpy as np
    from PIL import Image
    try:
        a = _stencil_pixels(mask_png)
    except (OSError, ValueError):
        return None, None
    col = np.ones(a.shape[:2] + (3,)) * np.asarray(base[:3])
    metal = np.zeros(a.shape[:2])
    for c, rgb in enumerate(colors[:2]):
        ch = a[..., c]
        if ch.max() - ch.min() < 0.02:
            continue
        w = (ch * opacity)[..., None]
        col = col * (1 - w) + np.asarray(rgb) * w
        metal = np.maximum(metal, ch * opacity)

    def png(lin, mode):
        srgb = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.power(np.clip(lin, 0, 1), 1 / 2.4) - 0.055)
        buf = io.BytesIO()
        Image.fromarray((np.clip(srgb, 0, 1) * 255).round().astype("uint8"), mode).save(buf, "PNG")
        return buf.getvalue()
    mr = np.zeros(a.shape[:2] + (3,))
    mr[..., 1] = 0.55 - 0.35 * metal      # roughness: the flecks polished
    mr[..., 2] = metal                    # metalness
    buf = io.BytesIO()
    Image.fromarray((mr * 255).round().astype("uint8"), "RGB").save(buf, "PNG")   # linear data
    return png(col, "RGB"), buf.getvalue()


def _set_paint(pbr: dict, color: list[float], spec: list[float] | None, metal: bool = False) -> None:
    base = pbr.get("baseColorFactor") or [1.0, 1.0, 1.0, 1.0]
    alpha = base[3] if len(base) > 3 else 1.0
    if spec and (metal or _metal_paint(color, spec)):
        pbr["baseColorFactor"] = [*spec, alpha]   # metallic finish: its color is its spec
        pbr["metallicFactor"] = 1.0
    else:
        pbr["baseColorFactor"] = [*color, alpha]


# a base color texture darker than this (mean linear luminance) has a dark tint baked in
DARK_TEXTURE = 0.2


def _neutral_base_texture(doc: dict, bin_: bytes, pbr: dict, cache: dict) -> bytes:
    """Point pbr at a neutral copy of its base color texture when that texture is dark
    (the panel detail kept, brightness lifted to about 0.85): the paint color then shows."""
    import io

    import numpy as np
    from PIL import Image
    t = pbr.get("baseColorTexture")
    if not t or "index" not in t:
        return bin_
    ti = t["index"]
    if ti not in cache:
        cache[ti] = None
        try:
            img = doc["images"][doc["textures"][ti]["source"]]
            view = doc["bufferViews"][img["bufferView"]]
            off = view.get("byteOffset", 0)
            a = np.asarray(Image.open(io.BytesIO(bin_[off:off + view["byteLength"]])).convert("RGB"), float) / 255.0
        except (KeyError, IndexError, OSError, ValueError):
            return bin_
        lin = np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)
        lum = lin @ np.array([0.2126, 0.7152, 0.0722])
        mean = float(lum.mean())
        if mean < DARK_TEXTURE:
            g = np.clip(lum / max(mean, 1e-4) * 0.85, 0, 1)
            s = np.where(g <= 0.0031308, g * 12.92, 1.055 * np.power(g, 1 / 2.4) - 0.055)
            buf = io.BytesIO()
            Image.fromarray((s * 255).round().astype("uint8"), "L").convert("RGB").save(buf, "PNG")
            bin_, cache[ti] = _add_texture(doc, bin_, buf.getvalue())
    if cache[ti] is not None:
        pbr["baseColorTexture"] = {**t, "index": cache[ti]}
    return bin_


def repaint(doc: dict, bin_: bytes, old: dict, new: dict, sources=None, root: Path | None = None,
            own_paint: set[str] | None = None) -> tuple[bytes, int]:
    """Paint a glTF document: hull materials follow the livery's material (its palette
    routing, stencil and textures) when sources are known; other paintable materials
    (gear, attachments in the ship's palette) swap the default colors for the paint's.
    Returns (binary, materials changed)."""
    stem, base, liv = sources if sources else (None, {}, None)
    prefix = f"{stem.lower()}_mtl_" if stem else None
    changed = 0
    neutral: dict = {}   # texture index -> its neutral copy
    mounted = _split_mounted_materials(doc, prefix, own_paint or set()) if prefix and liv is not None else set()
    for mi, m in enumerate(doc.get("materials", [])):
        ex = m.get("extras") or {}
        pbr = m.setdefault("pbrMetallicRoughness", {})
        sub = ex.get("source_name")
        hull = bool(prefix) and (m.get("name") or "").lower().startswith(prefix) \
            and (sub in base or (liv is not None and sub in liv))
        if not hull:
            k = ex.get("palette_tint")
            if k not in (1, 2, 3) or old is new:   # the default paint: attachments keep theirs
                continue
            ch = CHANNELS[k - 1]
            cur = pbr.get("baseColorFactor") or [1.0, 1.0, 1.0, 1.0]
            if all(abs(cur[i] - old[ch][i]) <= MATCH for i in range(3)):
                _set_paint(pbr, new[ch], new["spec"][ch])
                changed += 1
            continue
        b = base.get(sub)
        # parts mounted on hardpoints whose own record names this paint (the Idris's
        # railgun and torpedo rack) take its colors on their plain material, as the game
        # does; the rest (its hangar doors) wear the hull's livery
        sm = liv.get(sub) if liv is not None and mi not in mounted else b
        if sm is None:
            continue
        ch = _channel(sm)
        touched = False
        if ch != "glass":
            # the export bakes the panel's default tint into its texture: a dark one would
            # keep the panel dark whatever the paint, so swap in a neutral copy
            bin_ = _neutral_base_texture(doc, bin_, pbr, neutral)
        if ch in CHANNELS:
            # the panel's own tint scales its palette color: the Sharktooth's "Mid" panels
            # are its beige at 0.58, darker than its "Light" ones
            k = _palette_layer_tint(sm)
            _set_paint(pbr, [c * t for c, t in zip(new[ch], k)], new["spec"][ch], _bare_metal(sm))
            touched = True
        elif ch == "glass":
            # the palette's glass color tints what shows through; the pane itself is dark
            # and polished (taken whole, the Sharktooth's bridge windows were flat purple)
            cur = pbr.get("baseColorFactor") or [1.0, 1.0, 1.0, 1.0]
            pbr["baseColorFactor"] = [*(c * GLASS_TINT for c in new["glass"]), cur[3] if len(cur) > 3 else 1.0]
            pbr["roughnessFactor"] = min(float(pbr.get("roughnessFactor", 1.0)), 0.1)
            touched = True
        elif _bare_metal(sm):
            # bare metal with no palette entry: its own spec color, as metal
            spec = _layer_spec(sm)
            if spec:
                _set_paint(pbr, spec, spec, True)
                touched = True
        else:
            # no palette entry: the main layer's own tint (a livery may recolor the panel
            # this way; the export sometimes takes a wear layer's palette color instead)
            tint = _layer_tint(sm)
            if tint:
                cur = pbr.get("baseColorFactor") or [1.0, 1.0, 1.0, 1.0]
                pbr["baseColorFactor"] = [*tint, cur[3] if len(cur) > 3 else 1.0]
                pbr["metallicFactor"] = 0.0
                touched = True
        if liv is not None and root is not None:
            # the livery's own color texture, where it differs from the base material's
            tex = _texture(sm, "base_color", "TexSlot1")
            if tex and tex != _texture(b, "base_color", "TexSlot1") and (root / tex).is_file():
                bin_, ti = _add_texture(doc, bin_, (root / tex).read_bytes())
                pbr["baseColorTexture"] = {"index": ti, "texCoord": 0}
                touched = True
        if root is not None:
            # the stencil (the stock paint's too: the Railen's sparkle is one)
            st = _texture(sm, "stencil", "TexSlot7")
            if st and (root / st).is_file():
                uv = 1 if _param(sm, "UseUV2ForStencil", 0) > 0.5 else 0
                flecks = _stencil_specs(sm)
                if flecks:
                    png, mr = stencil_flecks(root / st, pbr.get("baseColorFactor") or [1, 1, 1, 1], *flecks)
                    pbr["baseColorFactor"] = [1.0, 1.0, 1.0, (pbr.get("baseColorFactor") or [1, 1, 1, 1])[3]]
                elif _param(sm, "StencilTintOverride", 0) > 0:
                    png, mr = stencil_palette(root / st, pbr.get("baseColorFactor") or [1, 1, 1, 1],
                                              _stencil_colors(sm, new),
                                              max(0.0, min(1.0, _param(sm, "StencilOpacity", 1.0)))), None
                    pbr["baseColorFactor"] = [1.0, 1.0, 1.0, (pbr.get("baseColorFactor") or [1, 1, 1, 1])[3]]
                else:
                    png, mr = stencil_tone(root / st, 1), None
                if png:
                    tiling = _param(sm, "StencilTiling", 1.0) or 1.0
                    ext = {"KHR_texture_transform": {"scale": [tiling, tiling]}}
                    bin_, ti = _add_texture(doc, bin_, png)
                    pbr["baseColorTexture"] = {"index": ti, "texCoord": uv, "extensions": ext}
                    if mr:
                        bin_, mi = _add_texture(doc, bin_, mr)
                        pbr["metallicRoughnessTexture"] = {"index": mi, "texCoord": uv, "extensions": ext}
                        pbr["metallicFactor"], pbr["roughnessFactor"] = 1.0, 1.0
                    _use_extension(doc, "KHR_texture_transform")
                    touched = True
        # the iridescent sheen (the Gatac Railen's panels; most of its liveries turn it off)
        irid = _param(sm, "IridescenceStrength", 0.0) if _texture(sm, "iridescence", "TexSlot10") \
            or "IridescenceStrength" in (sm.get("public_params") or {}) else 0.0
        exts = m.setdefault("extensions", {})
        if irid > 0:
            exts["KHR_materials_iridescence"] = {"iridescenceFactor": min(1.0, irid), "iridescenceIor": 1.3,
                                                 "iridescenceThicknessMinimum": 150.0,
                                                 "iridescenceThicknessMaximum": 450.0}
            _use_extension(doc, "KHR_materials_iridescence")
            touched = True
        elif "KHR_materials_iridescence" in exts:
            del exts["KHR_materials_iridescence"]
        if not exts:
            m.pop("extensions", None)
        changed += touched
    return bin_, changed


def painted_glb(install: Install, settings: dict, asset: dict, glb: Path, paint: str, log=print) -> Path:
    """The model file in a paint (cached beside it). The default paint goes through the
    same rebuild on ships that have paints (the export leaves bare metal panels black:
    the Railen); anything else, or a failure there, keeps the file itself."""
    rec = _record_json(install, asset)
    default, paints = _palettes_in(rec or {})
    if not paint or paint == DEFAULT:
        if not paints or not default:
            return glb
        try:
            ensure_palettes(install, settings, log)
            pal = palette_lin(install, default)
            return _repainted(install, settings, asset, glb, DEFAULT, pal, pal, log) if pal else glb
        except Exception as e:  # noqa: BLE001  the plain export still shows the ship
            log(f"default paint skipped: {e}")
            return glb
    if paint not in paints:
        raise ValueError(f"{asset.get('record')} has no paint {paint}")
    ensure_palettes(install, settings, log)
    old, new = palette_lin(install, default) if default else None, palette_lin(install, paints[paint])
    if not old or not new:
        raise ValueError(f"couldn't read the colors of {paint}")
    return _repainted(install, settings, asset, glb, paint, old, new, log)


def _paint_geometry(record: dict, paint: str) -> str:
    """The hull geometry file a paint's node (or the ship's own) uses."""
    found = {"": ""}

    def walk(o):
        if isinstance(o, dict):
            if o.get("_Type_") == "SGeometryNodeParams":
                path = (((o.get("Geometry") or {}).get("Geometry") or {}).get("path")) or ""
                tags = [t for t in re.split(r"[\s,]+", o.get("Tags") or "") if t]
                if path and paint in tags:
                    found.setdefault(paint, path)
                elif path and not tags and not found[""]:
                    found[""] = path
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(record)
    return found.get(paint) or found[""]


def _activated(base: dict, liv: dict | None) -> list[str]:
    """Submaterials the livery switches on that the base material leaves off: the export
    has no faces for them (the Idris Sharktooth's shark mouth decal)."""
    state = lambda sm: (((sm or {}).get("activation_state") or {}).get("state") or "active")  # noqa: E731
    return [n for n, sm in (liv or {}).items() if n in base and state(base[n]) == "inactive" and state(sm) == "active"]


def graft_faces(doc: dict, bin_: bytes, parts: list[dict], prefix: str) -> tuple[bytes, int]:
    """Add livery-only faces (from livery_faces.py: per object, in its own coordinates)
    under the export's node of the same name, drawn with the doc's material for that
    submaterial. Returns (binary, pieces added)."""
    import numpy as np
    mats = {}
    for i, m in enumerate(doc.get("materials", [])):
        sub = (m.get("extras") or {}).get("source_name")
        if sub and (m.get("name") or "").lower().startswith(prefix) and sub not in mats:
            mats[sub] = i
    by_name: dict[str, list[int]] = {}
    for i, nd in enumerate(doc.get("nodes", [])):
        by_name.setdefault(nd.get("name") or "", []).append(i)
    added = 0
    for part in parts:
        targets = by_name.get(part.get("object") or "", [])
        sub = part.get("material")
        if len(targets) != 1 or sub not in mats or not part.get("triangles"):
            continue   # no single place to put it, or nothing to draw it with
        attrs = {}
        bin_, attrs["POSITION"] = _add_array(doc, bin_, np.asarray(part["positions"], np.float32), True)
        bin_, attrs["NORMAL"] = _add_array(doc, bin_, np.asarray(part["normals"], np.float32), False)
        for key, uv in (("TEXCOORD_0", "uv0"), ("TEXCOORD_1", "uv1")):
            a = np.asarray(part[uv], np.float32)
            a[:, 1] = 1.0 - a[:, 1]          # Blender's v runs up, glTF's down
            bin_, attrs[key] = _add_array(doc, bin_, a, False)
        bin_, ia = _add_array(doc, bin_, np.asarray(part["triangles"], np.uint32).reshape(-1, 1), False, indices=True)
        doc["meshes"].append({"name": f"livery_{part['object']}",
                              "primitives": [{"attributes": attrs, "indices": ia, "material": mats[sub], "mode": 4}]})
        node = len(doc["nodes"])
        doc["nodes"].append({"name": f"livery_{part['object']}_{sub}", "mesh": len(doc["meshes"]) - 1})
        doc["nodes"][targets[0]].setdefault("children", []).append(node)
        added += 1
    return bin_, added


def _add_array(doc: dict, bin_: bytes, arr, with_bounds: bool, indices: bool = False) -> tuple[bytes, int]:
    import numpy as np
    bin_ += b"\0" * (-len(bin_) % 4)
    data = np.ascontiguousarray(arr).tobytes()
    views = doc.setdefault("bufferViews", [])
    views.append({"buffer": 0, "byteOffset": len(bin_), "byteLength": len(data),
                  "target": 34963 if indices else 34962})
    bin_ += data
    doc.setdefault("buffers", [{}])[0]["byteLength"] = len(bin_)
    acc = {"bufferView": len(views) - 1, "componentType": 5125 if indices else 5126, "count": int(arr.shape[0]),
           "type": "SCALAR" if indices else {2: "VEC2", 3: "VEC3", 4: "VEC4"}[arr.shape[1]]}
    if with_bounds:
        acc["min"] = [float(x) for x in arr.min(axis=0)]
        acc["max"] = [float(x) for x in arr.max(axis=0)]
    doc.setdefault("accessors", []).append(acc)
    return bin_, len(doc["accessors"]) - 1


LIVERY_FACES_SCRIPT = Path(__file__).parent / "blender" / "livery_faces.py"


def livery_faces(install: Install, settings: dict, asset: dict, pkg: Path, geo_path: str,
                 names: list[str], log) -> list[dict]:
    """The faces of the named submaterials from the paint package's mesh file for the
    hull (StarBreaker keeps them there; the bundled export drops faces whose material is
    off in the stock paint). Needs Blender; cached. [] when not available."""
    import hashlib
    import subprocess
    root = pkg.parent.parent
    rel = Path(geo_path.replace("\\", "/").lstrip("/"))
    folder = root / "Data" / rel.parent
    if not folder.is_dir():
        hits = [d for d in (root / "Data").rglob("*") if d.is_dir() and d.as_posix().lower().endswith(rel.parent.as_posix().lower())]
        folder = hits[0] if hits else folder
    # the hull and everything beside it (doors and covers wear the hull's material too)
    blends = sorted(folder.glob("*_LOD*.blend")) if folder.is_dir() else []
    if not blends:
        return []
    key = hashlib.sha1(("|".join(sorted(names)) + "|".join(map(str, blends))).encode()).hexdigest()[:12]
    cache = models.models_dir(install) / f"{models._safe(asset['record'])}.livery_{key}.json"
    if not cache.exists():
        try:
            blender = tools.find("blender", settings)
        except tools.ToolError:
            log("livery decals skipped: needs Blender")
            return []
        log("reading the livery's own decals (Blender)")
        tmp = cache.with_name(cache.name + ".part")
        try:
            subprocess.run([str(blender), "-b", "--python", str(LIVERY_FACES_SCRIPT), "--", str(tmp), ",".join(names),
                            *map(str, blends)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, env=tools.child_env(None), timeout=300,
                           creationflags=tools.NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"livery decals skipped: {e}")
            return []
        if not tmp.exists():
            log("livery decals skipped: Blender wrote nothing")
            return []
        tmp.replace(cache)
    try:
        return json.loads(cache.read_text(encoding="utf-8")).get("parts") or []
    except (OSError, ValueError):
        return []


# Stencils this narrow came out of the paint package at an eighth of their size: a fine
# camo (the Idris Sharktooth's tiger stripes) blurs into flat grey there.
STENCIL_MIN_PX = 1024


def sharpen_stencils(install: Install, settings: dict, root: Path, sources, log=print) -> int:
    """Swap the package's small stencil images for the full-size ones decoded from the
    game files. Returns how many were replaced."""
    from PIL import Image
    _, base, liv = sources
    paths = {_texture(sm, "stencil", "TexSlot7") for sm in [*base.values(), *(liv or {}).values()]}
    done = 0
    for rel in sorted(p for p in paths if p):
        png = root / rel
        try:
            if not png.is_file() or Image.open(png).width >= STENCIL_MIN_PX:
                continue
        except OSError:
            continue
        dds = re.sub(r"_TEX\d+\.png$", ".dds", rel, flags=re.I)
        if dds == rel:
            continue
        tmp = png.with_name(png.stem + ".full.png")
        try:
            tools.run(settings, "decode_stencil", install, input=dds.replace("/", "\\"), out=str(tmp))
            if Image.open(tmp).width > Image.open(png).width:
                tmp.replace(png)
                done += 1
        except (tools.ToolError, OSError) as e:
            log(f"kept the small stencil {Path(rel).name}: {e}")
        finally:
            tmp.unlink(missing_ok=True)
    return done


def _repainted(install: Install, settings: dict, asset: dict, glb: Path, paint: str, old: dict, new: dict, log) -> Path:
    out = glb.with_name(glb.name[:-4] + f".{models._safe(paint).lower()}.l{LIVERY_VERSION}.glb")
    pkg = paint_package(install, settings, asset, log)
    with models._lock(str(out)):   # the viewer and a print may paint the same file at once
        if out.exists() and out.stat().st_mtime >= glb.stat().st_mtime:
            return out
        sources = livery_sources(pkg, asset["record"], paint) if pkg else None
        if sources:
            sharpen_stencils(install, settings, pkg.parent.parent, sources, log)
        doc, bin_, version = models._read_glb(glb)
        own = own_paint_items(install, doc, paint) if sources and sources[2] is not None else set()
        bin_, n = repaint(doc, bin_, old, new, sources, pkg.parent.parent if pkg else None, own)
        if sources and sources[2] is not None:
            names = _activated(sources[1], sources[2])
            geo = _paint_geometry(_record_json(install, asset) or {}, paint) if names else ""
            parts = livery_faces(install, settings, asset, pkg, geo, names, log) if geo else []
            if parts:
                bin_, k = graft_faces(doc, bin_, parts, f"{sources[0].lower()}_mtl_")
                if k:
                    log(f"added {k} livery-only part(s): {', '.join(names)}")
        models._write_glb(out, doc, bin_, version)
        log(f"painted {n} material(s) in {paint}" + ("" if sources else " (colors only)"))
    return out
