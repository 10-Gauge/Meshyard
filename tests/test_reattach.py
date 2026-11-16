import json
import struct

import trimesh

from scsandbox import models


def _read_json(path):
    raw = path.read_bytes()
    n = struct.unpack("<I", raw[12:16])[0]
    return json.loads(raw[20:20 + n])


def test_item_hanging_from_its_own_bone_is_put_back_on_the_ship(tmp_path):
    """The Idris railgun case: the item's skeleton has a bone named like the ship's
    hardpoint, and StarBreaker parents the item to that inner bone (a loop)."""
    glb = tmp_path / "ship.glb"
    sc = trimesh.Scene()
    sc.add_geometry(trimesh.creation.box((10, 2, 30)), node_name="hull", geom_name="hull")
    sc.graph.update(frame_to="hardpoint_nose_gun", frame_from="hull", matrix=trimesh.transformations.translation_matrix([0, -2, -12]))
    sc.graph.update(frame_to="Railgun", frame_from="hardpoint_nose_gun")
    sc.graph.update(frame_to="hardpoint_nose_gun_inner", frame_from="Railgun")
    sc.add_geometry(trimesh.creation.box((1, 1, 20)), node_name="barrel", geom_name="barrel", parent_node_name="Railgun")
    sc.export(glb)

    # make the loop: the item root under its own inner bone, which has the ship's hardpoint's name
    doc = _read_json(glb)
    nodes = doc["nodes"]
    idx = {nd.get("name"): i for i, nd in enumerate(nodes)}
    hp, item, inner = idx["hardpoint_nose_gun"], idx["Railgun"], idx["hardpoint_nose_gun_inner"]
    nodes[hp]["children"].remove(item)
    nodes[inner]["name"] = "hardpoint_nose_gun"
    nodes[inner].setdefault("children", []).append(item)
    models._edit_glb_json(glb, lambda d: d.update(nodes=nodes) or True)
    assert "barrel" not in trimesh.load(glb, force="scene").graph.nodes_geometry  # the part is lost

    assert models.reattach_orphans(glb, log=lambda *_: None) == 1
    back = trimesh.load(glb, force="scene")
    assert "barrel" in back.graph.nodes_geometry
    # it hangs from the ship's hardpoint, so it sits under the nose
    assert abs(back.graph.get("barrel")[0][1, 3] - -2) < 1e-6
    assert models.reattach_orphans(glb, log=lambda *_: None) == 0  # nothing left to fix


def _reachable(path, node):
    """Whether trimesh can place `node` (what the print loader needs)."""
    try:
        trimesh.load(path, force="scene").graph[node]
        return True
    except ValueError:
        return False


def test_empty_mesh_reference_no_longer_hides_a_subtree(tmp_path):
    """A node pointing at a mesh with no primitives makes trimesh lose its link to the
    parent, so everything under it can't be placed (the Idris-M's front-left landing
    gear vanished from prints)."""
    glb = tmp_path / "ship.glb"
    sc = trimesh.Scene()
    sc.add_geometry(trimesh.creation.box((10, 2, 30)), node_name="hull", geom_name="hull")
    sc.graph.update(frame_to="Hardpoint_LandingGear_FL", frame_from="hull")
    sc.graph.update(frame_to="Gear_Left.cdf", frame_from="Hardpoint_LandingGear_FL")
    sc.graph.update(frame_to="Gear_Left", frame_from="Gear_Left.cdf")
    sc.graph.update(frame_to="Gear_Left_root", frame_from="Gear_Left")
    sc.add_geometry(trimesh.creation.box((1, 4, 1)), node_name="strut", geom_name="strut",
                    parent_node_name="Gear_Left_root")
    sc.export(glb)
    doc = _read_json(glb)
    k = next(i for i, nd in enumerate(doc["nodes"]) if nd.get("name") == "Gear_Left")
    doc["nodes"][k]["mesh"] = len(doc["meshes"])
    doc["meshes"].append({"name": "Gear_Left", "primitives": []})
    models._edit_glb_json(glb, lambda d: d.update(nodes=doc["nodes"], meshes=doc["meshes"]) or True)
    assert not _reachable(glb, "strut")                        # the leg is lost

    assert models.drop_empty_meshes(glb, log=lambda *_: None) == 1
    assert _reachable(glb, "strut")
    assert models.drop_empty_meshes(glb, log=lambda *_: None) == 0
