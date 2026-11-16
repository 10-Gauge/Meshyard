import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SCSANDBOX_HOME", str(home))
    root = tmp_path / "StarCitizen"
    for ch, ver, change in (("LIVE", "4.3.1-live.10234567", "10234567"), ("PTU", "4.4.0-ptu.10300001", "10300001")):
        d = root / ch
        d.mkdir(parents=True)
        (d / "Data.p4k").write_bytes(b"PK")
        (d / "build_manifest.id").write_text(json.dumps({"Data": {"Branch": f"sc-alpha-{ver[:5]}", "Version": ver,
                                                                    "RequestedP4ChangeNum": change}}))
    from scsandbox import config
    s = config.load()
    s["install_roots"] = [str(root)]
    fake = FIXTURES / "fake_starbreaker.py"
    if sys.platform == "win32":  # .py files aren't directly executable on Windows
        wrapper = tmp_path / "fake_starbreaker.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{fake}" %*\r\n')
        fake = wrapper
    s["tools"]["starbreaker"] = str(fake)
    s["print"]["voxel_mm"] = 0.5
    config.save(s)
    return {"home": home, "root": root, "settings": config.load()}


def test_discovers_live_and_ptu(env):
    from scsandbox import installs
    found = installs.discover(env["settings"]["install_roots"])
    assert [i.channel for i in found] == ["LIVE", "PTU"]
    assert found[0].build_id == "4.3.1-live.10234567-10234567"


def test_index_per_channel_and_new_build(env):
    from scsandbox import catalog, installs
    s = env["settings"]
    live, ptu = installs.pick("LIVE", s), installs.pick("PTU", s)
    catalog.build(live, s, log=lambda *_: None)
    catalog.build(ptu, s, log=lambda *_: None)
    names = lambda inst: {r["name"] for r in catalog.search(catalog.connect(inst), kind="entity")[1]}
    assert names(live) == {"Aegis Gladius", "Anvil Arrow", "Klaus & Werner Laser Repeater"}
    assert catalog.search(catalog.connect(live), kind="entity")[1][0]["category"] == "spaceships"  # ships before parts
    assert "RSI Zeus Mk II CL" in names(ptu)
    g = catalog.search(catalog.connect(live), "gladius", kind="entity")[1][0]
    assert g["manufacturer"] == "AEGS" and g["description"].startswith("A light fighter.\nFast")
    assert "AEGS_Gladius.cga" in g["geometry"]
    total_raw = catalog.search(catalog.connect(live), kind="geometry")[0]
    assert total_raw == 2  # listed once per pattern, stored once per path
    # A patch lands: new change number -> new build id -> not current until re-indexed.
    (live.path / "build_manifest.id").write_text(json.dumps({"Data": {"Version": "4.3.2-live.1", "RequestedP4ChangeNum": "10400000"}}))
    patched = installs.pick("LIVE", s)
    assert patched.build_id != live.build_id and not catalog.is_current(patched)
    catalog.build(patched, s, log=lambda *_: None)
    assert catalog.is_current(patched)


