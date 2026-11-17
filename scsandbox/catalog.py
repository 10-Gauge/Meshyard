"""Per-build asset index.

`meshyard update` builds one index per (channel, build). It asks StarBreaker
for the entity records (ships, vehicles, items, props, characters) and the
English localization, then flattens them into SQLite so the browser can search
instantly. A new game build gets its own folder, so LIVE and PTU never mix and
an old index keeps working until the new one is complete.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from . import config, tools
from .installs import Install

GEOMETRY_RE = re.compile(r"\.(cga|cgf|skin|cdf|chr)$", re.I)
SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,              -- entity | geometry
    record TEXT NOT NULL UNIQUE,     -- DataCore record name, or file path for raw geometry
    name TEXT NOT NULL,              -- display name
    category TEXT NOT NULL,          -- spaceships, groundvehicles, scitem, props, characters, geometry
    subcategory TEXT NOT NULL DEFAULT '',
    manufacturer TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    record_path TEXT NOT NULL DEFAULT '',
    geometry TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_assets_name ON assets(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_assets_cat ON assets(category, subcategory);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def build_dir(install: Install) -> Path:
    return config.home() / "builds" / install.channel / install.build_id


def db_path(install: Install) -> Path:
    # versioned name: re-indexing a build writes a new file, so it never has to replace
    # one a running app still has open (Windows won't delete an open file)
    return build_dir(install) / ("index.sqlite" if INDEX_VERSION == 1 else f"index_v{INDEX_VERSION}.sqlite")


# Bump when what the index stores changes, so existing builds are indexed again.
# 2: no names taken from interaction labels; variants borrow their base item's name.
INDEX_VERSION = 2


def is_current(install: Install) -> bool:
    state = build_dir(install) / "state.json"
    if not state.exists():
        return False
    try:
        st = json.loads(state.read_text())
        return st.get("complete") is True and st.get("index_version", 1) >= INDEX_VERSION
    except json.JSONDecodeError:
        return False


# --- parsing -----------------------------------------------------------------

def parse_localization(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if "=" not in line or line.lstrip().startswith(("#", ";")):
            continue
        key, _, value = line.partition("=")
        key = key.strip().split(",", 1)[0].lower()
        out[key] = value.strip()
    return out


def _walk(node, key: str = "", path: tuple = (), nested: bool = False, root: bool = True):
    """Yield (key, value, path, nested) for every leaf. `nested` is true inside
    another record that this one references (it carries its own _RecordName_)."""
    if isinstance(node, dict):
        nested = nested or (not root and "_RecordName_" in node)
        for k, v in node.items():
            yield from _walk(v, k, path + (k,), nested, False)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key, path, nested, False)
    else:
        yield key, node, path, nested


def _usable(text: str) -> bool:
    t = text.strip().lower()
    return bool(t) and "placeholder" not in t and "uninitialized" not in t and not t.startswith("<=")


def _base_item_name(record: str, loc: dict[str, str]) -> str:
    """For a variant without a name of its own (store, prop and collector copies such as
    behr_rifle_ballistic_01_store01): its base item's name and the variant tag,
    "P4-AR Rifle (store01)"."""
    parts = record.lower().split("_")
    for n in range(len(parts) - 1, 1, -1):
        text = loc.get("item_name" + "_".join(parts[:n]), "")
        if _usable(text):
            tag = " ".join(parts[n:])
            return f"{text} ({tag})" if tag else text
    return ""


def _record_name(doc: dict, path: Path) -> str:
    if isinstance(doc, dict):
        for k, v in doc.items():
            if re.fullmatch(r"_*record_?name_*", k, re.I) and isinstance(v, str) and v:
                return v.split(".", 1)[-1] if v.startswith("EntityClassDefinition.") else v
    return path.stem


def humanize(record: str) -> str:
    parts = record.split("_")
    if len(parts) > 1 and len(parts[0]) == 4 and parts[0].isalpha():
        parts = parts[1:]
    return " ".join(p if p.isupper() else p.capitalize() for p in parts if p)


def parse_record(path: Path, records_root: Path, loc: dict[str, str]) -> dict | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None
    record = _record_name(doc, path)
    rel = path.relative_to(records_root).as_posix().lower()
    segs = rel.split("/")
    if "entities" in segs:
        segs = segs[segs.index("entities") + 1:]
    category = segs[0] if len(segs) > 1 else "other"
    subcategory = "/".join(segs[1:-1])

    # Records inline the records they reference (loadouts, parent ships), so
    # several localized names appear. Prefer one under a Localization block,
    # then the shallowest, and never one from a nested record.
    names: list[tuple[int, int, str]] = []
    descs: list[tuple[int, int, str]] = []
    geometry: list[str] = []
    for key, val, path, nested in _walk(doc):
        if not isinstance(val, str):
            continue
        k = key.lower()
        if val.startswith("@") and len(val) > 1:
            # interaction prompts ("Carry Lowered", "Drop") are not the item's name
            if nested or re.match(r"@(interaction|innerthought|ui_interact)", val, re.I)                     or any("interact" in str(p).lower() for p in path):
                continue
            text = loc.get(val[1:].lower(), "")
            if not _usable(text):
                continue
            rank = (0 if any("localization" in p.lower() for p in path) else 1, len(path))
            if k in ("name", "displayname", "vehiclename"):
                names.append((*rank, text))
            elif k in ("description", "vehicledescription"):
                descs.append((*rank, text))
        elif GEOMETRY_RE.search(val) and val not in geometry:
            geometry.append(val.replace("\\", "/"))
    name = min(names)[2] if names else ""
    desc = min(descs)[2] if descs else ""
    if not name:
        name = _base_item_name(record, loc)

    manufacturer = record.split("_", 1)[0].upper() if "_" in record else ""
    if len(manufacturer) != 4:
        manufacturer = ""
    return {
        "kind": "entity",
        "record": record,
        "name": name or humanize(record),
        "category": category,
        "subcategory": subcategory,
        "manufacturer": manufacturer,
        "description": desc.replace("\\n", "\n")[:2000],
        "record_path": rel,
        "geometry": json.dumps(geometry),
    }


def parse_file_list(stdout: str) -> list[str]:
    files = []
    for line in stdout.splitlines():
        line = line.strip().strip('"')
        # Tolerate "size  path" or "path (size)" output shapes.
        m = re.search(r"([\w\-./\\ ]+\.(?:cga|cgf|skin|chr|cdf))\b", line, re.I)
        if m:
            files.append(m.group(1).strip().replace("\\", "/"))
    return files


# --- building ----------------------------------------------------------------

def write_index(db: Path, rows: list[dict], meta: dict) -> int:
    tmp = db.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    cols = ["kind", "record", "name", "category", "subcategory", "manufacturer",
            "description", "record_path", "geometry"]
    con.executemany(
        f"INSERT OR IGNORE INTO assets ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        ([r.get(c, "") for c in cols] for r in rows),
    )
    con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [(k, str(v)) for k, v in meta.items()])
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    con.close()
    tmp.replace(db)
    for old in db.parent.glob("index*.sqlite"):   # earlier index versions, if not in use
        if old != db:
            try:
                old.unlink()
            except OSError:
                pass
    return n


_build_locks: dict[str, threading.Lock] = {}


def build(install: Install, settings: dict, force: bool = False, log=print,
          include_raw_geometry: bool = True) -> Path:
    lock = _build_locks.setdefault(install.channel, threading.Lock())
    if not lock.acquire(blocking=False):
        log(f"{install.channel} is already being indexed; waiting for it to finish")
        lock.acquire()
        force = False
    try:
        return _build(install, settings, force, log, include_raw_geometry)
    finally:
        lock.release()


def _build(install: Install, settings: dict, force: bool, log, include_raw_geometry: bool) -> Path:
    bdir = build_dir(install)
    if is_current(install) and not force:
        log(f"{install.channel} {install.version} is already indexed.")
        return db_path(install)
    bdir.mkdir(parents=True, exist_ok=True)
    records_dir = bdir / "records"
    loc_dir = bdir / "localization"
    t0 = time.time()

    log(f"Indexing {install.channel} {install.version} (change {install.change})")
    for flt in settings["record_filters"]:
        log(f"  extracting records {flt}")
        tools.run(settings, "extract_records", install, out=str(records_dir), filter=flt)
    log("  extracting localization")
    tools.run(settings, "extract_files", install, out=str(loc_dir), filter="**/english/global.ini")
    loc_files = sorted(loc_dir.rglob("global.ini"))
    loc = parse_localization(loc_files[0]) if loc_files else {}

    rows = [r for p in records_dir.rglob("*.json") if (r := parse_record(p, records_dir, loc))]
    log(f"  {len(rows)} entity records")

    if include_raw_geometry:
        seen = {r["record"] for r in rows}
        for flt in ("**/*.cga", "**/*.cgf", "**/*.skin"):
            out = tools.run(settings, "list_files", install, filter=flt)
            for f in parse_file_list(out):
                if f not in seen:
                    seen.add(f)
                    rows.append({"kind": "geometry", "record": f, "name": Path(f).stem,
                                 "category": "geometry",
                                 "subcategory": "/".join(f.lower().split("/")[1:-1][:3]),
                                 "geometry": json.dumps([f])})
        log(f"  {len(rows)} assets including raw geometry files")

    n = write_index(db_path(install), rows, install.to_json())
    (bdir / "state.json").write_text(json.dumps({**install.to_json(), "complete": True, "index_version": INDEX_VERSION,
                                                  "assets": n, "indexed_at": time.time()}, indent=2))
    log(f"  done: {n} assets in {time.time() - t0:.0f}s")
    prune(install.channel, settings.get("keep_builds", 2), keep=install.build_id, log=log)
    return db_path(install)


def prune(channel: str, keep_n: int, keep: str = "", log=print) -> None:
    cdir = config.home() / "builds" / channel
    if not cdir.is_dir():
        return
    builds = sorted((d for d in cdir.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
    for old in builds[max(keep_n, 1):]:
        if old.name != keep:
            log(f"  removing old build cache {channel}/{old.name}")
            shutil.rmtree(old, ignore_errors=True)


# --- querying ----------------------------------------------------------------

def connect(install: Install) -> sqlite3.Connection:
    db = db_path(install)
    if not db.exists():
        raise FileNotFoundError(f"{install.channel} is not indexed yet. Run: meshyard update --channel {install.channel}")
    con = sqlite3.connect(db, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def search(con: sqlite3.Connection, q: str = "", category: str = "", kind: str = "",
           limit: int = 60, offset: int = 0) -> tuple[int, list[dict]]:
    where, args = [], []
    for term in q.split():
        where.append("(name LIKE ? OR record LIKE ? OR manufacturer LIKE ? OR subcategory LIKE ?)")
        args += [f"%{term}%"] * 4
    if category:
        where.append("category = ?")
        args.append(category)
    if kind:
        where.append("kind = ?")
        args.append(kind)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = con.execute(f"SELECT COUNT(*) FROM assets {clause}", args).fetchone()[0]
    rows = con.execute(
        f"SELECT * FROM assets {clause} ORDER BY kind, "
        "CASE category WHEN 'spaceships' THEN 0 WHEN 'groundvehicles' THEN 1 ELSE 2 END, "
        "category, name COLLATE NOCASE LIMIT ? OFFSET ?",
        args + [limit, offset]).fetchall()
    return total, [dict(r) for r in rows]


def get(con: sqlite3.Connection, asset_id: int) -> dict | None:
    r = con.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    return dict(r) if r else None


def categories(con: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT category, COUNT(*) AS n FROM assets GROUP BY category ORDER BY category")]
