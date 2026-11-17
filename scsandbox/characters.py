"""Character builder: assemble a body, head and chosen armor/clothing pieces into one
model for previewing and printing.

Every wearable item in the index lists its worn mesh (.skin) for the male and the
female body. StarBreaker's `skin export` writes those in the shared bind pose of the
body skeleton, so the pieces line up by simply being placed together (cgf-converter
decodes these newer meshes with per-file offsets, so it isn't used here).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import trimesh

from . import catalog, models, tools
from .installs import Install

BODIES = {
    # folders: the body's own meshes, and shared head meshes (hair, beards) made for it
    "male": {"folders": ("/male_v7/", "/sc/male/"), "body": "Objects/Characters/Human/male_v7/body/m_body.skin"},
    "female": {"folders": ("/female_v2/", "/sc/female/"), "body": "Objects/Characters/Human/female_v2/body/f_body.skin"},
}

# slot -> (label, index subcategory patterns for SQL LIKE). Order = drawing order in the UI.
SLOTS = {
    "undersuit": ("Undersuit", ["characters/human/armor/pu_armor/undersuit"]),
    "suit": ("Full suit (one piece)", ["characters/human/armor/pu_armor/bespokesuit"]),
    "helmet": ("Helmet", ["characters/human/starwear/helmet/%"]),
    "core": ("Torso armor", ["characters/human/armor/pu_armor/%/core"]),
    "arms": ("Arm armor", ["characters/human/armor/pu_armor/%/arms"]),
    "legs": ("Leg armor", ["characters/human/armor/pu_armor/%/legs"]),
    "torso": ("Shirt / jacket", ["characters/human/clothing/pu_clothing/clothing_torso0",
                                  "characters/human/clothing/pu_clothing/clothing_torso1"]),
    "pants": ("Pants", ["characters/human/clothing/pu_clothing/clothing_legs"]),
    "feet": ("Shoes", ["characters/human/clothing/pu_clothing/clothing_feet"]),
    "hands": ("Gloves", ["characters/human/clothing/pu_clothing/clothing_hands"]),
    "hat": ("Hat", ["characters/human/clothing/pu_clothing/clothing_hats"]),
    "hair": ("Hair", ["characters/human/head/shared/hair"]),
}
_SKIP = re.compile(r"(?i)fp_visor|_prop\b|_prop\.|_lod\d|/crate")


def worn_skins(asset: dict, body: str) -> list[str]:
    """The worn .skin meshes of an item for the given body (first-person visors,
    dropped-item props and loot crates left out)."""
    folders = BODIES[body]["folders"]
    out = []
    for g in json.loads(asset.get("geometry") or "[]"):
        p = g.replace("\\", "/")
        if p.lower().endswith(".skin") and any(f in p.lower() for f in folders) and not _SKIP.search(p):
            if p not in out:
                out.append(p)
    return out


def items(con, slot: str, body: str, q: str = "", limit: int = 400) -> list[dict]:
    """Items for a slot that have a worn mesh for this body, by name."""
    if slot not in SLOTS:
        raise LookupError(f"unknown slot {slot}")
    pats = SLOTS[slot][1]
    where = " or ".join("subcategory like ?" for _ in pats)
    args = list(pats)
    sql = f"select * from assets where kind='entity' and ({where})"
    if q:
        sql += " and (name like ? or record like ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " order by name"
    out, seen = [], set()
    for row in con.execute(sql, args):
        a = dict(row)
        if "UNINITIALIZED" in (a.get("name") or "") or not worn_skins(a, body):
            continue
        key = (a["name"], tuple(worn_skins(a, body)))
        if key in seen:  # color variants that share the same mesh and name
            continue
        seen.add(key)
        out.append({"id": a["id"], "name": a["name"], "record": a["record"]})
        if len(out) >= limit:
            break
    return out


def heads(install: Install, settings: dict, body: str) -> list[dict]:
    """Faces to put on a bare head: the game's heads made for this body."""
    cache = catalog.build_dir(install) / f"heads_{body}.json"
    if cache.exists():
        found = json.loads(cache.read_text(encoding="utf-8"))
        if found:
            return found
    listing = tools.run(settings, "list_files", install,
                        filter=f"Data/Objects/Characters/Human/heads/{body}/**/*_head.skinm")
    out, seen = [], set()
    for line in listing.splitlines():
        p = line.split("\t")[0].strip().replace("\\", "/")
        if not p.lower().endswith("_t1_head.skinm"):
            continue
        name = Path(p).stem.replace("_t1_head", "")
        if name in seen:
            continue
        seen.add(name)
        label = re.sub(r"(?i)^(female|male)(\d+)$", lambda m: f"{m.group(1).title()} {m.group(2)}", name)
        label = re.sub(r"(?i)^(fe)?male_?archetype_?v?0*", "Archetype ", label).replace("_", " ")
        out.append({"id": p[:-1].removeprefix("Data/"), "name": label})  # .skinm -> .skin
    out.sort(key=lambda h: h["name"])
    if out:
        cache.write_text(json.dumps(out), encoding="utf-8")
    return out


