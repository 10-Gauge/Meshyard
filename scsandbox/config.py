"""Settings and on-disk layout.

Everything the toolkit writes lives under one home folder:

    %LOCALAPPDATA%\\Meshyard        (Windows)
    ~/.local/share/meshyard          (elsewhere)

The folder used to be called sc-sandbox; home() moves it over the first time.

Override it with the MESHYARD_HOME environment variable.

    settings.json                    user settings (tool paths, install roots, defaults)
    tools/<tool>/                    downloaded helper tools (StarBreaker, cgf-converter)
    builds/<CHANNEL>/<build_id>/     per-build cache: index.sqlite, records/, models/
    exports/                         printable files you exported
    sandbox/queue.json               assets waiting to be spawned in Blender
"""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path

DEFAULTS: dict = {
    # Extra game folders (holding the data file, or one build folder per channel).
    # Auto-detection also checks launcher logs and the usual game folders on every drive.
    "install_roots": [],
    # Channel used when a command does not name one.
    "default_channel": "LIVE",
    # Paths to external tools. Empty means "look in tools/ then on PATH".
    "tools": {
        "starbreaker": "",
        "cgf_converter": "",
        "blender": "",
    },
    # Command templates. Edit these if a tool release changes its flags.
    # Placeholders: {exe} {p4k} {out} {filter} {name} {input} {objectdir} {lod}
    "commands": {
        "list_files": ["{exe}", "p4k", "list", "--filter", "{filter}"],
        "extract_files": ["{exe}", "p4k", "extract", "-o", "{out}", "--filter", "{filter}"],
        "extract_records": ["{exe}", "dcb", "extract", "--format", "json", "-o", "{out}", "--filter", "{filter}"],
        "export_entity": ["{exe}", "entity", "export", "{name}", "{out}", "--lod", "{lod}"],
        # viewer model: eighth-size textures and no light fixtures (the viewer drops
        # lights anyway); "Full detail" has everything
        "export_entity_preview": ["{exe}", "entity", "export", "{name}", "{out}", "--lod", "{lod}", "--mip", "3", "--no-lights"],
        # the same for capital ships, whose model at that detail is too heavy for the viewer
        "export_entity_preview_light": ["{exe}", "entity", "export", "{name}", "{out}", "--lod", "{lod}", "--mip", "4", "--no-lights"],
        # Decomposed package (scene.json + assets) with animations, for the
        # StarBreaker Blender add-on: landing gear, doors, ramps, canopies.
        "export_entity_animated": ["{exe}", "entity", "export", "{name}", "{out}", "--kind", "decomposed", "--lod", "{lod}"],
        # rig + animation clips only (landing gear up): no textures, interior or lights
        # each paint's materials, colors and livery textures (the Paint list)
        "export_entity_paints": ["{exe}", "entity", "export", "{name}", "{out}", "--kind", "decomposed", "--lod", "1", "--materials", "textures", "--no-interior", "--no-lights", "--mip", "3"],
        "export_entity_rig": ["{exe}", "entity", "export", "{name}", "{out}", "--kind", "decomposed", "--lod", "1", "--materials", "none", "--no-interior", "--no-lights", "--mip", "4"],
        "export_entity_print": ["{exe}", "entity", "export", "{name}", "{out}", "--lod", "0", "--materials", "colors", "--no-interior"],
        # one worn character mesh (.skinm) as GLB in the body's bind pose
        "export_skin": ["{exe}", "skin", "export", "{name}", "{out}"],
        # a texture from Data.p4k as PNG (manufacturer logos for the name plate)
        "decode_dds": ["{exe}", "dds", "decode", "{input}", "{out}"],
        # a livery stencil at full size (the paint package's copy is too small)
        "decode_stencil": ["{exe}", "dds", "decode", "{input}", "{out}", "--mip", "0"],
        "convert_geometry": ["{exe}", "{input}", "-glb", "-objectdir", "{objectdir}", "-outdir", "{out}"],
        # the body skin with the game's stance animations (.chrparams + .dba next to it)
        "convert_animated": ["{exe}", "{input}", "-glb", "-anim", "-notex", "-objectdir", "{objectdir}", "-outdir", "{out}"],
    },
    # Record folders (DataCore paths) that are indexed as spawnable assets.
    "record_filters": [
        "**/entities/spaceships/**",
        "**/entities/groundvehicles/**",
        "**/entities/scitem/**",
        "**/entities/props/**",
        "**/entities/characters/**",
    ],
    # Index new LIVE/PTU builds automatically when the browser starts.
    "auto_update": True,
    # Keep this many builds per channel in the cache; older ones are pruned.
    "keep_builds": 2,
    "server": {"host": "127.0.0.1", "port": 8765},
    "print": {
        "size_mm": 150.0,          # longest dimension of the printed model
        "mode": "solid",           # solid | parts
        "voxel_mm": 0.2,           # remesh resolution for solid mode
        "max_voxels": 250_000_000, # safety cap; pitch grows to stay under it
        "max_faces": 4_000_000,    # decimate above this (lower loses panel edges and ridges)
        "format": "3mf",           # 3mf | stl | obj | ply
        "nozzle_mm": 0.4,          # printer nozzle width
        "min_feature_nozzles": 3,  # thicken features thinner than this many nozzle widths
        "thicken": True,
        "hollow_mm": 0,            # wall thickness when hollowing (resin); 0 = solid
        "tilt_deg": 0,             # display angle
        "stand": "none",           # none | peg | attached
        "stand_height_mm": 30,
        "exclude_nodes": [
            r"(?i)lod[1-9]", r"(?i)proxy", r"(?i)collision", r"(?i)\$physics",
            r"(?i)_damage", r"(?i)damaged", r"(?i)shield", r"(?i)thruster_flame",
            r"(?i)helper", r"(?i)decal", r"(?i)glass_inner",
        ],
    },
}


