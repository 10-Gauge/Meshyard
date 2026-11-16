"""A running log of print exports, so you can look up what scale you printed something at.

One row per export in exports/print-log.csv (opens in Excel or any text editor).
"""

from __future__ import annotations

import csv
import threading
import time
from pathlib import Path

from . import config

FIELDS = ["date", "name", "key", "scale", "longest_side_mm", "size_mm", "body", "detail_mm",
          "stand", "plate", "file"]
_lock = threading.Lock()


def path() -> Path:
    return config.home() / "exports" / "print-log.csv"


def add(name: str, key: str, opts, report: dict) -> dict:
    """Append one export to the log and return the row."""
    files = report.get("files") or []
    plate = ""
    if opts.stand != "none" and opts.stand_shape == "rect":
        plate = " / ".join(t for t in (opts.stand_text, opts.stand_text2) if t)
    row = {
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "name": name,
        "key": key,
        "scale": report.get("scale", ""),
        "longest_side_mm": f"{opts.size_mm:g}",
        "size_mm": " x ".join(f"{v:.0f}" for v in report.get("size_mm") or []),
        "body": "solid" if opts.mode == "solid" else "parts",
        "detail_mm": f"{report.get('voxel_mm', opts.voxel_mm):g}" if opts.mode == "solid" else "",
        "stand": {"none": "", "peg": "separate", "attached": "attached"}.get(opts.stand, opts.stand),
        "plate": plate,
        "file": Path(files[0]).name if files else "",
    }
    p = path()
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        new = not p.exists() or p.stat().st_size == 0
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)
    return row


def entries(key: str | None = None, limit: int = 500) -> list[dict]:
    """Logged exports, newest first; only those for one item when key is given."""
    p = path()
    if not p.exists():
        return []
    with _lock, open(p, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if not key or r.get("key") == key]
    return rows[::-1][:limit]
