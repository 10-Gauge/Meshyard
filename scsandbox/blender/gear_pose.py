"""Blender (background) script: pose a StarBreaker package with its landing gear up, its
deployable weapons out or stowed, its landed shape (wings folded) or its VTOL mode, and
write how every moved object's world transform changed.

    blender -b <package>/scene.blend --python gear_pose.py -- out.json [gear|weapons_out|weapons_in|landed|vtol]

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

# Ships whose "wings_deploy" ends (and "wings_retract" starts) in their landed shape (the
# export's pose), not their flight shape: the Reliant lands with its wings flat and flies
# with them upright.
LANDING_WINGS = re.compile(r"(reliant|corsair)_wings?_(deploy|retract)")
# Wing clips for an alternate flight mode, not the landed shape: the Stingray draws its
# wings in, stands the wingtip fins up and turns its tail boom.
FLIGHT_WINGS = re.compile(r"krig_s65_wing_retract")

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
    names = list(po.available_package_animation_names(package))
    # The add-on leaves out some landing gear clips that play on the ship's own skeleton:
    # the Stingray's "landinggear_deploy" (its outer wings fold up and become the gear
    # legs). They are in the package's scene.json and play by name.
    try:
        with open(os.path.join(os.path.dirname(bpy.data.filepath), "scene.json"), encoding="utf-8") as f:
            listed = (json.load(f).get("root_entity") or {}).get("animations") or []
        for a in listed:
            frags = {re.sub(r"[^a-z]", "", (fr.get("fragment") or "").lower()) for fr in a.get("fragments") or []}
            if a.get("name") and a["name"] not in names and "landinggear" in frags:
                names.append(a["name"])
    except (OSError, ValueError):
        pass
    result["available"] = names
    plan = []
    gear = re.compile(r"(?i)(landing_?gears?|(^|_)lg_|gear)")

    def gear_plan(up):
        out = []
        for n in names:
            low = n.lower()
            if not gear.search(low) or "compress" in low or "idle" in low or "wing" in low or "vtol" in low:
                continue
            if "retract" in low:
                out.append((n, "snap_last" if up else "snap_first"))
            elif "deploy" in low or "extend" in low:
                out.append((n, "snap_first" if up else "snap_last"))
        return out

    if kind == "gear":
        plan = gear_plan(True)
    elif kind == "landed":
        # The shape a ship takes on the ground besides its gear: the Guardian moves its
        # wings with the gear ("landing_gear_wings_deploy"), the Talon, Vanguard and Zeus
        # spread theirs for flight ("wings_deploy"), the Eclipse folds its wingtips up in
        # a clip named "aegs_eclipse_gun_bay".
        for n in names:
            low = n.lower()
            if "vtol" in low or "nacelle" in low or LANDING_WINGS.search(low) or FLIGHT_WINGS.search(low):
                continue
            if re.search(r"landing_?gear_wings?_(deploy|extend|open)", low):
                plan.append((n, "snap_last"))
            elif re.search(r"landing_?gear_wings?_(retract|close)", low):
                plan.append((n, "snap_first"))
            elif re.search(r"(^|_)wings?_(retract|fold|close)", low):
                plan.append((n, "snap_last"))
            elif re.search(r"(^|_)wings?_(deploy|extend|unfold|open)", low):
                plan.append((n, "snap_first"))
            elif re.search(r"^fragment:0:aegs_eclipse_gun_bay$", low):
                plan.append((n, "snap_last"))
    elif kind == "flight":
        # flight mode: the Stingray opens its flaps ("flap_deploy", "fap_retract") and draws
        # its wings in; ships exported in their landed shape spread their wings: the
        # Reliant's stand upright, the Corsair's spread
        for n in names:
            low = n.lower()
            m = re.search(r"(^|_|:)(flaps?|faps?)_(deploy|open|extend|retract|close)$", low)
            if m:
                plan.append((n, "snap_first" if m.group(3) in ("retract", "close") else "snap_last"))
            elif LANDING_WINGS.search(low):
                plan.append((n, "snap_first" if low.endswith("deploy") else "snap_last"))
            elif FLIGHT_WINGS.search(low):
                plan.append((n, "snap_last"))
        if any(FLIGHT_WINGS.search(n.lower()) for n in names):
            # the Stingray's deployed mode also brings its nose cannons out
            for n in names:
                m = re.search(r"cannons?_(deploy|retract)$", n.lower())
                if m:
                    plan.append((n, "snap_last" if m.group(1) == "deploy" else "snap_first"))
    elif kind == "vtol":
        # thrusters that swing down for vertical flight: "nacelles_deploy" (Starlifter, Golem,
        # RAFT), "vtol_deploy" (Paladin, Hull B, Asgard, Fury), "nacelles_open" (Starlancer),
        # "vtol_mode_deploy" (Prowler Utility); not the gear's own VTOL variants ("lg_pl_vtol")
        for n in names:
            low = n.lower()
            if re.search(r"(^|_|:)lg_|nozzle|flare|compress|idle", low):
                continue
            m = re.search(r"(vtol|nacelles?)(_mode)?(_(deploy|open|extend|retract|close))?$", low)
            if not m:
                continue
            closing = (m.group(4) or "deploy") in ("retract", "close")
            plan.append((n, "snap_first" if closing else "snap_last"))
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

    def most_moved(clips):
        best = 0
        for n, mode in clips:
            reset()
            try:
                po.apply_animation_mode_to_package_root(bpy.context, root, n, mode)
                best = max(best, len(moved_now()))
                po.apply_animation_mode_to_package_root(bpy.context, root, n, "none")
            except Exception:
                pass
        reset()
        return best

    if kind == "landed":
        # Most ships are exported gear down. A few (the Stingray) come gear up: their
        # landed shape puts the gear down as well.
        up, down = most_moved(gear_plan(True)), most_moved(gear_plan(False))
        if down > 20 and up * 4 < down:
            plan += gear_plan(False)
            result["gear_down"] = True

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
