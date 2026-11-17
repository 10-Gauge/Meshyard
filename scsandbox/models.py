"""Turn an indexed asset into a GLB file, cached per build.

Entities (ships, vehicles, items) go through `starbreaker entity export`, which
assembles the full loadout. If that fails, or the asset is a raw geometry file,
the geometry is extracted from Data.p4k and converted with cgf-converter.
"""

from __future__ import annotations

import json
import math
import shutil
import struct
import re
import threading
from pathlib import Path

from . import catalog, tools
from .installs import Install

VARIANTS = {
    # variant: (command, lod)
    "preview": ("export_entity_preview", "1"),
    "full": ("export_entity", "0"),
    "print": ("export_entity_print", "0"),
}
# Cache file tag per variant; bump when a variant's export settings change.
FILE_TAG = {"print": "print-m",     # print-m: exported with material names
            "preview": "preview-d"}  # preview-d: LOD 1, eighth-size textures, no lights
# Past this the viewer model is exported again lighter (LOD 2, sixteenth-size textures).
PREVIEW_MAX_BYTES = 250 * 2**20
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]


def models_dir(install: Install) -> Path:
    d = catalog.build_dir(install) / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def glb_for(install: Install, settings: dict, asset: dict, variant: str = "preview", log=print) -> Path:
    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {list(VARIANTS)}")
    out = models_dir(install) / f"{_safe(asset['record'])}.{FILE_TAG.get(variant, variant)}.glb"
    with _lock(str(out)):
        if out.exists() and out.stat().st_size > 0:
            if asset["kind"] == "entity":
                if "fps_weapons" in (asset.get("subcategory") or ""):
                    complete_weapon(out, install, settings, asset, log)
                place_interiors(out, install, asset, log)
                fix_interior_hulls(out, log)
                fix_floating_parts(out, log)
                reattach_orphans(out, log)
                fix_port_parts(out, log)
                fix_tagged_geometry(out, install, settings, log)
                drop_empty_meshes(out, log)
                fix_borrowed_meshes(out, log)
                fix_item_meshes(out, log)
            return out
        errors = []
        if asset["kind"] == "entity":
            command, lod = VARIANTS[variant]
            try:
                tools.run(settings, command, install, name=asset["record"], out=str(out), lod=lod)
                if variant == "preview" and out.exists() and out.stat().st_size > PREVIEW_MAX_BYTES:
                    # capital ships (Idris, Javelin) at this detail are too heavy for the
                    # viewer: use the distance mesh and smaller textures ("Full detail" has all)
                    log("large ship: using a lighter viewer model")
                    tools.run(settings, "export_entity_preview_light", install, name=asset["record"], out=str(out), lod="2")
                if out.exists() and "fps_weapons" in (asset.get("subcategory") or ""):
                    complete_weapon(out, install, settings, asset, log)
                if out.exists():
                    place_interiors(out, install, asset, log)
                    fix_interior_hulls(out, log)
                    fix_floating_parts(out, log)
                    reattach_orphans(out, log)
                    fix_port_parts(out, log)
                    fix_tagged_geometry(out, install, settings, log)
                    drop_empty_meshes(out, log)
                    fix_borrowed_meshes(out, log)
                    fix_item_meshes(out, log)
                    return out
                errors.append("StarBreaker finished but wrote no file")
            except tools.ToolError as e:
                errors.append(str(e))
                log(f"entity export failed for {asset['record']}, trying raw geometry")
        geometry = json.loads(asset.get("geometry") or "[]")
        # A .cdf only wraps a mesh (some guns: an empty attachment list that StarBreaker
        # rejects); try the meshes it would load, which share its name.
        meshes = [g for g in geometry if not g.lower().endswith(".cdf")]
        for g in geometry:
            if g.lower().endswith(".cdf"):
                meshes += [g[:-4] + ext for ext in (".cga", ".cgf", ".skin", ".chr")]
        geometry = meshes or geometry
        for geo in geometry[:6]:
            try:
                return _convert_raw(install, settings, geo, out)
            except tools.ToolError as e:
                errors.append(str(e))
        raise tools.ToolError(f"Could not build a model for {asset['record']}:\n" + "\n---\n".join(errors[-3:]))


def package_for(install: Install, settings: dict, asset: dict, lod: str = "1", log=print) -> Path:
    """Export an entity as a StarBreaker decomposed package (with animations) and
    return its scene.json, which the StarBreaker Blender add-on imports."""
    if asset["kind"] != "entity":
        raise tools.ToolError("Animated export works for ships, vehicles and items, not raw geometry files")
    out = models_dir(install) / f"{_safe(asset['record'])}.animated"
    with _lock(str(out)):
        found = sorted(out.glob("Packages/**/scene.json"))
        if found:
            return found[0]
        log("exporting animated package (geometry, textures and animation clips)")
        tools.run(settings, "export_entity_animated", install, name=asset["record"], out=str(out), lod=lod)
        found = sorted(out.glob("Packages/**/scene.json"))
        if not found:
            raise tools.ToolError(f"StarBreaker wrote no scene.json under {out}")
        return found[0]


def _helper_points(scene) -> dict:
    geo = set(scene.graph.nodes_geometry)
    out = {}
    for n in scene.graph.nodes:
        if n not in geo:
            try:
                out[n] = scene.graph.get(n)[0][:3, 3]
            except Exception:
                pass
    return out


COMPLETE_VERSION = 1   # bump to re-check cached gun exports


