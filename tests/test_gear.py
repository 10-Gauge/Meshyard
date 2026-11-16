import json
import struct

import numpy as np
import trimesh

from scsandbox import gear


def _nodes(path):
    raw = path.read_bytes()
    n = struct.unpack("<I", raw[12:16])[0]
    return json.loads(raw[20:20 + n])["nodes"]


def test_key_strips_blender_prefixes_and_suffixes():
    assert gear._key("LG_Back_UpLeg_Skin_bn_001") == gear._key("LG_Back_UpLeg_Skin_bn")
    assert gear._key("Mesh.002") == "mesh"
    assert gear._key("AEGS_Gladius_LandingGear_BackLeft_Anim_2_LG_L_Back_Foot_Skin_bn") == "lg_l_back_foot_skin_bn"
    assert gear._key("Body_98_helper_landing_gear_front") == "helper_landing_gear_front"


def test_gear_up_closes_doors_and_hides_the_gear(tmp_path):
    """A ship with a gear door and a landing gear item; the pose comes from Blender in
    its own (Z-up) axes and must be mapped onto the GLB's (Y-up)."""
    sc = trimesh.Scene()
    box = trimesh.creation.box((1, 1, 1))
    sc.add_geometry(box, node_name="Body", geom_name="body")
    spots = {"Wing_L": [-4, 0, 0], "Wing_R": [4, 0, 0], "Nose": [0, 0, -6], "Tail": [0, 1, 6], "Fin": [0, 2, 5]}
    for name, p in spots.items():
        sc.graph.update(frame_to=name, frame_from="Body", matrix=trimesh.transformations.translation_matrix(p))
    sc.add_geometry(box.copy(), node_name="Landing_Gear_Door_A", geom_name="door", parent_node_name="Body",
                    transform=trimesh.transformations.translation_matrix([0, -1, -2]))
    sc.graph.update(frame_to="hardpoint_landing_gear_front", frame_from="Body",
                    matrix=trimesh.transformations.translation_matrix([0, -1, -3]))
    sc.graph.update(frame_to="Ship_LandingGear_Front.cdf", frame_from="hardpoint_landing_gear_front")
    sc.add_geometry(box.copy(), node_name="Ship_LandingGear_Front_Anim", geom_name="gear",
                    parent_node_name="Ship_LandingGear_Front.cdf")
    sc.add_geometry(box.copy(), node_name="LG_Front_Strut_Skin_bn", geom_name="strut",
                    parent_node_name="Ship_LandingGear_Front_Anim",
                    transform=trimesh.transformations.translation_matrix([0, -1, 0]))
    glb = tmp_path / "ship.glb"
    sc.export(glb)

    def world(name):
        return sc.graph.get(name)[0]

    # Blender world = Z-up: (x, y, z)_gltf -> (x, -z, y)
    C = np.array([[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1.0]])
    anchors = {n: (C @ world(n))[:3, 3].tolist() for n in [*spots, "Landing_Gear_Door_A", "LG_Front_Strut_Skin_bn"]}
    door_b = C @ world("Landing_Gear_Door_A")
    door_up = trimesh.transformations.translation_matrix([0, 0, 0.5]) @ door_b   # up 0.5 m in Blender Z
    strut_b = C @ world("LG_Front_Strut_Skin_bn")
    pose = {"blender": {"anchors": anchors, "objects": {
        "Landing_Gear_Door_A": {"bind": door_b.tolist(), "posed": door_up.tolist(), "source": ""},
        "LG_Front_Strut_Skin_bn": {"bind": strut_b.tolist(),
                                   "posed": (trimesh.transformations.translation_matrix([0, 0, 1]) @ strut_b).tolist(),
                                   "source": ""},
    }}}
    out = tmp_path / "up.glb"
    assert gear.apply_pose(glb, out, pose, log=lambda *_: None) > 0

    up = trimesh.load(out, force="scene")
    assert "Landing_Gear_Door_A" in up.graph.nodes_geometry                      # the door stays
    assert abs(up.graph.get("Landing_Gear_Door_A")[0][1, 3] - (-1 + 0.5)) < 1e-6  # up 0.5 m (GLB Y)
    assert "Ship_LandingGear_Front_Anim" not in up.graph.nodes_geometry           # gear hidden
    assert "LG_Front_Strut_Skin_bn" not in up.graph.nodes_geometry
    assert "Body" in up.graph.nodes_geometry                                      # the hull stays


