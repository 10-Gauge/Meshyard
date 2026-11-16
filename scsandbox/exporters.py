"""Export an asset to common 3D file types.

trimesh writes GLB, glTF, OBJ (+MTL and textures), DAE, PLY, STL and 3MF.
FBX, USDZ and .blend go through Blender (headless) when it is installed.

Game units are meters and glTF is Y-up. GLB/glTF/OBJ/DAE/FBX/USDZ/.blend keep
meters and the game's orientation (what engines and DCC tools expect). STL,
3MF and PLY are converted to millimeters and Z-up, which is what slicers and
CAD tools assume.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import trimesh

from . import tools

TRIMESH_FORMATS = ("glb", "gltf", "obj", "dae", "ply", "stl", "3mf")
BLENDER_FORMATS = ("fbx", "usdz", "blend")
FORMATS = TRIMESH_FORMATS + BLENDER_FORMATS
LABELS = {
    "glb": "GLB (glTF binary, textures embedded)", "gltf": "glTF (+ .bin and textures)",
    "obj": "OBJ (+ MTL and textures)", "fbx": "FBX (via Blender)", "dae": "Collada DAE",
    "usdz": "USDZ (via Blender)", "blend": "Blender .blend (via Blender)",
    "ply": "PLY (mm, Z-up)", "stl": "STL (mm, Z-up)", "3mf": "3MF (mm, Z-up)",
}
PRINT_STYLE = ("ply", "stl", "3mf")  # millimeters, Z-up

_BLENDER_SCRIPT = r'''
import bpy, sys
src, dst, fmt = sys.argv[sys.argv.index("--") + 1:]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=src)
if fmt == "fbx":
    bpy.ops.export_scene.fbx(filepath=dst, path_mode="COPY", embed_textures=True,
                             apply_scale_options="FBX_SCALE_ALL", axis_forward="-Z", axis_up="Y")
elif fmt == "usdz":
    bpy.ops.wm.usd_export(filepath=dst)
elif fmt == "blend":
    bpy.ops.file.pack_all()
    bpy.ops.wm.save_as_mainfile(filepath=dst, compress=True)
print("SC_EXPORT_OK", dst)
'''


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "model"


def export_asset(glb: Path, out_dir: Path, name: str, fmt: str, settings: dict, log=print) -> dict:
    """Write `name.fmt` (plus any side files) into out_dir. Returns {"files", "download"}:
    the download is the file itself, or a .zip when the format needs side files."""
    fmt = fmt.lower()
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    stem = _safe(name)
    folder = Path(out_dir) / f"{stem}_{fmt}"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    dst = folder / f"{stem}.{fmt}"
    if fmt == "glb":
        shutil.copy2(glb, dst)
    elif fmt in BLENDER_FORMATS:
        _blender_export(Path(glb), dst, fmt, settings, log)
    else:
        scene = trimesh.load(str(glb), force="scene")
        if fmt in PRINT_STYLE:
            scene = scene.copy()
            scene.apply_transform(trimesh.transformations.rotation_matrix(math.pi / 2, [1, 0, 0]))
            scene.apply_scale(1000.0)
        if fmt == "dae":
            dst.write_bytes(trimesh.exchange.dae.export_collada(scene.dump()))
        else:
            scene.export(str(dst), file_type=fmt)
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    if not files:
        raise RuntimeError(f"Nothing was written for {fmt}")
    if len(files) == 1:
        download = files[0]
    else:
        download = folder.with_suffix(".zip")
        with zipfile.ZipFile(download, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.write(f, f.relative_to(folder).as_posix())
    log(f"  wrote {dst.name}" + (f" (+{len(files) - 1} side files, zipped)" if len(files) > 1 else ""))
    return {"files": [str(f) for f in files], "main": str(dst), "download": str(download)}


def _blender_export(glb: Path, dst: Path, fmt: str, settings: dict, log) -> None:
    try:
        exe = tools.find("blender", settings)
    except tools.ToolError:
        raise tools.ToolError(f"{fmt.upper()} export needs Blender. Set it up in Setup > Blender, "
                              "or choose GLB, glTF, OBJ or DAE instead.") from None
    script = dst.parent / "_export.py"
    script.write_text(_BLENDER_SCRIPT, encoding="utf-8")
    log(f"  converting with Blender ({exe.name})")
    try:
        proc = subprocess.run([str(exe), "--background", "--factory-startup", "--python", str(script),
                               "--", str(glb), str(dst), fmt],
                              capture_output=True, text=True, errors="replace", timeout=3600,
                              creationflags=tools.NO_WINDOW, stdin=subprocess.DEVNULL, env=tools.child_env())
    finally:
        script.unlink(missing_ok=True)
    if "SC_EXPORT_OK" not in proc.stdout or not dst.exists():
        tail = (proc.stderr or proc.stdout).strip()[-1500:]
        raise tools.ToolError(f"Blender could not export {fmt.upper()}:\n{tail}")
