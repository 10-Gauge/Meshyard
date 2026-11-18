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
    assert m[3]["baseColorFactor"][:3] == [c * paints.GLASS_TINT for c in new["glass"]]  # tinted, dark pane
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


def test_bare_metal_panels_show_the_paints_spec_color():
    old = _pal([255, 255, 255], [0, 0, 0], [0, 0, 0])
    new = dict(old, spec={"primary": [1.0] * 3, "secondary": [0.13] * 3, "tertiary": [0.27, 0.16, 0.08]})
    metal = _sub("tertiary")
    metal["layer_manifest"] = [{"layer_snapshot": {"metallic": 1.0}}]
    paint = _sub("secondary")    # dull spec, not a metal layer: keeps its paint color
    doc = {"materials": [
        {"name": "gama_railen_ext_mtl_Metal_Secondary_056", "extras": {"source_name": "Metal_Secondary"},
         "pbrMetallicRoughness": {"baseColorFactor": [0, 0, 0, 1], "metallicFactor": 0.0}},
        {"name": "gama_railen_ext_mtl_Plastic_Tertiary_059", "extras": {"source_name": "Plastic_Tertiary"},
         "pbrMetallicRoughness": {"baseColorFactor": [0, 0, 0, 1], "metallicFactor": 0.0}}]}
    paints.repaint(doc, b"", old, new, ("gama_railen_ext", {"Metal_Secondary": metal, "Plastic_Tertiary": paint}, None))
    m = [x["pbrMetallicRoughness"] for x in doc["materials"]]
    assert m[0]["baseColorFactor"][:3] == [0.27, 0.16, 0.08] and m[0]["metallicFactor"] == 1.0   # bronze metal
    assert m[1]["baseColorFactor"][:3] == new["secondary"] and m[1]["metallicFactor"] == 0.0     # painted plastic


def test_metal_paint_needs_a_colored_or_much_brighter_spec():
    assert paints._metal_paint([0.02] * 3, [0.39, 0.003, 0.01])      # red metal
    assert paints._metal_paint([0.01] * 3, [0.6, 0.6, 0.6])           # chrome over black
    assert not paints._metal_paint([1.0] * 3, [1.0, 1.0, 1.0])        # pearl white paint
    assert not paints._metal_paint([0.2] * 3, [0.05] * 3)             # ordinary paint


def test_a_livery_recolors_panels_through_their_own_layer_tint():
    def panel(tint, palette="0"):
        sm = _sub(None)
        sm["layer_manifest"] = [{"authored_attributes": [{"name": "TintColor", "value": tint},
                                                         {"name": "PaletteTint", "value": palette}]}]
        return sm
    old = _pal([255, 255, 255], [0, 0, 0], [0, 0, 0])
    doc = {"materials": [{"name": "gama_railen_ext_mtl_Paint_Base_055", "extras": {"source_name": "Paint_Base"},
                          "pbrMetallicRoughness": {"baseColorFactor": [0.04, 0.04, 0.04, 1.0]}}]}
    paints.repaint(doc, b"", old, old, ("gama_railen_ext", {"Paint_Base": panel("0.04,0.04,0.04")},
                                       {"Paint_Base": panel("0.81,0.81,0.81")}))
    assert doc["materials"][0]["pbrMetallicRoughness"]["baseColorFactor"] == [0.81, 0.81, 0.81, 1.0]


def test_livery_slots_fall_back_to_position_for_renamed_submaterials():
    base = [("Glass", 1), ("--", 0), ("--", 0), ("Paint_Base", 2), ("Painted_Panel_Dark", 3)]
    liv = [("Glass", "g"), ("--", "x"), ("--", "y"), ("Paint_Base", "p"), ("Trim_Paint_Panel_Base", "t")]
    out = paints._match_livery(base, liv)
    assert out["Paint_Base"] == "p" and out["Painted_Panel_Dark"] == "t"   # slot 4, past the repeats


def test_livery_only_parts_are_found_and_grafted_under_their_node():
    import struct
    base = {"Decal_Emblem_Diff": {"activation_state": {"state": "inactive"}}, "Hull": {}}
    liv = {"Decal_Emblem_Diff": {"activation_state": {"state": "active"}}, "Hull": {}}
    assert paints._activated(base, liv) == ["Decal_Emblem_Diff"]
    doc = {"nodes": [{"name": "Nose"}], "meshes": [], "buffers": [{"byteLength": 0}],
           "materials": [{"name": "idris_frigate_exterior_wip_mtl_Decal_Emblem_Diff_059",
                          "extras": {"source_name": "Decal_Emblem_Diff"}}]}
    part = {"object": "Nose", "material": "Decal_Emblem_Diff", "positions": [[0, 0, 0], [1, 0, 0], [0, 1, 0]],
            "normals": [[0, 0, 1]] * 3, "uv0": [[0, 0], [1, 0], [0, 1]], "uv1": [[0, 0]] * 3, "triangles": [[0, 1, 2]]}
    bin_, n = paints.graft_faces(doc, b"", [part, dict(part, object="Missing")], "idris_frigate_exterior_wip_mtl_")
    assert n == 1 and doc["nodes"][0]["children"] == [1]
    prim = doc["meshes"][0]["primitives"][0]
    assert prim["material"] == 0 and set(prim["attributes"]) >= {"POSITION", "NORMAL", "TEXCOORD_0"}
    uv = doc["accessors"][prim["attributes"]["TEXCOORD_0"]]
    off = doc["bufferViews"][uv["bufferView"]]["byteOffset"]
    assert struct.unpack_from("<2f", bin_, off + 16) == (0.0, 0.0)   # v flipped: (0, 1) -> (0, 0)