def export_skin(install: Install, settings: dict, skin: str, log=print) -> Path:
    """One worn mesh as GLB in the body's bind pose (cached per build)."""
    rel = skin.replace("\\", "/").removeprefix("Data/")
    out = models.models_dir(install) / "skins" / (re.sub(r"[^A-Za-z0-9._-]+", "_", rel)[:150] + ".glb")
    out.parent.mkdir(exist_ok=True)
    if out.exists() and out.stat().st_size > 0:
        return out
    # StarBreaker matches a substring of the archive path, which uses backslashes
    name = ("Data/" + rel + "m").replace("/", "\\")
    log(f"exporting {Path(rel).name}")
    tools.run(settings, "export_skin", install, name=name, out=str(out))
    if not out.exists():
        raise tools.ToolError(f"StarBreaker wrote no file for {rel}")
    return out


def export_rig(install: Install, settings: dict, skin: str, log=print):
    """Skeleton + skin weights for a worn mesh (cgf-converter's export), or None."""
    from . import rig
    rel = skin.replace("\\", "/").removeprefix("Data/")
    out = models.models_dir(install) / "skins" / (re.sub(r"[^A-Za-z0-9._-]+", "_", rel)[:150] + ".rig.glb")
    if not (out.exists() and out.stat().st_size > 0):
        try:
            models._convert_raw(install, settings, rel, out)
        except tools.ToolError as e:
            log(f"  no skeleton for {Path(rel).name}: {str(e)[-150:]}")
            return None
    try:
        return rig.Rig(out)
    except Exception as e:  # no skin in the export
        log(f"  no skeleton for {Path(rel).name}: {e}")
        return None


def weapons(con, q: str = "", limit: int = 400) -> list[dict]:
    """Weapons for posed figures: rifles, SMGs, shotguns, snipers, LMGs, launchers,
    railgun, HMG and pistols."""
    sql = ("select id, name, record from assets where kind='entity' and subcategory='weapons/fps_weapons' "
           "and name not like '%UNINITIALIZED%'")
    args = []
    if q:
        sql += " and (name like ? or record like ?)"
        args += [f"%{q}%", f"%{q}%"]
    out = []
    for r in con.execute(sql + " order by name", args):
        kind = (re.match(r"[a-z]+_([a-z]+)_", (r["record"] or "").lower()) or [None, ""])[1]
        if kind in ("rifle", "smg", "shotgun", "sniper", "lmg", "hmg", "glauncher", "special", "crossbow", "pistol"):
            out.append({"id": r["id"], "name": r["name"], "record": r["record"]})
            if len(out) >= limit:
                break
    return out


