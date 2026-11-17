from types import SimpleNamespace

from scsandbox import printlog, printprep


def test_log_records_each_export_and_filters_by_item(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHYARD_HOME", str(tmp_path))
    opts = printprep.PrintOptions(size_mm=300, stand="peg", stand_shape="rect", stand_text="Aegis Eclipse",
                                  stand_text2="Truth Nuke   1:70")
    printlog.add("Aegis Eclipse", "AEGS_Eclipse", opts, {"scale": "1:70", "size_mm": [300, 167, 39],
                                                         "voxel_mm": 0.2, "files": [str(tmp_path / "e.3mf")]})
    printlog.add("Anvil Paladin", "ANVL_Paladin", printprep.PrintOptions(size_mm=150), {"scale": "1:180"})
    rows = printlog.entries()
    assert [r["name"] for r in rows] == ["Anvil Paladin", "Aegis Eclipse"]          # newest first
    e = printlog.entries("AEGS_Eclipse")
    assert len(e) == 1 and e[0]["scale"] == "1:70" and e[0]["stand"] == "separate"
    assert e[0]["plate"] == "Aegis Eclipse / Truth Nuke   1:70" and e[0]["size_mm"] == "300 x 167 x 39"
    assert printlog.path().read_text(encoding="utf-8").splitlines()[0].startswith("date,name,key,scale")


def test_scale_goes_on_the_plate(tmp_path, monkeypatch):
    import trimesh
    glb = tmp_path / "box.glb"
    trimesh.Scene({"hull": trimesh.creation.box((2, 1, 10))}).export(glb)
    seen = {}
    real = printprep._plan_rect

    def spy(s, w, d, text, text2="", logo="", *rest):
        seen["text2"] = text2
        return real(s, w, d, text, text2, logo, *rest)

    monkeypatch.setattr(printprep, "_plan_rect", spy)
    for line2, want in (("", "1:100"), ("Red Five", "Red Five   1:100")):
        opts = printprep.PrintOptions(size_mm=100, voxel_mm=0.5, stand="peg", stand_shape="rect",
                                      stand_text="Box", stand_text2=line2, stand_scale=True, format="stl")
        rep = printprep.prepare(glb, tmp_path / f"out{len(line2)}", opts, log=lambda *_: None)
        assert rep["scale"] == "1:100" and seen["text2"] == want
