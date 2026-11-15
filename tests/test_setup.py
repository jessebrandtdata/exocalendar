import base64
import http.client
import threading
from urllib.parse import urlencode

import pytest

from exocalendar import setup as websetup
from exocalendar.auth import verify
from exocalendar.config import load
from exocalendar.server import ExoCalendarServer

FORM = {"username": "cal", "password": "pw-1", "repeat": "pw-1"}


def post(form):
    return urlencode(form).encode()


def target(tmp_path, bind="127.0.0.1", port=0):
    return websetup.SetupTarget(
        config_path=tmp_path / "cfg" / "config.toml",
        data_dir=tmp_path / "data",
        bind=bind,
        port=port,
    )


# --- who may see the setup page ------------------------------------------


@pytest.mark.parametrize(
    "client",
    ["127.0.0.1", "::1", "192.168.1.20", "10.0.0.5", "100.101.102.103", "fd7a:115c::1"],
)
def test_non_global_clients_are_allowed(client):
    assert websetup.client_allowed(client, {})


@pytest.mark.parametrize("client", ["8.8.8.8", "2606:4700::1111", "not-an-ip"])
def test_global_or_unreadable_clients_are_refused(client):
    assert not websetup.client_allowed(client, {})


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-IP"])
def test_a_proxied_request_is_refused(header):
    assert not websetup.client_allowed("127.0.0.1", {header: "203.0.113.9"})
    assert not websetup.client_allowed("127.0.0.1", {header.lower(): "203.0.113.9"})


# --- the request logic ---------------------------------------------------


def test_get_serves_the_form(tmp_path):
    status, headers, body, cfg = websetup.handle("GET", "/", b"", target(tmp_path))
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b'name="password"' in body and b'action="/setup"' in body
    assert cfg is None


def test_other_paths_say_not_set_up(tmp_path):
    status, _, body, cfg = websetup.handle("PROPFIND", "/dav/", b"", target(tmp_path))
    assert status == 503
    assert b"not set up" in body
    assert cfg is None


def test_post_writes_the_config(tmp_path):
    tgt = target(tmp_path, bind="192.168.1.20", port=5232)
    status, headers, _, cfg = websetup.handle("POST", "/setup", post(FORM), tgt)
    assert status == 303 and headers["Location"] == "/"
    assert cfg is not None
    saved = load(tgt.config_path)
    assert saved.username == "cal"
    assert verify("pw-1", saved.password_hash)
    assert saved.data_dir == tgt.data_dir
    assert (saved.bind, saved.port) == ("192.168.1.20", 5232)


def test_username_defaults_to_calendar(tmp_path):
    tgt = target(tmp_path)
    form = dict(FORM, username="  ")
    _, _, _, cfg = websetup.handle("POST", "/setup", post(form), tgt)
    assert cfg.username == "calendar"


@pytest.mark.parametrize(
    "change, message",
    [
        ({"password": "", "repeat": ""}, b"must not be empty"),
        ({"repeat": "different"}, b"do not match"),
        ({"username": "a:b"}, b"colon"),
    ],
)
def test_bad_input_writes_nothing(tmp_path, change, message):
    tgt = target(tmp_path)
    status, _, body, cfg = websetup.handle("POST", "/setup", post(dict(FORM, **change)), tgt)
    assert status == 400
    assert message in body
    assert cfg is None
    assert not tgt.config_path.exists()


def test_an_existing_config_is_never_overwritten(tmp_path):
    tgt = target(tmp_path)
    websetup.handle("POST", "/setup", post(FORM), tgt)
    before = tgt.config_path.read_text()
    status, _, _, cfg = websetup.handle(
        "POST", "/setup", post(dict(FORM, password="other", repeat="other")), tgt
    )
    assert status == 409
    assert cfg is None
    assert tgt.config_path.read_text() == before


# --- the real server, from first run to first login -----------------------


@pytest.fixture
def server(tmp_path):
    srv = ExoCalendarServer(None, setup=target(tmp_path))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def request(srv, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), data


def test_first_run_then_login(server):
    status, _, body = request(server, "GET", "/")
    assert status == 200 and b"Set up exocalendar" in body
    assert request(server, "PROPFIND", "/dav/")[0] == 503

    status, headers, _ = request(
        server,
        "POST",
        "/setup",
        body=post(FORM),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert status == 303 and headers["Location"] == "/"

    # the browser that created the account is signed in already
    cookie = {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
    assert request(server, "GET", "/", headers=cookie)[0] == 200

    # the same process now serves the calendar, behind the new password
    status, headers, _ = request(server, "GET", "/")
    assert status == 303 and headers["Location"] == "/login"
    auth = {"Authorization": "Basic " + base64.b64encode(b"cal:pw-1").decode()}
    assert request(server, "PROPFIND", "/dav/", headers=auth)[0] == 207
    # and the setup route is gone
    assert request(server, "POST", "/setup", body=post(FORM), headers=auth)[0] != 303


def test_a_proxied_first_run_is_refused(server):
    status, _, body = request(server, "GET", "/", headers={"X-Forwarded-For": "203.0.113.9"})
    assert status == 403
    assert b"exocalendar setup" in body
