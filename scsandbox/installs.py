"""Find CryEngine game data (Data.p4k) on this PC and read its build info.

Games that keep several builds side by side put each in a channel folder (LIVE,
PTU, ...) holding Data.p4k, often with build_manifest.id, a small JSON file the
launcher writes. Its Version + change number identify the build, so a new patch
shows up as a new build id and triggers a re-index.
"""

from __future__ import annotations

import json
import os
import re
import string
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

CHANNELS = ["LIVE", "PTU", "EPTU", "HOTFIX", "TECH-PREVIEW"]

DATA_FILE = "Data.p4k"

# Folders under a drive root where game libraries usually live. The data file
# finder looks a few levels below each of them.
_LIBRARY_DIRS = [
    "",
    "Program Files",
    "Program Files (x86)",
    "Games",
    r"Program Files\Epic Games",
    r"SteamLibrary\steamapps\common",
    r"Program Files (x86)\Steam\steamapps\common",
]
_SKIP_DIRS = {"windows", "$recycle.bin", "system volume information", "programdata", "users",
              "msocache", "recovery", "perflogs", "$windows.~bt", "$windows.~ws", "windowsapps",
              "node_modules", "common files", "microsoft", "windows defender", "windows nt",
              "windowspowershell", "dotnet", "nvidia corporation"}


@dataclass
class Install:
    channel: str
    path: Path
    version: str = "unknown"
    change: str = "0"
    branch: str = ""

    @property
    def p4k(self) -> Path:
        return self.path / DATA_FILE

    @property
    def build_id(self) -> str:
        safe = "".join(c if c.isalnum() or c in ".-_" else "_" for c in self.version)
        return f"{safe}-{self.change}"

    def to_json(self) -> dict:
        d = asdict(self)
        d["path"] = str(self.path)
        d["build_id"] = self.build_id
        return d


