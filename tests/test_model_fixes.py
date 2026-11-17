import math

import numpy as np

from scsandbox import models


def _roll(deg):
    """A quaternion rolling 90 degrees about the gun's barrel axis (y)."""
    h = math.radians(deg) / 2
    return [0.0, math.sin(h), 0.0, math.cos(h)]


def _ship():
    # ship root -> hardpoint -> gun (rolled on its side) -> port "hardpoint_ven";
    # the ventilation part hangs on the ship root at the port's spot, unrolled
    return {"scene": 0, "scenes": [{"nodes": [0]}], "nodes": [
        {"name": "Ship", "children": [1, 4, 6]},
        {"name": "hardpoint_weapon", "translation": [2.0, 0.0, 0.0], "children": [2]},
        {"name": "Gun", "rotation": _roll(90), "children": [3]},
        {"name": "hardpoint_ven", "translation": [1.5, 0.0, 0.0]},
        {"name": "Gun_Ventilation", "translation": [2.0, 0.0, -1.5], "children": [5]},
        {"name": "gat_s7-9_bas\\gat_s7_ven_1", "mesh": 0},
        # a ship part that happens to sit at the same spot but isn't a gun part
        {"name": "antenna", "translation": [2.0, 0.0, -1.5]},
    ]}


def _world(doc, i):
    parent = {c: p for p, nd in enumerate(doc["nodes"]) for c in nd.get("children", [])}
    m = np.eye(4)
    while i is not None:
        m = models._node_matrix(doc["nodes"][i]) @ m
        i = parent.get(i)
    return m


def test_part_takes_its_ports_rotation():
    doc = _ship()
    before = _world(doc, 4)[:3, 3]
    moved = models.mount_port_parts(doc)
    assert moved == [("Gun_Ventilation", "hardpoint_ven")]
    assert 4 in doc["nodes"][3]["children"] and 4 not in doc["nodes"][0]["children"]
    w, port = _world(doc, 4), _world(doc, 3)
    assert np.allclose(w, port, atol=1e-6)                 # rotated like the port
    assert np.allclose(w[:3, 3], before, atol=1e-6)        # and still in the same spot
    assert 6 in doc["nodes"][0]["children"]                # the antenna stays put


def test_part_already_lined_up_is_left_alone():
    doc = _ship()
    doc["nodes"][2].pop("rotation")
    doc["nodes"][4]["translation"] = [3.5, 0.0, 0.0]
    assert models.mount_port_parts(doc) == []


def _interior_doc(faces):
    # a hull mesh (faces triangles) used once by the ship and once inside an interior,
    # plus a small exterior prop in the interior that has to stay
    return {"nodes": [
        {"name": "Ship", "children": [1, 2]},
        {"name": "body", "mesh": 0},
        {"name": "interior_ship_int_main", "children": [3, 4]},
        {"mesh": 0, "translation": [0.0, 0.0, 6.0]},
        {"mesh": 1},
    ], "meshes": [
        {"name": "Objects/Spaceships/Ships/X/Exterior/x.cga", "primitives": [{"indices": 0}]},
        {"name": "objects/spaceships/ships/x/exterior/x_headlight.cga", "primitives": [{"indices": 1}]},
    ], "accessors": [{"count": faces * 3}, {"count": 360}]}


def test_hull_copies_leave_the_interior():
    doc = _interior_doc(400_000)
    assert models.drop_interior_hulls(doc) == 1
    assert "mesh" not in doc["nodes"][3]          # the stray hull copy is gone
    assert doc["nodes"][1]["mesh"] == 0           # the ship keeps its own hull
    assert doc["nodes"][4]["mesh"] == 1           # small exterior props stay


def test_small_exterior_models_stay():
    assert models.drop_interior_hulls(_interior_doc(5_000)) == 0


def _cube_doc(offsets):
    """A model of unit cubes, one node each, sharing one mesh."""
    import struct
    pts = [(x, y, z) for x in (0, 1) for y in (0, 1) for z in (0, 1)]
    idx = [0, 1, 2, 1, 3, 2, 4, 5, 6, 5, 7, 6]
    bin_ = struct.pack("<24f", *[c for p in pts for c in p]) + struct.pack("<12H", *idx)
    doc = {"nodes": [{"name": f"part_{k}", "mesh": 0, "translation": list(o)} for k, o in enumerate(offsets)],
           "meshes": [{"name": "cube", "primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
           "accessors": [{"bufferView": 0, "componentType": 5126, "count": 8, "type": "VEC3"},
                         {"bufferView": 1, "componentType": 5123, "count": 12, "type": "SCALAR"}],
           "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 96}, {"buffer": 0, "byteOffset": 96, "byteLength": 24}]}
    return doc, bin_


def test_small_groups_floating_free_are_found():
    hull = [(k, 0, 0) for k in range(30)]               # 30 cubes in a row: the ship
    doc, bin_ = _cube_doc(hull + [(10, 8, 0), (11, 8, 0)])  # a two-part group 7 m above it
    assert sorted(models.detached_parts(doc, bin_)) == [30, 31]


def test_a_ship_in_one_piece_loses_nothing():
    doc, bin_ = _cube_doc([(k, 0, 0) for k in range(30)] + [(30.3, 0, 0)])  # last one close enough
    assert models.detached_parts(doc, bin_) == []
