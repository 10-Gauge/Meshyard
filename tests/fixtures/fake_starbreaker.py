#!/usr/bin/env python3
"""Stand-in for StarBreaker used by the tests. Mimics the CLI shapes sc-sandbox calls."""
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
p4k = os.environ.get("SC_DATA_P4K", "")
assert p4k.endswith("Data.p4k"), "SC_DATA_P4K not set"
channel = Path(p4k).parent.name

def opt(name):
    return args[args.index(name) + 1]

SHIPS = {"AEGS_Gladius": "@vehicle_NameAEGS_Gladius", "ANVL_Arrow": "@vehicle_NameANVL_Arrow"}
if channel == "PTU":
    SHIPS["RSI_Zeus_CL"] = "@vehicle_NameRSI_Zeus_CL"

if args[:2] == ["dcb", "extract"]:
    out, flt = Path(opt("-o")), opt("--filter")
    base = out / "libs/foundry/records/entities"
    if "spaceships" in flt:
        for rec, loc in SHIPS.items():
            f = base / "spaceships" / f"{rec.lower()}.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"_RecordName_": f"EntityClassDefinition.{rec}", "Components": [
                {"VehicleComponentParams": {"vehicleName": loc, "vehicleDescription": "@desc_" + rec}},
                {"SAttachableComponentParams": {"AttachDef": {"Localization": {"Name": loc, "Description": "@desc_" + rec}}}},
                {"SGeometryResourceParams": {"Geometry": {"Geometry": {"Geometry": {"path": f"Objects/Spaceships/Ships/{rec}/{rec}.cga"}}}}}]}))
    if "scitem" in flt:
        f = base / "scitem/ships/weapons/klwe_laserrepeater_s1.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"_RecordName_": "EntityClassDefinition.KLWE_LaserRepeater_S1",
                                 "Components": [{"SAttachableComponentParams": {"AttachDef": {"Localization": {"Name": "@item_NameKLWE_LaserRepeater_S1"}}}},
                                                {"SGeometryResourceParams": {"Geometry": {"Geometry": {"Geometry": {"path": "Objects/Weapons/klwe_s1.cgf"}}}}}]}))
elif args[:2] == ["p4k", "extract"]:
    out = Path(opt("-o"))
    f = out / "Data/Localization/english/global.ini"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("﻿vehicle_NameAEGS_Gladius=Aegis Gladius\nvehicle_NameANVL_Arrow,P=Anvil Arrow\n"
                 "vehicle_NameRSI_Zeus_CL=RSI Zeus Mk II CL\ndesc_AEGS_Gladius=A light fighter.\\nFast.\n"
                 "item_NameKLWE_LaserRepeater_S1=Klaus & Werner Laser Repeater\n", encoding="utf-8")
elif args[:2] == ["p4k", "list"]:
    print("Data/Objects/Spaceships/Ships/AEGS_Gladius/AEGS_Gladius.cga")
    print("Data/Objects/Props/crate_small.cgf")
elif args[:2] == ["entity", "export"] and "--kind" in args and opt("--kind") == "decomposed":
    name, out = args[2], Path(args[3])
    pkg = out / "Packages" / f"{name}_LOD1_TEX2"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "scene.json").write_text(json.dumps({"root": name, "animations": ["landing_gear_extend"]}))
elif args[:2] == ["entity", "export"]:
    import trimesh
    name, out = args[2], args[3]
    if name not in SHIPS:
        print(f"entity {name} not found", file=sys.stderr); sys.exit(3)
    scene = trimesh.Scene()
    scene.add_geometry(trimesh.creation.capsule(height=16, radius=2), node_name="hull")
    w = trimesh.creation.box([10, 0.2, 4]); scene.add_geometry(w, node_name="wings")
    scene.add_geometry(trimesh.creation.box([40, 40, 40]), node_name="hull_collision")
    scene.export(out)
else:
    print("unknown args", args, file=sys.stderr); sys.exit(1)
