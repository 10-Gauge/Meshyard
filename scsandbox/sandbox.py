"""Spawn queue shared by the browser and the Blender sandbox add-on.

The browser's "Spawn in sandbox" button exports the model and appends an entry
here; the add-on polls the local server and imports anything new at the 3D
cursor. Kept as a small JSON file so it survives restarts of either side.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import config

_lock = threading.Lock()


def queue_path() -> Path:
    d = config.home() / "sandbox"
    d.mkdir(exist_ok=True)
    return d / "queue.json"


def _read() -> list[dict]:
    p = queue_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def add(record: str, name: str, glb: Path, channel: str, build: str) -> dict:
    with _lock:
        items = _read()
        entry = {"seq": (items[-1]["seq"] + 1) if items else 1, "record": record, "name": name,
                 "glb": str(glb), "channel": channel, "build": build, "added_at": time.time()}
        items = (items + [entry])[-500:]
        queue_path().write_text(json.dumps(items, indent=1), encoding="utf-8")
        return entry


def since(seq: int) -> list[dict]:
    with _lock:
        return [i for i in _read() if i["seq"] > seq]


def clear() -> None:
    with _lock:
        queue_path().unlink(missing_ok=True)