def _magazine(install, settings, con, weapon_glb, log):
    """The weapon's loaded magazine exported on its own (glb path), or None."""
    import trimesh

    from . import rig
    try:
        sc = trimesh.load(str(weapon_glb), force="scene")
        if "magAttach" not in sc.graph.nodes:
            return None
        sub = rig._subtree(sc, "magAttach")
    except Exception:
        return None
    geo = set(sc.graph.nodes_geometry)
    # the magazine's own entity node is named after its record; else match its mesh file
    rows = [con.execute("SELECT id FROM assets WHERE record = ? COLLATE NOCASE AND subcategory LIKE "
                        "'weapons/magazines%' LIMIT 1", (n,)).fetchone() for n in sorted(sub - geo)]
    for n in sorted(sub & geo):
        geom = re.sub(r"_[0-9a-f]{6}$", "", n).replace("_", r"\_")
        rows.append(con.execute("SELECT id FROM assets WHERE subcategory LIKE 'weapons/magazines%' "
                                r"AND geometry LIKE ? ESCAPE '\' LIMIT 1", (f"%/{geom}.cgf%",)).fetchone())
    for row in rows:
        if row:
            a = catalog.get(con, row[0])
            try:
                return models.glb_for(install, settings, a, "print", log=log)
            except Exception as e:
                log(f"  magazine export failed: {str(e)[-200:]}")
                return None
    return None


def base_weapon(con, record: str) -> dict | None:
    """The base gun a variant belongs to (behr_rifle_ballistic_01 for
    behr_rifle_ballistic_01_store01): its own record if there is one, else the
    shortest-named record of the family."""
    m = re.match(r"([a-z0-9]+_[a-z]+_[a-z]+_\d+)", (record or "").lower())
    if not m:
        return None
    row = con.execute("SELECT id FROM assets WHERE kind='entity' AND subcategory='weapons/fps_weapons' "
                      "AND (record = ? COLLATE NOCASE OR record LIKE ? ESCAPE '\\') "
                      "ORDER BY length(record), record LIMIT 1",
                      (m.group(1), m.group(1).replace("_", "\\_") + "\\_%")).fetchone()
    return catalog.get(con, row[0]) if row else None


def weapon_kind(install: Install, asset: dict) -> str:
    """How the game handles the weapon (rig.KINDS), from its record's equipCategory, else
    from its name."""
    try:
        path = catalog.build_dir(install) / "records" / asset["record_path"]
        m = re.search(r'"equipCategory":\s*"(\w+)"', path.read_text(encoding="utf-8"))
        cat = m.group(1).lower() if m else ""
    except (OSError, KeyError, TypeError):
        cat = ""
    kind = {"stocked": "stocked", "pistol": "pistol", "shouldered": "shouldered", "heavyhip": "heavy"}.get(cat)
    if kind:
        return kind
    name = (asset.get("record") or "").lower()
    return ("pistol" if "_pistol_" in name else "heavy" if "_hmg_" in name
            else "shouldered" if "_special_" in name else "stocked")


# The game's skeleton (its .chrparams lists the animation databases) and the stance
# databases used for posing, per body.
SKELETONS = {"male": "Objects/Characters/Human/male_v7/export/bhm_skeleton_v7.chrparams",
             "female": "Objects/Characters/Human/female_v2/export/bhf_skeleton_v2.chrparams"}
STANCE_DBAS = ("weapons/stocked/locomotion/stand.dba", "weapons/stocked/locomotion/crouch.dba",
               "weapons/stocked/locomotion/stand/relaxed.dba",
               "weapons/pistol/locomotion/stand.dba", "weapons/pistol/locomotion/crouch.dba",
               "weapons/shouldered/locomotion/stand.dba", "weapons/shouldered/locomotion/crouch.dba",
               "weapons/no_weapon/locomotion/stand.dba")
STANCES_VERSION = 2


