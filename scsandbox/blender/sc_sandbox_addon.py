bl_info = {
    "name": "SC Sandbox",
    "author": "sc-sandbox",
    "version": (0, 1, 0),
    "blender": (3, 6, 0),
    "location": "3D Viewport > Sidebar > SC Sandbox",
    "description": "Spawn Star Citizen assets from your local install into a sandbox scene, and prep them for 3D printing",
    "category": "Import-Export",
}

# Talks to the local asset browser (`sc-sandbox serve`), which does the
# extraction. Nothing here touches the network beyond 127.0.0.1.

import json
import math
import os
import traceback
import urllib.parse
import urllib.request

import bpy
from mathutils import Vector
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, StringProperty

COLL = "SC Sandbox"
SPAWN_COLL = "SC Spawned"


def prefs():
    return bpy.context.preferences.addons[__name__].preferences


def call(path, body=None, timeout=10):
    url = prefs().server.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ensure_collection(name, parent=None):
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
        (parent or bpy.context.scene.collection).children.link(coll)
    return coll


# --- spawning -----------------------------------------------------------------

_spawn_offset = [0.0]


def import_glb(path, label):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    new = [o for o in bpy.data.objects if o not in before]
    if not new:
        return None
    coll = ensure_collection(SPAWN_COLL)
    root = bpy.data.objects.new(label, None)
    root.empty_display_type = "ARROWS"
    coll.objects.link(root)
    for o in new:
        for c in list(o.users_collection):
            c.objects.unlink(o)
        coll.objects.link(o)
        if o.parent is None:
            o.parent = root
    # Place at the 3D cursor, stepping sideways so repeated spawns don't overlap.
    bpy.context.view_layer.update()
    xs = [(o.matrix_world @ Vector(c)).x for o in new if o.type == "MESH" for c in o.bound_box]
    width = (max(xs) - min(xs)) if xs else 5.0
    root.location = bpy.context.scene.cursor.location.copy()
    root.location.x += _spawn_offset[0]
    _spawn_offset[0] += width * 1.2
    for o in bpy.context.selected_objects:
        o.select_set(False)
    root.select_set(True)
    bpy.context.view_layer.objects.active = root
    return root


def starbreaker_ready():
    return "starbreaker_addon" in bpy.context.preferences.addons


def _view3d_override():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                return {"window": window, "screen": window.screen, "area": area, "region": region}
    return {}


def append_package_scene(blend, label):
    """StarBreaker 0.3+ writes each package as a ready Blender scene (scene.blend, with
    meshes linked from per-part .blend files). Append its "StarBreaker ..." collection."""
    with bpy.data.libraries.load(blend, link=False) as (src, dst):
        dst.collections = [c for c in src.collections if c.startswith("StarBreaker")]
    colls = [c for c in dst.collections if c is not None]
    if not colls:
        raise RuntimeError(f"No StarBreaker collection found in {blend}")
    parent = ensure_collection(SPAWN_COLL)
    roots = []
    for c in colls:
        parent.children.link(c)
        roots += [o for o in c.all_objects if o.parent is None]
    bpy.context.view_layer.update()
    offset = bpy.context.scene.cursor.location.copy()
    offset.x += _spawn_offset[0]
    xs = [(o.matrix_world @ Vector(c)).x for o in colls[0].all_objects if o.type == "MESH" for c in o.bound_box]
    _spawn_offset[0] += ((max(xs) - min(xs)) if xs else 5.0) * 1.2
    for o in bpy.context.selected_objects:
        o.select_set(False)
    # The package root stores its scene.json relative to an export root it leaves blank,
    # which only resolves while scene.blend itself is open. Point both at this package.
    export_root = os.path.dirname(os.path.dirname(os.path.dirname(blend)))
    for r in roots:
        if r.get("starbreaker_package_root"):
            r["starbreaker_export_root"] = export_root
            r["starbreaker_scene_path"] = os.path.join(os.path.dirname(blend), "scene.json")
        r.location += offset
        r.select_set(True)
    if roots:
        bpy.context.view_layer.objects.active = roots[0]
    st = bpy.context.window_manager.sc_sandbox
    pkg_roots = [r for r in roots if r.get("starbreaker_package_root")]
    if pkg_roots:
        st.last_package = pkg_roots[0].name
    st.status = f"Spawned {label}: pick a pose under Animations below"
    return roots[0] if roots else None


