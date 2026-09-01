from pathlib import Path

import pytest

from exocalendar.auth import check_basic, hash_password, verify
from exocalendar.config import Config, load, save


def test_password_hash_verify():
    stored = hash_password("hunter2")
    assert stored.startswith("pbkdf2$sha256$")
    assert verify("hunter2", stored)
    assert not verify("hunter3", stored)
    assert not verify("", stored)
    # unique salts
    assert hash_password("hunter2") != stored


def test_verify_rejects_malformed():
    assert not verify("x", "garbage")
    assert not verify("x", "pbkdf2$sha256$notanumber$AA$BB")


def test_check_basic():
    import base64

    cfg = Config(username="u", password_hash=hash_password("pw"), data_dir=Path("/tmp/x"))
    ok = "Basic " + base64.b64encode(b"u:pw").decode()
    assert check_basic(ok, cfg)
    bad_pw = "Basic " + base64.b64encode(b"u:nope").decode()
    assert not check_basic(bad_pw, cfg)
    bad_user = "Basic " + base64.b64encode(b"x:pw").decode()
    assert not check_basic(bad_user, cfg)
    assert not check_basic(None, cfg)
    assert not check_basic("Bearer xyz", cfg)
    assert not check_basic("Basic !!!notb64!!!", cfg)


def test_config_round_trip(tmp_path):
    cfg = Config(
        username="jane",
        password_hash=hash_password("pw"),
        bind="0.0.0.0",
        port=8321,
        data_dir=tmp_path / "data",
        tls_cert=tmp_path / "cert.pem",
        tls_key=tmp_path / "key.pem",
    )
    path = tmp_path / "config.toml"
    save(path, cfg)
    got = load(path)
    assert got == cfg


def test_config_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'username = "u"\npassword_hash = "pbkdf2$sha256$1$aa$bb"\n'
        f'data_dir = "{tmp_path}/d"\n'
    )
    cfg = load(path)
    assert cfg.bind == "127.0.0.1"
    assert cfg.port == 5232
    assert cfg.tls_cert is None and cfg.tls_key is None


def test_config_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load(tmp_path / "nope.toml")
