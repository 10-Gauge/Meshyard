import numpy as np
import trimesh

from scsandbox import rig


def test_hand_frame_is_a_rotation_with_palm_on_minus_z():
    R = rig._hand_frame([0, -0.5, -1.0], [1.0, 0.2, 0])
    assert np.allclose(R.T @ R, np.eye(3), atol=1e-9)
    assert np.isclose(np.linalg.det(R), 1.0)
    x = np.array([0, -0.5, -1.0]) / np.linalg.norm([0, -0.5, -1.0])
    assert np.allclose(R[:, 0], x)
    assert R[0, 2] > 0.9  # back of the hand stays on the requested side


def test_drop_crumbs_keeps_the_gun_and_drops_a_loose_chip():
    gun = trimesh.creation.box([0.6, 0.08, 0.03])
    grip = trimesh.creation.box([0.03, 0.1, 0.03])
    grip.apply_translation([0.1, -0.08, 0])          # touches the gun
    chip = trimesh.creation.box([0.01, 0.01, 0.01])
    chip.apply_translation([0.0, -0.3, 0])           # 20 cm below: a crumb
    m = rig._drop_crumbs(trimesh.util.concatenate([gun, grip, chip]))
    assert m.bounds[0][1] > -0.2
    assert m.bounds[0][1] < -0.12                    # grip still there


class _Rig:
    """Two-bone arm with a hand and one finger, rest pose along +X."""
    def __init__(self):
        pos = {"Arm": [0, 0, 0], "ForeArm": [0.3, 0, 0], "RightHand": [0.55, 0, 0],
               "RightHandIndex1": [0.65, 0, 0], "RightHandIndex2": [0.7, 0, 0]}
        self.names = list(pos)
        self.rest = {}
        for n, p in pos.items():
            M = np.eye(4)
            M[:3, 3] = p
            self.rest[n] = M
        self.parent = {"Arm": None, "ForeArm": "Arm", "RightHand": "ForeArm",
                       "RightHandIndex1": "RightHand", "RightHandIndex2": "RightHandIndex1"}


def test_ik_reaches_target_and_orient_sets_hand_rotation():
    r = _Rig()
    p = rig.Poser(r)
    target = np.array([0.3, -0.25, -0.1])
    p.ik("Arm", "ForeArm", "RightHand", target, pole=[0, 0, -1])
    R = rig._hand_frame([0, -1.0, 0], [0, 0, 1.0])
    p.orient("RightHand", R)
    D = p.solve()
    assert np.allclose(rig.apply(D["RightHand"], r.rest["RightHand"][:3, 3]), target, atol=1e-6)
    assert np.allclose(D["RightHand"][:3, :3] @ r.rest["RightHand"][:3, :3], R, atol=1e-6)
    # the finger follows the hand: it now points down from the wrist
    f = rig.apply(D["RightHandIndex1"], r.rest["RightHandIndex1"][:3, 3])
    assert np.allclose(f, target + [0, -0.1, 0], atol=1e-6)


def test_curl_bends_fingers_toward_the_palm():
    r = _Rig()
    p = rig.Poser(r)
    p.curl("Right", {"Index": (90, 0, 0)})
    D = p.solve()
    tip = rig.apply(D["RightHandIndex2"], r.rest["RightHandIndex2"][:3, 3])
    # rest hand frame is identity: palm is -Z, so the curled finger ends up below the knuckle in Z
    assert np.allclose(tip, [0.65, 0, -0.05], atol=1e-6)


def _gun(tmp_path, helpers, boxes):
    """A weapon GLB: boxes ((size, center), ...) and named helper points (game Y-up frame)."""
    sc = trimesh.Scene()
    for i, (size, center) in enumerate(boxes):
        b = trimesh.creation.box(size)
        b.apply_translation(center)
        sc.add_geometry(b, node_name=f"part{i}", geom_name=f"part{i}")
    for name, p in helpers.items():
        sc.graph.update(frame_to=name, frame_from=sc.graph.base_frame, matrix=trimesh.transformations.translation_matrix(p))
    path = tmp_path / "gun.glb"
    sc.export(path)
    return path