def _ensure_starbreaker_import():
    # StarBreaker's add-on (0.2.2+addon.6) defines its package importer and progress popup
    # but leaves them out of its CLASSES list, so Blender never registers them.
    if hasattr(bpy.types, "STARBREAKER_OT_import_decomposed_package"):
        return
    from starbreaker_addon import ui as sb_ui
    for name in ("STARBREAKER_OT_import_progress_popup", "STARBREAKER_OT_import_decomposed_package"):
        cls = getattr(sb_ui, name, None)
        if cls is not None and not hasattr(bpy.types, name):
            bpy.utils.register_class(cls)


def spawn_path(path, label):
    """Import a spawned asset: a GLB, or a StarBreaker animated package (scene.json)."""
    if not path.lower().endswith("scene.json"):
        return import_glb(path, label)
    if not starbreaker_ready():
        raise RuntimeError("Animated spawns need the StarBreaker add-on: run  sc-sandbox blender install")
    blend = os.path.join(os.path.dirname(path), "scene.blend")
    if os.path.isfile(blend):
        return append_package_scene(blend, label)
    _ensure_starbreaker_import()
    with bpy.context.temp_override(**_view3d_override()):
        bpy.ops.starbreaker.import_decomposed_package("EXEC_DEFAULT", filepath=path, prefer_cycles=False)
    st = bpy.context.window_manager.sc_sandbox
    st.status = f"Importing {label}… then use the StarBreaker tab for gear, doors and ramps"
    return None


def wait_for_job(job_id, label, on_done):
    """Poll a server job from a timer so Blender's UI stays responsive."""
    def tick():
        try:
            j = call(f"/api/jobs/{job_id}")
        except Exception as e:
            print("SC Sandbox:", e)
            return 2.0
        st = bpy.context.window_manager.sc_sandbox
        if j["state"] == "running":
            st.status = f"{label}: " + (j["log"][-1] if j["log"] else "working…")
            return 1.0
        if j["state"] == "error":
            st.status = f"{label} failed: {j['error'][:200]}"
            return None
        try:
            on_done(j["result"])
        except Exception as e:
            traceback.print_exc()
            st.status = f"{label} failed in Blender: {e}"
            return None
        st.status = f"{label}: done"
        return None
    bpy.app.timers.register(tick, first_interval=0.5)


def poll_queue():
    """Import anything the browser's "Spawn in sandbox" button queued."""
    st = bpy.context.window_manager.sc_sandbox
    if not st.watch:
        return None
    try:
        items = call(f"/api/sandbox/queue?after={st.queue_seq}")["items"]
    except Exception:
        return 3.0
    for it in items:
        st.queue_seq = max(st.queue_seq, it["seq"])
        try:
            spawn_path(it["glb"], it["name"])
            st.status = f"Spawned {it['name']} from the browser"
        except Exception as e:
            st.status = f"Could not import {it['name']}: {e}"
    return 2.0


def start_watch(st):
    if not st.watch or bpy.app.timers.is_registered(poll_queue):
        return
    try:  # only spawn what is sent from now on, not the whole history
        items = call("/api/sandbox/queue?after=0")["items"]
        st.queue_seq = max([st.queue_seq] + [i["seq"] for i in items])
    except Exception:
        pass
    bpy.app.timers.register(poll_queue)


# --- data ------------------------------------------------------------------------

CATEGORIES = [("spaceships", "Ships", ""), ("groundvehicles", "Vehicles", ""), ("scitem", "Ship items", ""),
              ("ALL", "Everything", "")]


def run_search(st):
    """Fill the results list from the asset browser. Returns an error message, or None."""
    params = {"channel": st.channel, "q": st.query, "kind": "entity", "limit": 100}
    if st.category != "ALL":
        params["category"] = st.category
    try:
        d = call("/api/assets?" + urllib.parse.urlencode(params))
    except Exception as e:
        st.status = "Asset browser not running: start it with  sc-sandbox serve"
        return f"Asset browser not reachable at {prefs().server} ({e}). Run: sc-sandbox serve"
    st.results.clear()
    for a in d["items"]:
        r = st.results.add()
        r.asset_id, r.record, r.label = a["id"], a["record"], a["name"]
        r.detail = " · ".join(x for x in (a["manufacturer"], a["category"]) if x)
    st.index = 0
    st.status = f"{d['total']} matches" + (" (showing 100)" if d["total"] > 100 else "")
    # Results filled from a timer don't show until the sidebar redraws.
    for win in getattr(bpy.context.window_manager, "windows", []):
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    return None


def _initial_search():
    # Fill the list once Blender is up, so it isn't empty until the first search.
    wm = bpy.context.window_manager
    if wm is None:
        return 1.0
    if not wm.sc_sandbox.results:
        run_search(wm.sc_sandbox)
    return None