def test_server_print_flow(env):
    from scsandbox import catalog, installs, server
    s = env["settings"]
    catalog.build(installs.pick("LIVE", s), s, log=lambda *_: None)
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(server.App(s)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(path):
        with urllib.request.urlopen(base + path) as r:
            return r.read() if not r.headers["Content-Type"].startswith("application/json") else json.load(r)

    def post(path, body):
        req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.load(r)

    def wait(job):
        for _ in range(300):
            j = get(f"/api/jobs/{job}")
            if j["state"] != "running":
                assert j["state"] == "done", j
                return j["result"]
            time.sleep(0.2)
        raise AssertionError("job timed out")

    assert b"SC Asset Browser" in get("/")
    info = get("/api/installs")
    assert [i["indexed"] for i in info["installs"]] == [True, False]
    hits = get("/api/assets?channel=LIVE&q=gladius&kind=entity")
    assert hits["total"] == 1
    aid = hits["items"][0]["id"]

    model = wait(post("/api/model", {"channel": "LIVE", "id": aid, "variant": "preview"})["job"])
    assert get(model["url"])[:4] == b"glTF"

    rep = wait(post("/api/print", {"channel": "LIVE", "id": aid, "options": {"size_mm": 120, "format": "stl"}})["job"])
    assert rep["watertight"] and abs(max(rep["size_mm"]) - 120) < 3
    stl = get(rep["downloads"][0])
    assert len(stl) > 1000

    thin = wait(post("/api/print/analyze", {"channel": "LIVE", "id": aid, "options": {"size_mm": 120}})["job"])
    assert "slender" in thin and thin["min_feature_mm"] == 1.2
    rep = wait(post("/api/print", {"channel": "LIVE", "id": aid, "options": {
        "size_mm": 120, "format": "3mf", "stand": "peg", "stand_height_mm": 20, "nozzle_mm": 0.4,
        "min_feature_nozzles": 3, "thicken": True, "tilt_deg": 10, "part_overrides": {"nope": {"action": "remove"}}}})["job"])
    assert rep["bodies"] == 2 and rep["stand"] == "peg"

    exp = wait(post("/api/export", {"channel": "LIVE", "id": aid, "format": "obj", "textures": True})["job"])
    assert exp["url"].startswith("/files/export/models/") and len(get(exp["url"])) > 100

    spawned = wait(post("/api/model", {"channel": "LIVE", "id": aid, "variant": "full", "spawn": True})["job"])
    q = get("/api/sandbox/queue?after=0")["items"]
    assert q[-1]["record"] == "AEGS_Gladius" and q[-1]["glb"] == spawned["path"]

    anim = wait(post("/api/model", {"channel": "LIVE", "id": aid, "variant": "animated", "spawn": True})["job"])
    assert anim["path"].endswith("scene.json") and anim["url"] is None
    assert get("/api/sandbox/queue?after=0")["items"][-1]["glb"] == anim["path"]

    # PTU not indexed yet -> 409 with needs_index so the page can say so
    with pytest.raises(urllib.error.HTTPError) as e:
        get("/api/assets?channel=PTU")
    assert e.value.code == 409
    # path traversal is refused
    with pytest.raises(urllib.error.HTTPError) as e:
        get("/files/export/..%2Fsettings.json")
    assert e.value.code == 404
    httpd.shutdown()


def test_cli_print(env, capsys):
    from scsandbox import cli
    assert cli.main(["update", "--channel", "PTU"]) == 0
    assert cli.main(["print", "zeus", "-c", "PTU", "--size", "90", "--mode", "parts"]) == 0
    out = list((env["home"] / "exports").glob("*.3mf"))
    assert out and "Zeus" in out[0].name


def test_desktop_add_game_folder(env, tmp_path):
    import urllib.error
    from scsandbox import config, server
    s = env["settings"]
    s["install_roots"] = []
    config.save(s)
    httpd, app, url = server.start(s, port=0, auto=False)
    try:
        def post(path, body):
            req = urllib.request.Request(url + path.lstrip("/"), data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        assert post("/api/roots", {"path": str(tmp_path / "nope")})[0] == 404
        empty = tmp_path / "empty"
        empty.mkdir()
        assert post("/api/roots", {"path": str(empty)})[0] == 404  # no LIVE/PTU in here
        code, r = post("/api/roots", {"path": str(tmp_path)})  # the folder above StarCitizen works too
        assert code == 200 and r["root"] == str(env["root"])
        assert r["root"] in config.load()["install_roots"]
        for _ in range(100):  # adding the folder kicks off indexing
            if all(app.index_state.get(c, {}).get("state") == "done" for c in ("LIVE", "PTU")):
                break
            time.sleep(0.2)
        assert app.index_state["LIVE"]["state"] == "done"
    finally:
        httpd.shutdown()
