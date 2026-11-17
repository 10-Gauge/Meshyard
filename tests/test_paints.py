from scsandbox import paints


def _node(tags, palette, sub=()):
    return {"_Type_": "SGeometryNodeParams", "Tags": tags,
            "Geometry": {"Palette": {"RootRecord": palette} if palette else {"RootRecord": None}},
            "SubGeometry": list(sub)}


def test_record_lists_the_default_and_each_paint():
    root = "file://./../../../libs/foundry/records/tintpalettes/brand/krig/"
    rec = {"_RecordValue_": {"Components": [{"_Type_": "SGeometryResourceParams", "Geometry": _node("", root + "stingray_default.json", [
        _node("Paint_Stingray_Black_Black_Orange", root + "stingray_black_black_orange.json"),
        _node("ui_preview", None),
    ])}]}}
    default, found = paints._palettes_in(rec)
    assert default == "libs/foundry/records/tintpalettes/brand/krig/stingray_default.json"
    assert found == {"Paint_Stingray_Black_Black_Orange": "libs/foundry/records/tintpalettes/brand/krig/stingray_black_black_orange.json"}


def _pal(a, b, c, spec_c=(0.04, 0.04, 0.04)):
    lin = lambda col: [paints._linear(v) for v in col]  # noqa: E731
    return {"primary": lin(a), "secondary": lin(b), "tertiary": lin(c), "glass": [0.8, 0.5, 0.2],
            "spec": {"primary": [0.04] * 3, "secondary": [0.04] * 3, "tertiary": list(spec_c)}}


def _sub(channel=None, stencil=None, **params):
    sm = {"palette_routing": {"material_channel": {"name": channel}} if channel else None,
          "texture_slots": [], "public_params": params}
    if stencil:
        sm["texture_slots"].append({"role": "stencil", "slot": "TexSlot7", "export_path": stencil})
    return sm


def test_repaint_swaps_only_the_ships_own_paint_without_livery_data():
    old, new = _pal([124, 134, 112], [70, 74, 64], [161, 135, 83]), _pal([23, 23, 23], [41, 41, 41], [255, 0, 0])
    doc = {"materials": [
        {"name": "primary", "extras": {"palette_tint": 1}, "pbrMetallicRoughness": {"baseColorFactor": [*old["primary"], 1.0]}},
        {"name": "tertiary", "extras": {"palette_tint": 3}, "pbrMetallicRoughness": {"baseColorFactor": [*old["tertiary"], 0.5]}},
        # a gun's own paint (its palette, not the ship's) keeps its color
        {"name": "gun", "extras": {"palette_tint": 1}, "pbrMetallicRoughness": {"baseColorFactor": [0.02, 0.02, 0.02, 1.0]}},
        {"name": "glass", "pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 0.2]}},
    ]}
    _, n = paints.repaint(doc, b"", old, new)
    assert n == 2
    m = doc["materials"]
    assert m[0]["pbrMetallicRoughness"]["baseColorFactor"][:3] == new["primary"]
    assert m[1]["pbrMetallicRoughness"]["baseColorFactor"] == [*new["tertiary"], 0.5]  # alpha kept
    assert m[2]["pbrMetallicRoughness"]["baseColorFactor"] == [0.02, 0.02, 0.02, 1.0]


def test_a_livery_moves_materials_to_other_paint_entries_and_lays_its_stencil(tmp_path):
    import numpy as np
    from PIL import Image
    mask = np.zeros((8, 8, 4), np.uint8)
    mask[..., 0] = 255
    mask[:4, :, 0] = 0     # top half marked in the stencil's first channel
    mask[..., 3] = 255
    Image.fromarray(mask, "RGBA").save(tmp_path / "camo.png")
    old = _pal([124, 134, 112], [70, 74, 64], [161, 135, 83])
    new = _pal([57, 57, 57], [40, 40, 40], [9, 4, 4], spec_c=(0.39, 0.003, 0.01))
    base = {"primary_hardsurface": _sub("primary"), "tertiary_hardsurface": _sub("tertiary"),
            "metal_steel": _sub(None), "glass_ext": _sub("glass")}
    liv = {"primary_hardsurface": _sub("primary", "camo.png", StencilTintOverride=2, StencilTiling=3),
           "tertiary_hardsurface": _sub("secondary"), "metal_steel": _sub("tertiary"),
           "glass_ext": _sub("glass")}
    mat = lambda sub, k=None: {"name": f"AEGS_Gladius_mtl_{sub}_01", "extras": {"source_name": sub, **({"palette_tint": k} if k else {})},  # noqa: E731
                               "pbrMetallicRoughness": {"baseColorFactor": [0.1, 0.1, 0.1, 1.0], "metallicFactor": 0.0}}
    doc = {"materials": [mat("primary_hardsurface", 1), mat("tertiary_hardsurface", 3), mat("metal_steel"),
                         mat("glass_ext"), {"name": "AEGS_Gladius_LandingGear_mtl_x", "extras": {"palette_tint": 1},
                                            "pbrMetallicRoughness": {"baseColorFactor": [*old["primary"], 1.0]}}]}
    bin_, n = paints.repaint(doc, b"", old, new, ("AEGS_Gladius", base, liv), tmp_path)
    m = [x["pbrMetallicRoughness"] for x in doc["materials"]]
    assert n == 5
    assert m[1]["baseColorFactor"][:3] == new["secondary"]           # tertiary panels now secondary
    assert m[2]["baseColorFactor"][:3] == new["spec"]["tertiary"]    # metallic red finish
    assert m[2]["metallicFactor"] == 1.0
    assert m[3]["baseColorFactor"][:3] == new["glass"]
    assert m[4]["baseColorFactor"][:3] == new["primary"]             # gear keeps the palette swap
    tex = m[0]["baseColorTexture"]
    assert tex["extensions"]["KHR_texture_transform"]["scale"] == [3.0, 3.0]
    assert "KHR_texture_transform" in doc["extensionsUsed"]
    view = doc["bufferViews"][doc["images"][doc["textures"][tex["index"]]["source"]]["bufferView"]]
    assert bin_[view["byteOffset"]:view["byteOffset"] + 4] == b"\x89PNG"


def test_stencil_tone_darkens_only_the_marked_areas(tmp_path):
    import io

    import numpy as np
    from PIL import Image
    mask = np.full((4, 4, 4), 255, np.uint8)
    mask[:2, :, 0] = 0
    Image.fromarray(mask, "RGBA").save(tmp_path / "m.png")
    img = np.asarray(Image.open(io.BytesIO(paints.stencil_tone(tmp_path / "m.png", 1))))
    assert img[0, 0, 0] < img[3, 0, 0] == 255
