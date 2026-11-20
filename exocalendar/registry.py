"""Service-registry entry: `<services_dir>/exocalendar.json`.

A launcher on the same host (exolaunch) reads this record to know where the
running server is. The record is written only where a registry directory
already exists — `$ANVIL_SERVICES_DIR`, else `~/.local/state/anvil-services`
— so a host without one is left untouched.

`public_url` in the config, when set, is the address registered instead of
the bind address: the URL of a TLS front (a reverse proxy, `tailscale serve`)
that sits ahead of the plain port.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

NAME = "exocalendar"


def services_dir() -> Path:
    env = os.environ.get("ANVIL_SERVICES_DIR")
    if env:
        return Path(env)
    return Path.home() / ".local" / "state" / "anvil-services"


def registry_url(cfg: Config | None, scheme: str, host: str, port: int) -> str:
    if cfg is not None and cfg.public_url:
        return cfg.public_url.rstrip("/") + "/"
    return f"{scheme}://{host}:{port}/"


def register(directory: Path, host: str, port: int, url: str) -> str:
    rec = {
        "name": NAME,
        "pid": os.getpid(),
        "port": port,
        "host": host,
        "url": url,
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path = Path(directory) / f"{NAME}.json"
    path.write_text(json.dumps(rec, indent=1) + "\n")
    return str(path)


def unregister(directory: Path) -> None:
    """Remove the record only if this process wrote it, so a second instance
    exiting never hides the one still serving."""
    path = Path(directory) / f"{NAME}.json"
    try:
        rec = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(rec, dict) and rec.get("pid") == os.getpid():
        path.unlink(missing_ok=True)
