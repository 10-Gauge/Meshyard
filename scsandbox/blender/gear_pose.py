"""Blender (background) script: pose a StarBreaker package with its landing gear up, or
its deployable weapons out or stowed, and write how every moved object's world
transform changed.

    blender -b <package>/scene.blend --python gear_pose.py -- out.json [gear|weapons_out|weapons_in]

Uses the StarBreaker add-on's own animation code (the same call as its "snap" buttons).
Gear up: "retract" clips snap to their last frame, "deploy"/"extend" clips to their
first. Weapons out: "deploy" clips to their last frame, "retract" clips to their first;
weapons in the other way round. Output JSON:
    {"clips": [...], "objects": {name: {"bind": 4x4, "posed": 4x4}}, ...}
with matrices in Blender world space, row-major.
"""

import json
import os
import re
import sys

import addon_utils
import bpy

args = sys.argv[sys.argv.index("--") + 1:]
out_path = args[0]
kind = args[1] if len(args) > 1 else "gear"


def mat(m):
    return [[float(m[r][c]) for c in range(4)] for r in range(4)]


result = {"clips": [], "objects": {}, "error": None, "available": []}
try:
    # A Blender that has never had the add-on installed can still use the app's own copy.
    # Appended, so a copy already installed in Blender wins.
    addon_home = os.environ.get("SC_STARBREAKER_ADDON_HOME")
    if addon_home and addon_home not in sys.path:
        sys.path.append(addon_home)
    addon_utils.enable("starbreaker_addon", default_set=False, persistent=False)
    from starbreaker_addon.runtime import package_ops as po

    root = next(o for o in bpy.data.objects if o.get("starbreaker_package_root"))
    package = po._load_package_from_root(root)
    names = po.available_package_animation_names(package)
    result["available"] = names
    plan = []
    if kind == "gear":
        gear = re.compile(r"(?i)(landing_?gears?|(^|_)lg_|gear)")
        for n in names:
            low = n.lower()
            if not gear.search(low) or "compress" in low or "idle" in low:
                continue
            if "retract" in low:
                plan.append((n, "snap_last"))
            elif "deploy" in low or "extend" in low:
                plan.append((n, "snap_first"))
    else:
        # "cannons_deploy" (Alpha Wolf), "weapons_deploy" and "Missile_deploy" (M80),
        # "guns_retract", "turret_deploy", ...
        weapon = re.compile(r"(?i)(weapon|cannon|gun|missile|torpedo|rocket|turret)s?_(deploy|retract|open|close)")
        out = kind == "weapons_out"
        for n in names:
            m = weapon.search(n)
            if not m:
                continue
            opening = m.group(2).lower() in ("deploy", "open")
            plan.append((n, "snap_last" if opening == out else "snap_first"))
    objs = [o for o in bpy.data.objects if o.type in {"EMPTY", "MESH"}]
    bind = {o.name: o.matrix_world.copy() for o in objs}

    def moved_now():
        bpy.context.view_layer.update()
        out = set()
        for o in objs:
            b, p = bind[o.name], o.matrix_world
            if any(abs(b[r][c] - p[r][c]) > 1e-5 for r in range(4) for c in range(4)):
                out.add(o.name)
        return out

    def reset():
        for n, _ in plan:
            try:
                po.apply_animation_mode_to_package_root(bpy.context, root, n, "none")
            except Exception:
                pass
        po._restore_bind_pose(root)

    # Clips overlap (a retract, and extend fragments that also carry the doors), and
    # applying one after another lets a later clip undo an earlier one. Try each alone,
    # keep the one that moves the most parts, then add any that move other parts.
    tried = []
    for n, mode in plan:
        reset()
        try:
            po.apply_animation_mode_to_package_root(bpy.context, root, n, mode)
            tried.append((n, mode, moved_now()))
        except Exception as e:  # one bad clip shouldn't stop the others
            result["clips"].append({"name": n, "mode": mode, "error": str(e)})
    reset()
    taken = set()
    for n, mode, parts in sorted(tried, key=lambda t: -len(t[2])):
        if parts and not (parts & taken):
            po.apply_animation_mode_to_package_root(bpy.context, root, n, mode)
            taken |= parts
            result["clips"].append({"name": n, "mode": mode, "objects": len(parts)})
    bpy.context.view_layer.update()
    for o in objs:
        b, p = bind[o.name], o.matrix_world
        if any(abs(b[r][c] - p[r][c]) > 1e-5 for r in range(4) for c in range(4)):
            result["objects"][o.name] = {"bind": mat(b), "posed": mat(p),
                                         "source": str(o.get("starbreaker_source_node_name", "") or "")}
    # every object's bind position, to align Blender's axes with the GLB's
    result["anchors"] = {o.name: [float(v) for v in bind[o.name].translation] for o in objs}
except Exception as e:
    import traceback
    result["error"] = traceback.format_exc()
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(result, f)
print("GEAR_POSE_DONE", len(result["objects"]), "moved;", result["error"] or "")
