"""Wrappers around the community tools that read game files.

StarBreaker (github.com/diogotr7/StarBreaker) does the heavy lifting: it reads
Data.p4k and the DataCore (Game2.dcb) and exports whole ships/vehicles as GLB
with their loadout assembled. cgf-converter (github.com/Markemp/Cryengine-Converter)
is the fallback for single raw geometry files (.cgf/.cga/.skin).

Both are invoked through command templates in settings.json ("commands"), so if
a future release renames a flag you fix it there instead of in code. The chosen
install is passed to StarBreaker through SC_DATA_P4K, which is how one toolkit
works against LIVE and PTU side by side.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import config

REPOS = {
    "starbreaker": "diogotr7/StarBreaker",
    "cgf_converter": "Markemp/Cryengine-Converter",
}
EXE_NAMES = {
    "starbreaker": ["starbreaker.exe", "starbreaker-cli.exe", "starbreaker"],
    "cgf_converter": ["cgf-converter.exe", "cgf-converter"],
    "blender": ["blender.exe", "blender"],
}


class ToolError(RuntimeError):
    pass


# Keep StarBreaker/cgf-converter from flashing a console window when the
# toolkit runs as the windowed desktop app.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Variables the packaged engine (PyInstaller) sets for its own bundled Python. Inherited
# by Blender, they make Blender's embedded Python load the engine's files instead of
# its own, and Blender stalls at startup.
_BUNDLE_VARS = ("PYTHONHOME", "PYTHONPATH", "PYTHONIOENCODING", "PYTHONUNBUFFERED", "PYTHONSTARTUP",
                "PYTHONNOUSERSITE", "PYTHONUTF8", "TCL_LIBRARY", "TK_LIBRARY", "SSL_CERT_FILE")


def child_env(extra: dict | None = None) -> dict:
    """Environment for an external program (Blender, StarBreaker, cgf-converter),
    without the packaged engine's own Python settings and bundle folder."""
    env = dict(os.environ)
    for k in list(env):
        if k.upper() in _BUNDLE_VARS or k.upper().startswith(("_PYI", "_MEI")):
            env.pop(k)
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                      if p and not os.path.normcase(p).startswith(os.path.normcase(bundle)))
    env.update(extra or {})
    return env


def release_bundle_dll_path() -> None:
    """The PyInstaller bootloader points the Windows DLL search path at its bundle,
    and child processes inherit that: undo it so other programs load their own DLLs."""
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)


def tools_dir() -> Path:
    d = config.home() / "tools"
    d.mkdir(exist_ok=True)
    return d


def find(tool: str, settings: dict) -> Path:
    configured = settings.get("tools", {}).get(tool)
    if configured and Path(configured).exists():
        return Path(configured)
    names = EXE_NAMES[tool]
    local = tools_dir() / tool
    if local.is_dir():
        for name in names:
            hits = sorted(local.rglob(name))
            if hits:
                return hits[0]
    for name in names:
        hit = shutil.which(name)
        if hit:
            return Path(hit)
    if tool == "blender" and sys.platform == "win32":
        base = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Blender Foundation"
        hits = sorted(base.glob("Blender */blender.exe"), reverse=True)
        if hits:
            return hits[0]
    if tool in REPOS:
        hint = 'click "Download tools" in the app (or run: meshyard tools update)'
    else:
        hint = f"install it, or run: meshyard config set tools.{tool} <path>"
    raise ToolError(f"{tool} not found: {hint}")


def render(template: list[str], **values) -> list[str]:
    return [part.format(**values) for part in template]


def run(settings: dict, command: str, install=None, tool: str = "starbreaker",
        cwd: Path | None = None, timeout: int = 3600, **values) -> str:
    exe = find(tool, settings)
    argv = render(settings["commands"][command], exe=str(exe),
                  p4k=str(install.p4k) if install else "", **values)
    env = child_env()
    if install is not None:
        env["SC_DATA_P4K"] = str(install.p4k)
        exes = sorted((install.path / "Bin64").glob("*.exe")) if (install.path / "Bin64").is_dir() else []
        if exes:
            env["SC_EXE"] = str(max(exes, key=lambda f: f.stat().st_size))  # the game's own exe
    proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip()[-2000:]
        raise ToolError(f"{' '.join(argv[:4])} ... failed ({proc.returncode}):\n{tail}")
    return proc.stdout


# --- updating the tools themselves -------------------------------------------

