"""Local server for the asset browser (http://127.0.0.1:8765) and the desktop app.

Listens on localhost only. Serves the browser page, the desktop app's 3D viewer pane
(/viewer) and a JSON API. Long operations (indexing, model export, print conversion)
run as background jobs the client polls.
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import (blendertools, catalog, characters, config, exporters, gear, installs, logos, models, printlog,
               printprep, rig, sandbox, tools)

WEB = Path(__file__).parent / "web"
mimetypes.add_type("model/gltf-binary", ".glb")
mimetypes.add_type("application/javascript", ".js")


class App:
    def __init__(self, settings: dict):
        self.settings = settings
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()
        self._cons: dict[tuple, object] = {}
        self._installs: tuple[float, list] = (0.0, [])
        self.index_state: dict[str, dict] = {}  # channel -> {state, error}

    # installs are re-scanned at most every 10 s so a patch landing while the
    # server runs is picked up without a restart
    def installs(self) -> list[installs.Install]:
        t, cached = self._installs
        if time.time() - t > 10:
            cached = installs.discover(self.settings.get("install_roots"))
            self._installs = (time.time(), cached)
        return cached

    def install(self, channel: str | None) -> installs.Install:
        channel = (channel or self.settings.get("default_channel", "LIVE")).upper()
        for i in self.installs():
            if i.channel == channel:
                return i
        raise LookupError(f"No {channel} install found")

    def con(self, inst: installs.Install):
        key = (inst.channel, inst.build_id)
        with self.lock:
            if key not in self._cons:
                self._cons[key] = catalog.connect(inst)
            return self._cons[key]

    def submit(self, label: str, fn) -> str:
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "label": label, "state": "running", "log": [], "result": None,
               "error": None, "started": time.time()}
        with self.lock:
            self.jobs[jid] = job
            for old in sorted(self.jobs.values(), key=lambda j: j["started"])[:-100]:
                self.jobs.pop(old["id"], None)

        def log(*parts):
            job["log"].append(" ".join(str(p) for p in parts))

        def runner():
            try:
                job["result"] = fn(log)
                job["state"] = "done"
            except Exception as e:  # report to the page instead of dying silently
                job["state"] = "error"
                job["error"] = str(e)
                log(traceback.format_exc(limit=3))
        self.pool.submit(runner)
        return jid

    # --- job bodies ---
    def job_update(self, channel: str, force: bool):
        inst = self.install(channel)

        def fn(log):
            self.index_state[inst.channel] = {"state": "running", "error": None}
            try:
                catalog.build(inst, self.settings, force=force, log=log)
            except Exception as e:
                self.index_state[inst.channel] = {"state": "error", "error": str(e)}
                raise
            self.index_state[inst.channel] = {"state": "done", "error": None}
            return {"channel": inst.channel, "build": inst.build_id}
        return self.submit(f"Index {inst.channel} {inst.version}", fn)

    def job_model(self, channel: str, asset_id: int, variant: str, spawn: bool, gear_up: bool = False,
                  weapons: str | None = None, vtol: bool = False, flight: bool = False):
        inst = self.install(channel)
        asset = catalog.get(self.con(inst), asset_id)
        if not asset:
            raise LookupError("asset not found")

        def fn(log):
            if (gear_up or weapons or vtol or flight or _shape_pending(inst, asset, self.settings)) and variant != "animated":
                glb = _posed(inst, self.settings, asset, variant, gear_up, weapons, log, vtol, flight)
                if spawn:
                    sandbox.add(asset["record"], asset["name"], glb, inst.channel, inst.build_id)
                    log("queued for the Blender sandbox")
                return {"url": f"/files/model/{inst.channel}/{inst.build_id}/{quote(glb.name)}",
                        "path": str(glb), "gear": "up" if gear_up else "down", "weapons": weapons or "",
                        "vtol": vtol, "flight": flight}
            if variant == "animated":
                pkg = models.package_for(inst, self.settings, asset, log=log)
                if spawn:
                    sandbox.add(asset["record"], asset["name"], pkg, inst.channel, inst.build_id)
                    log("queued for the Blender sandbox")
                return {"url": None, "path": str(pkg)}
            glb = models.glb_for(inst, self.settings, asset, variant, log=log)
            if spawn:
                sandbox.add(asset["record"], asset["name"], glb, inst.channel, inst.build_id)
                log("queued for the Blender sandbox")
            return {"url": f"/files/model/{inst.channel}/{inst.build_id}/{quote(glb.name)}",
                    "path": str(glb)}
        verb = "Spawn" if spawn else "Load"
        return self.submit(f"{verb} {asset['name']}", fn)

    def job_tools(self, force: bool = False):
        def fn(log):
            errors = []
            for t in ("starbreaker", "cgf_converter"):
                try:
                    log(tools.update_tool(t, force=force))
                except Exception as e:
                    errors.append(f"{t}: {e}")
                    log(f"{t}: update failed: {e}")
            try:
                log(tools.update_starbreaker_addon(force=force))
            except Exception as e:
                log(f"StarBreaker Blender add-on: update failed: {e}")
            if errors:
                raise tools.ToolError("; ".join(errors))
            # Builds that could not be indexed without the tools get indexed now.
            for inst in self.installs():
                if not catalog.is_current(inst) and self.index_state.get(inst.channel, {}).get("state") != "running":
                    self.job_update(inst.channel, False)
            return {"ok": True}
        return self.submit("Download tools", fn)

    def add_root(self, path: str) -> str:
        """Remember a game folder the user picked. Accepts the StarCitizen folder,
        a LIVE/PTU folder inside it, or the folder above StarCitizen."""
        p = Path(path.strip().strip('"'))
        if not p.is_dir():
            raise LookupError(f"{p} is not a folder")
        for cand in (p, p / "StarCitizen", p / "Roberts Space Industries" / "StarCitizen"):
            if installs._discover([cand]):
                root = str(cand)
                break
        else:
            raise LookupError(f"No LIVE or PTU folder with Data.p4k found in {p}. "
                              "Pick the StarCitizen folder (the one that contains LIVE).")
        s = config.load()
        roots = s.setdefault("install_roots", [])
        if root not in roots:
            roots.append(root)
            config.save(s)
        self.settings["install_roots"] = s["install_roots"]
        self._installs = (0.0, [])
        for inst in self.installs():
            if not catalog.is_current(inst) and self.settings.get("auto_update", True):
                try:
                    tools.find("starbreaker", self.settings)
                except tools.ToolError:
                    continue
                self.job_update(inst.channel, False)
        return root

    def job_blender(self, action: str):
        def fn(log):
            if action == "install":
                blendertools.install_addons(self.settings, log=log)
            else:
                blendertools.install_addons(self.settings, log=log)
                blendertools.open_sandbox(self.settings)
                log("Blender is opening: press N and open the SC Sandbox tab")
            return {"ok": True}
        return self.submit("Blender sandbox", fn)

    def _target(self, inst, b: dict):
        """What a print/analyze/export request is about: an indexed asset ({"id": ...})
        or a figure from the character builder ({"figure": ..., "label": ...}).
        Returns (display name, glb getter(variant, log), is_figure, manufacturer code)."""
        if b.get("figure"):
            path = characters.figure_path(inst, str(b["figure"]))
            label = str(b.get("label") or "Character")[:80]
            return label, (lambda variant, log: path), True, ""
        asset = catalog.get(self.con(inst), int(b["id"]))
        if not asset:
            raise LookupError("asset not found")
        gear_up, weapons, vtol, flight = b.get("gear") == "up", _weapons(b), bool(b.get("vtol")), bool(b.get("flight"))
        if gear_up or weapons or vtol or flight or _shape_pending(inst, asset, self.settings):
            getter = lambda variant, log: _posed(inst, self.settings, asset, variant, gear_up, weapons, log, vtol, flight)  # noqa: E731
        else:
            getter = lambda variant, log: models.glb_for(inst, self.settings, asset, variant, log=log)  # noqa: E731
        return asset["name"], getter, False, asset.get("manufacturer") or ""

    def job_print(self, channel: str, b: dict, opts: dict):
        inst = self.install(channel)
        name, glb_for, figure, maker = self._target(inst, b)
        key = log_key(self.con(inst), b)
        if figure:
            opts = {**opts, "figure": True}
        logo = str(opts.pop("stand_logo", "") or "")
        if logo.lower() == "auto":
            logo = maker

        def fn(log):
            o = dict(opts)
            if logo and opts.get("stand_shape") == "rect":
                mask = logos.mask_for(inst, self.settings, logo)
                if mask:
                    o["stand_logo"] = str(mask)
                else:
                    log(f"no logo in the game files for {logos.NAMES.get(logo.upper(), logo)}: plate without a logo")
            po = printprep.PrintOptions.from_settings(self.settings, **o)
            log("exporting untextured full-detail model")
            glb = glb_for("print", log)
            log(f"converting ({po.mode}, " + (f"1:{po.scale:g}" if po.scale > 0 else f"{po.size_mm:g} mm") + ")")
            stem = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "model"
            out = config.home() / "exports" / f"{stem}_{printprep.file_tag(po)}_{po.mode}"
            rep = printprep.prepare(glb, out, po, log=log)
            try:
                printlog.add(name, key, po, rep)
            except OSError as e:
                log(f"couldn't add this export to the print log: {e}")
            rep["downloads"] = [f"/files/export/{quote(Path(f).relative_to(config.home() / 'exports').as_posix())}"
                                for f in rep["files"]]
            return rep
        return self.submit(f"Print {name}", fn)

    def job_analyze(self, channel: str, b: dict, opts: dict):
        inst = self.install(channel)
        name, glb_for, _, _ = self._target(inst, b)
        opts = {k: v for k, v in opts.items() if k != "stand_logo"}
        po = printprep.PrintOptions.from_settings(self.settings, **opts)

        def fn(log):
            glb = glb_for("print", log)
            log("measuring parts at print size")
            return printprep.analyze(glb, po)
        return self.submit(f"Find thin parts: {name}", fn)

    def job_measure(self, channel: str, b: dict):
        """The model's real size in meters, for turning a scale into a print size and back.
        Measures the model the viewer already has; the answer is kept beside the file."""
        inst = self.install(channel)
        name, glb_for, _, _ = self._target(inst, b)
        exclude = printprep.PrintOptions.from_settings(self.settings).exclude_nodes
        variant = b.get("variant") if b.get("variant") in ("preview", "full") else "preview"

        def fn(log):
            glb = Path(glb_for(variant, log))
            cache = glb.with_name(glb.stem + ".size.json")
            stamp = glb.stat().st_mtime
            try:
                c = json.loads(cache.read_text())
                if c.get("mtime") == stamp:
                    return c["size"]
            except (OSError, ValueError, KeyError):
                pass
            size = printprep.measure(glb, exclude)
            try:
                cache.write_text(json.dumps({"mtime": stamp, "size": size}))
            except OSError:
                pass
            return size
        return self.submit(f"Measure {name}", fn)

    def job_export_model(self, channel: str, b: dict, fmt: str, textures: bool):
        inst = self.install(channel)
        name, glb_for, _, _ = self._target(inst, b)
        if fmt not in exporters.FORMATS:
            raise ValueError(f"unknown format {fmt}")

        def fn(log):
            glb = glb_for("full" if textures else "print", log)
            exports = config.home() / "exports"
            rep = exporters.export_asset(glb, exports / "models", name, fmt, self.settings, log=log)
            rep["url"] = f"/files/export/{quote(Path(rep['download']).relative_to(exports).as_posix())}"
            return rep
        return self.submit(f"Export {name} as {fmt.upper()}", fn)

    def job_character(self, channel: str, b: dict):
        inst = self.install(channel)
        body = str(b.get("body") or "male")
        picks = {k: int(v) for k, v in (b.get("picks") or {}).items() if v}
        head = b.get("head") or None
        pose = b.get("pose") or None
        weapon = int(b["weapon"]) if b.get("weapon") and pose else None

        def fn(log):
            glb, name = characters.build(inst, self.settings, self.con(inst), body, picks, head, log=log,
                                         pose=pose, weapon=weapon)
            return {"figure": name, "path": str(glb),
                    "url": f"/files/model/{inst.channel}/{inst.build_id}/figures/{quote(glb.name)}"}
        return self.submit("Build character", fn)


def _shape_pending(inst, asset, settings) -> bool:
    """A ship that may look different with the gear down than in its export: its landed
    shape is known, or not worked out yet (and Blender is there to do it)."""
    if asset.get("category") != "spaceships":
        return False
    if gear.has_landed_shape(inst, asset) or gear.has_shape(inst, asset, "closed"):
        return True
    unknown = gear.landed_unknown(inst, asset) or not gear._shape_known(inst, asset, "closed")
    return unknown and gear.blender_available(settings)


def _weapons(b: dict) -> str | None:
    """Requested weapon pose: "out" (deployed), "in" (stowed) or None (as exported)."""
    w = str(b.get("weapons") or "").lower()
    return w if w in ("out", "in") else None


def _posed(inst, settings, asset, variant, gear_up, weapons, log, vtol=False, flight=False):
    """The model with its landing gear up, weapons deployed and/or VTOL mode, with plain-language errors."""
    try:
        return gear.posed_glb(inst, settings, asset, variant, gear_up=gear_up, weapons=weapons, log=log, vtol=vtol, flight=flight)
    except gear.NoPose as e:
        if e.kind == "gear":
            raise LookupError(f"{asset['name']} can't show its landing gear up: {e}") from e
        if e.kind == "vtol":
            raise LookupError(f"{asset['name']} has no VTOL mode ({e}).") from e
        if e.kind == "flight":
            raise LookupError(f"{asset['name']} has no flight mode flaps ({e}).") from e
        raise LookupError(f"{asset['name']} has no deployable weapons ({e}).") from e
    except tools.ToolError as e:
        if "blender" in str(e).lower() and "not found" in str(e).lower():
            raise LookupError("Landing gear up, deployed weapons, folded wings, VTOL and flight mode need Blender (blender.org): they use "
                              "the StarBreaker add-on to read the ship's animations.") from e
        raise


PRINT_OPTS = {"size_mm": float, "scale": float, "mode": str, "voxel_mm": float, "format": str, "nozzle_mm": float,
              "min_feature_nozzles": float, "thicken": bool, "hollow_mm": float, "tilt_deg": float,
              "stand": str, "stand_height_mm": float, "part_overrides": dict,
              "stand_shape": str, "stand_text": lambda v: str(v)[:60], "stand_text2": lambda v: str(v)[:60],
              "stand_scale": bool, "stand_forward_mm": float, "stand_side_mm": float,
              "stand_logo": lambda v: re.sub(r"[^A-Za-z]", "", str(v))[:8]}  # manufacturer code or "auto"


def log_key(con, b: dict) -> str:
    """What a print is logged under: the asset's record name, or the figure's name."""
    if b.get("figure"):
        return "figure:" + str(b.get("label") or b["figure"])
    a = catalog.get(con, int(b["id"])) if b.get("id") is not None else None
    return a["record"] if a else ""


def print_opts(body: dict) -> dict:
    """The print options the page may set, type-checked."""
    out = {}
    for k, v in (body.get("options") or {}).items():
        t = PRINT_OPTS.get(k)
        if t is None or v is None or v == "":
            continue
        out[k] = t(v) if t is not dict else {str(n): {"action": str(o.get("action", "auto")),
                                                      "mm": float(o.get("mm") or 0)}
                                              for n, o in v.items() if isinstance(o, dict)}
    return out


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "sc-sandbox"

        def log_message(self, fmt, *args):  # quiet console
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def file(self, base: Path, rel: str, download: bool = False):
            base = base.resolve()
            target = (base / unquote(rel)).resolve()
            if not target.is_relative_to(base) or not target.is_file():
                return self.json({"error": "not found"}, 404)
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            extra = {"Content-Disposition": f'attachment; filename="{target.name}"'} if download else None
            self._send(200, target.read_bytes(), ctype, extra)

        def body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            p = u.path
            try:
                if p in ("/", "/index.html"):
                    return self.file(WEB, "index.html")
                if p == "/viewer":  # the desktop app's 3D pane
                    return self.file(WEB, "viewer.html")
                if p.startswith("/static/"):
                    return self.file(WEB, p[len("/static/"):])
                if p == "/api/character/slots":
                    inst = app.install(q.get("channel"))
                    body = q.get("body", "male")
                    return self.json({"bodies": list(characters.BODIES),
                                      "slots": [{"id": k, "label": v[0]} for k, v in characters.SLOTS.items()],
                                      "heads": characters.heads(inst, app.settings, body),
                                      "poses": [{"id": k, "label": v} for k, v in rig.POSES.items()]})
                if p == "/api/logos":  # manufacturers whose logo can go on the name plate
                    inst = app.install(q.get("channel"))
                    return self.json({"items": logos.choices(inst, app.settings)})
                if p == "/api/character/weapons":
                    inst = app.install(q.get("channel"))
                    return self.json({"items": characters.weapons(app.con(inst), q.get("q", ""))})
                if p == "/api/character/items":
                    inst = app.install(q.get("channel"))
                    return self.json({"items": characters.items(app.con(inst), q.get("slot", ""),
                                                                q.get("body", "male"), q.get("q", ""))})
                if p == "/api/installs":
                    out = []
                    for i in app.installs():
                        d = i.to_json()
                        d["indexed"] = catalog.is_current(i)
                        d["index"] = app.index_state.get(i.channel, {"state": "idle", "error": None})
                        out.append(d)
                    searched = [] if out else [str(r) for r in installs.candidate_roots(
                        app.settings.get("install_roots"), deep=False) if r.exists()]
                    tool_state = {}
                    for t in ("starbreaker", "cgf_converter", "blender"):
                        try:
                            tool_state[t] = str(tools.find(t, app.settings))
                        except tools.ToolError:
                            tool_state[t] = None
                    return self.json({"installs": out, "tools": tool_state, "searched": searched,
                                      "default_channel": app.settings.get("default_channel"),
                                      "print": app.settings["print"],
                                      "export_formats": [[f, exporters.LABELS[f]] for f in exporters.FORMATS]})
                if p == "/api/assets":
                    inst = app.install(q.get("channel"))
                    con = app.con(inst)
                    total, rows = catalog.search(con, q.get("q", ""), q.get("category", ""), q.get("kind", ""),
                                                 limit=min(int(q.get("limit", 60)), 200),
                                                 offset=int(q.get("offset", 0)))
                    return self.json({"total": total, "items": rows, "categories": catalog.categories(con)})
                m = re.fullmatch(r"/api/assets/(\d+)", p)
                if m:
                    inst = app.install(q.get("channel"))
                    a = catalog.get(app.con(inst), int(m.group(1)))
                    return self.json(a) if a else self.json({"error": "not found"}, 404)
                m = re.fullmatch(r"/api/jobs/(\w+)", p)
                if m:
                    job = app.jobs.get(m.group(1))
                    return self.json(job) if job else self.json({"error": "not found"}, 404)
                if p == "/api/print/log":  # past print exports (all, or one item's)
                    return self.json({"items": printlog.entries(q.get("key") or None),
                                      "path": str(printlog.path())})
                if p == "/api/sandbox/queue":
                    return self.json({"items": sandbox.since(int(q.get("after", 0)))})
                m = re.fullmatch(r"/files/model/([\w-]+)/([\w.\-]+)/(.+)", p)
                if m:
                    return self.file(config.home() / "builds" / m.group(1) / m.group(2) / "models", m.group(3))
                if p.startswith("/files/export/"):
                    return self.file(config.home() / "exports", p[len("/files/export/"):], download=True)
                return self.json({"error": "not found"}, 404)
            except FileNotFoundError as e:
                return self.json({"error": str(e), "needs_index": True}, 409)
            except LookupError as e:
                return self.json({"error": str(e)}, 404)
            except Exception as e:
                return self.json({"error": str(e)}, 500)

        def do_POST(self):
            p = urlparse(self.path).path
            # Only accept requests from our own page (blocks drive-by POSTs from other sites).
            origin = self.headers.get("Origin")
            if origin and not re.fullmatch(r"https?://(127\.0\.0\.1|localhost)(:\d+)?", origin):
                return self.json({"error": "forbidden"}, 403)
            try:
                b = self.body()
                if p == "/api/update":
                    return self.json({"job": app.job_update(b.get("channel"), bool(b.get("force")))})
                if p == "/api/model":
                    return self.json({"job": app.job_model(b.get("channel"), int(b["id"]),
                                                           b.get("variant", "preview"), bool(b.get("spawn")),
                                                           gear_up=b.get("gear") == "up", weapons=_weapons(b),
                                                           vtol=bool(b.get("vtol")), flight=bool(b.get("flight")))})
                if p == "/api/character/build":
                    return self.json({"job": app.job_character(b.get("channel"), b)})
                if p == "/api/print":
                    return self.json({"job": app.job_print(b.get("channel"), b, print_opts(b))})
                if p == "/api/print/measure":
                    return self.json({"job": app.job_measure(b.get("channel"), b)})
                if p == "/api/print/analyze":
                    return self.json({"job": app.job_analyze(b.get("channel"), b, print_opts(b))})
                if p == "/api/export":
                    return self.json({"job": app.job_export_model(b.get("channel"), b,
                                                                  str(b.get("format", "glb")).lower(),
                                                                  bool(b.get("textures", True)))})
                if p == "/api/tools/update":
                    return self.json({"job": app.job_tools(bool(b.get("force")))})
                if p == "/api/roots":
                    return self.json({"root": app.add_root(str(b.get("path") or ""))})
                if p == "/api/blender":
                    return self.json({"job": app.job_blender(b.get("action") or "open")})
                if p == "/api/sandbox/clear":
                    sandbox.clear()
                    return self.json({"ok": True})
                return self.json({"error": "not found"}, 404)
            except FileNotFoundError as e:
                return self.json({"error": str(e), "needs_index": True}, 409)
            except LookupError as e:
                return self.json({"error": str(e)}, 404)
            except Exception as e:
                return self.json({"error": str(e)}, 500)

    return Handler


def start(settings: dict, port: int | None = None, auto: bool = True):
    """Start the server in a background thread; returns (httpd, app, url).
    Falls back to a free port when the configured one is taken."""
    host = settings["server"]["host"]
    port = settings["server"]["port"] if port is None else port
    app = App(settings)
    try:
        httpd = ThreadingHTTPServer((host, port), make_handler(app))
    except OSError:
        httpd = ThreadingHTTPServer((host, 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    if auto:
        try:
            tools.find("starbreaker", settings)
            have_tools = True
        except tools.ToolError:
            have_tools = False
            print("StarBreaker missing: downloading the tools in the background")
            app.job_tools()  # indexes any new build once the tools are in
        for inst in app.installs():
            if catalog.is_current(inst):
                print(f"{inst.channel} {inst.version}: indexed")
            elif have_tools and settings.get("auto_update", True):
                print(f"{inst.channel} {inst.version}: new build, indexing in the background")
                app.job_update(inst.channel, False)
            elif have_tools:
                print(f"{inst.channel} {inst.version}: not indexed (click Update index)")
    return httpd, app, f"http://{host}:{httpd.server_address[1]}/"


def serve(settings: dict, open_browser: bool = True, port: int | None = None) -> None:
    httpd, app, url = start(settings, port)
    print(f"Asset browser running at {url}  (Ctrl+C to stop)")
    if open_browser:
        import webbrowser
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        httpd.shutdown()