def read_manifest(channel_dir: Path) -> dict:
    """Return the "Data" block of build_manifest.id, or {} if missing/unreadable."""
    f = channel_dir / "build_manifest.id"
    if not f.exists():
        return {}
    try:
        raw = json.loads(f.read_text(encoding="utf-8-sig", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return raw.get("Data", raw) if isinstance(raw, dict) else {}


def load_install(channel_dir: Path, channel: str | None = None) -> Install | None:
    channel_dir = Path(channel_dir)
    if not (channel_dir / DATA_FILE).exists():
        return None
    m = read_manifest(channel_dir)
    version = str(m.get("Version") or m.get("Branch") or "unknown")
    change = str(m.get("RequestedP4ChangeNum") or m.get("BuildId") or "")
    if not change:
        # No manifest: fall back to the data file's size+mtime so updates still register.
        st = (channel_dir / DATA_FILE).stat()
        change = f"{st.st_size:x}{int(st.st_mtime):x}"
    return Install(
        channel=(channel or channel_dir.name).upper(),
        path=channel_dir,
        version=version,
        change=change,
        branch=str(m.get("Branch", "")),
    )


_LAUNCHER_PATH_RE = re.compile(
    r"([A-Za-z]:(?:\\\\|\\|/)[^\"'\r\n<>|*?,;]*?)"
    r"(?=(?:\\\\|\\|/)(?:LIVE|PTU|EPTU|HOTFIX|TECH-PREVIEW)(?:\\\\|\\|/|\"|'|\s|$))",
    re.I)


def launcher_roots() -> list[Path]:
    """Game folders that a game launcher has mentioned in its logs or settings.

    Launchers keep these in a folder under %APPDATA% with "launcher" in its name.
    Any folder path followed by a channel folder (LIVE, PTU, ...) is a candidate;
    the caller checks that it really holds game data.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata or not Path(appdata).is_dir():
        return []
    try:
        bases = sorted(d for d in Path(appdata).iterdir() if d.is_dir() and "launcher" in d.name.lower())
    except OSError:
        return []
    files: list[Path] = []
    for base in bases:
        logs = [*base.glob("logs/*.log"), *base.glob("*.log")]
        files += sorted(logs, key=lambda f: f.stat().st_mtime, reverse=True)[:5]
        files += list(base.glob("*.json"))
    found: list[Path] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")[-4_000_000:]
        except OSError:
            continue
        for m in _LAUNCHER_PATH_RE.finditer(text):
            p = Path(m.group(1).replace("\\\\", "\\"))
            if p not in found:
                found.append(p)
    return found


def _scan_for_data(base: Path, depth: int, budget: int = 6000) -> list[Path]:
    """Folders holding a data file, up to `depth` levels below `base`."""
    out: list[Path] = []
    level, seen = [base], 0
    for _ in range(depth + 1):
        nxt: list[Path] = []
        for d in level:
            try:
                with os.scandir(d) as it:
                    for e in it:
                        name = e.name.lower()
                        if e.is_file(follow_symlinks=False):
                            if name == DATA_FILE.lower():
                                out.append(Path(d))
                        elif (e.is_dir(follow_symlinks=False) and name not in _SKIP_DIRS
                              and not name.startswith(".")):
                            nxt.append(Path(e.path))
            except OSError:
                continue
            seen += 1
            if seen >= budget:
                return out
        level = nxt
    return out


def root_of(data_dir: Path) -> Path:
    """The folder to remember for a folder holding a data file: its parent when it's a channel folder."""
    return data_dir.parent if data_dir.name.upper() in CHANNELS else data_dir


_scan_cache: tuple[float, list[Path]] = (0.0, [])


def find_data_files(extra: list[str] | None = None, refresh: bool = False) -> list[Path]:
    """Every CryEngine data file on this PC: in folders the user added, in places
    game launchers mention, and a few folders deep in the usual game library
    folders on every drive. The drive scan is remembered for ten minutes."""
    global _scan_cache
    dirs: list[Path] = []
    for r in [Path(p) for p in (extra or [])] + [Path(p) for p in _remembered()] + launcher_roots():
        for d in (r, *(r / ch for ch in CHANNELS)):
            if (d / DATA_FILE).is_file():
                dirs.append(d)
    if sys.platform == "win32":
        t, cached = _scan_cache
        if refresh or time.time() - t > 600:
            cached = []
            for letter in string.ascii_uppercase[2:]:
                drive = Path(f"{letter}:\\")
                if not drive.exists():
                    continue
                for lib in _LIBRARY_DIRS:
                    base = drive / lib if lib else drive
                    if base.is_dir():
                        cached += _scan_for_data(base, depth=3 if lib else 4)
            _scan_cache = (time.time(), cached)
        dirs += cached
    seen, out = set(), []
    for d in dirs:
        f = d / DATA_FILE
        if str(f).lower() not in seen:
            seen.add(str(f).lower())
            out.append(f)
    return out


def _found_file() -> Path:
    from . import config
    return config.home() / "found_roots.json"


def _remembered() -> list[str]:
    try:
        f = _found_file()
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    except (OSError, ValueError):
        return []


def _remember(roots: list[Path]) -> None:
    try:
        f = _found_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps([str(r) for r in roots]), encoding="utf-8")
    except OSError:
        pass


def candidate_roots(extra: list[str] | None = None, deep: bool = True) -> list[Path]:
    roots = [Path(p) for p in (extra or [])]
    roots += [Path(p) for p in _remembered()]
    if sys.platform == "win32":
        roots += launcher_roots()
        if deep:
            roots += [root_of(f.parent) for f in find_data_files(extra)]
    seen, out = set(), []
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def discover(extra_roots: list[str] | None = None) -> list[Install]:
    found = _discover(candidate_roots(extra_roots, deep=False))
    if not found and sys.platform == "win32":
        # Nothing in the usual places: look through the game library folders on every drive,
        # and remember what turns up so the next start doesn't have to.
        found = _discover(candidate_roots(extra_roots, deep=True))
        if found:
            _remember(sorted({root_of(i.path) for i in found}, key=str))
    return found


def _discover(roots: list[Path]) -> list[Install]:
    found: dict[str, Install] = {}
    for root in roots:
        # A root may itself be a channel folder (user pointed straight at LIVE).
        direct = load_install(root)
        if direct and direct.channel in CHANNELS:
            found.setdefault(direct.channel, direct)
        if not root.is_dir():
            continue
        for ch in CHANNELS:
            inst = load_install(root / ch, ch)
            if inst:
                found.setdefault(ch, inst)
    return [found[c] for c in CHANNELS if c in found]


def pick(channel: str | None, settings: dict) -> Install:
    channel = (channel or settings.get("default_channel") or "LIVE").upper()
    installs = discover(settings.get("install_roots"))
    for inst in installs:
        if inst.channel == channel:
            return inst
    have = ", ".join(i.channel for i in installs) or "none"
    raise SystemExit(
        f"No {channel} build found (found: {have}). Add the game's folder with:\n"
        f'  sc-sandbox config add-root "D:\\Games\\<game folder>"'
    )
