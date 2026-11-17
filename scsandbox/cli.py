"""Command line: `meshyard <command>` (or `python -m scsandbox <command>`)."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, catalog, config, exporters, installs, models, paints, printlog, printprep, tools


def cmd_installs(a, s):
    found = installs.discover(s.get("install_roots"))
    if not found:
        print("No game data found. Add the game's folder: meshyard config add-root \"<path>\"")
        return 1
    for i in found:
        state = "indexed" if catalog.is_current(i) else "NOT indexed (run: meshyard update)"
        print(f"{i.channel:<13} {i.version:<22} change {i.change:<10} {state}\n{'':14}{i.path}")


def cmd_config(a, s):
    if a.action == "show":
        print(json.dumps(s, indent=2))
        print(f"\n# file: {config.settings_path()}")
    elif a.action == "add-root":
        root = str(Path(a.value[0]))
        if root not in s["install_roots"]:
            s["install_roots"].append(root)
        config.save(s)
        print(f"Added {root}")
        cmd_installs(a, s)
    elif a.action == "set":
        key, raw = a.value[0], " ".join(a.value[1:])
        try:
            val = json.loads(raw)
        except json.JSONDecodeError:
            val = raw
        config.save(config.set_value(s, key, val))
        print(f"{key} = {json.dumps(val)}")


def cmd_tools(a, s):
    if a.action == "update":
        for t in ("starbreaker", "cgf_converter"):
            try:
                print(tools.update_tool(t, force=a.force))
            except Exception as e:
                print(f"{t}: update failed: {e}")
        try:
            print(tools.update_starbreaker_addon(force=a.force))
        except Exception as e:
            print(f"StarBreaker Blender add-on: update failed: {e}")
    for t in ("starbreaker", "cgf_converter", "blender"):
        try:
            path = tools.find(t, s)
            ver = tools.installed_version(t)
            print(f"{t:<14} {path}" + (f"  ({ver})" if ver else ""))
        except tools.ToolError as e:
            print(f"{t:<14} missing: {e}")


def _targets(a, s):
    if a.all:
        return installs.discover(s.get("install_roots"))
    return [installs.pick(a.channel, s)]


def cmd_update(a, s):
    targets = _targets(a, s)
    if not targets:
        print("No installs found.")
        return 1
    for inst in targets:
        catalog.build(inst, s, force=a.force, include_raw_geometry=not a.no_raw)


def _find_asset(con, query: str) -> dict:
    row = con.execute("SELECT * FROM assets WHERE record = ? COLLATE NOCASE", (query,)).fetchone()
    if row:
        return dict(row)
    total, rows = catalog.search(con, query, kind="entity", limit=10)
    if not rows:
        raise SystemExit(f"No asset matches '{query}'")
    if total > 1:
        print(f"{total} matches, using the first. Pass the record name to be exact:")
        for r in rows:
            print(f"  {r['record']:<45} {r['name']}")
    return rows[0]


def cmd_search(a, s):
    inst = installs.pick(a.channel, s)
    total, rows = catalog.search(catalog.connect(inst), " ".join(a.query), a.category or "", "" if a.raw else "entity", limit=a.limit)
    for r in rows:
        print(f"{r['record']:<50} {r['name']:<40} {r['category']}/{r['subcategory']}")
    print(f"-- {total} matches in {inst.channel} {inst.version}")


def cmd_model(a, s):
    inst = installs.pick(a.channel, s)
    asset = _find_asset(catalog.connect(inst), a.asset)
    glb = models.glb_for(inst, s, asset, a.variant)
    if a.out:
        shutil.copy2(glb, a.out)
        glb = Path(a.out)
    print(glb)


def cmd_print(a, s):
    inst = installs.pick(a.channel, s)
    if a.glb:
        glb, stem = Path(a.asset), Path(a.asset).stem
        key = str(Path(a.asset).resolve())
    else:
        asset = _find_asset(catalog.connect(inst), a.asset)
        print(f"Exporting {asset['name']} from {inst.channel} {inst.version}")
        glb = models.glb_for(inst, s, asset, "print")
        if a.paint:
            glb = paints.painted_glb(inst, s, asset, glb, a.paint)
        stem = asset["name"]
        key = asset["record"]
    overrides = {n: {"action": "remove"} for n in a.remove}
    overrides.update({n: {"action": "thicken", "mm": float(mm)} for n, mm in (t.rsplit("=", 1) for t in a.thicken_part)})
    opts = printprep.PrintOptions.from_settings(
        s, size_mm=a.size, scale=a.scale, mode=a.mode, voxel_mm=a.voxel, format=a.format, nozzle_mm=a.nozzle,
        min_feature_nozzles=a.min_nozzles, thicken=False if a.no_thicken else None, hollow_mm=a.hollow,
        tilt_deg=a.tilt, roll_deg=a.roll, stand=a.stand, stand_height_mm=a.stand_height, part_overrides=overrides or None,
        stand_shape="rect" if (a.plate is not None or a.plate_scale) else None,
        stand_text=a.plate if a.plate is not None else (stem if a.plate_scale else None),
        stand_text2=a.plate2, stand_scale=True if a.plate_scale else None, paint_colors=a.colors)
    if a.list_thin:
        info = printprep.analyze(glb, opts)
        for d in info["slender"]:
            print(f"{'THIN ' if d['too_thin'] else '     '}{d['thickness_mm']:>6} mm x {d['length_mm']:>7} mm  {d['name']}")
        print(f"-- {info['size_mm']} mm at {info['scale']}, minimum {info['min_feature_mm']} mm")
        return
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem).strip("_")
    out = Path(a.out) if a.out else config.home() / "exports" / f"{safe}_{printprep.file_tag(opts)}_{opts.mode}"
    rep = printprep.prepare(glb, out, opts)
    printlog.add(stem, key, opts, rep)
    print(json.dumps(rep, indent=2))


def cmd_prints(a, s):
    rows = printlog.entries(limit=a.last)
    if a.q:
        rows = [r for r in rows if a.q.lower() in (r["name"] + r["key"]).lower()]
    if not rows:
        print(f"No print exports logged yet ({printlog.path()})")
        return
    for r in rows:
        print(f"{r['date']}  {r['scale']:>7}  {r['longest_side_mm']:>5} mm  {r['name']}")
    print(f"-- {printlog.path()}")


def cmd_export(a, s):
    inst = installs.pick(a.channel, s)
    asset = _find_asset(catalog.connect(inst), a.asset)
    glb = models.glb_for(inst, s, asset, "print" if a.no_textures else "full")
    rep = exporters.export_asset(glb, config.home() / "exports" / "models", asset["name"], a.format, s)
    print(rep["download"])


def cmd_serve(a, s):
    from . import server
    server.serve(s, open_browser=not a.no_browser, port=a.port)


def cmd_blender(a, s):
    from . import blendertools
    if a.action == "install":
        blendertools.install_addons(s)
    else:
        blendertools.open_sandbox(s)
        print("Blender is opening with the sandbox scene. Open the sidebar (N) > Meshyard.")


def cmd_schedule(a, s):
    if sys.platform != "win32":
        raise SystemExit("Scheduling uses Windows Task Scheduler; on other systems add a cron job for: meshyard update --all")
    exe = Path(sys.executable).with_name("pythonw.exe")
    exe = exe if exe.exists() else Path(sys.executable)
    cmd = f'"{exe}" -m scsandbox update --all'
    subprocess.run(["schtasks", "/Create", "/F", "/TN", "Meshyard update", "/SC", "DAILY", "/ST", a.time,
                    "/TR", cmd], check=True)
    print(f"Task 'Meshyard update' runs daily at {a.time} and re-indexes LIVE and PTU after patches.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="meshyard", description="Meshyard: offline game asset browser, sandbox and 3D-print exporter")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("installs", help="list detected LIVE/PTU installs").set_defaults(fn=cmd_installs)

    c = sub.add_parser("config", help="show or change settings")
    c.add_argument("action", choices=["show", "set", "add-root"])
    c.add_argument("value", nargs="*")
    c.set_defaults(fn=cmd_config)

    t = sub.add_parser("tools", help="download/update StarBreaker and cgf-converter, or show status")
    t.add_argument("action", nargs="?", choices=["status", "update"], default="status")
    t.add_argument("--force", action="store_true")
    t.set_defaults(fn=cmd_tools)

    def channel_args(sp):
        sp.add_argument("--channel", "-c", help="LIVE, PTU, EPTU, ... (default from settings)")

    u = sub.add_parser("update", help="index the current build (run after each patch)")
    channel_args(u)
    u.add_argument("--all", action="store_true", help="every detected install")
    u.add_argument("--force", action="store_true", help="re-index even if this build is already indexed")
    u.add_argument("--no-raw", action="store_true", help="skip listing raw geometry files (faster)")
    u.set_defaults(fn=cmd_update)

    se = sub.add_parser("search", help="search the index")
    channel_args(se)
    se.add_argument("query", nargs="*")
    se.add_argument("--category")
    se.add_argument("--raw", action="store_true", help="include raw geometry files")
    se.add_argument("--limit", type=int, default=40)
    se.set_defaults(fn=cmd_search)

    m = sub.add_parser("model", help="export an asset as GLB")
    channel_args(m)
    m.add_argument("asset", help="record name or search text")
    m.add_argument("--variant", choices=list(models.VARIANTS), default="full")
    m.add_argument("--out")
    m.set_defaults(fn=cmd_model)

    pr = sub.add_parser("print", help="export an asset (or a .glb with --glb) as a printable 3MF/STL")
    channel_args(pr)
    pr.add_argument("asset", help="record name, search text, or a .glb path with --glb")
    pr.add_argument("--glb", action="store_true", help="treat ASSET as a local .glb file")
    pr.add_argument("--size", type=float, help="longest side in mm")
    pr.add_argument("--scale", type=float, metavar="N", help="print at 1:N instead of a set size (wins over --size)")
    pr.add_argument("--mode", choices=["solid", "parts"])
    pr.add_argument("--voxel", type=float, help="detail size in mm (solid mode)")
    pr.add_argument("--format", choices=list(printprep.PRINT_FORMATS))
    pr.add_argument("--paint", metavar="PAINT", help="print in one of the ship's paints (a Paint_... item name)")
    pr.add_argument("--colors", type=int, metavar="N", help="3MF: paint the print in N filament colors for Bambu Studio")
    pr.add_argument("--nozzle", type=float, help="nozzle width in mm (default 0.4)")
    pr.add_argument("--min-nozzles", type=float, help="thicken features thinner than this many nozzle widths (default 3)")
    pr.add_argument("--no-thicken", action="store_true", help="leave thin parts as they are")
    pr.add_argument("--list-thin", action="store_true", help="list slender parts at this size and stop")
    pr.add_argument("--thicken-part", action="append", default=[], metavar="NAME=MM", help="thicken one part to MM")
    pr.add_argument("--remove", action="append", default=[], metavar="NAME", help="leave a part out")
    pr.add_argument("--hollow", type=float, help="hollow with walls this thick in mm (resin)")
    pr.add_argument("--stand", choices=["none", "peg", "attached"])
    pr.add_argument("--stand-height", type=float, help="mm between stand base and model")
    pr.add_argument("--tilt", type=float, help="incline in degrees: nose up (+) or down (-)")
    pr.add_argument("--roll", type=float, help="tilt in degrees: right wing down (+) or left wing down (-)")
    pr.add_argument("--plate", help="name plate text (rectangular base with a plate)")
    pr.add_argument("--plate2", help="smaller second line on the plate")
    pr.add_argument("--plate-scale", action="store_true", help="put the print scale (1:85) on the plate")
    pr.add_argument("--out")
    pr.set_defaults(fn=cmd_print)

    pl = sub.add_parser("prints", help="list past print exports and the scale each was printed at")
    pl.add_argument("q", nargs="?", help="only names containing this")
    pl.add_argument("--last", type=int, default=50)
    pl.set_defaults(fn=cmd_prints)

    ex = sub.add_parser("export", help="export an asset as GLB, glTF, OBJ, FBX, DAE, USDZ, PLY, STL or 3MF")
    channel_args(ex)
    ex.add_argument("asset", help="record name or search text")
    ex.add_argument("--format", "-f", choices=list(exporters.FORMATS), default="glb")
    ex.add_argument("--no-textures", action="store_true")
    ex.set_defaults(fn=cmd_export)

    sv = sub.add_parser("serve", help="start the asset browser")
    sv.add_argument("--port", type=int)
    sv.add_argument("--no-browser", action="store_true")
    sv.set_defaults(fn=cmd_serve)

    b = sub.add_parser("blender", help="install the Blender add-on, or open the sandbox scene")
    b.add_argument("action", choices=["install", "open"])
    b.set_defaults(fn=cmd_blender)

    sc = sub.add_parser("schedule", help="re-index LIVE and PTU daily via Windows Task Scheduler")
    sc.add_argument("--time", default="09:00")
    sc.set_defaults(fn=cmd_schedule)

    a = p.parse_args(argv)
    s = config.load()
    try:
        return a.fn(a, s) or 0
    except tools.ToolError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
