import zipfile

import numpy as np
import pytest
import trimesh
from PIL import Image

from scsandbox import exporters, tools


@pytest.fixture
def textured_glb(tmp_path):
    box = trimesh.creation.box([2, 1, 4])
    uv = np.random.default_rng(0).random((len(box.vertices), 2))
    img = Image.new("RGB", (8, 8), (200, 50, 50))
    box.visual = trimesh.visual.TextureVisuals(uv=uv, material=trimesh.visual.material.PBRMaterial(baseColorTexture=img))
    p = tmp_path / "ship.glb"
    trimesh.Scene({"hull": box}).export(str(p))
    return p


@pytest.mark.parametrize("fmt", ["glb", "gltf", "obj", "dae", "ply", "stl", "3mf"])
def test_trimesh_formats(textured_glb, tmp_path, fmt):
    rep = exporters.export_asset(textured_glb, tmp_path / "out", "Aegis Gladius", fmt, {}, log=lambda *_: None)
    assert rep["main"].endswith(f"Aegis_Gladius.{fmt}")
    m = trimesh.load(rep["main"], force="mesh")
    assert len(m.faces) == 12
    if fmt in ("stl", "3mf", "ply"):
        assert max(m.extents) == pytest.approx(4000, rel=1e-3)   # meters -> mm
        assert m.extents[1] == pytest.approx(4000, rel=1e-3)     # Y-up -> Z-up: length lies along Y
    if fmt in ("obj", "gltf"):
        names = zipfile.ZipFile(rep["download"]).namelist()
        assert len(names) >= 2                                    # side files (mtl/bin/textures) are zipped


def test_blender_formats_need_blender(textured_glb, tmp_path, monkeypatch):
    def missing(tool, settings):
        raise tools.ToolError("not found")
    monkeypatch.setattr(tools, "find", missing)
    with pytest.raises(tools.ToolError, match="needs Blender"):
        exporters.export_asset(textured_glb, tmp_path / "out", "x", "fbx", {}, log=lambda *_: None)
