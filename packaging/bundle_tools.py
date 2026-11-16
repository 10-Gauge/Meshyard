"""Collect the external tools into one zip that build_exe.ps1 appends to the app exe.

    python packaging/bundle_tools.py <out.zip> [--with-blender] [--blender-version 5.2.2]

Contents (each top-level folder is unpacked to %LOCALAPPDATA%\\sc-sandbox\\tools\\<name>):
    starbreaker/        StarBreaker CLI (latest release; MIT)
    starbreaker_addon/  StarBreaker's Blender add-on from the same release
    cgf_converter/      cgf-converter (latest release; GPL-2.0)
    blender/            only with --with-blender: portable Blender (GPL-3.0) with both
                        add-ons pre-installed in its own "portable" settings folder, so it
                        never touches an installed Blender's settings. Left out by default
                        (it adds about 400 MB); the app then uses the Blender the user has
                        installed and offers the download when there is none.
    bundle.json         {tool: version}, read by the app to decide what to unpack

Downloads are cached in build/cache. Every tool keeps its license next to it
(NOTICE.txt with the source address; Blender ships its own license folder).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "build" / "cache"
SOURCES = {
    "starbreaker": ("StarBreaker", "https://github.com/diogotr7/StarBreaker", "diogotr7/StarBreaker"),
    "starbreaker_addon": ("StarBreaker Blender add-on", "https://github.com/diogotr7/StarBreaker", "diogotr7/StarBreaker"),
    "cgf_converter": ("cgf-converter (Cryengine Converter)", "https://github.com/Markemp/Cryengine-Converter",
                      "Markemp/Cryengine-Converter"),
}


def _get(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "sc-sandbox-build"})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=1800) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    tmp.replace(dest)
    return dest


# download.blender.org refuses automated downloads; its official mirrors don't
BLENDER_SITES = ["https://download.blender.org/release",
                 "https://mirrors.ocf.berkeley.edu/blender/release",
                 "https://ftp.nluug.nl/pub/graphics/blender/release",
                 "https://mirror.clarkson.edu/blender/release"]


def _blender_zip(version: str) -> Path:
    """The official portable Blender zip, checked against Blender's published SHA-256."""
    import hashlib
    major = ".".join(version.split(".")[:2])
    name = f"blender-{version}-windows-x64.zip"
    dest = CACHE / name
    errors = []
    for site in BLENDER_SITES:
        base = f"{site}/Blender{major}"
        try:
            req = urllib.request.Request(f"{base}/blender-{version}.sha256", headers={"User-Agent": "sc-sandbox-build"})
            with urllib.request.urlopen(req, timeout=120) as r:
                sums = dict(reversed(line.split()) for line in r.read().decode().splitlines() if line.strip())
            want = sums[name]
            _get(f"{base}/{name}", dest)
            h = hashlib.sha256()
            with open(dest, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != want:
                dest.unlink()
                raise ValueError("checksum mismatch")
            return dest
        except Exception as e:
            errors.append(f"{site}: {e}")
    raise SystemExit("Could not download Blender: " + "; ".join(errors))


def _license(repo: str) -> tuple[str, str]:
    """(SPDX id, license text) from GitHub."""
    import base64
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/license",
                                 headers={"User-Agent": "sc-sandbox-build", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        j = json.load(r)
    return j["license"]["spdx_id"], base64.b64decode(j["content"]).decode("utf-8", "replace")


def main() -> int:
    out = Path(sys.argv[1]).resolve()
    bver = sys.argv[sys.argv.index("--blender-version") + 1] if "--blender-version" in sys.argv else "5.2.2"
    with_blender = "--with-blender" in sys.argv
    with tempfile.TemporaryDirectory() as home:
        os.environ["SCSANDBOX_HOME"] = home          # the app's own updater, into a scratch home
        sys.path.insert(0, str(ROOT))
        from scsandbox import tools
        tdir = tools.tools_dir()
        print("== StarBreaker, its Blender add-on and cgf-converter (latest releases)")
        print(" ", tools.update_tool("starbreaker", force=True))
        print(" ", tools.update_starbreaker_addon(force=True))
        print(" ", tools.update_tool("cgf_converter", force=True))
        versions = {t: (tdir / t / "VERSION").read_text().strip() for t in SOURCES}
        for t, (name, url, repo) in SOURCES.items():
            spdx, text = _license(repo)
            (tdir / t / "LICENSE.txt").write_text(text, encoding="utf-8")
            (tdir / t / "NOTICE.txt").write_text(
                f"{name} {versions[t]}\nLicense: {spdx} (see LICENSE.txt)\nSource code: {url}\n"
                f"Bundled unmodified with SC Asset Browser.\n", encoding="utf-8")

        if with_blender:
            print(f"== Blender {bver} (portable)")
            z = _blender_zip(bver)
            bdir = tdir / "blender"
            shutil.rmtree(bdir, ignore_errors=True)
            with zipfile.ZipFile(z) as zz:
                zz.extractall(bdir)
            exe = next(bdir.rglob("blender.exe"))
            # Blender keeps its settings and add-ons in a "portable" folder next to the exe
            addons = exe.parent / "portable" / "scripts" / "addons"
            addons.mkdir(parents=True, exist_ok=True)
            shutil.copytree(tdir / "starbreaker_addon" / "starbreaker_addon", addons / "starbreaker_addon")
            shutil.copy2(ROOT / "scsandbox" / "blender" / "sc_sandbox_addon.py", addons / "sc_sandbox_addon.py")
            (bdir / "NOTICE.txt").write_text(
                f"Blender {bver}\nLicense: GPL-3.0-or-later (see the license folder and copyright.txt)\n"
                f"Source code: https://projects.blender.org/blender/blender (tag v{bver})\n"
                "Bundled unmodified with SC Asset Browser; the StarBreaker and SC Sandbox add-ons are "
                "pre-installed in its portable folder.\n", encoding="utf-8")
            versions["blender"] = bver

        print(f"== writing {out}")
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".part")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr("bundle.json", json.dumps(versions, indent=1))
            for t in versions:
                for f in sorted((tdir / t).rglob("*")):
                    if f.is_file() and f.name not in ("VERSION",):
                        zf.write(f, f"{t}/{f.relative_to(tdir / t).as_posix()}")
        tmp.replace(out)
        print(f"  {out.stat().st_size / 1e6:.0f} MB: " + ", ".join(f"{k} {v}" for k, v in versions.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
