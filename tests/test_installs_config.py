import json

from scsandbox import config, installs


def test_save_writes_only_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHYARD_HOME", str(tmp_path))
    s = config.load()
    s["install_roots"] = ["D:/Games/SomeGame"]
    config.save(s)
    saved = json.loads(config.settings_path().read_text())
    assert saved == {"install_roots": ["D:/Games/SomeGame"]}  # default commands not frozen in
    assert config.load()["commands"] == config.DEFAULTS["commands"]


def test_root_can_be_the_channel_folder(tmp_path):
    live = tmp_path / "SomeGame" / "LIVE"
    live.mkdir(parents=True)
    (live / "Data.p4k").write_bytes(b"PK")
    found = [i for i in installs.discover([str(live)]) if str(tmp_path) in str(i.path)]  # this PC may have a real PTU
    assert [i.channel for i in found] == ["LIVE"]
    assert found[0].version == "unknown" and found[0].change  # no manifest still gives a build id


def test_launcher_log_paths(tmp_path, monkeypatch):
    logs = tmp_path / "rsilauncher" / "logs"
    logs.mkdir(parents=True)
    (logs / "log.log").write_text('[2026] Launching game "D:\\\\Games\\\\Publisher\\\\SomeGame\\\\LIVE\\\\Bin64\\\\Game.exe"\n'
                                  "verify E:\\SC\\SomeGame\\PTU done\n")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    roots = [str(r) for r in installs.launcher_roots()]
    assert roots == ["D:\\Games\\Publisher\\SomeGame", "E:\\SC\\SomeGame"]


def test_find_data_files_and_root_of(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHYARD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(installs, "_scan_cache", (1e18, []))  # skip the drive scan
    game = tmp_path / "Games" / "SomeGame"
    for ch in ("LIVE", "PTU"):
        (game / ch).mkdir(parents=True)
        (game / ch / "Data.p4k").write_bytes(b"PK")
    files = installs.find_data_files([str(game)])
    assert files == [game / "LIVE" / "Data.p4k", game / "PTU" / "Data.p4k"]
    assert installs.root_of(files[0].parent) == game
    assert installs.root_of(game) == game


def test_scan_finds_data_a_few_levels_down(tmp_path):
    deep = tmp_path / "Publisher" / "Game" / "LIVE"
    deep.mkdir(parents=True)
    (deep / "Data.p4k").write_bytes(b"PK")
    (tmp_path / "Windows" / "x" / "LIVE").mkdir(parents=True)
    (tmp_path / "Windows" / "x" / "LIVE" / "Data.p4k").write_bytes(b"PK")  # system folders are skipped
    assert installs._scan_for_data(tmp_path, depth=4) == [deep]


def test_old_data_folder_moves_over(tmp_path, monkeypatch):
    monkeypatch.delenv("MESHYARD_HOME", raising=False)
    monkeypatch.delenv("SCSANDBOX_HOME", raising=False)
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(config, "_migrated", False)
    old = tmp_path / "sc-sandbox"
    (old / "exports").mkdir(parents=True)
    (old / "settings.json").write_text(json.dumps({"tools": {"starbreaker": str(old / "tools" / "sb.exe")}}))
    home = config.home()
    assert home == tmp_path / "Meshyard" and not old.exists()
    assert (home / "exports").is_dir()
    assert json.loads((home / "settings.json").read_text())["tools"]["starbreaker"] == str(home / "tools" / "sb.exe")
