import base64
import http.client
import threading

import pytest

from exocalendar.auth import hash_password
from exocalendar.config import Config
from exocalendar.server import ExoCalendarServer

PASSWORD = "test-pw"


@pytest.fixture(scope="module")
def password_hash():
    return hash_password(PASSWORD)


@pytest.fixture
def server(tmp_path, password_hash):
    cfg = Config(
        username="u",
        password_hash=password_hash,
        data_dir=tmp_path / "data",
        bind="127.0.0.1",
        port=0,  # OS-assigned
    )
    srv = ExoCalendarServer(cfg)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()


def request(srv, method, path, headers=None, auth=False):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    hdrs = dict(headers or {})
    if auth:
        hdrs["Authorization"] = "Basic " + base64.b64encode(
            f"u:{PASSWORD}".encode()
        ).decode()
    conn.request(method, path, headers=hdrs)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), body


def test_dav_requires_auth(server):
    status, headers, _ = request(server, "PROPFIND", "/dav/")
    assert status == 401
    assert "Basic" in headers["WWW-Authenticate"]


def test_dav_with_auth(server):
    status, _, body = request(server, "PROPFIND", "/dav/", auth=True)
    assert status == 207
    assert b"multistatus" in body


def test_well_known_with_auth(server):
    status, headers, _ = request(server, "GET", "/.well-known/caldav", auth=True)
    assert status == 301
    assert headers["Location"] == "/dav/"


def test_api_requires_auth(server):
    status, _, _ = request(server, "GET", "/api/calendars")
    assert status == 401


def test_static_requires_auth(server):
    status, _, _ = request(server, "GET", "/")
    assert status == 401


def test_feed_is_exempt_from_basic_auth(server):
    # wrong/absent token means 404, but never 401
    status, _, _ = request(server, "GET", "/feed/nope.ics?t=x")
    assert status == 404


def test_wrong_password_rejected(server):
    bad = "Basic " + base64.b64encode(b"u:wrong").decode()
    status, _, _ = request(server, "PROPFIND", "/dav/", headers={"Authorization": bad})
    assert status == 401


def test_no_auth_refused_on_public_bind(tmp_path, password_hash):
    cfg = Config(
        username="u", password_hash=password_hash,
        data_dir=tmp_path / "d", bind="0.0.0.0", port=0,
    )
    with pytest.raises(SystemExit):
        ExoCalendarServer(cfg, no_auth=True)


def test_no_auth_on_loopback(tmp_path, password_hash):
    cfg = Config(
        username="u", password_hash=password_hash,
        data_dir=tmp_path / "d", bind="127.0.0.1", port=0,
    )
    srv = ExoCalendarServer(cfg, no_auth=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, _ = request(srv, "PROPFIND", "/dav/")
        assert status == 207
    finally:
        srv.shutdown()
