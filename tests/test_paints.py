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


def test_recolor_swaps_only_the_ships_own_paint():
    old = {"tint": [[124, 134, 112], [70, 74, 64], [161, 135, 83]]}
    new = {"tint": [[23, 23, 23], [41, 41, 41], [255, 0, 0]]}
    lin = lambda c: [paints._linear(v) for v in c]  # noqa: E731
    doc = {"materials": [
        {"name": "primary", "extras": {"palette_tint": 1}, "pbrMetallicRoughness": {"baseColorFactor": [*lin(old["tint"][0]), 1.0]}},
        {"name": "tertiary", "extras": {"palette_tint": 3}, "pbrMetallicRoughness": {"baseColorFactor": [*lin(old["tint"][2]), 0.5]}},
        # a gun's own paint (its palette, not the ship's) keeps its color
        {"name": "gun", "extras": {"palette_tint": 1}, "pbrMetallicRoughness": {"baseColorFactor": [0.02, 0.02, 0.02, 1.0]}},
        {"name": "glass", "pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 0.2]}},
    ]}
    assert paints.recolor(doc, old, new) == 2
    m = doc["materials"]
    assert m[0]["pbrMetallicRoughness"]["baseColorFactor"][:3] == lin(new["tint"][0])
    assert m[1]["pbrMetallicRoughness"]["baseColorFactor"] == [*lin(new["tint"][2]), 0.5]  # alpha kept
    assert m[2]["pbrMetallicRoughness"]["baseColorFactor"] == [0.02, 0.02, 0.02, 1.0]
