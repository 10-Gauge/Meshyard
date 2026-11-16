import json
import sqlite3

import numpy as np
import trimesh

from scsandbox import catalog, models


def _geometry(own, variants):
    return {"_Type_": "SGeometryResourceParams", "Geometry": {
        "Geometry": {"Geometry": {"path": own}},
        "SubGeometry": [{"Tags": tag, "Geometry": {"Geometry": {"path": path}}} for tag, path in variants]}}


FREELANCER = "objects/spaceships/thrusters/misc/misc_freelancer/misc_freelancer_thruster_main_"
OWN = "Objects/Spaceships/Thrusters/CRUS/CRUS_Starlifter_Thrusters/CRUS_Starlifter_VTOL_Side.cga"


def test_only_leftover_variants_of_another_model_count():
    rec = {"Components": [_geometry(OWN, [("Left", FREELANCER + "left.cga"), ("Right", FREELANCER + "right.cga")])]}
    own, tagged = models.tagged_geometry(rec)
    assert own == OWN and tagged == {"misc_freelancer_thruster_main_left", "misc_freelancer_thruster_main_right"}
    # the Arrow's right retro thruster is a real variant of the same model
    arrow = {"Components": [_geometry("objects/ships/anvl/arrow/anvl_arrow_thruster_retro_left.cga",
                                      [("Right", "objects/ships/anvl/arrow/anvl_arrow_thruster_retro_right.cga")])]}
    assert models.tagged_geometry(arrow) is None
    # and so are the 350r's, kept in another folder
    orig = {"Components": [_geometry("objects/ships/orig/orig_350r_thruster_main.cga",
                                     [("Left", "objects/thrusters/orig/orig_350r_thruster_main_left.cga")])]}
    assert models.tagged_geometry(orig) is None


def test_starlifter_thruster_gets_its_own_model_back(tmp_path, monkeypatch):
    """StarBreaker built the Starlifter's VTOL thruster from the Freelancer engine left in
    its record as a "Right" variant (a 10 m nozzle under the hull)."""
    build = tmp_path / "build"
    (build / "records").mkdir(parents=True)
    (build / "records" / "vtol.json").write_text(json.dumps(
        {"Components": [_geometry(OWN, [("Left", FREELANCER + "left.cga"), ("Right", FREELANCER + "right.cga")])]}))
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.execute("CREATE TABLE assets (record TEXT, kind TEXT, record_path TEXT)")
    con.execute("INSERT INTO assets VALUES ('CRUS_Starlifter_Thruster_VTOL_Side', 'entity', 'vtol.json')")
    monkeypatch.setattr(catalog, "connect", lambda inst: con)
    monkeypatch.setattr(catalog, "build_dir", lambda inst: build)

    # the item's own model, as cgf-converter writes it: Y-up, a small box 2 m up
    def convert(inst, settings, geo, out):
        assert geo == OWN
        part = trimesh.Scene()
        part.add_geometry(trimesh.creation.box((1, 0.5, 3)), node_name="CRUS_Starlifter_Vtol_Side_Geo",
                          geom_name="geo", transform=trimesh.transformations.translation_matrix([0, 2, 0]))
        part.export(out)
        return out
    monkeypatch.setattr(models, "_convert_raw", convert)

    glb = build / "ship.glb"
    sc = trimesh.Scene()
    sc.add_geometry(trimesh.creation.box((10, 30, 4)), node_name="hull", geom_name="hull")
    sc.graph.update(frame_to="hardpoint_thruster_VTOL_side_right", frame_from="hull",
                    matrix=trimesh.transformations.translation_matrix([3, -10, -1]))
    sc.graph.update(frame_to="CRUS_Starlifter_Thruster_VTOL_Side", frame_from="hardpoint_thruster_VTOL_side_right")
    sc.graph.update(frame_to="MISC_Freelancer_Thruster_Main_Right", frame_from="CRUS_Starlifter_Thruster_VTOL_Side")
    sc.add_geometry(trimesh.creation.box((3, 10, 3)), node_name="Freelancer_Thruster_Main_Right_Nozzle",
                    geom_name="nozzle", parent_node_name="MISC_Freelancer_Thruster_Main_Right")
    sc.export(glb)

    assert models.fix_tagged_geometry(glb, None, {}, log=lambda *_: None) == 1
    back = trimesh.load(glb, force="scene")
    names = set(back.graph.nodes_geometry)
    assert "Freelancer_Thruster_Main_Right_Nozzle" not in names
    assert "CRUS_Starlifter_Vtol_Side_Geo" in names
    # Y-up "2 m up" is CryEngine +Z: on the mount, 2 m up
    T, _ = back.graph.get("CRUS_Starlifter_Vtol_Side_Geo")
    assert np.allclose(T[:3, 3], [3, -10, 1])
    assert np.allclose(back.extents, [10, 30, 4])  # nothing sticks out of the hull any more
    assert models.fix_tagged_geometry(glb, None, {}, log=lambda *_: None) == 0
