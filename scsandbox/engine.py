"""Headless engine for the Windows desktop app.

The WinUI front end (desktop/) starts this as a hidden child process. It serves the
local API on a free localhost port, prints one line ``READY <url>`` on stdout, then
runs until its stdin closes (the app exited or was killed), so it never outlives the
window.

``--self-test`` checks a packaged engine has everything it needs (used by CI).
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path

from . import config


def self_test() -> int:
    """Start the server, fetch the pages and API the app uses, and run the print and
    export pipelines on a tiny model. Writes self-test.log in the toolkit's home folder."""
    import tempfile
    import urllib.request

    lines = []
    try:
        import trimesh

        from . import exporters, printprep, server
        httpd, _app, url = server.start(config.load(), port=0, auto=False)
        for path in ("", "viewer", "static/viewer.js", "static/vendor/three/build/three.module.js",
                     "api/installs"):
            # Generous timeout: the first request on a cold CI runner can be slow
            # (api/installs scans drives for game installs).
            try:
                with urllib.request.urlopen(url + path, timeout=120) as r:
                    assert r.status == 200 and r.read(), path
            except Exception as e:
                raise RuntimeError(f"GET /{path} failed: {e!r}") from e
        httpd.shutdown()
        assert (Path(__file__).parent / "blender" / "sc_sandbox_addon.py").is_file()
        with tempfile.TemporaryDirectory() as d:
            glb = Path(d) / "box.glb"
            rod = trimesh.creation.cylinder(radius=0.01, height=2)
            rod.apply_translation([0, 0, 2])
            trimesh.Scene({"hull": trimesh.creation.box((1, 2, 3)), "antenna": rod}).export(glb)
            for mode, stand in (("solid", "peg"), ("parts", "none")):
                opts = printprep.PrintOptions.from_settings(config.load(), size_mm=60, mode=mode, voxel_mm=0.3,
                                                            stand=stand)
                rep = printprep.prepare(glb, Path(d) / mode, opts, log=lambda *_: None)
                assert rep["files"] and rep.get("thickened"), rep
            for fmt in ("obj", "dae", "gltf", "stl"):
                exporters.export_asset(glb, Path(d) / "models", "box", fmt, {}, log=lambda *_: None)
        lines.append("SELF-TEST OK")
        code = 0
    except Exception:
        lines += [traceback.format_exc(), "SELF-TEST FAILED"]
        code = 1
    home = config.home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "self-test.log").write_text("\n".join(lines), encoding="utf-8")
    print(lines[-1], flush=True)
    return code


def main() -> None:
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    from . import server, tools
    tools.release_bundle_dll_path()  # so Blender and the game tools load their own DLLs
    # SCSANDBOX_NO_AUTO=1: don't download tools or start indexing on launch (tests)
    auto = os.environ.get("SCSANDBOX_NO_AUTO") != "1"
    httpd, _app, url = server.start(config.load(), port=0, auto=auto)
    print(f"READY {url}", flush=True)

    def watch_parent():
        try:
            while sys.stdin.read(4096):
                pass
        except Exception:
            pass
        os._exit(0)
    threading.Thread(target=watch_parent, daemon=True).start()
    threading.Event().wait()


if __name__ == "__main__":
    main()
