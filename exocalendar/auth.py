"""Single-user credential handling: PBKDF2 password hashes and HTTP Basic."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

_ITERATIONS = 600_000


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
    user_ok = hmac.compare_digest(username.encode(), cfg.username.encode())
    pass_ok = verify(password, cfg.password_hash)
    return user_ok and pass_ok