class SCResult(bpy.types.PropertyGroup):
    asset_id: IntProperty()
    record: StringProperty()
    label: StringProperty()
    detail: StringProperty()


class SCState(bpy.types.PropertyGroup):
    query: StringProperty(name="Search", description="Ship, vehicle, weapon or prop name (Enter to search)",
                          update=lambda self, ctx: run_search(self))
    category: EnumProperty(name="Show", items=CATEGORIES, default="spaceships",
                           update=lambda self, ctx: run_search(self))
    channel: EnumProperty(name="Install", items=[("LIVE", "LIVE", ""), ("PTU", "PTU", ""), ("EPTU", "EPTU", ""),
                                                ("HOTFIX", "HOTFIX", ""), ("TECH-PREVIEW", "TECH-PREVIEW", "")])
    results: CollectionProperty(type=SCResult)
    index: IntProperty()
    status: StringProperty()
    last_package: StringProperty()
    watch: BoolProperty(name="Spawn from browser", description="Import assets sent from the web browser",
                        default=False, update=lambda self, ctx: start_watch(self))
    queue_seq: IntProperty(default=0)
    print_size: FloatProperty(name="Longest side (mm)", default=150.0, min=10.0)
    print_voxel: FloatProperty(name="Detail (mm)", default=0.2, min=0.05, max=2.0)


class SC_UL_results(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.label, icon="OBJECT_DATA")
        layout.label(text=item.detail)


# --- operators --------------------------------------------------------------------

class SC_OT_build_scene(bpy.types.Operator):
    bl_idname = "sc_sandbox.build_scene"
    bl_label = "Build sandbox scene"
    bl_description = "Add a ground grid, sky, sun, camera and a 1.8 m scale figure"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        scene.unit_settings.system = "METRIC"
        coll = ensure_collection(COLL)

        if "SC Ground" not in bpy.data.objects:
            mesh = bpy.data.meshes.new("SC Ground")
            s = 1000.0
            mesh.from_pydata([(-s, -s, 0), (s, -s, 0), (s, s, 0), (-s, s, 0)], [], [(0, 1, 2, 3)])
            ground = bpy.data.objects.new("SC Ground", mesh)
            coll.objects.link(ground)
            mat = bpy.data.materials.new("SC Grid")
            mat.use_nodes = True
            nt = mat.node_tree
            bsdf = nt.nodes.get("Principled BSDF")
            tex = nt.nodes.new("ShaderNodeTexChecker")
            tex.inputs["Scale"].default_value = 200.0
            tex.inputs["Color1"].default_value = (0.18, 0.19, 0.21, 1)
            tex.inputs["Color2"].default_value = (0.24, 0.25, 0.27, 1)
            nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
            bsdf.inputs["Roughness"].default_value = 0.8
            mesh.materials.append(mat)
            ground.hide_select = True

        if "SC Sun" not in bpy.data.objects:
            light = bpy.data.lights.new("SC Sun", "SUN")
            light.energy = 4.0
            light.angle = math.radians(1.5)
            sun = bpy.data.objects.new("SC Sun", light)
            sun.rotation_euler = (math.radians(50), 0, math.radians(35))
            coll.objects.link(sun)

        if "SC Scale 1.8m" not in bpy.data.objects:
            bpy.ops.mesh.primitive_cylinder_add(radius=0.25, depth=1.8, location=(-3, -3, 0.9))
            fig = context.active_object
            fig.name = "SC Scale 1.8m"
            for c in list(fig.users_collection):
                c.objects.unlink(fig)
            coll.objects.link(fig)

        if "SC Camera" not in bpy.data.objects:
            cam = bpy.data.objects.new("SC Camera", bpy.data.cameras.new("SC Camera"))
            cam.location = (40, -40, 20)
            cam.rotation_euler = (math.radians(70), 0, math.radians(45))
            cam.data.clip_end = 20000
            coll.objects.link(cam)
            scene.camera = cam

        world = scene.world or bpy.data.worlds.new("SC World")
        scene.world = world
        world.use_nodes = True
        nt = world.node_tree
        bg = nt.nodes.get("Background")
        if bg and not any(n.bl_idname == "ShaderNodeTexSky" for n in nt.nodes):
            sky = nt.nodes.new("ShaderNodeTexSky")
            try:
                sky.sky_type = "NISHITA"
            except TypeError:
                pass
            nt.links.new(sky.outputs["Color"], bg.inputs["Color"])
            bg.inputs["Strength"].default_value = 0.4

        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.spaces.active.clip_end = 20000
        ensure_collection(SPAWN_COLL)
        _spawn_offset[0] = 0.0
        self.report({"INFO"}, "Sandbox scene ready")
        return {"FINISHED"}


