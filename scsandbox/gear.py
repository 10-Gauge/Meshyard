"""Animated poses for ships: landing gear up, and deployable weapons out or stowed
(the Alpha Wolf's cannons, the M80's guns and missile racks).

StarBreaker exports ships in their bind pose (gear down). The other poses live in the
ship's animation clips, which only StarBreaker's Blender add-on decodes
reliably (several sample decoders, bone hash matching, per-part position policies).
So, once per ship:

1. export a light animated package (no textures, no interior),
2. run Blender in the background with blender/gear_pose.py, which asks the add-on to
   snap the matching clips (gear retracted, weapons deployed or retracted) and records
   how each part's world transform moved,
3. fit Blender's axes to the GLB's from the parts' rest positions,

and cache that per pose (<record>.gearup.json, <record>.weapons_out.json, ...). Any
export of the ship (preview, full detail, print) then gets the same moves applied by
part name, poses stacked: <stem>.gearup.glb, <stem>.wout.glb, <stem>.gearup.win.glb.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np

from . import blendertools, models, tools
from .installs import Install

POSE_VERSION = 1
# pose kind -> (cache name, file suffix, what it is in messages)
KINDS = {"gear": ("gearup", "gearup", "landing gear"),
         "weapons_out": ("weapons_out", "wout", "deployable weapons"),
         "weapons_in": ("weapons_in", "win", "deployable weapons")}
SCRIPT = Path(__file__).parent / "blender" / "gear_pose.py"
# the root node of a landing gear item mounted on the ship ("AEGS_Gladius_LandingGear_Front.cdf",
# "..._LandingGear_BackLeft_Anim"), not the ship's own gear doors or mount points
GEAR_ITEM = re.compile(r"(?i)landing_?gear.*(\.cdf|\.cga|\.chr|_anim)$")
GEAR_MOUNT = re.compile(r"(?i)^(hardpoint|helper)_|anchor|door")


class NoPose(Exception):
    """The ship has no animation for this pose (no gear clips, no deploying weapons)."""
    kind = "gear"


NoGear = NoPose  # older name


def _pose_path(install: Install, asset: dict, kind: str = "gear") -> Path:
    return models.models_dir(install) / f"{models._safe(asset['record'])}.{KINDS[kind][0]}.json"


def pose_for(install: Install, settings: dict, asset: dict, log=print, kind: str = "gear") -> dict:
    """The ship's pose from Blender: {"blender": {"objects": {name: {bind, posed}}, ...}} (cached)."""
    what = KINDS[kind][2]
    cache = _pose_path(install, asset, kind)
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("version") == POSE_VERSION:
            if not (data.get("blender") or {}).get("objects"):
                raise NoPose(data.get("reason") or f"no {what} animation")
            return data
    blender = tools.find("blender", settings)
    out = models.models_dir(install) / f"{models._safe(asset['record'])}.rig"
    with models._lock(str(out)):
        scenes = sorted(out.glob("Packages/**/scene.blend"))
        if not scenes:
            log("exporting the ship's rig and animation clips")
            tools.run(settings, "export_entity_rig", install, name=asset["record"], out=str(out))
            scenes = sorted(out.glob("Packages/**/scene.blend"))
        if not scenes:
            raise tools.ToolError(f"StarBreaker wrote no scene.blend under {out}")
        raw = out / f"{kind}_pose_raw.json"
        log({"gear": "posing the landing gear up", "weapons_out": "deploying the weapons",
             "weapons_in": "stowing the weapons"}[kind] + " (Blender, StarBreaker add-on)")
        raw.unlink(missing_ok=True)
        try:
            # clean environment and closed stdin: the packaged engine's own Python settings
            # leak into Blender's embedded Python otherwise, and Blender stalls at startup
            addon = tools.starbreaker_addon_dir()
            extra = {"SC_STARBREAKER_ADDON_HOME": str(addon.parent)} if addon else None
            proc = subprocess.run([str(blender), "-b", str(scenes[0]), "--python", str(SCRIPT), "--", str(raw), kind],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  stdin=subprocess.DEVNULL, env=tools.child_env(extra),
                                  timeout=300, creationflags=tools.NO_WINDOW)
        except subprocess.TimeoutExpired as e:
            raise tools.ToolError(f"Blender didn't finish posing the {what} within 5 minutes "
                                  "(it normally takes seconds), so it was stopped.") from e
        if not raw.exists():
            raise tools.ToolError(f"Blender didn't pose the {what}:\n" + (proc.stderr or proc.stdout)[-1500:])
        result = json.loads(raw.read_text(encoding="utf-8"))
    if result.get("error"):
        raise tools.ToolError(f"The StarBreaker add-on couldn't pose the {what}:\n" + result["error"][-1500:])
    data = {"version": POSE_VERSION, "clips": result.get("clips", []), "blender": result}
    if not result.get("objects"):
        data["reason"] = (f"this ship has no {what} animation" if not result.get("clips")
                          else f"its {what} clips moved nothing")
    cache.write_text(json.dumps(data), encoding="utf-8")
    if not result.get("objects"):
        raise NoPose(data["reason"])
    return data