def stance_clips(install: Install, settings: dict, body: str, log=print):
    """The game's stance animations baked onto the body skeleton (rig.Clips), or None.
    cgf-converter reads the .dba databases the skeleton's .chrparams points to when the
    file sits next to the body skin; built once per game build."""
    from . import rig
    out = models.models_dir(install) / "skins" / f"{body}_stances_v{STANCES_VERSION}.glb"
    if not (out.exists() and out.stat().st_size > 0):
        try:
            work = catalog.build_dir(install) / "stances" / body
            skin = BODIES[body]["body"]
            folder = BODIES[body]["folders"][0].strip("/")
            anims = f"Data/Animations/Characters/Human/{folder}"
            log("reading the game's stance animations (first time only)")
            for flt in (f"Data/{skin.rsplit('/', 1)[0]}/{Path(skin).stem}.skin*", f"Data/{SKELETONS[body]}",
                        *(f"{anims}/{d}" for d in STANCE_DBAS)):
                tools.run(settings, "extract_files", install, out=str(work), filter=flt)
            data = models._find_case_insensitive(work, "Data")
            src = models._find_case_insensitive(work, "Data/" + skin)
            params = models._find_case_insensitive(work, "Data/" + SKELETONS[body])
            if data is None or src is None or params is None:
                raise tools.ToolError("the skeleton or body files were not found in the game archive")
            (src.parent / (src.stem + ".chrparams")).write_bytes(params.read_bytes())
            conv = work / "out"
            conv.mkdir(exist_ok=True)
            tools.run(settings, "convert_animated", None, tool="cgf_converter",
                      input=str(src), objectdir=str(data), out=str(conv))
            glbs = sorted(conv.rglob("*.glb"), key=lambda q: q.stat().st_mtime, reverse=True)
            if not glbs:
                raise tools.ToolError("cgf-converter wrote no file")
            out.parent.mkdir(parents=True, exist_ok=True)   # no skins folder yet on a fresh install
            glbs[0].replace(out)
        except (tools.ToolError, OSError) as e:
            log(f"  game stances unavailable, using built-in ones: {str(e)[-200:]}")
            return None
    try:
        clips = rig.Clips(out)
        return clips if clips.by_name else None
    except Exception as e:
        log(f"  game stances unreadable, using built-in ones: {e}")
        return None


def _pose_meshes(install, settings, con, body, meshes, pose, weapon_id, log):
    """Pose the assembled pieces (in place) and return the placed weapon mesh or None."""
    from scipy.spatial import cKDTree

    from . import rig
    labels = [l for l, _, _ in meshes]
    body_rig = export_rig(install, settings, BODIES[body]["body"], log)
    if body_rig is None:
        raise tools.ToolError("could not read the body skeleton, so the figure can't be posed")
    allv = np.vstack([m.vertices for _, _, m in meshes])
    torso = allv[(allv[:, 1] > 1.1) & (allv[:, 1] < 1.45) & (np.abs(allv[:, 0]) < 0.15)]
    back_z = float(torso[:, 2].max()) if len(torso) else 0.15
    weapon = None
    if weapon_id:
        a = catalog.get(con, int(weapon_id))
        if not a:
            raise LookupError(f"weapon {weapon_id} not found")
        log(f"weapon: {a['name']}")
        wpath = models.glb_for(install, settings, a, "print", log=log)
        base = base_weapon(con, a["record"]) or a
        kind = weapon_kind(install, base)
        log(f"  held as: {kind}")
        weapon = rig.Weapon(wpath, female=(body == "female"), mag=_magazine(install, settings, con, wpath, log),
                            kind=kind)
        if base["record"] != a["record"]:
            # a skin/store variant: held exactly like its base gun
            try:
                bpath = models.glb_for(install, settings, base, "print", log=log)
                weapon.adopt(rig.Weapon(bpath, female=(body == "female"), kind=kind))
                log(f"  grip from the base gun: {base['name']}")
            except Exception as e:
                log(f"  base gun unavailable, using this model's own points: {str(e)[-150:]}")
    clips = stance_clips(install, settings, body, log)
    D, M_w = rig.build_pose(body_rig, pose, weapon, back_z, clips=clips)
    body_mesh = next(m for l, _, m in meshes if l == "body")
    # weights are borrowed from the nearest point of the body *at rest*: the body is
    # posed first, and matching against its posed shape put an unrigged undersuit's
    # gloves on the hips
    rest_body = body_mesh.vertices.copy()
    body_tree = None
    for label, skin, m in meshes:
        r = body_rig if label == "body" else export_rig(install, settings, skin, log)
        if r is not None and r.count == len(m.vertices):
            m.vertices = rig.skin(m.vertices, r.J, r.W, r.names, rig.expand(D, r, body_rig.parent))
        else:  # weights from the nearest point of the body
            if body_tree is None:
                body_tree = cKDTree(rest_body)
            _, nn = body_tree.query(m.vertices, workers=-1)
            m.vertices = rig.skin(m.vertices, body_rig.J[nn], body_rig.W[nn], body_rig.names, D)
            log(f"  {label}: weights borrowed from the body")
    if weapon is None:
        return None
    w = weapon.mesh.copy()
    w.apply_transform(M_w)
    if pose == "relaxed" and weapon.kind != "pistol":
        _rest_on_back(w, np.vstack([m.vertices for _, _, m in meshes]))
    return w