class SC_OT_search(bpy.types.Operator):
    bl_idname = "sc_sandbox.search"
    bl_label = "Search"

    def execute(self, context):
        err = run_search(context.window_manager.sc_sandbox)
        if err:
            self.report({"ERROR"}, err)
            return {"CANCELLED"}
        return {"FINISHED"}


class SC_OT_spawn(bpy.types.Operator):
    bl_idname = "sc_sandbox.spawn"
    bl_label = "Spawn at cursor"
    bl_description = "Export the selected asset from your install and import it at the 3D cursor"

    animated: BoolProperty(default=False, description="Import with animations (landing gear, doors, ramps) via the StarBreaker add-on")

    def execute(self, context):
        st = context.window_manager.sc_sandbox
        if not st.results or st.index >= len(st.results):
            self.report({"WARNING"}, "Search and pick an asset first")
            return {"CANCELLED"}
        r = st.results[st.index]
        try:
            if self.animated and not starbreaker_ready():
                raise RuntimeError("Animated spawns need the StarBreaker add-on: run  sc-sandbox blender install")
            variant = "animated" if self.animated else "full"
            job = call("/api/model", {"channel": st.channel, "id": r.asset_id, "variant": variant})["job"]
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        label = r.label
        wait_for_job(job, f"Spawning {label}", lambda res: spawn_path(res["path"], label))
        st.status = f"Spawning {label}…"
        return {"FINISHED"}


_clip_cache = {}


def package_root_for_panel(context):
    """The animated ship the Animations box acts on: the selected one, else the last spawned."""
    if not starbreaker_ready():
        return None
    try:
        from starbreaker_addon.runtime.package_ops import find_package_root
    except Exception:
        return None
    root = find_package_root(context.active_object)
    if root is None:
        root = bpy.data.objects.get(context.window_manager.sc_sandbox.last_package)
    return root if root is not None and root.get("starbreaker_package_root") else None


def package_clips(root):
    path = root.get("starbreaker_scene_path", "")
    if path not in _clip_cache:
        try:
            from starbreaker_addon.manifest import PackageBundle
            from starbreaker_addon import ui as sb_ui
            _clip_cache[path] = [c for c in sb_ui.available_package_animation_items(PackageBundle.load(path))
                                 if c[0] not in ("*",) and not c[0].startswith("anim_")]
        except Exception as e:
            print("SC Sandbox: could not read clips:", e)
            _clip_cache[path] = []
    return _clip_cache[path]


class SC_OT_pose(bpy.types.Operator):
    bl_idname = "sc_sandbox.pose"
    bl_label = "Pose"
    bl_description = "Start: first frame of the clip. End: last frame. Play: insert the clip and play it"
    bl_options = {"REGISTER", "UNDO"}

    root: StringProperty()
    clip: StringProperty()
    mode: StringProperty(default="snap_last")

    def execute(self, context):
        root = bpy.data.objects.get(self.root)
        if root is None:
            self.report({"ERROR"}, "That ship is no longer in the scene")
            return {"CANCELLED"}
        for o in context.selected_objects:
            o.select_set(False)
        root.select_set(True)
        context.view_layer.objects.active = root
        sb_mode = "action" if self.mode == "play" else self.mode
        with context.temp_override(active_object=root, object=root, selected_objects=[root]):
            res = bpy.ops.starbreaker.apply_animation_mode(animation_name=self.clip, mode=sb_mode)
        if "FINISHED" not in res:
            return {"CANCELLED"}
        if self.mode == "play":
            if not context.screen.is_animation_playing:
                bpy.ops.screen.animation_play()
        return {"FINISHED"}