def _gun_ship(tmp_path):
    """A hull with a cannon on a hinged arm and a landing gear item."""
    sc = trimesh.Scene()
    box = trimesh.creation.box((1, 1, 1))
    sc.add_geometry(box, node_name="Body", geom_name="body")
    for name, p in {"Wing_L": [-4, 0, 0], "Wing_R": [4, 0, 0], "Nose": [0, 0, -6], "Tail": [0, 1, 6]}.items():
        sc.graph.update(frame_to=name, frame_from="Body", matrix=trimesh.transformations.translation_matrix(p))
    sc.add_geometry(box.copy(), node_name="anim_cannon_left", geom_name="arm", parent_node_name="Body",
                    transform=trimesh.transformations.translation_matrix([-2, 0, 0]))
    sc.add_geometry(box.copy(), node_name="Cannon_Barrel", geom_name="barrel", parent_node_name="anim_cannon_left",
                    transform=trimesh.transformations.translation_matrix([0, 0, -1]))
    sc.graph.update(frame_to="Ship_LandingGear_Front.cdf", frame_from="Body")
    sc.add_geometry(box.copy(), node_name="Ship_LandingGear_Front_Anim", geom_name="gear",
                    parent_node_name="Ship_LandingGear_Front.cdf")
    glb = tmp_path / "ship.preview-l.glb"
    sc.export(glb)
    anchors = {n: sc.graph.get(n)[0][:3, 3].tolist() for n in ["Wing_L", "Wing_R", "Nose", "Tail", "anim_cannon_left"]}
    arm = sc.graph.get("anim_cannon_left")[0]
    raised = trimesh.transformations.translation_matrix([0, 1, 0]) @ arm   # the arm lifts 1 m
    pose = {"version": gear.POSE_VERSION, "blender": {"anchors": anchors, "objects": {
        "anim_cannon_left": {"bind": arm.tolist(), "posed": raised.tolist(), "source": ""}}}}
    return glb, pose


def test_weapons_deploy_moves_the_arm_and_its_gun_and_keeps_the_gear(tmp_path):
    glb, pose = _gun_ship(tmp_path)
    out = tmp_path / "out.glb"
    assert gear.apply_pose(glb, out, pose, log=lambda *_: None, hide_gear=False) == 1
    sc = trimesh.load(out, force="scene")
    assert abs(sc.graph.get("anim_cannon_left")[0][1, 3] - 1) < 1e-6
    assert abs(sc.graph.get("Cannon_Barrel")[0][1, 3] - 1) < 1e-6   # the gun rides on the arm
    assert "Ship_LandingGear_Front_Anim" in sc.graph.nodes_geometry


def test_posed_glb_stacks_gear_and_weapons_and_names_the_missing_pose(tmp_path, monkeypatch):
    glb, wpose = _gun_ship(tmp_path)
    from scsandbox import models
    monkeypatch.setattr(models, "glb_for", lambda *a, **k: glb)
    calls = []

    monkeypatch.setattr(gear, "_pose_path", lambda install, asset, kind="gear": tmp_path / f"X.{kind}.json")

    def fake_pose(install, settings, asset, log=print, kind="gear"):
        calls.append(kind)
        if kind == "gear":
            return {"blender": {"anchors": wpose["blender"]["anchors"], "objects": {}}}
        if kind in ("weapons_in", "vtol"):
            raise gear.NoPose(f"this ship has no {gear.KINDS[kind][2]} animation")
        if kind == "landed":  # this ship looks the same on the ground
            (tmp_path / "X.landed.json").write_text('{"version": 1, "blender": {"objects": {}}}')
            raise gear.NoPose("this ship has no landed shape animation")
        return wpose

    monkeypatch.setattr(gear, "pose_for", fake_pose)
    monkeypatch.setattr(gear, "apply_pose", lambda g, o, p, log=print, hide_gear=True:
                        (__import__("shutil").copyfile(g, o), 1)[1])
    out = gear.posed_glb(None, {}, {"record": "X"}, "preview", gear_up=True, weapons="out", log=lambda *_: None)
    # raising the gear also works out the landed shape, once
    assert out.name == "ship.preview-l.gearup.wout.glb" and calls == ["landed", "gear", "weapons_out"]
    assert gear.posed_glb(None, {}, {"record": "X"}, "preview", gear_up=True, weapons="out") == out  # cached
    assert calls == ["landed", "gear", "weapons_out"]
    try:
        gear.posed_glb(None, {}, {"record": "X"}, "preview", vtol=True, log=lambda *_: None)
        raise AssertionError("expected NoPose")
    except gear.NoPose as e:
        assert e.kind == "vtol"
    try:
        gear.posed_glb(None, {}, {"record": "X"}, "preview", weapons="in", log=lambda *_: None)
        raise AssertionError("expected NoPose")
    except gear.NoPose as e:
        assert e.kind == "weapons_in"


def test_gear_down_shows_the_landed_shape_once_it_is_known(tmp_path, monkeypatch):
    """The Eclipse and Stingray fold their wings on the ground. Their export has the gear
    down but the wings flat, so gear down gets the landed pose when the ship has one."""
    glb, wing = _gun_ship(tmp_path)   # the "arm" stands in for a folding wingtip
    from scsandbox import models
    monkeypatch.setattr(models, "glb_for", lambda *a, **k: glb)
    monkeypatch.setattr(gear, "_pose_path", lambda install, asset, kind="gear": tmp_path / f"X.{kind}.json")
    monkeypatch.setattr(gear, "pose_for", lambda install, settings, asset, log=print, kind="gear": wing)
    # not known yet: the plain export, no Blender run
    assert gear.posed_glb(None, {}, {"record": "X"}, "preview") == glb
    (tmp_path / "X.landed.json").write_text(json.dumps(wing))
    out = gear.posed_glb(None, {}, {"record": "X"}, "preview", log=lambda *_: None)
    assert out.name == "ship.preview-l.landed.glb"
    sc = trimesh.load(out, force="scene")
    assert abs(sc.graph.get("anim_cannon_left")[0][1, 3] - 1) < 1e-6   # folded
    assert "Ship_LandingGear_Front_Anim" in sc.graph.nodes_geometry   # gear still down