def complete_weapon(glb: Path, install: Install, settings: dict, asset: dict, log=print) -> bool:
    """Some guns are animated .cga models behind a .cdf, and StarBreaker exports only some
    of their parts (CQ7, Pulverizer, Tripledown, Boomtube body) or puts some in the wrong
    place (Prism: pieces behind and above the gun). Convert the .cga directly once and use
    it when it's clearly more complete, or when the export sticks out past it, keeping
    the magazine StarBreaker placed from the loadout. True if the file was replaced."""
    import trimesh
    mark = glb.with_name(glb.name + ".checked")
    if mark.exists() and mark.read_text().strip() == str(COMPLETE_VERSION):
        return False
    cdfs = [g for g in json.loads(asset.get("geometry") or "[]") if g.lower().endswith(".cdf")]
    replaced = False
    try:
        sc = trimesh.load(str(glb), force="scene")
        if cdfs and sc.geometry:
            raw = glb.with_name(glb.stem + ".raw.glb")
            try:
                for ext in (".cga", ".cgf", ".skin", ".chr"):
                    try:
                        _convert_raw(install, settings, cdfs[0][:-4] + ext, raw)
                        break
                    except tools.ToolError:
                        continue
                else:
                    raise FileNotFoundError
                full = trimesh.load(str(raw), force="scene")
                lo, hi = sc.bounds
                flo, fhi = full.bounds
                missing = (fhi - flo)[2] > 1.15 * (hi - lo)[2]                   # parts left out
                stray = bool(((flo - lo) > 0.10).any() or ((hi - fhi) > 0.10).any())  # parts misplaced
                if full.geometry and (missing or stray):
                    for n in sc.graph.nodes_geometry:   # the magazine only comes with StarBreaker's export
                        if re.search(r"(?i)mgzn|_mag(_|$)|magazine", n):
                            T, g = sc.graph[n]
                            full.add_geometry(sc.geometry[g], node_name=n, geom_name=f"{n}_mesh", transform=T)
                    full.export(str(glb))
                    replaced = True
                    log(f"  {asset['record']}: StarBreaker's export was " + ("incomplete" if missing else "misassembled")
                        + "; using the whole model")
            finally:
                raw.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        mark.write_text(str(COMPLETE_VERSION))
    except OSError:
        pass
    return replaced


