"""Install the Blender add-ons and open the sandbox scene (used by the CLI and the app)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import tools

ADDON_SRC = Path(__file__).parent / "blender" / "sc_sandbox_addon.py"
_INSTALL_EXPR = (
    "import bpy, shutil, os, sys; d = bpy.utils.user_resource('SCRIPTS', path='addons', create=True); "
    "shutil.copy2(r'{src}', os.path.join(d, 'sc_sandbox_addon.py')); "
    "sb = r'{sb}'\n"
    "if sb:\n"
    "    t = os.path.join(d, 'starbreaker_addon'); shutil.rmtree(t, ignore_errors=True); shutil.copytree(sb, t)\n"
    # A freshly created add-ons folder is not on sys.path until Blender restarts.
    "if d not in sys.path: sys.path.append(d)\n"
    "bpy.ops.preferences.addon_refresh()\n"
    "if sb: bpy.ops.preferences.addon_enable(module='starbreaker_addon')\n"
    "bpy.ops.preferences.addon_enable(module='sc_sandbox_addon'); bpy.ops.wm.save_userpref()\n"
    "print('Add-ons installed to', d)"
)
_OPEN_EXPR = (
    "import bpy\n"
    "def _go():\n"
    "    try: bpy.ops.sc_sandbox.build_scene()\n"
    "    except Exception as e: print('SC Sandbox:', e)\n"
    "bpy.app.timers.register(_go, first_interval=1.0)"
)


def install_addons(settings: dict, log=print) -> None:
    exe = tools.find("blender", settings)
    sb = tools.starbreaker_addon_dir()
    if sb is None:
        try:
            log(tools.update_starbreaker_addon())
            sb = tools.starbreaker_addon_dir()
        except Exception as e:
            log(f"StarBreaker add-on not installed ({e}); animated spawns will be unavailable")
    proc = subprocess.run([str(exe), "--background", "--python-expr",
                           _INSTALL_EXPR.format(src=ADDON_SRC, sb=sb or "")],
                          capture_output=True, text=True, errors="replace", creationflags=tools.NO_WINDOW,
                          stdin=subprocess.DEVNULL, env=tools.child_env())
    if proc.returncode != 0:
        raise tools.ToolError(f"Blender add-on install failed:\n{(proc.stderr or proc.stdout)[-1500:]}")
    log("Blender add-ons installed")


def open_sandbox(settings: dict) -> None:
    exe = tools.find("blender", settings)
    subprocess.Popen([str(exe), "--python-expr", _OPEN_EXPR], env=tools.child_env())
