"""Manufacturer logos for the print name plate, from the game's own branding stencils.

Data.p4k carries a logo stencil per manufacturer under Data/Textures/Branding/<CODE>/
(e.g. aegs_logo_stencil_02_stencil.dds): the symbol on top, the wordmark below, in
the color channels a tint mask uses. The symbol alone reads at name-plate size, so
it's cut out and saved as a white-on-black mask that printprep embosses.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

from . import catalog, tools
from .installs import Install

NAMES = {
    "AEGS": "Aegis Dynamics", "ANVL": "Anvil Aerospace", "ARGO": "Argo Astronautics",
    "BANU": "Banu", "CNOU": "Consolidated Outland", "CRUS": "Crusader Industries",
    "DRAK": "Drake Interplanetary", "ESPR": "Esperia", "GAMA": "Gatac Manufacture",
    "GREY": "Grey's Market", "GRIN": "Greycat Industrial", "KRIG": "Kruger Intergalactic",
    "MISC": "MISC", "MRAI": "Mirai", "ORIG": "Origin Jumpworks", "RSI": "Roberts Space Industries",
    "TMBL": "Tumbril", "VNCL": "Vanduul", "XNAA": "Aopoa",
}
# logo stencils outside the Branding folder
EXTRA = {"TMBL": "Data/Textures/vehicles/emblems/manufacturer/TMBL_logo_stencil.dds"}


def _dir(install: Install) -> Path:
    d = catalog.build_dir(install) / "logos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rank(code: str, name: str) -> int | None:
    """How good a file is as the manufacturer's logo (lower is better), or None."""
    n = name.lower()
    c = code.lower()
    if not n.startswith(c + "_logo"):
        return None
    if "stencil_02" in n:
        return 0
    if "logo_stencil" in n:
        return 1
    if "logo_a" in n:
        return 2
    return 3


def available(install: Install, settings: dict) -> dict[str, str]:
    """Manufacturer code -> logo stencil path in Data.p4k (listed once per build)."""
    cache = _dir(install) / "catalog.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    out = tools.run(settings, "list_files", install, filter="Data/Textures/Branding/**/*.dds")
    best: dict[str, tuple[int, str]] = {}
    for line in out.splitlines():
        path = line.split("\t")[0].strip().replace("\\", "/")
        m = re.fullmatch(r"(?i)data/textures/branding/([a-z]+)/([^/]+)\.dds", path)
        if not m:
            continue  # also skips the split mip files (.dds.1, .dds.2 ...)
        code = m.group(1).upper()
        r = _rank(code, m.group(2))
        if r is not None and (code not in best or r < best[code][0]):
            best[code] = (r, path)
    found = {code: path for code, (_, path) in sorted(best.items())}
    for code, path in EXTRA.items():
        found.setdefault(code, path)
    cache.write_text(json.dumps(found, indent=1), encoding="utf-8")
    return found


def choices(install: Install, settings: dict) -> list[dict]:
    return [{"code": c, "name": NAMES.get(c, c)} for c in available(install, settings)]


def mask_for(install: Install, settings: dict, code: str) -> Path | None:
    """The manufacturer's symbol as a white-on-black PNG mask, or None if the game has no
    logo stencil for it."""
    code = (code or "").upper()
    path = available(install, settings).get(code)
    if not path:
        return None
    out = _dir(install) / f"{code}.png"
    if out.exists():
        return out
    raw = _dir(install) / f"{code}_raw.png"
    tools.run(settings, "decode_dds", install, input=path.replace("/", "\\"), out=str(raw))
    from PIL import Image
    img = np.asarray(Image.open(raw).convert("RGB")).max(axis=2)
    cut = symbol(img > 96)
    if cut is None:
        return None
    Image.fromarray((cut * 255).astype(np.uint8)).save(out)
    return out


def symbol(mask: np.ndarray) -> np.ndarray | None:
    """Crop a stencil to its top block (the symbol), above the wordmark when there is one.
    Blocks are separated by the widest band of empty rows."""
    rows = np.where(mask.any(axis=1))[0]
    if not len(rows):
        return None
    gaps = np.diff(rows)
    top = mask
    if len(gaps) and gaps.max() > max(4, 0.03 * mask.shape[0]):
        split = rows[int(np.argmax(gaps))]
        upper = mask[:split + 1]
        # keep the symbol only when it is the bigger part (else the logo is just a wordmark)
        if upper.sum() >= 0.35 * mask.sum():
            top = upper
    # side by side copies (a big and a small version): keep the main block
    cols = np.where(top.any(axis=0))[0]
    cuts = np.where(np.diff(cols) > max(4, 0.03 * top.shape[1]))[0]
    if len(cuts):
        edges = [cols[0], *[cols[i + 1] for i in cuts]]
        ends = [*[cols[i] for i in cuts], cols[-1]]
        weights = [top[:, a:b + 1].sum() for a, b in zip(edges, ends)]
        k = int(np.argmax(weights))
        if weights[k] >= 0.6 * sum(weights):
            keep = np.zeros_like(top)
            keep[:, edges[k]:ends[k] + 1] = top[:, edges[k]:ends[k] + 1]
            top = keep
    r = np.where(top.any(axis=1))[0]
    c = np.where(top.any(axis=0))[0]
    return top[r[0]:r[-1] + 1, c[0]:c[-1] + 1]
