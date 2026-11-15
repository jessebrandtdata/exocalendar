"""Single-user credential handling: PBKDF2 password hashes, HTTP Basic for
calendar apps, and the signed session cookie the browser carries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from http.cookies import CookieError, SimpleCookie
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

_ITERATIONS = 600_000

SESSION_COOKIE = "exocalendar_session"
SESSION_SECONDS = 30 * 24 * 3600


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return "pbkdf2$sha256$%d$%s$%s" % (
        _ITERATIONS,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify(password: str, stored: str) -> bool:
    try:
        scheme, algo, iterations, salt_b64, digest_b64 = stored.split("$")
        if scheme != "pbkdf2" or algo != "sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


def check_basic(header_value: str | None, cfg: "Config") -> bool:
    """Validate an Authorization header against the configured user."""
    if not header_value or not header_value.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header_value[6:], validate=True).decode()
        username, _, password = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return check_password(username, password, cfg)


def check_password(username: str, password: str, cfg: "Config") -> bool:
    user_ok = hmac.compare_digest(username.encode(), cfg.username.encode())
    pass_ok = verify(password, cfg.password_hash)
    return user_ok and pass_ok


def _session_mac(expiry: int, cfg: "Config") -> str:
    # keyed by the stored hash: nothing extra to persist, a session outlives a
    # restart, and `exocalendar passwd` ends every session
    key = hashlib.sha256(b"exocalendar-session\0" + cfg.password_hash.encode()).digest()
    msg = f"{cfg.username}\0{expiry}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def new_session(cfg: "Config", now: float | None = None) -> str:
    """A token good for SESSION_SECONDS: `<expiry>.<hmac>`."""
    expiry = int(time.time() if now is None else now) + SESSION_SECONDS
    return f"{expiry}.{_session_mac(expiry, cfg)}"


def check_session(token: str | None, cfg: "Config", now: float | None = None) -> bool:
    if not token:
        return False
    expiry_text, _, mac = token.partition(".")
    if not expiry_text.isascii() or not expiry_text.isdigit():
        return False
    expiry = int(expiry_text)
    if expiry < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(mac.encode(), _session_mac(expiry, cfg).encode())


def session_from_cookie(header_value: str | None) -> str | None:
    """The session token out of a Cookie request header, if it carries one."""
    if not header_value:
        return None
    try:
        morsel = SimpleCookie(header_value).get(SESSION_COOKIE)
    except CookieError:
        return None
    return morsel.value if morsel else None


def session_cookie(token: str, secure: bool) -> str:
    """A Set-Cookie value; an empty token clears the cookie."""
    age = SESSION_SECONDS if token else 0
    value = f"{SESSION_COOKIE}={token}; Path=/; Max-Age={age}; HttpOnly; SameSite=Lax"
    return value + "; Secure" if secure else value
