"""The browser signs in through a form and a session cookie; calendar apps
keep HTTP Basic on the DAV paths."""

import http.client
import threading
from urllib.parse import urlencode

import pytest

from exocalendar import auth
from exocalendar.auth import hash_password
from exocalendar.config import Config
from exocalendar.server import ExoCalendarServer

PASSWORD = "test-pw"


@pytest.fixture(scope="module")
def password_hash():
    return hash_password(PASSWORD)


@pytest.fixture
def cfg(tmp_path, password_hash):
    return Config(
        username="u",
        password_hash=password_hash,
        data_dir=tmp_path / "data",
        bind="127.0.0.1",
        port=0,
    )


@pytest.fixture
def server(cfg):
    srv = ExoCalendarServer(cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def request(srv, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request(method, path, body=body, headers=dict(headers or {}))
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), data


def sign_in(srv, username="u", password=PASSWORD):
    return request(
        srv,
        "POST",
        "/login",
        body=urlencode({"username": username, "password": password}),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def cookie_of(headers):
    return headers["Set-Cookie"].split(";", 1)[0]


# --- the session token --------------------------------------------------------

def test_a_token_verifies_until_it_expires(cfg):
    token = auth.new_session(cfg, now=1000)
    assert auth.check_session(token, cfg, now=1000 + auth.SESSION_SECONDS - 1)
    assert not auth.check_session(token, cfg, now=1000 + auth.SESSION_SECONDS + 1)


def test_a_tampered_token_is_refused(cfg):
    expiry, _, mac = auth.new_session(cfg, now=1000).partition(".")
    assert not auth.check_session(f"{int(expiry) + 9999}.{mac}", cfg, now=1000)
    assert not auth.check_session("garbage", cfg, now=1000)
    assert not auth.check_session("", cfg, now=1000)
    assert not auth.check_session(None, cfg, now=1000)


def test_a_password_change_ends_every_session(cfg):
    token = auth.new_session(cfg, now=1000)
    changed = Config(**{**cfg.__dict__, "password_hash": hash_password("other")})
    assert not auth.check_session(token, changed, now=1000)


# --- pages --------------------------------------------------------------------

def test_a_signed_out_page_request_goes_to_the_form(server):
    status, headers, _ = request(server, "GET", "/")
    assert status == 303
    assert headers["Location"] == "/login"
    assert "WWW-Authenticate" not in headers  # no native browser dialog


def test_the_form_is_one_a_password_manager_fills(server):
    status, headers, body = request(server, "GET", "/login")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    html = body.decode()
    assert 'method="post"' in html and 'action="/login"' in html
    assert 'autocomplete="username"' in html
    assert 'type="password"' in html and 'autocomplete="current-password"' in html


def test_signing_in_sets_a_cookie_that_opens_the_app(server):
    status, headers, _ = sign_in(server)
    assert status == 303
    assert headers["Location"] == "/"
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Path=/" in cookie
    assert "Secure" not in cookie  # this server speaks plain HTTP

    status, _, body = request(server, "GET", "/", headers={"Cookie": cookie_of(headers)})
    assert status == 200
    assert b"exocalendar" in body
    status, _, _ = request(
        server, "GET", "/api/calendars", headers={"Cookie": cookie_of(headers)}
    )
    assert status == 200


@pytest.mark.parametrize("username,password", [("u", "wrong"), ("other", PASSWORD), ("", "")])
def test_wrong_credentials_get_the_form_back(server, username, password):
    status, headers, body = sign_in(server, username, password)
    assert status == 401
    assert "Set-Cookie" not in headers
    assert "WWW-Authenticate" not in headers
    assert b"did not match" in body


def test_a_forged_cookie_does_not_open_the_app(server):
    status, _, _ = request(
        server, "GET", "/", headers={"Cookie": "exocalendar_session=9999999999.abcd"}
    )
    assert status == 303


def test_the_form_sends_a_signed_in_browser_on(server):
    _, headers, _ = sign_in(server)
    status, headers, _ = request(
        server, "GET", "/login", headers={"Cookie": cookie_of(headers)}
    )
    assert status == 303 and headers["Location"] == "/"


def test_signing_out_clears_the_cookie(server):
    _, headers, _ = sign_in(server)
    status, headers, _ = request(
        server, "POST", "/logout", headers={"Cookie": cookie_of(headers)}
    )
    assert status == 303 and headers["Location"] == "/login"
    assert headers["Set-Cookie"].startswith("exocalendar_session=;")
    assert "Max-Age=0" in headers["Set-Cookie"]


# --- the API and the DAV paths ------------------------------------------------

def test_a_signed_out_api_call_is_401_without_a_browser_dialog(server):
    status, headers, _ = request(server, "GET", "/api/calendars")
    assert status == 401
    assert "WWW-Authenticate" not in headers


def test_dav_still_challenges_for_basic(server):
    for path in ("/dav/", "/.well-known/caldav"):
        status, headers, _ = request(server, "PROPFIND", path)
        assert status == 401
        assert "Basic" in headers["WWW-Authenticate"]


def test_a_cookie_write_from_another_origin_is_refused(server):
    _, headers, _ = sign_in(server)
    port = server.server_address[1]
    base = {"Cookie": cookie_of(headers), "Content-Type": "application/json"}
    body = '{"name": "x"}'

    status, _, _ = request(
        server, "POST", "/api/calendars", body=body,
        headers={**base, "Origin": "http://elsewhere.example"},
    )
    assert status == 403
    status, _, _ = request(
        server, "POST", "/api/calendars", body=body,
        headers={**base, "Origin": f"http://127.0.0.1:{port}"},
    )
    assert status in (200, 201)
