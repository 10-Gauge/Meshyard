import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from scsandbox import config, server

ROOT = Path(__file__).resolve().parents[1]


def test_viewer_page_and_module_are_served(tmp_path, monkeypatch):
    monkeypatch.setenv("SCSANDBOX_HOME", str(tmp_path))
    httpd, _app, url = server.start(config.load(), port=0, auto=False)
    try:
        for path, needle in (("viewer", b"window.viewer"), ("static/viewer.js", b"export function createViewer"),
                             ("", b"/static/app.js")):
            with urllib.request.urlopen(url + path, timeout=30) as r:
                assert r.status == 200 and needle in r.read(), path
    finally:
        httpd.shutdown()


def test_engine_prints_ready_and_exits_when_stdin_closes(tmp_path):
    env = {**os.environ, "SCSANDBOX_HOME": str(tmp_path), "PYTHONUNBUFFERED": "1",
           "SCSANDBOX_NO_AUTO": "1"}
    p = subprocess.Popen([sys.executable, "-m", "scsandbox.engine"], cwd=ROOT, env=env, text=True,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        url = None
        for _ in range(200):
            line = p.stdout.readline()
            if not line:
                break
            if line.startswith("READY "):
                url = line.split(" ", 1)[1].strip()
                break
        assert url and url.startswith("http://127.0.0.1:")
        with urllib.request.urlopen(url + "api/installs", timeout=60) as r:
            assert r.status == 200
        p.stdin.close()  # what happens when the desktop app exits or is killed
        assert p.wait(timeout=30) == 0
    finally:
        if p.poll() is None:
            p.kill()