def test_launcher_front_grip_decides_which_end_is_the_muzzle(tmp_path):
    """The rocket launcher's muzzle guess came out backwards; the support hand target
    (ahead of the trigger hand on any long gun) turns it round."""
    glb = _gun(tmp_path, {"trigger": [0, 0.04, -0.07], "L_IkGripTarget": [-0.14, -0.04, -0.18]},
               [((0.2, 0.2, 1.1), (0, 0.18, 0.1)), ((0.03, 0.1, 0.03), (0, -0.03, 0))])
    w = rig.Weapon(glb, kind="shouldered")
    assert np.allclose(w.forward, [0, 0, -1])
    assert np.dot(w.lwrist - w.rgrip, w.forward) > 0.1        # support hand ahead of the grip
    # it rests on the shoulder at the underside of the tube behind the grip, not inside it
    assert abs(w.pad[1] - 0.08) < 0.01 and w.pad[2] > 0.1


def test_pistol_support_hand_wraps_the_grip(tmp_path):
    glb = _gun(tmp_path, {"trigger": [0, 0.03, -0.03], "muzzle_flash": [0, 0.08, -0.2],
                          "L_IkGripTarget": [-0.03, -0.04, 0.08]},
               [((0.03, 0.04, 0.22), (0, 0.08, -0.09)), ((0.03, 0.1, 0.03), (0, 0.0, 0.0))])
    pistol = rig.Weapon(glb, kind="pistol")
    assert np.allclose(pistol.lwrist, [-0.03, -0.04, 0.08])  # the game's target, behind the trigger
    rifle_like = rig.Weapon(glb, kind="stocked")
    assert np.dot(rifle_like.lwrist - rifle_like.rgrip, rifle_like.forward) > 0.05


def _anim_glb(path):
    """Two-joint chain (root -> arm, 1 m along +X) and one clip turning the root 90°
    about +Y, written as a minimal glTF binary."""
    import json
    import struct
    times = np.array([0.0, 1.0], np.float32)
    s = np.sqrt(0.5)
    rots = np.array([[0, s, 0, s], [0, s, 0, s]], np.float32)       # x, y, z, w
    bin_ = times.tobytes() + rots.tobytes()
    j = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
         "nodes": [{"name": "Root", "children": [1]}, {"name": "Arm", "translation": [1, 0, 0]}],
         "buffers": [{"byteLength": len(bin_)}],
         "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 8},
                         {"buffer": 0, "byteOffset": 8, "byteLength": 32}],
         "accessors": [{"bufferView": 0, "componentType": 5126, "count": 2, "type": "SCALAR"},
                       {"bufferView": 1, "componentType": 5126, "count": 2, "type": "VEC4"}],
         "animations": [{"name": "turn", "samplers": [{"input": 0, "output": 1}],
                         "channels": [{"sampler": 0, "target": {"node": 0, "path": "rotation"}}]}]}
    js = json.dumps(j).encode()
    js += b" " * (-len(js) % 4)
    body = struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(bin_), 0x004E4942) + bin_
    path.write_bytes(struct.pack("<III", 0x46546C67, 2, 12 + len(body)) + body)
    return path


def test_clip_pose_and_base_pose_deltas(tmp_path):
    clips = rig.Clips(_anim_glb(tmp_path / "a.glb"))
    assert clips.names() == ["turn"]
    posed = clips.pose("turn", 0.5)
    assert np.allclose(posed["Arm"][:3, 3], [0, 0, -1], atol=1e-6)    # +X turned 90° about +Y

    class R:
        names = ["Root", "Arm"]
        parent = {"Root": None, "Arm": "Root"}
        rest = {"Root": np.eye(4), "Arm": np.eye(4)}
    R.rest["Arm"] = np.eye(4)
    R.rest["Arm"][:3, 3] = [1, 0, 0]
    base = rig.clip_deltas(R, posed)
    D = rig.Poser(R).solve(base=base)                    # no rules: the clip pose itself
    assert np.allclose(rig.apply(D["Arm"], [1, 0, 0]), [0, 0, -1], atol=1e-6)


def test_weapon_bone_matches_the_game_skeleton():
    """RightWeaponBone sits 9 cm along the hand and 3 cm off it (skeleton values)."""
    assert np.allclose(rig.WEAPON_BONE[:3, 3], [0.09, 0.0, -0.03], atol=1e-6)
    assert np.allclose(rig.WEAPON_BONE[:3, :3] @ rig.WEAPON_BONE[:3, :3].T, np.eye(3), atol=1e-5)
