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
    "preview": ("export_entity_preview", "2"),
    "full": ("export_entity", "0"),
    "print": ("export_entity_print", "0"),
}
# Cache file tag per variant; bump when a variant's export settings change.
FILE_TAG = {"print": "print-m",     # print-m: exported with material names
            "preview": "preview-l"}  # preview-l: light (small textures, no lights)
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
                reattach_orphans(out, log)
                drop_empty_meshes(out, log)
                fix_item_meshes(out, log)
            return out
        errors = []
        if asset["kind"] == "entity":
            command, lod = VARIANTS[variant]
            try:
                tools.run(settings, command, install, name=asset["record"], out=str(out), lod=lod)
                if out.exists() and "fps_weapons" in (asset.get("subcategory") or ""):
                    complete_weapon(out, install, settings, asset, log)
                if out.exists():
                    place_interiors(out, install, asset, log)
                    reattach_orphans(out, log)
                    drop_empty_meshes(out, log)
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
