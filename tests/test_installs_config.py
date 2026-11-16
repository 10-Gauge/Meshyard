import json

from scsandbox import config, installs


def test_save_writes_only_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("SCSANDBOX_HOME", str(tmp_path))
    s = config.load()
    s["install_roots"] = ["D:/Games/StarCitizen"]
    config.save(s)
    saved = json.loads(config.settings_path().read_text())
    assert saved == {"install_roots": ["D:/Games/StarCitizen"]}  # default commands not frozen in
    assert config.load()["commands"] == config.DEFAULTS["commands"]


def test_root_can_be_the_channel_folder(tmp_path):
    live = tmp_path / "StarCitizen" / "LIVE"
    live.mkdir(parents=True)
    (live / "Data.p4k").write_bytes(b"PK")
    found = [i for i in installs.discover([str(live)]) if str(tmp_path) in str(i.path)]  # this PC may have a real PTU
    assert [i.channel for i in found] == ["LIVE"]
    assert found[0].version == "unknown" and found[0].change  # no manifest still gives a build id


def test_launcher_log_paths(tmp_path, monkeypatch):
    logs = tmp_path / "rsilauncher" / "logs"
    logs.mkdir(parents=True)
    (logs / "log.log").write_text('[2026] Launching game "D:\\\\Games\\\\RSI\\\\StarCitizen\\\\LIVE\\\\Bin64\\\\StarCitizen.exe"\n'
                                  "verify E:\\SC\\StarCitizen\\PTU done\n")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    roots = [str(r) for r in installs.launcher_roots()]
    assert roots == ["D:\\Games\\RSI\\StarCitizen", "E:\\SC\\StarCitizen"]