def _convert_raw(install: Install, settings: dict, geo_path: str, out: Path) -> Path:
    geo_path = geo_path.replace("\\", "/").lstrip("/")
    if not geo_path.lower().startswith("data/"):
        geo_path = "Data/" + geo_path
    folder = geo_path.rsplit("/", 1)[0]
    extract_root = catalog.build_dir(install) / "extracted"
    # Pull the whole folder: CryEngine splits meshes across .cga/.cgam/.cgf/.cgfm files.
    tools.run(settings, "extract_files", install, out=str(extract_root), filter=f"{folder}/*")
    src = _find_case_insensitive(extract_root, geo_path)
    if src is None:
        # The archive's filter is case-sensitive and some records spell paths in lower
        # case (objects/characters/...): find the file's real spelling by its name.
        real = _real_path(install, settings, geo_path)
        if real and real != geo_path:
            tools.run(settings, "extract_files", install, out=str(extract_root),
                      filter=f"{real.rsplit('/', 1)[0]}/*")
            src = _find_case_insensitive(extract_root, real)
    if src is None:
        raise tools.ToolError(f"{geo_path} was not found after extraction")
    data_dir = _find_case_insensitive(extract_root, "Data") or extract_root
    conv_out = out.parent / f".conv_{out.stem}"
    conv_out.mkdir(exist_ok=True)
    tools.run(settings, "convert_geometry", None, tool="cgf_converter",
              input=str(src), objectdir=str(data_dir), out=str(conv_out))
    glbs = sorted(conv_out.rglob("*.glb"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not glbs:
        raise tools.ToolError(f"cgf-converter wrote no .glb for {geo_path}")
    glbs[0].replace(out)
    return out


def _real_path(install: Install, settings: dict, geo_path: str) -> str | None:
    """geo_path as the archive spells it (matched without regard to case), or None."""
    name = geo_path.rsplit("/", 1)[-1]
    try:
        out = tools.run(settings, "list_files", install, filter=f"**/{name}")
    except tools.ToolError:
        return None
    want = geo_path.lower()
    for line in out.splitlines():
        path = line.split("\t")[0].strip().replace("\\", "/")
        if path.lower() == want:
            return path
    return None


def _find_case_insensitive(root: Path, rel: str) -> Path | None:
    cur = root
    for part in Path(rel).parts:
        if not cur.is_dir():
            return None
        match = next((c for c in cur.iterdir() if c.name.lower() == part.lower()), None)
        if match is None:
            return None
        cur = match
    return cur


# ---------- interior placement ----------
# StarBreaker exports a ship's interiors (its .socpak object containers) as nodes
# named "interior_<socpak name>" with an identity transform, so they all pile up
# at the ship's origin. The entity record lists each container's real placement
# (SVehicleObjectContainerParams.Offset, ship space, angles in degrees), which
# matches the ship's own animated_interior_* bones; apply it to those nodes.

_IDENTITY = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]


def _container_offsets(record: object) -> dict[str, list[float]]:
    found: dict[str, list[float]] = {}

    def walk(o):
        if isinstance(o, dict):
            if o.get("_Type_") == "SVehicleObjectContainerParams" and o.get("fileName") and o.get("Offset"):
                name = o["fileName"].replace("\\", "/").split("/")[-1].rsplit(".", 1)[0].lower()
                found.setdefault(f"interior_{name}", _quatt_matrix(o["Offset"]))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(record)
    return found


def _quatt_matrix(offset: dict) -> list[float]:
    """CryEngine QuatT with an Ang3 rotation (degrees, applied X then Y then Z) as a glTF column-major matrix."""
    pos, ang = offset.get("Position") or {}, offset.get("Rotation") or {}
    x, y, z = (math.radians(float(ang.get(k, 0.0))) for k in "xyz")
    cx, sx, cy, sy, cz, sz = math.cos(x), math.sin(x), math.cos(y), math.sin(y), math.cos(z), math.sin(z)
    # Rz * Ry * Rx, row-major
    r = [
        [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
        [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
        [-sy, cy * sx, cy * cx],
    ]
    t = [float(pos.get(k, 0.0)) for k in "xyz"]
    return [r[0][0], r[1][0], r[2][0], 0.0, r[0][1], r[1][1], r[2][1], 0.0,
            r[0][2], r[1][2], r[2][2], 0.0, t[0], t[1], t[2], 1.0]


def _is_identity(node: dict) -> bool:
    if "matrix" in node:
        return all(abs(a - b) < 1e-6 for a, b in zip(node["matrix"], _IDENTITY))
    return not any(k in node for k in ("translation", "rotation", "scale"))


def place_interiors(glb: Path, install: Install, asset: dict, log=print) -> int:
    """Move interior nodes of an exported GLB to their container offsets. Returns how many moved."""
    rec = catalog.build_dir(install) / "records" / (asset.get("record_path") or "")
    if not asset.get("record_path") or not rec.is_file():
        return 0
    try:
        offsets = _container_offsets(json.loads(rec.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return 0
    if not offsets:
        return 0
    with open(glb, "rb") as f:
        magic, version, _ = struct.unpack("<4sII", f.read(12))
        json_len, json_type = struct.unpack("<II", f.read(8))
        if magic != b"glTF" or json_type != 0x4E4F534A:
            return 0
        doc = json.loads(f.read(json_len))
        moved = 0
        nodes = doc.get("nodes", [])
        for scene in doc.get("scenes", []):
            for root in scene.get("nodes", []):
                # Interiors sit next to the ship under the Z-up root node (or at scene level).
                for i in [root, *nodes[root].get("children", [])]:
                    node = nodes[i]
                    m = offsets.get(node.get("name", "").lower())
                    if m and _is_identity(node) and m != _IDENTITY:
                        for k in ("translation", "rotation", "scale"):
                            node.pop(k, None)
                        node["matrix"] = m
                        moved += 1
        if not moved:
            return 0
        body = json.dumps(doc, separators=(",", ":")).encode("utf-8")
        body += b" " * (-len(body) % 4)
        tmp = glb.with_name(glb.name + ".tmp")
        with open(tmp, "wb") as w:
            rest = glb.stat().st_size - 20 - json_len
            w.write(struct.pack("<4sII", b"glTF", version, 12 + 8 + len(body) + rest))
            w.write(struct.pack("<II", len(body), 0x4E4F534A))
            w.write(body)
            shutil.copyfileobj(f, w, 16 * 1024 * 1024)
    tmp.replace(glb)
    log(f"placed {moved} interior section(s) inside {asset['record']}")
    return moved


# ---------- loadout item mesh repair ----------
# StarBreaker sometimes binds the wrong mesh to one part of one copy of a repeated
# loadout item: on the F7C-M Mk II one of four KLWE repeaters had its barrel_01 node
# pointing at the gun's whole body mesh, which printed as a second gun. Copies of
# the same item must use the same mesh for the same part, so take the majority.

def _edit_glb_json(glb: Path, edit) -> bool:
    """Run edit(doc) on the GLB's JSON chunk; rewrite the file if it returns True."""
    with open(glb, "rb") as f:
        magic, version, _ = struct.unpack("<4sII", f.read(12))
        json_len, json_type = struct.unpack("<II", f.read(8))
        if magic != b"glTF" or json_type != 0x4E4F534A:
            return False
        doc = json.loads(f.read(json_len))
        if not edit(doc):
            return False
        body = json.dumps(doc, separators=(",", ":")).encode("utf-8")
        body += b" " * (-len(body) % 4)
        tmp = glb.with_name(glb.name + ".tmp")
        with open(tmp, "wb") as w:
            rest = glb.stat().st_size - 20 - json_len
            w.write(struct.pack("<4sII", b"glTF", version, 12 + 8 + len(body) + rest))
            w.write(struct.pack("<II", len(body), 0x4E4F534A))
            w.write(body)
            shutil.copyfileobj(f, w, 16 * 1024 * 1024)
    tmp.replace(glb)
    return True


def drop_empty_meshes(glb: Path, log=print) -> int:
    """Remove references to meshes with no primitives. StarBreaker sometimes writes one
    (the Idris-M's front-left landing gear root), and trimesh, which the print, export
    and figure code load models with, skips such a node together with everything under
    it, so the whole gear leg vanished from prints. Returns references removed."""
    removed = 0

    def edit(doc) -> bool:
        nonlocal removed
        meshes = doc.get("meshes", [])
        for nd in doc.get("nodes", []):
            m = nd.get("mesh")
            if m is not None and (m >= len(meshes) or not meshes[m].get("primitives")):
                del nd["mesh"]
                removed += 1
        return removed > 0

    try:
        _edit_glb_json(glb, edit)
    except OSError:  # open elsewhere; fixed on the next load
        glb.with_name(glb.name + ".tmp").unlink(missing_ok=True)
        return 0
    if removed:
        log(f"removed {removed} empty mesh reference(s) that hid parts from prints")
    return removed


# an exterior model this big inside an interior is a whole hull, not a prop
HULL_FACES = 20_000


def drop_interior_hulls(doc: dict) -> int:
    """Unhook whole ship hulls StarBreaker drops into an interior.

    The Guardian MX's interior lists 22 pieces StarBreaker can't resolve, and each
    comes out as the base Guardian's entire exterior ("Exterior/mrai_guardian.cga",
    420,000 faces) at the piece's spot, so the ship showed five or so hulls stacked
    inside each other. Interiors never carry an exterior model that big. Returns
    references removed."""
    nodes, meshes, acc = doc.get("nodes", []), doc.get("meshes", []), doc.get("accessors", [])

    def faces(m):
        return sum(acc[p["indices"]]["count"] // 3 for p in meshes[m].get("primitives", [])
                   if "indices" in p and p["indices"] < len(acc))

    removed = 0
    for nd in nodes:
        if not nd.get("name", "").lower().startswith("interior_"):
            continue
        todo, seen = list(nd.get("children", [])), set()
        while todo:
            k = todo.pop()
            if k in seen or k >= len(nodes):
                continue
            seen.add(k)
            todo += nodes[k].get("children", [])
            m = nodes[k].get("mesh")
            if m is None or m >= len(meshes):
                continue
            name = meshes[m].get("name", "").replace("\\", "/").lower()
            if "/exterior/" in name and faces(m) > HULL_FACES:
                del nodes[k]["mesh"]
                removed += 1
    return removed


def stray_limit(dist) -> float:
    """How far from a model's middle a part can sit and still belong to it: stray parts
    are a few odd ones beyond a clear gap. A long ship's engines (the Javelin's, 200 m
    back) run on continuously from the hull and stay in. Same rule as the viewer's."""
    import numpy as np
    d = np.sort(np.asarray(dist, dtype=float))
    if len(d) < 20:
        return float("inf")
    start = int(len(d) * 0.9)
    gap = max(2.0, 0.25 * d[start])
    for i in range(max(start, 1), len(d)):
        if d[i] - d[i - 1] > gap:
            return float(d[i - 1]) + 1e-6
    return float("inf")


def drop_stray_interior_parts(doc: dict) -> int:
    """Unhook interior pieces StarBreaker places far outside the ship: a Carrack panel
    sits 100 m off to the side, a Tiburon one 180 m away. They framed the ship as a
    speck and shrank prints. Only interior pieces, and only beyond a clear gap from
    the rest of the model. Returns references removed."""
    import numpy as np
    nodes = doc.get("nodes", [])
    parent = {}
    for i, nd in enumerate(nodes):
        for c in nd.get("children", []):
            parent.setdefault(c, i)
    cache: dict = {}

    def world(i, depth=0):
        if i not in cache:
            p = parent.get(i)
            up = world(p, depth + 1) if p is not None and depth < 200 else np.eye(4)
            cache[i] = up @ _node_matrix(nodes[i])
        return cache[i]

    def inside(i):
        k, n = i, 0
        while k is not None and n < 200:
            if nodes[k].get("name", "").lower().startswith("interior_"):
                return True
            k, n = parent.get(k), n + 1
        return False

    parts = [i for i, nd in enumerate(nodes) if "mesh" in nd]
    if len(parts) < 20:
        return 0
    pos = np.array([world(i)[:3, 3] for i in parts])
    mid = np.median(pos, axis=0)
    dist = np.linalg.norm(pos - mid, axis=1)
    limit = stray_limit(dist)
    removed = 0
    for i, d in zip(parts, dist):
        if d > limit and inside(i):
            del nodes[i]["mesh"]
            removed += 1
    return removed


def _accessor(doc: dict, bin_: bytes, ai: int):
    import numpy as np
    a = doc["accessors"][ai]
    bv = doc["bufferViews"][a["bufferView"]]
    comp = {5126: np.float32, 5125: np.uint32, 5123: np.uint16, 5121: np.uint8}[a["componentType"]]
    nc = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[a["type"]]
    start = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    stride, isz = bv.get("byteStride", 0), np.dtype(comp).itemsize * nc
    if stride and stride != isz:
        raw = np.frombuffer(bin_, np.uint8, count=stride * (a["count"] - 1) + isz, offset=start)
        return raw[np.arange(a["count"])[:, None] * stride + np.arange(isz)].copy().view(comp).reshape(a["count"], nc)
    return np.frombuffer(bin_, comp, count=a["count"] * nc, offset=start).reshape(a["count"], nc)


def part_boxes(doc: dict, bin_: bytes) -> list[tuple[int, object, object]]:
    """(node, world min, world max) of every mesh node, measured on the vertices its
    triangles use (parts share big vertex buffers)."""
    import numpy as np
    nodes, meshes = doc.get("nodes", []), doc.get("meshes", [])
    parent = {}
    for i, nd in enumerate(nodes):
        for c in nd.get("children", []):
            parent.setdefault(c, i)
    cache: dict = {}

    def world(i, depth=0):
        if i not in cache:
            p = parent.get(i)
            cache[i] = (world(p, depth + 1) if p is not None and depth < 200 else np.eye(4)) @ _node_matrix(nodes[i])
        return cache[i]

    local: dict = {}
    out = []
    for i, nd in enumerate(nodes):
        m = nd.get("mesh")
        if m is None or m >= len(meshes):
            continue
        if m not in local:
            pts = []
            for p in meshes[m].get("primitives", []):
                if "POSITION" not in p.get("attributes", {}):
                    continue
                pos = _accessor(doc, bin_, p["attributes"]["POSITION"])
                if "indices" in p:
                    pos = pos[np.unique(_accessor(doc, bin_, p["indices"]).ravel())]
                pts.append(pos)
            local[m] = np.concatenate(pts).astype(float) if pts else None
        pts = local[m]
        if pts is None or not len(pts):
            continue
        # a box's 8 corners are enough to place it
        lo, hi = pts.min(0), pts.max(0)
        corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
        w = world(i)
        wc = corners @ w[:3, :3].T + w[:3, 3]
        out.append((i, wc.min(0), wc.max(0)))
    return out


# an interior piece this far from every other part floats in mid-air
FLOAT_GAP_M = 0.75


def isolated_interior_parts(doc: dict, bin_: bytes) -> list[int]:
    """Interior pieces that touch nothing: the Avenger's carries one 1.5 m above its
    hull. Every other part's box is at least FLOAT_GAP_M away."""
    import numpy as np
    nodes = doc.get("nodes", [])
    parent = {}
    for i, nd in enumerate(nodes):
        for c in nd.get("children", []):
            parent.setdefault(c, i)

    def inside(i):
        k, n = i, 0
        while k is not None and n < 200:
            if nodes[k].get("name", "").lower().startswith("interior_"):
                return True
            k, n = parent.get(k), n + 1
        return False

    boxes = part_boxes(doc, bin_)
    if len(boxes) < 20:
        return []
    lo = np.array([b[1] for b in boxes])
    hi = np.array([b[2] for b in boxes])
    out = []
    for k, (i, a, b) in enumerate(boxes):
        if not inside(i):
            continue
        gap = np.maximum(0, np.maximum(lo - b, a - hi)).max(axis=1)
        gap[k] = np.inf
        if gap.min() > FLOAT_GAP_M:
            out.append(i)
    return out


def fix_floating_parts(glb: Path, log=print) -> int:
    """Drop floating interior pieces, once per model file (it reads the whole model)."""
    mark = glb.with_name(glb.name + ".floating")
    try:
        if mark.exists() and mark.read_text().strip() == str(glb.stat().st_mtime_ns):
            return 0
    except OSError:
        pass
    try:
        doc, bin_, _ = _read_glb(glb)
        drop = set(isolated_interior_parts(doc, bin_))
    except (OSError, ValueError, KeyError, IndexError) as e:
        log(f"couldn't check {glb.name} for floating parts: {e}")
        return 0
    if drop:
        def edit(d) -> bool:
            for i in drop:
                d["nodes"][i].pop("mesh", None)
            return True
        try:
            _edit_glb_json(glb, edit)
        except OSError:
            glb.with_name(glb.name + ".tmp").unlink(missing_ok=True)
            return 0
        log(f"removed {len(drop)} interior piece(s) floating outside the hull")
    try:
        mark.write_text(str(glb.stat().st_mtime_ns))
    except OSError:
        pass
    return len(drop)


def fix_interior_hulls(glb: Path, log=print) -> int:
    removed = 0

    def edit(doc) -> bool:
        nonlocal removed
        hulls = drop_interior_hulls(doc)
        strays = drop_stray_interior_parts(doc)
        removed = hulls + strays
        if strays:
            log(f"removed {strays} interior piece(s) placed far outside the ship")
        return removed > 0

    try:
        _edit_glb_json(glb, edit)
    except OSError:
        glb.with_name(glb.name + ".tmp").unlink(missing_ok=True)
        return 0
    if removed:
        log(f"removed {removed} stray copies of the hull from the interior")
    return removed


def reattach_orphans(glb: Path, log=print) -> int:
    """Hang loadout items that StarBreaker left out of the scene back on their hardpoint.

    When an item's own skeleton has a bone named like the ship's hardpoint it mounts on
    (the Idris's spinal railgun has its own "hardpoint_nose_railgun"), StarBreaker
    parents the item to that inner bone instead of the ship's. The item then hangs from
    itself: a loop no viewer, slicer or exporter reaches, so the part silently vanishes.
    Move each such item to the reachable node of the same name. Returns items moved."""
    moved = 0

    def edit(doc) -> bool:
        nonlocal moved
        nodes = doc.get("nodes", [])
        scene = doc.get("scenes", [{}])[doc.get("scene", 0)] if doc.get("scenes") else {}

        def reach():
            seen, todo = set(), list(scene.get("nodes", []))
            while todo:
                k = todo.pop()
                if k in seen or k >= len(nodes):
                    continue
                seen.add(k)
                todo += nodes[k].get("children", [])
            return seen

        def subtree(i):
            seen, todo = set(), [i]
            while todo:
                k = todo.pop()
                if k in seen:
                    continue
                seen.add(k)
                todo += nodes[k].get("children", [])
            return seen

        reachable = reach()
        by_name: dict[str, list[int]] = {}
        for i in reachable:
            if nodes[i].get("name"):
                by_name.setdefault(nodes[i]["name"], []).append(i)
        for p, nd in enumerate(nodes):
            if p in reachable or not nd.get("children"):
                continue
            targets = by_name.get(nd.get("name", ""), [])
            if not targets:
                continue
            for c in list(nd["children"]):
                if c in reachable or p not in subtree(c):
                    continue  # only items that hang from themselves
                # prefer the ship's empty mount point of that name
                h = min(targets, key=lambda t: len(nodes[t].get("children", [])))
                nd["children"].remove(c)
                nodes[h].setdefault("children", []).append(c)
                moved += 1
                reachable |= subtree(c)
                log(f"reattached {nodes[c].get('name', c)} to {nodes[h].get('name')}")
        return moved > 0

    try:
        _edit_glb_json(glb, edit)
    except OSError as e:  # the file is open elsewhere (a viewer); fixed on the next load
        glb.with_name(glb.name + ".tmp").unlink(missing_ok=True)
        log(f"couldn't update {glb.name} yet ({e.strerror}); it's repaired next time it's loaded")
        return 0
    return moved


def _node_matrix(nd: dict):
    import numpy as np
    if "matrix" in nd:
        return np.array(nd["matrix"], dtype=float).reshape(4, 4).T
    x, y, z, w = nd.get("rotation", [0.0, 0.0, 0.0, 1.0])
    r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    m = np.eye(4)
    m[:3, :3] = r * np.array(nd.get("scale", [1.0, 1.0, 1.0]))
    m[:3, 3] = nd.get("translation", [0.0, 0.0, 0.0])
    return m


def mount_port_parts(doc: dict) -> list[tuple[str, str]]:
    """Hang a modular weapon's parts on the ports of the weapon they belong to.

    Ship guns are built from parts: the Ares Inferno's Behring gatling has a barrel,
    firing mechanism, power array and ventilation, each mounted on a port of the gun
    ("hardpoint_bar", "hardpoint_mec", "hardpoint_pow", "hardpoint_ven"). StarBreaker
    puts each part at its port's position but hangs it on the ship with the ship's
    rotation, not the port's. Where the gun is mounted rolled (the Ares's sits on its
    side) the parts come out turned 90 degrees: the ventilation box stuck out beside the
    gun instead of sitting inside its housing. A part is recognized by its port's name
    inside its own model name ("behr_bal_gat_s7_ven_1" for "hardpoint_ven") and by
    sitting exactly at the port. Returns (part, port) names moved."""
    import numpy as np
    nodes = doc.get("nodes", [])
    parent = {}
    for i, nd in enumerate(nodes):
        for c in nd.get("children", []):
            parent.setdefault(c, i)
    cache: dict[int, object] = {}

    def world(i, seen=()):
        if i not in cache:
            p = parent.get(i)
            up = world(p, seen + (i,)) if p is not None and p not in seen else np.eye(4)
            cache[i] = up @ _node_matrix(nodes[i])
        return cache[i]

    def ancestors(i):
        out, k = set(), i
        while k in parent and parent[k] not in out:
            k = parent[k]
            out.add(k)
        return out

    ports = [i for i, nd in enumerate(nodes)
             if re.fullmatch(r"(?i)hardpoint_[a-z]{3,}", nd.get("name", "")) and not nd.get("children")]
    moved = []
    for h in ports:
        suffix = nodes[h]["name"].split("_", 1)[1].lower()
        token = re.compile(rf"(?i)_{re.escape(suffix)}(?:_|\d|$)")
        hw, above = world(h), ancestors(h)
        for c in list(parent):
            nd = nodes[c]
            if c == h or c in above or nd.get("name", "").lower().startswith("hardpoint_"):
                continue
            if parent[c] not in above:
                continue  # only parts hung on something the port's weapon also hangs from
            names = [nd.get("name", "")] + [nodes[k].get("name", "") for k in nd.get("children", [])]
            if not any(token.search(n.replace("\\", "/").split("/")[-1]) for n in names):
                continue
            cw = world(c)
            if np.linalg.norm(cw[:3, 3] - hw[:3, 3]) > 1e-3 or np.allclose(cw[:3, :3], hw[:3, :3], atol=1e-4):
                continue
            nodes[parent[c]]["children"].remove(c)
            nodes[h].setdefault("children", []).append(c)
            for k in ("matrix", "translation", "rotation", "scale"):
                nd.pop(k, None)
            parent[c] = h
            cache.clear()
            moved.append((nd.get("name", str(c)), nodes[h]["name"]))
            break
    return moved


def fix_port_parts(glb: Path, log=print) -> int:
    moved = []

    def edit(doc) -> bool:
        moved.extend(mount_port_parts(doc))
        return bool(moved)

    try:
        if _edit_glb_json(glb, edit):
            log(f"turned {len(moved)} weapon part(s) to match their mount: " + ", ".join(p for p, _ in moved))
    except OSError:
        glb.with_name(glb.name + ".tmp").unlink(missing_ok=True)
    return len(moved)


# ---------- tagged variant geometry ----------
# An item's geometry can list variants under SubGeometry, picked by a tag ("Left",
# "Right"). Crusader copied the Freelancer thruster template for the Starlifter, so its
# thrusters still carry the Freelancer's left and right engines as tagged variants. The
# game shows the item's own model; StarBreaker uses the variant instead, so a 10 m
# Freelancer engine (red and gray) sticks out under the hull and into the cargo bay
# where each small Crusader VTOL and main thruster should be. Swap the item's own
# model back in (converted with cgf-converter, which has no UVs: plain material).

TAGGED_VERSION = 1
# cgf-converter writes glTF Y-up; StarBreaker's items sit in CryEngine Z-up space
_Y_UP_TO_CRY = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]
_PART_MATERIAL = {"name": "thruster_part", "pbrMetallicRoughness": {
    "baseColorFactor": [0.30, 0.30, 0.32, 1.0], "metallicFactor": 0.6, "roughnessFactor": 0.5}}


def _stem(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()


def _folder(path: str) -> str:
    return path.replace("\\", "/").lower().lstrip("/").removeprefix("data/").rsplit("/", 1)[0]


def _model(path: str) -> str:
    """Maker and model from a geometry file name: "crus_starlifter" from CRUS_Starlifter_VTOL_R.cga."""
    return "_".join(_stem(path).split("_")[:2])


def tagged_geometry(record: object) -> tuple[str, set[str]] | None:
    """(own geometry path, stems of its leftover tagged variants) for an item record, or None.

    StarBreaker picks a tagged variant when the tag ("right") appears in the mount's name.
    That's right for real left and right parts of the same model (the Arrow's, Hawk's
    and 350r's thrusters), so only variants of another model in another folder count:
    geometry left over from the template the item was copied from (the Starlifter's
    thrusters point at misc_freelancer/, the Carrack's at the 600i's)."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("_Type_") == "SGeometryResourceParams":
                node = o.get("Geometry") or {}
                own = (((node.get("Geometry") or {}).get("Geometry") or {}).get("path") or "")
                paths = [((s.get("Geometry") or {}).get("Geometry") or {}).get("path") or ""
                         for s in node.get("SubGeometry") or [] if (s.get("Tags") or "").strip()]
                tagged = {_stem(p) for p in paths if p and _folder(p) != _folder(own)
                          and _model(p) != _model(own)}
                if own and tagged and _stem(own) not in tagged:
                    found.append((own, tagged))
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(record)
    return found[0] if found else None


def _read_glb(path: Path) -> tuple[dict, bytes, int]:
    data = path.read_bytes()
    magic, version, _ = struct.unpack_from("<4sII", data, 0)
    json_len, _ = struct.unpack_from("<II", data, 12)
    doc = json.loads(data[20:20 + json_len])
    rest = data[20 + json_len:]
    bin_ = b""
    if len(rest) >= 8:
        blen, _ = struct.unpack_from("<II", rest, 0)
        bin_ = rest[8:8 + blen]
    return doc, bin_, version


def _write_glb(path: Path, doc: dict, bin_: bytes, version: int = 2) -> None:
    body = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    body += b" " * (-len(body) % 4)
    bin_ += b"\0" * (-len(bin_) % 4)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as w:
        w.write(struct.pack("<4sII", b"glTF", version, 12 + 8 + len(body) + 8 + len(bin_)))
        w.write(struct.pack("<II", len(body), 0x4E4F534A))
        w.write(body)
        w.write(struct.pack("<II", len(bin_), 0x004E4942))
        w.write(bin_)
    tmp.replace(path)


def graft_part(doc: dict, bin_: bytes, part: dict, part_bin: bytes, material: int) -> tuple[bytes, dict]:
    """Copy a part GLB's meshes into doc (one shared plain material, skinning dropped).
    Returns the new binary chunk and the part's root nodes, ready to be instanced with
    instance_part()."""
    bin_ += b"\0" * (-len(bin_) % 4)
    offset = len(bin_)
    bin_ += part_bin
    doc.setdefault("buffers", [{}])[0]["byteLength"] = len(bin_)
    bv0, acc0, mesh0 = len(doc.setdefault("bufferViews", [])), len(doc.setdefault("accessors", [])), \
        len(doc.setdefault("meshes", []))
    for bv in part.get("bufferViews", []):
        doc["bufferViews"].append({**bv, "buffer": 0, "byteOffset": bv.get("byteOffset", 0) + offset})
    for a in part.get("accessors", []):
        a = dict(a)
        if "bufferView" in a:
            a["bufferView"] += bv0
        doc["accessors"].append(a)
    for m in part.get("meshes", []):
        prims = []
        for p in m.get("primitives", []):
            attrs = {k: v + acc0 for k, v in p.get("attributes", {}).items() if not k.startswith(("JOINTS_", "WEIGHTS_"))}
            q = {"attributes": attrs, "material": material, "mode": p.get("mode", 4)}
            if "indices" in p:
                q["indices"] = p["indices"] + acc0
            prims.append(q)
        doc["meshes"].append({"name": m.get("name", ""), "primitives": prims})
    scene = part.get("scenes", [{}])[part.get("scene", 0)]
    return bin_, {"part": part, "mesh0": mesh0, "roots": scene.get("nodes", [])}


def instance_part(doc: dict, grafted: dict, name: str) -> int:
    """Add a copy of a grafted part's node tree under a new node (Y-up to Z-up); returns it."""
    part, nodes = grafted["part"], doc["nodes"]

    def copy(i):
        src = part["nodes"][i]
        nd = {k: v for k, v in src.items() if k in ("name", "matrix", "translation", "rotation", "scale")}
        if "mesh" in src:
            nd["mesh"] = src["mesh"] + grafted["mesh0"]
        k = len(nodes)
        nodes.append(nd)
        kids = [copy(c) for c in src.get("children", [])]
        if kids:
            nodes[k]["children"] = kids
        return k

    wrap = len(nodes)
    nodes.append({"name": name, "matrix": list(_Y_UP_TO_CRY)})
    nodes[wrap]["children"] = [copy(r) for r in grafted["roots"]]
    return wrap


def fix_tagged_geometry(glb: Path, install: Install, settings: dict, log=print) -> int:
    """Replace tagged variant geometry StarBreaker chose over an item's own model (the
    Starlifter's thrusters were Freelancer engines). Returns items fixed."""
    mark = glb.with_name(glb.name + ".tagged")
    stamp = lambda: f"{TAGGED_VERSION}:{glb.stat().st_size}"  # noqa: E731  (a new export is checked again)
    try:
        if mark.read_text().strip() == stamp():
            return 0
    except OSError:
        pass
    doc, bin_, version = _read_glb(glb)
    nodes = doc.get("nodes", [])
    swaps = []   # (item node, bad child, own geometry path)
    records: dict[str, tuple | None] = {}
    con = None
    try:
        for hp in nodes:
            if not (hp.get("name") or "").lower().startswith("hardpoint_"):
                continue
            for it in hp.get("children", []):
                item = nodes[it].get("name") or ""
                kids = [(c, (nodes[c].get("name") or "").lower()) for c in nodes[it].get("children", [])]
                if not item or not kids:
                    continue
                if item not in records:
                    records[item] = None
                    con = con or catalog.connect(install)
                    row = con.execute("SELECT record_path FROM assets WHERE record = ? COLLATE NOCASE "
                                      "AND kind = 'entity'", (item,)).fetchone()
                    rec = catalog.build_dir(install) / "records" / row[0] if row and row[0] else None
                    if rec and rec.is_file():
                        try:
                            records[item] = tagged_geometry(json.loads(rec.read_text(encoding="utf-8")))
                        except (OSError, ValueError):
                            pass
                tg = records[item]
                if not tg:
                    continue
                for c, cname in kids:
                    if cname in tg[1]:
                        swaps.append((it, c, tg[0]))
    except FileNotFoundError:
        return 0
    finally:
        if con is not None:
            con.close()
    fixed = 0
    if swaps:
        parts_dir = models_dir(install) / "parts"
        parts_dir.mkdir(exist_ok=True)
        doc.setdefault("materials", []).append(dict(_PART_MATERIAL))
        material = len(doc["materials"]) - 1
        grafted: dict[str, dict | None] = {}
        for it, bad, own in swaps:
            key = _stem(own)
            if key not in grafted:
                grafted[key] = None
                part_glb = parts_dir / f"{_safe(key)}.glb"
                try:
                    if not part_glb.exists():
                        _convert_raw(install, settings, own, part_glb)
                    part, part_bin, _ = _read_glb(part_glb)
                    bin_, g = graft_part(doc, bin_, part, part_bin, material)
                    grafted[key] = g
                except (tools.ToolError, OSError, ValueError) as e:
                    if "not found" not in str(e):  # no own model in the game files: the variant is all there is
                        log(f"couldn't convert {own}: {e}")
            g = grafted[key]
            if g is None:
                continue
            kids = nodes[it]["children"]
            kids.remove(bad)
            kids.append(instance_part(doc, g, key))
            fixed += 1
        if fixed:
            _write_glb(glb, doc, bin_, version)
            log(f"swapped {fixed} item(s) built from leftover variant geometry back to their own model")
    try:
        mark.write_text(stamp())
    except OSError:
        pass
    return fixed


def _base_name(s) -> str:
    return re.sub(r"(_\d+)+$", "", str(s or "").lower())


def rebind_borrowed_meshes(doc: dict) -> tuple[int, int]:
    """Give parts that borrowed another part's mesh their own back.

    StarBreaker numbers each item's meshes as it goes, and the ranges overlap, so a part
    can end up drawing another part's mesh: the Storm AA's fins drew the whole body, the
    Wolf's missile pod the body, the Redeemer's thruster cover plate a 19 m wing thruster
    standing on end, the Idris's dashboard buttons chair bases. Meshes carry their part's
    name, so a part whose mesh is named for another part, while a part of that name also
    uses it, borrowed it. It gets the unused mesh of its own name (the one numbered among
    its neighbours' meshes), or one its namesakes use, or none at all. Returns
    (rebound, dropped)."""
    nodes, meshes = doc.get("nodes", []), doc.get("meshes", [])
    users: dict[int, list[int]] = {}
    for i, nd in enumerate(nodes):
        m = nd.get("mesh")
        if m is not None and m < len(meshes):
            users.setdefault(m, []).append(i)
    by_name: dict[str, list[int]] = {}
    for m, me in enumerate(meshes):
        by_name.setdefault(_base_name(me.get("name")), []).append(m)
    parent = {}
    for i, nd in enumerate(nodes):
        for c in nd.get("children", []):
            parent.setdefault(c, i)
    borrowed = []
    for m, us in users.items():
        mname = _base_name(meshes[m].get("name"))
        if not mname or not any(_base_name(nodes[i].get("name")) == mname for i in us):
            continue
        borrowed += [i for i in us if nodes[i].get("name") and _base_name(nodes[i]["name"]) != mname]
    rebound = dropped = 0
    for i in borrowed:
        own = _base_name(nodes[i]["name"])
        wrong = nodes[i]["mesh"]
        cands = [c for c in by_name.get(own, []) if c != wrong]
        if not cands:
            del nodes[i]["mesh"]
            dropped += 1
            continue
        # neighbours: meshes of the other parts of the same item (siblings and parent)
        p = parent.get(i)
        near = [nodes[k]["mesh"] for k in ([p] if p is not None else []) + (nodes[p].get("children", []) if p is not None else [])
                if k != i and "mesh" in nodes[k]]
        ref = sorted(near)[len(near) // 2] if near else wrong
        unused = [c for c in cands if not users.get(c)]
        pool = unused or [c for c in cands if all(_base_name(nodes[u].get("name")) == own for u in users.get(c, []))] or cands
        best = min(pool, key=lambda c: abs(c - ref))
        nodes[i]["mesh"] = best
        users.setdefault(best, []).append(i)
        rebound += 1
    return rebound, dropped


def fix_borrowed_meshes(glb: Path, log=print) -> int:
    counts = (0, 0)

    def edit(doc) -> bool:
        nonlocal counts
        counts = rebind_borrowed_meshes(doc)
        return sum(counts) > 0

    try:
        _edit_glb_json(glb, edit)
    except OSError:
        glb.with_name(glb.name + ".tmp").unlink(missing_ok=True)
        return 0
    if sum(counts):
        log(f"gave {counts[0]} part(s) their own mesh back" + (f", removed {counts[1]} that had none" if counts[1] else ""))
    return sum(counts)


def fix_item_meshes(glb: Path, log=print) -> int:
    """Give every part of a repeated loadout item the mesh most copies use. Returns fixes."""
    fixed = 0

    def edit(doc) -> bool:
        nonlocal fixed
        nodes, meshes, acc = doc.get("nodes", []), doc.get("meshes", []), doc.get("accessors", [])

        def faces(mi):
            return sum(acc[p["indices"]]["count"] // 3 for p in meshes[mi]["primitives"] if "indices" in p)

        # (item name, path of part names inside it) -> [(node index, face count)]
        groups: dict[tuple, list] = {}

        def walk(i, item, rel):
            nd = nodes[i]
            name = nd.get("name", "")
            if item is not None and "mesh" in nd:
                groups.setdefault((item, rel), []).append((i, faces(nd["mesh"])))
            for c in nd.get("children", []):
                child = nodes[c].get("name", "")
                if name.lower().startswith("hardpoint_"):
                    walk(c, child, ())          # a loadout item mounted on a hardpoint
                elif item is not None:
                    walk(c, item, rel + (child,))
                else:
                    walk(c, None, ())

        for scene in doc.get("scenes", []):
            for root in scene.get("nodes", []):
                walk(root, None, ())
        # A mesh also bound to a different part somewhere else was handed out twice:
        # StarBreaker numbers each copy's meshes in sequence and the ranges overlap.
        users: dict[int, set] = {}
        for key, members in groups.items():
            for i, _ in members:
                users.setdefault(nodes[i]["mesh"], set()).add(key)
        for key, members in groups.items():
            if len(members) < 2:
                continue
            borrowed = {i for i, _ in members if len(users[nodes[i]["mesh"]]) > 1}
            voters = [(i, fc) for i, fc in members if i not in borrowed] or members
            counts: dict[int, int] = {}
            for _, fc in voters:
                counts[fc] = counts.get(fc, 0) + 1
            ranked = sorted(counts.items(), key=lambda kv: -kv[1])
            if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
                continue  # no clear answer
            best = ranked[0][0]
            good_mesh = nodes[next(i for i, fc in voters if fc == best)]["mesh"]
            for i, fc in members:
                if fc != best:
                    nodes[i]["mesh"] = good_mesh
                    fixed += 1
        # A mount (gimbal, turret) part bound to the very mesh of an item mounted under it
        # is the same mix-up with no good copy to borrow from; drop it from the mount.
        parent = {c: i for i, nd in enumerate(nodes) for c in nd.get("children", [])}
        for i, nd in enumerate(nodes):
            if "mesh" not in nd:
                continue
            k, seen = i, {i}
            # some exports list a node under two parents, which can make this map cyclic
            while k in parent and parent[k] not in seen:
                k = parent[k]
                seen.add(k)
                if nodes[k].get("mesh") == nd["mesh"]:
                    del nodes[k]["mesh"]
                    fixed += 1
                    break
        return fixed > 0

    if _edit_glb_json(glb, edit):
        log(f"fixed {fixed} mis-bound part mesh(es) on repeated loadout items")
    return fixed