class SC_OT_make_printable(bpy.types.Operator):
    bl_idname = "sc_sandbox.make_printable"
    bl_label = "Export selection for printing"
    bl_description = "Join the selected meshes, voxel-remesh them into one watertight solid, scale and save as STL"

    filepath: StringProperty(subtype="FILE_PATH", default="//print.stl")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        st = context.window_manager.sc_sandbox
        # Everything under the selection, as currently posed (gear up/down, doors
        # open), including collection instances the StarBreaker importer uses.
        wanted = set()
        for o in context.selected_objects:
            wanted.add(o.name)
            wanted.update(c.name for c in o.children_recursive)
        depsgraph = context.evaluated_depsgraph_get()
        import bmesh
        bm = bmesh.new()
        count = 0
        for inst in depsgraph.object_instances:
            owner = inst.parent if inst.is_instance else inst.object
            if owner is None or owner.original.name not in wanted or inst.object.type != "MESH":
                continue
            ob = inst.object
            tmp = ob.to_mesh()
            tmp.transform(inst.matrix_world)
            bm.from_mesh(tmp)
            ob.to_mesh_clear()
            count += 1
        if not count:
            bm.free()
            self.report({"WARNING"}, "Select a spawned asset (or its meshes) first")
            return {"CANCELLED"}
        me = bpy.data.meshes.new("SC Print")
        bm.to_mesh(me)
        bm.free()
        obj = bpy.data.objects.new("SC Print", me)
        context.scene.collection.objects.link(obj)

        dims = obj.dimensions
        longest = max(dims) or 1.0
        scale = (st.print_size / 1000.0) / longest  # Blender units are meters; STL is read as mm below
        obj.scale = (scale, scale, scale)
        bpy.context.view_layer.objects.active = obj
        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        bpy.ops.object.transform_apply(scale=True)
        rem = obj.modifiers.new("Voxel", "REMESH")
        rem.mode = "VOXEL"
        rem.voxel_size = st.print_voxel / 1000.0
        rem.use_smooth_shade = False
        bpy.ops.object.modifier_apply(modifier=rem.name)
        # Write millimeters: slicers read STL units as mm.
        obj.scale = (1000, 1000, 1000)
        bpy.ops.object.transform_apply(scale=True)
        path = bpy.path.abspath(self.filepath)
        if hasattr(bpy.ops.wm, "stl_export"):
            bpy.ops.wm.stl_export(filepath=path, export_selected_objects=True, global_scale=1.0)
        else:
            bpy.ops.export_mesh.stl(filepath=path, use_selection=True, global_scale=1.0)
        bpy.data.objects.remove(obj)
        self.report({"INFO"}, f"Saved {path}")
        return {"FINISHED"}


class SC_PT_panel(bpy.types.Panel):
    bl_label = "SC Sandbox"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "SC Sandbox"

    def draw(self, context):
        st = context.window_manager.sc_sandbox
        col = self.layout.column()
        col.operator("sc_sandbox.build_scene", icon="WORLD")
        col.separator()
        col.prop(st, "channel")
        col.prop(st, "category")
        row = col.row(align=True)
        row.prop(st, "query", text="")
        row.operator("sc_sandbox.search", text="", icon="VIEWZOOM")
        col.template_list("SC_UL_results", "", st, "results", st, "index", rows=8)
        row = col.row(align=True)
        row.operator("sc_sandbox.spawn", icon="IMPORT").animated = False
        sub = row.row(align=True)
        sub.enabled = starbreaker_ready()
        sub.operator("sc_sandbox.spawn", text="Spawn animated", icon="ARMATURE_DATA").animated = True
        if starbreaker_ready():
            col.label(text="Gear, doors, ramps: StarBreaker tab", icon="INFO")
        col.prop(st, "watch", icon="LINKED")
        root = package_root_for_panel(context)
        if root is not None:
            clips = package_clips(root)
            box = col.box()
            box.label(text=f"Animations: {root.get('starbreaker_package_name', root.name)}", icon="ARMATURE_DATA")
            if not clips:
                box.label(text="No clips in this export")
            for clip, label in clips:
                row = box.row(align=True)
                row.label(text=label)
                for mode, text in (("snap_first", "Start"), ("snap_last", "End"), ("play", "Play")):
                    op = row.operator("sc_sandbox.pose", text=text)
                    op.root, op.clip, op.mode = root.name, clip, mode
        col.separator()
        box = col.box()
        box.label(text="3D print", icon="MOD_REMESH")
        box.prop(st, "print_size")
        box.prop(st, "print_voxel")
        box.operator("sc_sandbox.make_printable", icon="EXPORT")
        if st.status:
            col.separator()
            col.label(text=st.status)


class SCPrefs(bpy.types.AddonPreferences):
    bl_idname = __name__
    server: StringProperty(name="Asset browser URL", default="http://127.0.0.1:8765")

    def draw(self, context):
        self.layout.prop(self, "server")


CLASSES = (SCResult, SCState, SC_UL_results, SC_OT_build_scene, SC_OT_search, SC_OT_spawn,
           SC_OT_pose, SC_OT_make_printable, SC_PT_panel, SCPrefs)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.WindowManager.sc_sandbox = bpy.props.PointerProperty(type=SCState)
    bpy.app.timers.register(_initial_search, first_interval=1.0)


def unregister():
    if bpy.app.timers.is_registered(_initial_search):
        bpy.app.timers.unregister(_initial_search)
    del bpy.types.WindowManager.sc_sandbox
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
