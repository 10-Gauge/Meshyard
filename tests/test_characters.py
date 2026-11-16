import json

import pytest

from scsandbox import characters


def test_worn_skins_pick_the_body_and_skip_props_visors_and_crates():
    asset = {"geometry": json.dumps([
        "Objects/Characters/Human/female_v2/armor/ccc/f_ccc_medium_helmet_01.skin",
        "Objects/Characters/Human/male_v7/armor/ccc/m_ccc_medium_helmet_01.skin",
        "Objects/Characters/Human/male_v7/armor/ccc/m_ccc_medium_helmet_01_prop.cdf",
        "Objects/Characters/Human/male_v7/armor/shared/visor/m_fp_visor_16x9.skin",
        "Objects/buildingsets/human/lowtech/prop/storage/crate/crate_armor_core_1_005x005x005.cgf",
        "Objects/Characters/Human/heads/shared/facialhair/sc/male/m_facial_hair_001.skin",
    ])}
    assert characters.worn_skins(asset, "male") == [
        "Objects/Characters/Human/male_v7/armor/ccc/m_ccc_medium_helmet_01.skin",
        "Objects/Characters/Human/heads/shared/facialhair/sc/male/m_facial_hair_001.skin",
    ]
    assert characters.worn_skins(asset, "female") == [
        "Objects/Characters/Human/female_v2/armor/ccc/f_ccc_medium_helmet_01.skin",
    ]


def test_figure_names_are_validated(tmp_path):
    with pytest.raises(LookupError):
        characters.figure_path(None, "../../settings")
    with pytest.raises(LookupError):
        characters.figure_path(None, "figure_xyz")
