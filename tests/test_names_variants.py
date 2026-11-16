import json
import sqlite3

import numpy as np

from scsandbox import catalog, characters, rig


def _record(tmp_path, record, body):
    root = tmp_path / "records"
    p = root / "libs/foundry/records/entities/scitem/weapons/fps_weapons" / f"{record}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"_RecordName_": f"EntityClassDefinition.{record}", "_RecordValue_": body}))
    return p, root


def test_interaction_prompts_are_never_the_name_and_variants_borrow_the_base_name(tmp_path):
    loc = {"item_namebehr_rifle_ballistic_01": "P4-AR Rifle", "interaction_carry_lowered": "Carry Lowered",
           "ui_interactor_place": "Place", "loc_placeholder": "<= PLACEHOLDER =>"}
    body = {"Components": [
        {"AttachDef": {"Localization": {"Name": "@LOC_PLACEHOLDER"}}},
        {"Interactable": {"SharedInteractions": [{"DisplayName": "@interaction_carry_lowered"},
                                                 {"DisplayName": "@ui_interactor_place"}]}}]}
    p, root = _record(tmp_path, "behr_rifle_ballistic_01_store01", body)
    assert catalog.parse_record(p, root, loc)["name"] == "P4-AR Rifle (store01)"
    p, root = _record(tmp_path, "zzzz_crate_01", body)
    assert catalog.parse_record(p, root, loc)["name"] == catalog.humanize("zzzz_crate_01")  # readable record name


def test_variants_are_posed_from_their_base_gun(tmp_path):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(catalog.SCHEMA)
    for rec in ("behr_rifle_ballistic_01", "behr_rifle_ballistic_01_store01", "behr_rifle_ballistic_02_tan01",
                "behr_rifle_ballistic_02_black01_prop"):
        con.execute("INSERT INTO assets (kind, record, name, category, subcategory) VALUES "
                    "('entity', ?, ?, 'scitem', 'weapons/fps_weapons')", (rec, rec))
    assert characters.base_weapon(con, "behr_rifle_ballistic_01_store01")["record"] == "behr_rifle_ballistic_01"
    # no record of its own: the family's shortest-named one stands in
    assert characters.base_weapon(con, "behr_rifle_ballistic_02_black01_prop")["record"] == "behr_rifle_ballistic_02_tan01"
    assert characters.base_weapon(con, "not_a_gun") is None


def test_adopt_copies_the_base_grip_and_keeps_the_variant_mesh():
    base, var = rig.Weapon.__new__(rig.Weapon), rig.Weapon.__new__(rig.Weapon)
    for w, off in ((base, 0.0), (var, 0.3)):
        w.kind = "stocked" if w is base else "pistol"
        for k in ("forward", "up", "right", "rgrip", "lwrist", "lpalm", "ads", "pad", "butt"):
            setattr(w, k, np.array([off, 1.0, 0.0]))
        w.mesh = "mesh-" + ("base" if w is base else "variant")
    var.adopt(base)
    assert var.kind == "stocked" and np.allclose(var.lwrist, base.lwrist) and var.mesh == "mesh-variant"
    base.lwrist[0] = 9.0
    assert var.lwrist[0] == 0.0                                   # a copy, not shared
