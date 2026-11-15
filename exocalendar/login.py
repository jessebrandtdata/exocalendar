"""The browser's sign-in form.

Pure request→response logic, like `setup.py`. A browser that is not signed in
is sent here instead of being challenged for HTTP Basic: a real form is what a
password manager fills, and the session cookie it earns keeps the browser
signed in. Calendar apps never see this; they use Basic on the DAV paths.
"""

from __future__ import annotations

from html import escape
from urllib.parse import parse_qs

from .auth import check_password, new_session, session_cookie
from .config import Config

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in to exocalendar</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:26rem;margin:10vh auto;padding:0 1rem}}
label{{display:block;margin-top:1rem}}
input{{display:block;width:100%;box-sizing:border-box;font:inherit;padding:.4rem}}
button{{margin-top:1.5rem;font:inherit;padding:.5rem 1rem}}
.err{{color:#b00020}}
</style></head><body>
<h1>Sign in to exocalendar</h1>
{error}
<form method="post" action="/login">
<label>Username <input name="username" value="{username}" autocomplete="username" required autofocus></label>
<label>Password <input name="password" type="password" autocomplete="current-password" required></label>
<button type="submit">Sign in</button>
</form>
</body></html>
"""

_HTML = {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}


def _page(status: int = 200, error: str = "", username: str = ""):
    html = _PAGE.format(
        error=f'<p class="err">{escape(error)}</p>' if error else "",
        username=escape(username, quote=True),
    )
    return status, dict(_HTML), html.encode()


def _redirect(location: str, cookie: str | None = None):
    headers = {"Location": location, "Content-Type": "text/plain"}
    if cookie is not None:
        headers["Set-Cookie"] = cookie
    return 303, headers, b""


def handle(
    method: str, body: bytes, cfg: Config, signed_in: bool, secure: bool
) -> tuple[int, dict[str, str], bytes]:
    """One request to /login."""
    if method in ("GET", "HEAD"):
        return _redirect("/") if signed_in else _page()
    if method != "POST":
        return 405, {"Allow": "GET, HEAD, POST"}, b""
    form = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    username = (form.get("username") or [""])[0].strip()
    password = (form.get("password") or [""])[0]
    if not check_password(username, password, cfg):
        return _page(401, "That username and password did not match.", username)
    return _redirect("/", session_cookie(new_session(cfg), secure))


def sign_out(secure: bool) -> tuple[int, dict[str, str], bytes]:
    return _redirect("/login", session_cookie("", secure))
