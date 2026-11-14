"""config.toml load/save and first-run interactive setup.

The config stays a hand-editable TOML file; `save` writes only the keys
exocalendar owns, `load` tolerates missing optional keys.
"""

from __future__ import annotations

import getpass
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "exocalendar" / "config.toml"


def default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return Path(base) / "exocalendar"


@dataclass(frozen=True)
class Config:
    username: str
    password_hash: str
    data_dir: Path
    bind: str = "127.0.0.1"
    port: int = 5232
    tls_cert: Path | None = None
    tls_key: Path | None = None


def load(path: Path) -> Config:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    try:
        return Config(
            username=str(raw["username"]),
            password_hash=str(raw["password_hash"]),
            data_dir=Path(raw["data_dir"]).expanduser(),
            bind=str(raw.get("bind", "127.0.0.1")),
            port=int(raw.get("port", 5232)),
            tls_cert=Path(raw["tls_cert"]).expanduser() if raw.get("tls_cert") else None,
            tls_key=Path(raw["tls_key"]).expanduser() if raw.get("tls_key") else None,
        )
    except KeyError as exc:
        raise ValueError(f"config {path} is missing required key {exc}") from None


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(path: Path, cfg: Config) -> None:
    lines = [
        "# exocalendar configuration — hand-editable; restart the server to apply.",
        f"username = {_toml_str(cfg.username)}",
        f"password_hash = {_toml_str(cfg.password_hash)}  # manage with: exocalendar passwd",
        f"data_dir = {_toml_str(str(cfg.data_dir))}",
        f"bind = {_toml_str(cfg.bind)}",
        f"port = {cfg.port}",
    ]
    if cfg.tls_cert and cfg.tls_key:
        lines.append(f"tls_cert = {_toml_str(str(cfg.tls_cert))}")
        lines.append(f"tls_key = {_toml_str(str(cfg.tls_key))}")
    else:
        lines.append('# tls_cert = "/path/to/cert.pem"')
        lines.append('# tls_key = "/path/to/key.pem"')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def interactive_setup(path: Path) -> Config:
    """First-run prompt: asks for the essentials, writes config.toml."""
    from .auth import hash_password

    print("exocalendar setup — writing", path)
    username = input("Username [calendar]: ").strip() or "calendar"
    while True:
        pw = getpass.getpass("Password: ")
        if not pw:
            print("Password must not be empty.")
            continue
        if getpass.getpass("Repeat password: ") == pw:
            break
        print("Passwords do not match, try again.")
    data_dir = input(f"Data directory [{default_data_dir()}]: ").strip()
    bind = input("Bind address [127.0.0.1]: ").strip() or "127.0.0.1"
    port_raw = input("Port [5232]: ").strip()
    cfg = Config(
        username=username,
        password_hash=hash_password(pw),
        data_dir=Path(data_dir).expanduser() if data_dir else default_data_dir(),
        bind=bind,
        port=int(port_raw) if port_raw else 5232,
    )
    save(path, cfg)
    print(f"Wrote {path}. Start the server with: exocalendar serve")
    return cfg


def load_or_setup(path: Path) -> Config:
    if path.is_file():
        return load(path)
    if sys.stdin.isatty():
        return interactive_setup(path)
    raise SystemExit(
        f"No config at {path}. Run `exocalendar setup` (interactive) first, "
        f"or point --config at an existing file."
    )