# ---------- applying the pose to a GLB ----------

def _local(nd) -> np.ndarray:
    if "matrix" in nd:
        return np.array(nd["matrix"], float).reshape(4, 4).T
    T = np.eye(4)
    if "translation" in nd:
        T[:3, 3] = nd["translation"]
    if "rotation" in nd:
        x, y, z, w = nd["rotation"]
        T[:3, :3] = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                              [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                              [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    if "scale" in nd:
        T = T @ np.diag([*nd["scale"], 1.0])
    return T


def _key(name: str) -> str:
    """Comparable part name: Blender's '<item>_<n>_<bone>' and '.001' suffixes stripped."""
    n = re.sub(r"[._]\d{3}$", "", name or "")  # Blender's duplicate suffix (".001" or "_001")
    n = re.sub(r"^.*_anim_\d+_", "", n, flags=re.I)
    n = re.sub(r"^body_\d+_", "", n, flags=re.I)
    return n.lower()


def _fit(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Rigid transform (4x4) taking points src onto dst (Kabsch)."""
    cs, cd = src.mean(0), dst.mean(0)
    H = (src - cs).T @ (dst - cd)
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    R = Vt.T @ np.diag([1, 1, d]) @ U.T
    A = np.eye(4)
    A[:3, :3] = R
    A[:3, 3] = cd - R @ cs
    return A


def apply_pose(glb: Path, out: Path, pose: dict, log=print, hide_gear: bool = True) -> int:
    """Write a copy of glb with the pose's moves applied; returns parts moved. hide_gear:
    hide the gear legs (gear up; see below)."""
    import shutil
    b = pose["blender"]
    moved_b = b["objects"]
    anchors = b.get("anchors", {})
    shutil.copyfile(glb, out)
    count = 0

    def edit(doc) -> bool:
        nonlocal count
        nodes = doc.get("nodes", [])
        parent = {c: i for i, nd in enumerate(nodes) for c in nd.get("children", [])}
        order, seen = [], set()
        for root in doc.get("scenes", [{}])[doc.get("scene", 0)].get("nodes", []):
            todo = [root]
            while todo:
                k = todo.pop()
                if k in seen:
                    continue
                seen.add(k)
                order.append(k)
                todo += nodes[k].get("children", [])
        world = {}
        for k in order:
            p = parent.get(k)
            world[k] = (world[p] if p in world else np.eye(4)) @ _local(nodes[k])
        by_key: dict[str, list[int]] = {}
        for k in order:
            by_key.setdefault(_key(nodes[k].get("name", "")), []).append(k)

        # Blender world -> GLB world, from parts whose name is unique on both sides
        names_b: dict[str, list[str]] = {}
        for n in anchors:
            names_b.setdefault(_key(n), []).append(n)
        pairs = [(anchors[ns[0]], world[by_key[k][0]][:3, 3]) for k, ns in names_b.items()
                 if len(ns) == 1 and len(by_key.get(k, [])) == 1 and k]
        if len(pairs) < 4:
            raise tools.ToolError("couldn't line the gear pose up with the model (too few matching parts)")
        src, dst = np.array([p[0] for p in pairs], float), np.array([p[1] for p in pairs], float)
        A = _fit(src, dst)
        resid = np.linalg.norm((src @ A[:3, :3].T + A[:3, 3]) - dst, axis=1)
        good = resid < max(0.05, np.percentile(resid, 50) * 3)
        if good.sum() >= 4 and not good.all():
            A = _fit(src[good], dst[good])  # refit without outliers (moved helpers, duplicates)
        Ainv = np.linalg.inv(A)

        target = {}
        for name, mv in moved_b.items():
            cands = by_key.get(_key(mv.get("source") or name)) or by_key.get(_key(name)) or []
            if not cands:
                continue
            bind, posed = np.array(mv["bind"], float), np.array(mv["posed"], float)
            want = (A @ bind)[:3, 3]
            k = min(cands, key=lambda c: np.linalg.norm(world[c][:3, 3] - want))
            D = A @ posed @ np.linalg.inv(bind) @ Ainv      # the move, in GLB space
            target[k] = D @ world[k]

        # The gear legs themselves can't be posed from this export: their main mesh is
        # skinned in the game, and the bundled GLB keeps it as one rigid mesh in the
        # down pose (only small parts are split per bone). Retracted gear sits inside
        # the hull behind its doors, so the gear items are hidden and only the doors
        # and other ship parts take their animated pose.
        def gear_item(k):
            top = None
            while k is not None:
                nm = nodes[k].get("name", "")
                if GEAR_ITEM.search(nm) and not GEAR_MOUNT.search(nm):
                    top = k
                k = parent.get(k)
            return top

        hidden = ({gear_item(k) for k in target} - {None}) if hide_gear else set()
        drop = set()
        for h in hidden:
            todo = [h]
            while todo:
                k = todo.pop()
                if k in drop:
                    continue
                drop.add(k)
                todo += nodes[k].get("children", [])
        for k in drop:
            if nodes[k].pop("mesh", None) is not None:
                count += 1
            target.pop(k, None)
        new_world = {}
        for k in order:
            p = parent.get(k)
            pw = new_world[p] if p in new_world else np.eye(4)
            if k in target:
                new_world[k] = target[k]
                L = np.linalg.inv(pw) @ target[k]
                nd = nodes[k]
                for f in ("translation", "rotation", "scale"):
                    nd.pop(f, None)
                nd["matrix"] = [float(v) for v in L.T.reshape(-1)]
                count += 1
            else:
                new_world[k] = pw @ _local(nodes[k])
        return count > 0

    models._edit_glb_json(out, edit)
    log(f"{'landing gear up' if hide_gear else 'pose'}: {count} parts moved")
    return count


def posed_glb(install: Install, settings: dict, asset: dict, variant: str, gear_up: bool = False,
              weapons: str | None = None, log=print) -> Path:
    """The ship's model for `variant` with the gear up and/or the weapons "out" or "in"
    (cached; stacked in that order)."""
    glb = models.glb_for(install, settings, asset, variant, log=log)
    kinds = (["gear"] if gear_up else []) + ([f"weapons_{weapons}"] if weapons in ("out", "in") else [])
    for kind in kinds:
        out = glb.with_name(glb.name[:-4] + f".{KINDS[kind][1]}.glb")
        if not (out.exists() and out.stat().st_mtime >= glb.stat().st_mtime):
            try:
                pose = pose_for(install, settings, asset, log, kind=kind)
                tmp = out.with_name(out.name + ".part")
                n = apply_pose(glb, tmp, pose, log, hide_gear=kind == "gear")
                if n == 0:
                    tmp.unlink(missing_ok=True)
                    raise NoPose(f"none of the {KINDS[kind][2]} parts were found in the model")
            except NoPose as e:
                e.kind = kind
                raise
            tmp.replace(out)
        glb = out
    return glb


def gearup_glb(install: Install, settings: dict, asset: dict, variant: str, log=print) -> Path:
    """The ship's model for `variant` with the landing gear up (cached)."""
    return posed_glb(install, settings, asset, variant, gear_up=True, log=log)
