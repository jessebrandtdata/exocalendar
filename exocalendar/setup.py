"""First run in the browser: the one page a server with no config serves.

`exocalendar serve` started without a config and without a terminal (a
service manager, a launcher) cannot prompt, so it serves this form instead
of exiting. Submitting it writes the same config.toml `exocalendar setup`
writes, and the running server switches to the calendar.

Pure request -> response logic, like `dav.py`: no sockets here.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import parse_qs

from .auth import hash_password
from .config import Config, save

# Headers a reverse proxy adds. Behind one, every client looks like the
# proxy's own address, so the address test below would pass for anyone.
_PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip")


@dataclass(frozen=True)
class SetupTarget:
    """Where the config will be written, and the address this server is on."""

    config_path: Path
    data_dir: Path
    bind: str
    port: int


def client_allowed(client_ip: str, headers: dict[str, str]) -> bool:
    """May this client create the account? Only from a non-global address
    (loopback, a private LAN, a VPN's shared range) and never through a
    proxy: whoever submits the form first owns the calendar."""
    if any(name.lower() in _PROXY_HEADERS for name in headers):
        return False
    try:
        return not ip_address(client_ip.split("%", 1)[0]).is_global
    except ValueError:
        return False


REFUSED = (
    403,
    {"Content-Type": "text/plain; charset=utf-8"},
    b"exocalendar is not set up yet, and first-run setup in the browser is only "
    b"offered to a direct connection from a private address. "
    b"Run `exocalendar setup` on the host instead.\n",
)

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Set up exocalendar</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:26rem;margin:10vh auto;padding:0 1rem}}
label{{display:block;margin-top:1rem}}
input{{display:block;width:100%;box-sizing:border-box;font:inherit;padding:.4rem}}
button{{margin-top:1.5rem;font:inherit;padding:.5rem 1rem}}
.err{{color:#b00020}} .note{{color:#555;font-size:.9rem}}
</style></head><body>
<h1>Set up exocalendar</h1>
<p>Choose the account this calendar answers to. Your calendar apps sign in
with the same username and password.</p>
{error}
<form method="post" action="/setup">
<label>Username <input name="username" value="{username}" autocomplete="username"></label>
<label>Password <input name="password" type="password" autocomplete="new-password" required></label>
<label>Repeat password <input name="repeat" type="password" autocomplete="new-password" required></label>
<button type="submit">Create account</button>
</form>
<p class="note">Written to {config_path}. Calendars are stored in {data_dir}.</p>
</body></html>
"""


def _page(target: SetupTarget, status: int = 200, error: str = "", username: str = "calendar"):
    html = _PAGE.format(
        error=f'<p class="err">{escape(error)}</p>' if error else "",
        username=escape(username, quote=True),
        config_path=escape(str(target.config_path)),
        data_dir=escape(str(target.data_dir)),
    )
    return status, {"Content-Type": "text/html; charset=utf-8"}, html.encode(), None


def handle(
    method: str, path: str, body: bytes, target: SetupTarget
) -> tuple[int, dict[str, str], bytes, Config | None]:
    """One request to a server that has no config yet. The fourth element is
    the new Config once the form has been accepted and written, else None."""
    route = path.split("?", 1)[0]
    if method in ("GET", "HEAD") and route in ("/", "/setup"):
        return _page(target)
    if method == "POST" and route == "/setup":
        return _submit(body, target)
    return (
        503,
        {"Content-Type": "text/plain; charset=utf-8", "Retry-After": "30"},
        b"exocalendar is not set up yet. Open / in a browser to create the account.\n",
        None,
    )


def _submit(body: bytes, target: SetupTarget):
    form = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    username = (form.get("username") or [""])[0].strip() or "calendar"
    password = (form.get("password") or [""])[0]
    repeat = (form.get("repeat") or [""])[0]

    if target.config_path.exists():
        return (
            409,
            {"Content-Type": "text/plain; charset=utf-8"},
            b"exocalendar is already set up. Change the password with `exocalendar passwd`.\n",
            None,
        )
    if ":" in username:
        # HTTP Basic splits credentials at the first colon
        return _page(target, 400, "A username cannot contain a colon.", username)
    if not password:
        return _page(target, 400, "Password must not be empty.", username)
    if password != repeat:
        return _page(target, 400, "Passwords do not match.", username)

    cfg = Config(
        username=username,
        password_hash=hash_password(password),
        data_dir=target.data_dir,
        bind=target.bind,
        port=target.port,
    )
    save(target.config_path, cfg)
    return 303, {"Location": "/", "Content-Type": "text/plain"}, b"", cfg
