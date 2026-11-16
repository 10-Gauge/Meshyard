"""Find Star Citizen installs (LIVE, PTU, EPTU, ...) and read their build info.

Each channel folder holds Data.p4k plus build_manifest.id, a small JSON file
the launcher writes. Its Version + RequestedP4ChangeNum identify the build, so
a new patch (or a PTU wave) shows up as a new build id and triggers a re-index.
"""

from __future__ import annotations

import json
import os
import re
import string
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

CHANNELS = ["LIVE", "PTU", "EPTU", "HOTFIX", "TECH-PREVIEW"]

_ROOT_SUFFIXES = [
    r"Program Files\Roberts Space Industries\StarCitizen",
    r"Roberts Space Industries\StarCitizen",
    r"Games\Roberts Space Industries\StarCitizen",
    r"StarCitizen",
]


@dataclass
class Install:
    channel: str
    path: Path
    version: str = "unknown"
    change: str = "0"
    branch: str = ""

    @property
    def p4k(self) -> Path:
        return self.path / "Data.p4k"

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
    if not (channel_dir / "Data.p4k").exists():
        return None
    m = read_manifest(channel_dir)
    version = str(m.get("Version") or m.get("Branch") or "unknown")
    change = str(m.get("RequestedP4ChangeNum") or m.get("BuildId") or "")
    if not change:
        # No manifest: fall back to Data.p4k's size+mtime so updates still register.
        st = (channel_dir / "Data.p4k").stat()
        change = f"{st.st_size:x}{int(st.st_mtime):x}"
    return Install(
        channel=(channel or channel_dir.name).upper(),
        path=channel_dir,
        version=version,
        change=change,
        branch=str(m.get("Branch", "")),
    )


_LAUNCHER_PATH_RE = re.compile(
    r"([A-Za-z]:(?:\\\\|\\|/)[^\"'\r\n<>|*?]*?StarCitizen)(?=(?:\\\\|\\|/)(?:LIVE|PTU|EPTU|HOTFIX|TECH-PREVIEW)\b)",
    re.I)


def launcher_roots() -> list[Path]:
    """StarCitizen folders the RSI Launcher has mentioned in its logs/settings."""
    base = Path(os.environ.get("APPDATA", "")) / "rsilauncher"
    if not os.environ.get("APPDATA") or not base.is_dir():
        return []
    files = sorted(base.glob("logs/*.log"), key=lambda f: f.stat().st_mtime, reverse=True)[:5]
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


def _shallow_scan(drive: str, depth: int = 3) -> list[Path]:
    """Folders named StarCitizen within a few levels of a drive root (custom library folders)."""
    skip = {"windows", "$recycle.bin", "system volume information", "programdata", "users", "msocache"}
    out, level = [], [Path(drive)]
    for _ in range(depth):
        nxt = []
        for d in level:
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if not e.is_dir(follow_symlinks=False) or e.name.lower() in skip:
                            continue
                        if e.name.lower() == "starcitizen":
                            out.append(Path(e.path))
                        else:
                            nxt.append(Path(e.path))
            except OSError:
                continue
        level = nxt[:400]
    return out


def candidate_roots(extra: list[str] | None = None, deep: bool = True) -> list[Path]:
    roots = [Path(p) for p in (extra or [])]
    if sys.platform == "win32":
        roots += launcher_roots()
        for letter in string.ascii_uppercase[2:]:
            drive = f"{letter}:\\"
            if not os.path.exists(drive):
                continue
            roots += [Path(drive) / s for s in _ROOT_SUFFIXES]
            if deep:
                roots += _shallow_scan(drive)
    else:
        roots.append(Path.home() / "Games/star-citizen/drive_c/Program Files/Roberts Space Industries/StarCitizen")
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
        # Nothing in the usual places: look a few folders deep on every drive.
        found = _discover(candidate_roots(extra_roots, deep=True))
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
        f"No {channel} install found (found: {have}). Add your StarCitizen folder with:\n"
        f'  sc-sandbox config add-root "D:\\Roberts Space Industries\\StarCitizen"'
    )