def test_fleck_stencils_color_the_marked_areas_and_make_them_metal(tmp_path):
    import io

    import numpy as np
    from PIL import Image
    mask = np.zeros((4, 4, 4), np.uint8)
    mask[..., 3] = 255
    mask[:2, :, 0] = 255                  # top half marked
    Image.fromarray(mask, "RGBA").save(tmp_path / "m.png")
    sm = {"public_params": {"StencilTintOverride": 0, "StencilSpecularColor": "0.85,0.11,0.41",
                            "StencilOpacity": 1}}
    colors, opacity = paints._stencil_specs(sm)
    col, mr = paints.stencil_flecks(tmp_path / "m.png", [0.03, 0.03, 0.03, 1], colors, opacity)
    c = np.asarray(Image.open(io.BytesIO(col)))
    m = np.asarray(Image.open(io.BytesIO(mr)))
    assert c[0, 0, 0] > 200 and c[3, 0, 0] < 60          # pink flecks over dark paint
    assert m[0, 0, 2] == 255 and m[3, 0, 2] == 0         # metal only where marked


def _stencil_png(path, blue, green):
    import numpy as np
    from PIL import Image
    mask = np.zeros((4, 4, 4), np.uint8)
    mask[..., 3] = 255
    for r, c in blue:
        mask[r, c, 2] = 255
    for r, c in green:
        mask[r, c, 1] = 255
    Image.fromarray(mask, "RGBA").save(path)


def test_camo_generator_stencil_paints_dark_camo_with_lighter_stripes(tmp_path):
    import io

    import numpy as np
    from PIL import Image
    # blue marks the camo (top half), green its pattern inside the blue (top left corner)
    mask = np.zeros((32, 32, 4), np.uint8)
    mask[..., 3] = 255
    mask[:16, :, 2] = 255
    mask[:8, :8, 1] = 255
    Image.fromarray(mask, "RGBA").save(tmp_path / "s.png")
    cols = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]]          # tertiary black
    png = paints.stencil_palette(tmp_path / "s.png", [0.6, 0.5, 0.4], cols, 1.0)
    img = np.asarray(Image.open(io.BytesIO(png)))
    assert img.shape[:2] == (64, 64)                     # enlarged for crisp edges
    assert list(img[20, 40]) == [0, 0, 0]                # camo: the tertiary paint
    stripe = img[4, 4]
    assert 0 < stripe[0] < 120 and stripe[0] == stripe[1] == stripe[2]   # neutral grey stripe
    assert list(img[60, 4]) == [203, 188, 170]           # unmarked: the panel's own color, right way up


def test_other_tint_override_stencils_lay_each_channel_in_its_paint(tmp_path):
    import io

    import numpy as np
    from PIL import Image
    # green outside the blue: not a camo generator
    _stencil_png(tmp_path / "s.png", [(0, 1)], [(1, 0), (1, 1)])
    cols = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    img = np.asarray(Image.open(io.BytesIO(paints.stencil_palette(tmp_path / "s.png", [0.5, 0.5, 0.5], cols, 1.0))))
    assert list(img[0, 1]) == [0, 0, 255] and list(img[1, 0]) == [0, 255, 0]
    assert list(img[3, 0]) == [188, 188, 188]


def test_panel_tint_scales_its_palette_color():
    sm = {"layer_manifest": [{"authored_attributes": [{"name": "TintColor", "value": "0.5,0.5,0.5"},
                                                      {"name": "PaletteTint", "value": "1"}]}]}
    assert paints._palette_layer_tint(sm) == [0.5, 0.5, 0.5]
    assert paints._palette_layer_tint({}) == [1.0, 1.0, 1.0]


def test_parts_on_hardpoints_get_their_own_copy_of_hull_materials():
    doc = {"nodes": [{"name": "Ship", "children": [1, 2]}, {"name": "hull", "mesh": 0},
                     {"name": "hardpoint_nose_railgun", "children": [3]}, {"name": "Railgun", "mesh": 1}],
           "meshes": [{"primitives": [{"material": 0}]}, {"primitives": [{"material": 0}, {"material": 1}]}],
           "materials": [{"name": "Idris_mtl_Dark_Panel_07"}, {"name": "railgun_own"}]}
    copies = paints._split_mounted_materials(doc, "idris_mtl_")
    assert doc["meshes"][0]["primitives"][0]["material"] == 0          # the hull keeps its material
    moved = doc["meshes"][1]["primitives"][0]["material"]
    assert moved in copies and doc["materials"][moved]["name"].endswith("_mounted")
    assert doc["meshes"][1]["primitives"][1]["material"] == 1          # its own material stays
