import json
import os
from pathlib import Path

from exocalendar.auth import hash_password
from exocalendar.config import Config, load, save
from exocalendar.registry import register, registry_url, unregister


def _cfg(tmp_path, **kw):
    return Config(username="u", password_hash=hash_password("pw"), data_dir=tmp_path / "d", **kw)


def test_public_url_round_trips(tmp_path):
    cfg = _cfg(tmp_path, public_url="https://box.example.ts.net:8451")
    path = tmp_path / "config.toml"
    save(path, cfg)
    assert load(path) == cfg


def test_public_url_defaults_to_none(tmp_path):
    path = tmp_path / "config.toml"
    save(path, _cfg(tmp_path))
    assert load(path).public_url is None


def test_registry_url_prefers_public_url(tmp_path):
    cfg = _cfg(tmp_path, public_url="https://box.example.ts.net:8451")
    assert registry_url(cfg, "http", "100.64.0.1", 5232) == "https://box.example.ts.net:8451/"


def test_registry_url_falls_back_to_bind_address(tmp_path):
    assert registry_url(_cfg(tmp_path), "http", "100.64.0.1", 5232) == "http://100.64.0.1:5232/"
    assert registry_url(None, "http", "127.0.0.1", 5232) == "http://127.0.0.1:5232/"


def test_register_writes_the_exolaunch_record(tmp_path):
    path = register(tmp_path, "100.64.0.1", 5232, "https://box.example.ts.net:8451/")
    rec = json.loads(Path(path).read_text())
    assert rec["name"] == "exocalendar"
    assert rec["pid"] == os.getpid()
    assert rec["port"] == 5232
    assert rec["host"] == "100.64.0.1"
    assert rec["url"] == "https://box.example.ts.net:8451/"
    assert rec["started"]


def test_unregister_removes_only_its_own_record(tmp_path):
    register(tmp_path, "h", 1, "http://h:1/")
    unregister(tmp_path)
    assert not (tmp_path / "exocalendar.json").exists()
    # a record another process wrote (a second instance's) is left alone
    (tmp_path / "exocalendar.json").write_text(json.dumps({"name": "exocalendar", "pid": 1}))
    unregister(tmp_path)
    assert (tmp_path / "exocalendar.json").exists()
    unregister(tmp_path / "missing")  # no directory: no error


def test_serve_registers_public_url_and_cleans_up(tmp_path, monkeypatch):
    from exocalendar import server as srvmod

    reg = tmp_path / "services"
    reg.mkdir()
    monkeypatch.setenv("ANVIL_SERVICES_DIR", str(reg))
    seen = {}

    def fake_forever(self, *a, **kw):
        seen["rec"] = json.loads((reg / "exocalendar.json").read_text())
        raise KeyboardInterrupt

    monkeypatch.setattr(srvmod.ExoCalendarServer, "serve_forever", fake_forever)
    monkeypatch.setattr(srvmod.ExoCalendarServer, "shutdown", lambda self: None)
    cfg = _cfg(tmp_path, port=0, public_url="https://box.example.ts.net:8451/")
    srvmod.serve(cfg)
    assert seen["rec"]["url"] == "https://box.example.ts.net:8451/"
    assert not (reg / "exocalendar.json").exists()


def test_serve_leaves_a_host_without_a_registry_alone(tmp_path, monkeypatch):
    from exocalendar import server as srvmod

    reg = tmp_path / "absent"
    monkeypatch.setenv("ANVIL_SERVICES_DIR", str(reg))
    monkeypatch.setattr(
        srvmod.ExoCalendarServer, "serve_forever", lambda self, *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt)
    )
    monkeypatch.setattr(srvmod.ExoCalendarServer, "shutdown", lambda self: None)
    srvmod.serve(_cfg(tmp_path, port=0))
    assert not reg.exists()


def test_sigterm_removes_the_record(tmp_path):
    import subprocess
    import sys
    import time

    reg = tmp_path / "services"
    reg.mkdir()
    cfgpath = tmp_path / "config.toml"
    save(cfgpath, _cfg(tmp_path, port=0))
    env = {**os.environ, "ANVIL_SERVICES_DIR": str(reg)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "exocalendar", "--config", str(cfgpath), "serve"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if (reg / "exocalendar.json").exists():
                break
            time.sleep(0.05)
        assert (reg / "exocalendar.json").exists()
        proc.terminate()
        proc.wait(timeout=10)
    finally:
        proc.kill()
    assert not (reg / "exocalendar.json").exists()