def _rest_on_back(w, body_v, overlap=0.004):
    """Slide the slung weapon (+Z is behind the figure here) forward until it touches
    the back, sinking in slightly so the print joins it to the figure."""
    from scipy.spatial import cKDTree
    behind = body_v[body_v[:, 2] > w.vertices[:, 2].min() - 0.3]
    if not len(behind):
        return
    tree = cKDTree(behind[:, :2])
    pts = w.vertices[::max(1, len(w.vertices) // 20000)]
    hits = tree.query_ball_point(pts[:, :2], r=0.008, workers=-1)
    gaps = [p[2] - behind[h, 2].max() for p, h in zip(pts, hits) if h]
    if gaps:
        w.vertices[:, 2] -= min(gaps) + overlap


def build(install: Install, settings: dict, con, body: str, picks: dict, head: str | None,
          log=print, pose: str | None = None, weapon: int | None = None) -> tuple[Path, str]:
    """Assemble the figure; returns (glb path, figure name). Cached by its contents."""
    from . import rig
    if body not in BODIES:
        raise LookupError(f"unknown body {body}")
    if pose and pose not in rig.POSES:
        raise LookupError(f"unknown pose {pose}")
    pieces: list[tuple[str, str]] = [("body", BODIES[body]["body"])]
    if head:
        pieces.append(("head", head))
    labels = []
    for slot in SLOTS:
        aid = picks.get(slot)
        if not aid:
            continue
        a = catalog.get(con, int(aid))
        if not a:
            raise LookupError(f"item {aid} not found")
        skins = worn_skins(a, body)
        if not skins:
            raise LookupError(f"{a['name']} has no worn mesh for the {body} body")
        labels.append(a["name"])
        pieces += [(f"{slot}_{a['record']}", s) for s in skins]
    pose_key = [pose or "", int(weapon) if (weapon and pose) else 0, POSE_VERSION if pose else 0]
    key = hashlib.sha1(json.dumps([body, pieces, pose_key]).encode()).hexdigest()[:12]
    name = f"figure_{key}"
    out = models.models_dir(install) / "figures" / f"{name}.glb"
    out.parent.mkdir(exist_ok=True)
    if out.exists() and out.stat().st_size > 0:
        return out, name
    meshes = []
    for label, skin in pieces:
        try:
            meshes.append((label, skin, trimesh.load(str(export_skin(install, settings, skin, log)), force="mesh")))
        except tools.ToolError as e:
            if label in ("body", "head"):
                raise
            log(f"  skipped {Path(skin).name}: {str(e)[-200:]}")
    extra = []
    if pose:
        log(f"posing: {rig.POSES[pose]}")
        w = _pose_meshes(install, settings, con, body, meshes, pose, weapon, log)
        if w is not None:
            extra.append(("weapon", None, w))
    scene = trimesh.Scene()
    # glTF from StarBreaker faces -Z; turn the figure to face +Z, which the print
    # routine's Y-up -> Z-up turn makes the front (-Y) where the name plate is.
    turn = trimesh.transformations.rotation_matrix(np.pi, [0, 1, 0])
    for label, _, m in meshes + extra:
        m.apply_transform(turn)
        scene.add_geometry(m, node_name=label, geom_name=label)
    scene.export(str(out))
    log(f"figure assembled from {len(meshes) + len(extra)} meshes" + (f": {', '.join(labels)}" if labels else ""))
    return out, name


POSE_VERSION = 21  # bump when pose definitions change, so cached figures are rebuilt


def figure_path(install: Install, name: str) -> Path:
    if not re.fullmatch(r"figure_[0-9a-f]{12}", name or ""):
        raise LookupError("unknown figure")
    p = models.models_dir(install) / "figures" / f"{name}.glb"
    if not p.exists():
        raise LookupError("figure not built yet")
    return p
