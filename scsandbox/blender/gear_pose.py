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
from mathutils import Vector

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
    # The game tags each clip's uses: the landing gear's "Deploy" and "Retract" actions,
    # each playing a clip forward or backward. The add-on names them "fragment:<i>:<clip>"
    # in that order, and snapping one to its last frame gives the action's end state. So
    # a landing gear "Deploy" fragment ends gear down whatever its clip is called (the
    # Fury's gear clip is "VTOL_Deploy", the Prowler's "landing_mode_deploy_left").
    gear_end = {}       # "fragment:i:clip" -> mode that puts the gear down
    shut_end = {}       # "fragment:i:clip" -> mode that ends its action closed/retracted
    gear_clips = set()  # clips the game uses for the landing gear
    try:
        with open(os.path.join(os.path.dirname(bpy.data.filepath), "scene.json"), encoding="utf-8") as f:
            listed = (json.load(f).get("root_entity") or {}).get("animations") or []
        for a in listed:
            frags = [fr for fr in a.get("fragments") or []]
            kinds = [re.sub(r"[^a-z]", "", (fr.get("fragment") or "").lower()) for fr in frags]
            # door, ramp and canopy clips listed only here play by name too (the Railen's
            # cargo door)
            if a.get("name") and a["name"] not in names and \
                    re.search(r"(?i)(door|airlock|hatch|ramp|canopy|cockpit)\w*_(close|open|deploy|retract)", a["name"]):
                names.append(a["name"])
            # each leg's own clip, when the ship's gear clip only moves a dummy (the
            # Basher's ".../landing_gear/glsn_basher_lg_rear_left_landing_gear_retract.caf")
            if a.get("name") and a["name"] not in names and not frags and \
                    re.search(r"(?i)landing_?gear/.*landing_?gear_(deploy|retract)\.caf$", a["name"]):
                names.append(a["name"])
            if not a.get("name") or "landinggear" not in kinds:
                continue
            gear_clips.add(a["name"])
            if a["name"] not in names:
                names.append(a["name"])
            for i, fr in enumerate(frags):
                tags = {t.lower() for t in fr.get("frag_tags") or []}
                if len(tags & {"open", "close", "deploy", "retract"}) == 1:
                    shut_end[f"fragment:{i}:{a['name']}"] = "snap_last" if tags & {"close", "retract"} else "snap_first"
                if kinds[i] == "landinggear" and tags & {"deploy", "retract"} and len(tags & {"deploy", "retract"}) == 1:
                    gear_end[f"fragment:{i}:{a['name']}"] = "snap_last" if "deploy" in tags else "snap_first"
    except (OSError, ValueError):
        pass
    result["available"] = names
    plan = []
    # "landing_gear_retract", "lg_extend", the Gatac Railen's "landing_deploy" and the
    # Reclaimer's ".../reclaimer/lg_arm_deploy_main_left.caf"
    gear = re.compile(r"(?i)(landing_?gears?|(^|[_/:])lg_|gear|(^|_|:)landing_(deploy|retract|extend))")

    def is_gear_clip(n):
        return re.sub(r"^fragment:\d+:", "", n) in gear_clips

    def gear_plan(up):
        out = []
        for n in names:
            low = n.lower()
            if n in gear_end:
                down = gear_end[n]
                out.append((n, ("snap_first" if down == "snap_last" else "snap_last") if up else down))
                continue
            if "compress" in low or "idle" in low or "door" in low:
                continue
            if not is_gear_clip(n) and (not gear.search(low) or "wing" in low or "vtol" in low):
                continue
            if n.startswith("fragment:") and is_gear_clip(n):
                continue  # a tagged use that isn't deploy or retract (compress, idle)
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
    elif kind == "closed":
        # doors and airlocks the export leaves open (the Gatac Railen's rear airlock):
        # close them so the hull has no hole
        # and the Railen's "exterior/gama_railen_cargo_door_close.caf", the MPUV's canopy
        # hatches ("left_hatch_deploy"), ramps and canopies left open. Not the landing
        # gear's own doors (they follow the gear) or lockers and seats inside.
        shut = re.compile(r"(?:^|_|:)(?:doors?|airlock|hatch|ramp|canopy|cockpit)(?:_(?:left|right|l|r|\d+))?"
                          r"_(close|open|deploy|retract)(?:_(?:left|right|l|r|\d+))?$")
        inside = re.compile(r"(?i)/interior/|locker|storage|inventory|seat|dash|fuel|component|elevator|shutter|cooler|relay|batter"
                            r"|landing|gear|(^|[_/:])lg_")
        for n in names:
            stem = re.sub(r"\.caf$", "", n.lower())
            m = shut.search(stem)
            if not m or inside.search(n) or is_gear_clip(n):
                continue
            if n.startswith("fragment:"):
                # a tagged use may play the clip backwards (the Gladius's "canopy_close"
                # is also its "Open" action): go by the action, or leave it to the
                # plain clip
                if n in shut_end:
                    plan.append((n, shut_end[n]))
                    continue
                if re.sub(r"^fragment:\d+:", "", n) in names:
                    continue
            if re.search(r"(?i)canopy|cockpit", n):
                continue  # canopy clips are often authored opening whatever their name: tags only
            plan.append((n, "snap_last" if m.group(1) in ("close", "retract") else "snap_first"))
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
            elif not is_gear_clip(n) and re.search(r"(^|_|:)wings?_(deploy|extend|unfold|open)(\.caf)?$", low):
                # wings that spread for flight from a folded export: the Scorpius's,
                # the Aurora Mk II's, the Fury's
                plan.append((n, "snap_last"))
            elif not is_gear_clip(n) and re.search(r"(^|_|:)wings?_(retract|fold|close)(\.caf)?$", low):
                plan.append((n, "snap_first"))
        # the guns stay as the Weapons switch has them (the Stingray's cannons too)
    elif kind == "vtol":
        # thrusters that swing down for vertical flight: "nacelles_deploy" (Starlifter, Golem,
        # RAFT), "vtol_deploy" (Paladin, Hull B, Asgard, Fury), "nacelles_open" (Starlancer),
        # "vtol_mode_deploy" (Prowler Utility); not the gear's own VTOL variants ("lg_pl_vtol")
        for n in names:
            low = n.lower()
            if re.search(r"(^|_|:)lg_|nozzle|flare|compress|idle", low) or is_gear_clip(n):
                continue  # not the Fury's "VTOL_Deploy", which is its landing gear
            # also the Constellation's "rsi_constellation_nacelle_bottom_deploy"; not the
            # San'tok.yai's component hatches ("components_nacelle_left_open")
            m = re.search(r"(vtol|nacelles?)(_mode|_bottom|_top|_left|_right|_front|_rear)?(_(deploy|open|extend|retract|close))?(\.caf)?$", low)
            if not m or "component" in low:
                continue
            closing = (m.group(4) or "deploy") in ("retract", "close")
            plan.append((n, "snap_first" if closing else "snap_last"))
    else:
        # "cannons_deploy" (Alpha Wolf), "weapons_deploy" and "Missile_deploy" (M80),
        # "guns_retract", "turret_deploy", the Eclipse's "missile_01_deploy", the Hornet's
        # "missile_bay_left_open", the Lightning's "weapon_nose_left_deploy", the
        # Gladiator's "bomber_doors_open", the Sabre's "bay_doors_open"
        weapon = re.compile(
            r"(?i)(?:^|[_/:])(?:weapons?|cannons?|guns?|missiles?|missilebays?|torpedo(?:e?s)?|rockets?|turrets?"
            r"|bomb(?:er)?|bay)(?:_[a-z0-9]+){0,3}?_(deploy|retract|open|close)(?:_(?:left|right|\d+))?(?:\.caf)?$")
        not_weapon = re.compile(r"(?i)seat|enter|exit|ladder|cockpit|canopy|cargo|turret_door|operator|pilot|rack|component|locker")
        out = kind == "weapons_out"
        # Gun covers that open on their own ("weapon_left_close" on the Stingray's top wing
        # guns: the game's open-all-exterior action) stay shut when the ship has a separate
        # clip that brings its guns out; missile and bomb bays still open.
        covers = re.compile(r"(?i)(?:^|[_/:])(?:weapons?|guns?)(?:_(?:left|right|top|\d+))*_(open|close)(?:\.caf)?$")
        has_deploy = any(weapon.search(n) and not not_weapon.search(n)
                         and weapon.search(n).group(1).lower() in ("deploy", "retract") for n in names)
        for n in names:
            m = weapon.search(n)
            if not m or not_weapon.search(n):
                continue
            if has_deploy and covers.search(n):
                continue
            opening = m.group(1).lower() in ("deploy", "open")
            plan.append((n, "snap_last" if opening == out else "snap_first"))
    # The rules above read a clip's direction from its name. A tagged use can play it
    # backwards ("fragment:1:canopy_close" as the "Open" action), so flip those.
    if kind not in ("gear", "closed"):
        fixed = []
        for n, mode in plan:
            if n.startswith("fragment:") and n in shut_end and n not in gear_end:
                stem = re.sub(r"^fragment:\d+:", "", n).lower()
                says_open = bool(re.search(r"deploy|open|extend|unfold", stem))
                acts_open = shut_end[n] == "snap_first"
                if says_open != acts_open:
                    mode = "snap_first" if mode == "snap_last" else "snap_last"
            fixed.append((n, mode))
        plan = fixed
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
        """(most parts one clip moves, how far all the clips lower their parts, summed)"""
        best, drop = 0, 0.0
        for n, mode in clips:
            reset()
            try:
                po.apply_animation_mode_to_package_root(bpy.context, root, n, mode)
                parts = moved_now()
                best = max(best, len(parts))
                gear_parts.update(parts)
                if parts:
                    dz = [bind[k].translation.z - bpy.data.objects[k].matrix_world.translation.z for k in parts]
                    drop += sum(dz)
                po.apply_animation_mode_to_package_root(bpy.context, root, n, "none")
            except Exception:
                pass
        reset()
        return best, drop

    gear_parts: set = set()

    def gear_hangs_below():
        """In the export, does the gear reach below the rest of the ship? Then the export
        has its gear down, whatever the clips say (the F7C-R Mk II plays the F7A's gear
        clip, which fits its legs badly and lowers them from either end)."""
        def lowest(names):
            zs = [(o.matrix_world @ Vector(c)).z for o in objs if o.name in names and o.type == "MESH" for c in o.bound_box]
            return min(zs) if zs else None
        gear_low = lowest(gear_parts)
        hull_low = lowest({o.name for o in objs if o.type == "MESH"} - gear_parts)
        if gear_low is None or hull_low is None:
            return False
        zs = [(o.matrix_world @ Vector(c)).z for o in objs if o.type == "MESH" for c in o.bound_box]
        height = (max(zs) - min(zs)) if zs else 0.0
        return gear_low < hull_low - 0.03 * height

    if kind == "landed":
        # Most ships are exported gear down. A few (the Stingray, the Reclaimer) come gear
        # up: their landed shape puts the gear down as well. Told apart by part counts, or
        # by how far the gear drops: the Reclaimer's clips start from a rest pose that isn't
        # the export's, so both ends move its legs, but the down end lowers them much more.
        (up, up_drop), (down, down_drop) = most_moved(gear_plan(True)), most_moved(gear_plan(False))
        hangs = gear_hangs_below()
        result["gear_check"] = [up, round(up_drop, 1), down, round(down_drop, 1), hangs]
        # The Ironclad's and Basher's clips move whole legs: a handful of parts, all
        # still at rest in the export with the gear up.
        # A ship exported gear down lifts its gear going up (the Gladius: its gear-down end
        # just stretches the struts a little past the export's), so the drop test only
        # counts when going up doesn't lift much.
        # When both ends lower the gear (clips that don't fit the model's own legs: the
        # Reclaimer's rest pose, the F7C-R Mk II borrowing the F7A's clip), only an export
        # whose gear doesn't already hang below the hull is gear up.
        by_drop = down_drop > max(20.0, 1.3 * up_drop) and up_drop > -0.5 * abs(down_drop) \
            and not (hangs and up_drop > 0.5 * down_drop)
        # Gear that hangs below the hull, lifts going up and barely drops going down is
        # down already, however many rig bones the down clip touches (the Mk I Hornets,
        # Archimedes and Skylark: their deploy clips move the whole rig a little).
        already_down = hangs and up_drop < 0 and abs(down_drop) < 20.0
        if not already_down and ((down > 20 and (up * 4 < down or by_drop))
                                 or (up == 0 and down > 0 and down_drop > 0.3)):
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
    if kind == "landed" and result.get("gear_down"):
        # Gear down on a ship exported gear up: the ship's own clip (the Stingray's wing
        # fold) and the gear items' extend clip overlap, but both end gear down. Lay the
        # gear's own clips on top so the struts extend instead of staying compressed.
        base = lambda n: re.sub(r"^fragment:\d+:", "", n).lower()  # noqa: E731
        used = {base(c["name"]) for c in result["clips"]}
        for n, mode, parts in tried:
            if parts and base(n) not in used and (n, mode) in gear_plan(False):
                po.apply_animation_mode_to_package_root(bpy.context, root, n, mode)
                used.add(base(n))
                result["clips"].append({"name": n, "mode": mode, "objects": len(parts), "layered": True})
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