def _gh_json(url: str):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "meshyard"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def latest_release(repo: str) -> dict:
    """Newest published release that has a Windows download (prereleases count:
    some projects only publish those)."""
    try:
        rel = _gh_json(f"https://api.github.com/repos/{repo}/releases/latest")
        if choose_asset(rel.get("assets", [])):
            return rel
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    for rel in _gh_json(f"https://api.github.com/repos/{repo}/releases?per_page=15"):
        if not rel.get("draft") and choose_asset(rel.get("assets", [])):
            return rel
    raise ToolError(f"{repo} has no published release with a Windows download yet")


def choose_asset(assets: list[dict]) -> dict | None:
    """Pick the Windows build from a release's assets (names vary by project)."""
    def score(a: dict) -> int:
        n = a["name"].lower()
        if any(x in n for x in ("linux", "macos", "darwin", "osx", ".sig", ".sha", "source", "setup")):
            return -1
        s = 0
        if "win" in n or "msvc" in n:
            s += 4
        if "cli" in n:
            s += 2
        if "gui" in n or "app" in n:
            s -= 1
        if n.endswith(".zip"):
            s += 2
        elif n.endswith(".exe"):
            s += 3
        else:
            return -1
        return s
    ranked = sorted((a for a in assets if score(a) >= 0), key=score, reverse=True)
    return ranked[0] if ranked else None


def installed_version(tool: str) -> str | None:
    f = tools_dir() / tool / "VERSION"
    return f.read_text().strip() if f.exists() else None


def starbreaker_addon_dir() -> Path | None:
    d = tools_dir() / "starbreaker_addon" / "starbreaker_addon"
    return d if (d / "__init__.py").exists() else None


def update_starbreaker_addon(force: bool = False) -> str:
    """Fetch StarBreaker's Blender add-on from the source of the same release as
    the installed CLI, so its package format matches what the CLI exports."""
    tag = installed_version("starbreaker")
    if not tag:
        raise ToolError("Install StarBreaker first (meshyard tools update)")
    dest = tools_dir() / "starbreaker_addon"
    if not force and (dest / "VERSION").exists() and (dest / "VERSION").read_text().strip() == tag:
        return f"StarBreaker Blender add-on {tag} is already current"
    url = f"https://codeload.github.com/{REPOS['starbreaker']}/zip/refs/tags/{tag}"
    req = urllib.request.Request(url, headers={"User-Agent": "meshyard"})
    with urllib.request.urlopen(req, timeout=600) as r:
        data = r.read()
    staging = tools_dir() / ".starbreaker_addon.new"
    shutil.rmtree(staging, ignore_errors=True)
    target = staging / "starbreaker_addon"
    n = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for member in z.namelist():
            marker = "/blender_addon/starbreaker_addon/"
            if marker not in member or member.endswith("/"):
                continue
            rel = member.split(marker, 1)[1]
            out = (target / rel).resolve()
            if not str(out).startswith(str(target.resolve())):
                raise ToolError(f"Refusing unsafe path in archive: {member}")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(z.read(member))
            n += 1
    if not n:
        raise ToolError(f"No Blender add-on found in StarBreaker {tag} source")
    (staging / "VERSION").write_text(tag)
    shutil.rmtree(dest, ignore_errors=True)
    staging.rename(dest)
    return f"StarBreaker Blender add-on updated to {tag}"


def update_tool(tool: str, force: bool = False) -> str:
    rel = latest_release(REPOS[tool])
    tag = rel.get("tag_name", "?")
    if not force and installed_version(tool) == tag:
        return f"{tool} {tag} is already current"
    asset = choose_asset(rel.get("assets", []))
    if not asset:
        raise ToolError(f"No Windows download found in {REPOS[tool]} release {tag}. "
                        f"Download it manually and run: meshyard config set tools.{tool} <path to exe>")
    dest = tools_dir() / tool
    staging = tools_dir() / f".{tool}.new"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "meshyard"})
    with urllib.request.urlopen(req, timeout=600) as r:
        data = r.read()
    if asset["name"].lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for member in z.namelist():
                target = (staging / member).resolve()
                if not str(target).startswith(str(staging.resolve())):
                    raise ToolError(f"Refusing unsafe path in archive: {member}")
            z.extractall(staging)
    else:
        (staging / asset["name"]).write_bytes(data)
    (staging / "VERSION").write_text(tag)
    shutil.rmtree(dest, ignore_errors=True)
    staging.rename(dest)
    return f"{tool} updated to {tag} ({asset['name']})"