# The folder's name before the rename. Its contents move to the new folder the
# first time the new name is used.
_OLD_NAME = "sc-sandbox"
_migrated: Path | bool = False


def _migrate(new: Path) -> Path:
    """Move the old data folder to `new` once, fixing paths saved in its settings.
    If the move can't happen yet (a file in it is open), keep using the old folder."""
    old = new.parent / _OLD_NAME
    if new.exists() or not old.is_dir():
        return new
    try:
        old.rename(new)
    except OSError:
        return old
    for name in ("settings.json", "desktop.json"):
        f = new / name
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for a, b in ((str(old), str(new)), (json.dumps(str(old))[1:-1], json.dumps(str(new))[1:-1])):
            text = text.replace(a, b)
        try:
            f.write_text(text, encoding="utf-8")
        except OSError:
            pass
    return new


def home() -> Path:
    global _migrated
    env = os.environ.get("MESHYARD_HOME") or os.environ.get("SCSANDBOX_HOME")
    if env:
        p = Path(env)
    else:
        if sys.platform == "win32":
            p = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Meshyard"
        else:
            p = Path.home() / ".local" / "share" / "meshyard"
        if _migrated is False:
            _migrated = _migrate(p)  # once per process: the folder this run uses
        p = _migrated
    p.mkdir(parents=True, exist_ok=True)
    return p


def _merge(base: dict, over: dict) -> dict:
    out = deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def settings_path() -> Path:
    return home() / "settings.json"


# Command templates from older toolkit versions that newer StarBreaker releases
# reject. Early versions saved every setting, so these can linger in settings.json
# and override the fixed defaults; drop them on load.
_STALE_COMMANDS = {
    "export_entity_preview": [
        ["{exe}", "entity", "export", "{name}", "{out}", "--lod", "{lod}", "--mip", "4", "--no-lights"],
    ],
    "export_entity_print": [
        ["{exe}", "entity", "export", "{name}", "{out}", "--lod", "0", "--no-textures"],
        # material names are needed to drop decal/parallax layers when printing
        ["{exe}", "entity", "export", "{name}", "{out}", "--lod", "0", "--materials", "none", "--no-interior"],
    ],
}


# Print settings older versions saved in full; dropped so the current defaults apply.
_STALE_PRINT = {"max_faces": (1_500_000,)}


def _drop_stale(saved: dict) -> dict:
    pr = saved.get("print")
    if isinstance(pr, dict):
        for key, old in _STALE_PRINT.items():
            if pr.get(key) in old:
                del pr[key]
    cmds = saved.get("commands")
    if isinstance(cmds, dict):
        for key, old in _STALE_COMMANDS.items():
            if cmds.get(key) in old:
                del cmds[key]
    return saved


def load() -> dict:
    p = settings_path()
    if p.exists():
        try:
            return _merge(DEFAULTS, _drop_stale(json.loads(p.read_text(encoding="utf-8"))))
        except json.JSONDecodeError as e:
            raise SystemExit(f"settings.json is not valid JSON ({e}). Fix or delete {p}")
    return deepcopy(DEFAULTS)


def _diff(cur: dict, base: dict) -> dict:
    out = {}
    for k, v in cur.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            d = _diff(v, base[k])
            if d:
                out[k] = d
        elif k not in base or base[k] != v:
            out[k] = v
    return out


def save(settings: dict) -> None:
    # Only what differs from the defaults is written, so fixes to default
    # commands in a toolkit update still reach people who changed other settings.
    settings_path().write_text(json.dumps(_diff(settings, DEFAULTS), indent=2), encoding="utf-8")


def set_value(settings: dict, dotted: str, value) -> dict:
    node = settings
    keys = dotted.split(".")
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value
    return settings
