import xml.etree.ElementTree as ET

import pytest

from exocalendar.dav import DavHandlerLogic
from exocalendar.store import Store

D = "{DAV:}"
C = "{urn:ietf:params:xml:ns:caldav}"
CS = "{http://calendarserver.org/ns/}"
A = "{http://apple.com/ns/ical/}"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def dav(store):
    return DavHandlerLogic(store)


def req(dav, method, path, body="", depth="0", headers=None):
    hdrs = {"Depth": depth}
    hdrs.update(headers or {})
    return dav.handle(method, path, hdrs, body.encode() if isinstance(body, str) else body)


def multistatus(body: bytes) -> ET.Element:
    root = ET.fromstring(body)
    assert root.tag == f"{D}multistatus"
    return root


def hrefs_of(root) -> list[str]:
    return [r.findtext(f"{D}href") for r in root.findall(f"{D}response")]


def prop_of(root, href, tag):
    for resp in root.findall(f"{D}response"):
        if resp.findtext(f"{D}href") == href:
            for ps in resp.findall(f"{D}propstat"):
                if "200" in ps.findtext(f"{D}status", ""):
                    el = ps.find(f"{D}prop/{tag}")
                    if el is not None:
                        return el
    return None


EVENT = """BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//exocalendar//EN\r
BEGIN:VEVENT\r
UID:{uid}\r
DTSTART:20260601T100000Z\r
DTEND:20260601T110000Z\r
SUMMARY:{summary}\r
END:VEVENT\r
END:VCALENDAR\r
"""

RECURRING = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:rec1\r
DTSTART:20260101T090000Z\r
DTEND:20260101T093000Z\r
RRULE:FREQ=DAILY\r
SUMMARY:Daily\r
END:VEVENT\r
END:VCALENDAR\r
"""


# --- discovery ---------------------------------------------------------------

def test_well_known_redirect(dav):
    status, headers, _ = req(dav, "GET", "/.well-known/caldav")
    assert status == 301
    assert headers["Location"] == "/dav/"


def test_options_advertises_caldav(dav):
    status, headers, _ = req(dav, "OPTIONS", "/dav/")
    assert status == 200
    assert "calendar-access" in headers["DAV"]
    assert "REPORT" in headers["Allow"]


def test_propfind_root_current_user_principal(dav):
    body = (
        '<?xml version="1.0"?><d:propfind xmlns:d="DAV:">'
        "<d:prop><d:current-user-principal/></d:prop></d:propfind>"
    )
    status, _, out = req(dav, "PROPFIND", "/dav/", body)
    assert status == 207
    root = multistatus(out)
    el = prop_of(root, "/dav/", f"{D}current-user-principal")
    assert el.findtext(f"{D}href") == "/dav/u/"


def test_propfind_principal_home_set(dav):
    body = (
        '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" '
        'xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><c:calendar-home-set/><d:resourcetype/></d:prop></d:propfind>"
    )
    status, _, out = req(dav, "PROPFIND", "/dav/u/", body)
    root = multistatus(out)
    home = prop_of(root, "/dav/u/", f"{C}calendar-home-set")
    assert home.findtext(f"{D}href") == "/dav/u/"
    rtype = prop_of(root, "/dav/u/", f"{D}resourcetype")
    assert rtype.find(f"{D}principal") is not None


def test_propfind_home_depth1_lists_calendars(dav, store):
    store.create_calendar("work", "Work", "#1b9e77")
    body = (
        '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" '
        'xmlns:cs="http://calendarserver.org/ns/" xmlns:a="http://apple.com/ns/ical/">'
        "<d:prop><d:resourcetype/><d:displayname/><cs:getctag/>"
        "<a:calendar-color/></d:prop></d:propfind>"
    )
    status, _, out = req(dav, "PROPFIND", "/dav/u/", body, depth="1")
    root = multistatus(out)
    assert "/dav/u/work/" in hrefs_of(root)
    rtype = prop_of(root, "/dav/u/work/", f"{D}resourcetype")
    assert rtype.find(f"{C}calendar") is not None
    assert prop_of(root, "/dav/u/work/", f"{D}displayname").text == "Work"
    assert prop_of(root, "/dav/u/work/", f"{CS}getctag").text
    assert prop_of(root, "/dav/u/work/", f"{A}calendar-color").text == "#1b9e77"


def test_propfind_unknown_prop_404(dav):
    body = (
        '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:x="urn:x">'
        "<d:prop><x:nope/></d:prop></d:propfind>"
    )
    status, _, out = req(dav, "PROPFIND", "/dav/", body)
    root = multistatus(out)
    resp = root.find(f"{D}response")
    statuses = [ps.findtext(f"{D}status") for ps in resp.findall(f"{D}propstat")]
    assert any("404" in s for s in statuses)


def test_propfind_missing_path_404(dav):
    status, _, _ = req(dav, "PROPFIND", "/dav/u/ghost/", "")
    assert status == 404


# --- calendar management -----------------------------------------------------

def test_mkcalendar(dav, store):
    body = (
        '<?xml version="1.0"?><c:mkcalendar xmlns:d="DAV:" '
        'xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:a="http://apple.com/ns/ical/">'
        "<d:set><d:prop><d:displayname>Family</d:displayname>"
        "<a:calendar-color>#7570b3</a:calendar-color></d:prop></d:set></c:mkcalendar>"
    )
    status, _, _ = req(dav, "MKCALENDAR", "/dav/u/family/", body)
    assert status == 201
    info = store.get_calendar("family")
    assert info.displayname == "Family"
    assert info.color == "#7570b3"
    # creating again fails
    status, _, _ = req(dav, "MKCALENDAR", "/dav/u/family/", body)
    assert status == 405


def test_proppatch(dav, store):
    store.create_calendar("work", "Work", "#1b9e77")
    body = (
        '<?xml version="1.0"?><d:propertyupdate xmlns:d="DAV:" '
        'xmlns:a="http://apple.com/ns/ical/"><d:set><d:prop>'
        "<d:displayname>Job</d:displayname>"
        "<a:calendar-color>#d95f02</a:calendar-color>"
        "</d:prop></d:set></d:propertyupdate>"
    )
    status, _, out = req(dav, "PROPPATCH", "/dav/u/work/", body)
    assert status == 207
    info = store.get_calendar("work")
    assert info.displayname == "Job"
    assert info.color == "#d95f02"


def test_delete_calendar(dav, store):
    store.create_calendar("work", "Work", "#1b9e77")
    status, _, _ = req(dav, "DELETE", "/dav/u/work/")
    assert status == 204
    assert store.get_calendar("work") is None


# --- resources ---------------------------------------------------------------

def test_put_get_delete_resource(dav, store):
    store.create_calendar("cal", "Cal", "#111111")
    ics = EVENT.format(uid="e1", summary="Hi")
    status, headers, _ = req(dav, "PUT", "/dav/u/cal/e1.ics", ics)
    assert status == 201
    etag = headers["ETag"]
    assert etag.startswith('"')

    status, headers, out = req(dav, "GET", "/dav/u/cal/e1.ics")
    assert status == 200
    assert headers["ETag"] == etag
    assert "text/calendar" in headers["Content-Type"]
    assert out.decode() == ics

    status, headers, _ = req(dav, "PUT", "/dav/u/cal/e1.ics", EVENT.format(uid="e1", summary="Edit"))
    assert status == 204
    assert headers["ETag"] != etag

    status, _, _ = req(dav, "DELETE", "/dav/u/cal/e1.ics")
    assert status == 204
    status, _, _ = req(dav, "GET", "/dav/u/cal/e1.ics")
    assert status == 404


def test_put_preconditions(dav, store):
    store.create_calendar("cal", "Cal", "#111111")
    ics = EVENT.format(uid="e1", summary="Hi")
    _, headers, _ = req(dav, "PUT", "/dav/u/cal/e1.ics", ics)
    etag = headers["ETag"]
    # If-None-Match * on existing -> 412
    status, _, _ = req(dav, "PUT", "/dav/u/cal/e1.ics", ics, headers={"If-None-Match": "*"})
    assert status == 412
    # If-Match wrong -> 412
    status, _, _ = req(
        dav, "PUT", "/dav/u/cal/e1.ics", ics, headers={"If-Match": '"beef"'}
    )
    assert status == 412
    # If-Match right -> ok
    status, _, _ = req(
        dav, "PUT", "/dav/u/cal/e1.ics",
        EVENT.format(uid="e1", summary="Two"), headers={"If-Match": etag},
    )
    assert status == 204
    # garbage body -> 400
    status, _, _ = req(dav, "PUT", "/dav/u/cal/e2.ics", "junk")
    assert status == 400


def test_put_to_missing_calendar_404(dav):
    status, _, _ = req(dav, "PUT", "/dav/u/nope/e1.ics", EVENT.format(uid="e1", summary="x"))
    assert status == 404


# --- reports -----------------------------------------------------------------

def test_calendar_query_time_range_expands_recurrence(dav, store):
    store.create_calendar("cal", "Cal", "#111111")
    req(dav, "PUT", "/dav/u/cal/rec1.ics", RECURRING)
    req(dav, "PUT", "/dav/u/cal/e1.ics", EVENT.format(uid="e1", summary="Solo"))
    body = (
        '<?xml version="1.0"?><c:calendar-query xmlns:d="DAV:" '
        'xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
        '<c:filter><c:comp-filter name="VCALENDAR">'
        '<c:comp-filter name="VEVENT">'
        '<c:time-range start="20260301T000000Z" end="20260302T000000Z"/>'
        "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
    )
    status, _, out = req(dav, "REPORT", "/dav/u/cal/", body, depth="1")
    assert status == 207
    root = multistatus(out)
    # recurring event has a March 1 occurrence though DTSTART is Jan 1;
    # the solo June event must NOT match
    assert hrefs_of(root) == ["/dav/u/cal/rec1.ics"]
    assert "RRULE" in prop_of(root, "/dav/u/cal/rec1.ics", f"{C}calendar-data").text


def test_calendar_query_no_filter_returns_all(dav, store):
    store.create_calendar("cal", "Cal", "#111111")
    req(dav, "PUT", "/dav/u/cal/e1.ics", EVENT.format(uid="e1", summary="One"))
    body = (
        '<?xml version="1.0"?><c:calendar-query xmlns:d="DAV:" '
        'xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><d:getetag/></d:prop>"
        '<c:filter><c:comp-filter name="VCALENDAR">'
        '<c:comp-filter name="VEVENT"/>'
        "</c:comp-filter></c:filter></c:calendar-query>"
    )
    status, _, out = req(dav, "REPORT", "/dav/u/cal/", body, depth="1")
    root = multistatus(out)
    assert hrefs_of(root) == ["/dav/u/cal/e1.ics"]


def test_calendar_multiget(dav, store):
    store.create_calendar("cal", "Cal", "#111111")
    req(dav, "PUT", "/dav/u/cal/e1.ics", EVENT.format(uid="e1", summary="One"))
    body = (
        '<?xml version="1.0"?><c:calendar-multiget xmlns:d="DAV:" '
        'xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
        "<d:href>/dav/u/cal/e1.ics</d:href>"
        "<d:href>/dav/u/cal/ghost.ics</d:href>"
        "</c:calendar-multiget>"
    )
    status, _, out = req(dav, "REPORT", "/dav/u/cal/", body, depth="1")
    root = multistatus(out)
    responses = {r.findtext(f"{D}href"): r for r in root.findall(f"{D}response")}
    assert "UID:e1" in prop_of(root, "/dav/u/cal/e1.ics", f"{C}calendar-data").text
    ghost = responses["/dav/u/cal/ghost.ics"]
    assert "404" in ghost.findtext(f"{D}status")


def test_sync_collection_cycle(dav, store):
    store.create_calendar("cal", "Cal", "#111111")

    def sync(token):
        token_el = f"<d:sync-token>{token}</d:sync-token>" if token else "<d:sync-token/>"
        body = (
            '<?xml version="1.0"?><d:sync-collection xmlns:d="DAV:">'
            f"{token_el}<d:sync-level>1</d:sync-level>"
            "<d:prop><d:getetag/></d:prop></d:sync-collection>"
        )
        return req(dav, "REPORT", "/dav/u/cal/", body, depth="0")

    status, _, out = sync(None)
    assert status == 207
    root = multistatus(out)
    token1 = root.findtext(f"{D}sync-token")
    assert token1

    req(dav, "PUT", "/dav/u/cal/e1.ics", EVENT.format(uid="e1", summary="One"))
    status, _, out = sync(token1)
    root = multistatus(out)
    assert hrefs_of(root) == ["/dav/u/cal/e1.ics"]
    token2 = root.findtext(f"{D}sync-token")

    req(dav, "DELETE", "/dav/u/cal/e1.ics")
    status, _, out = sync(token2)
    root = multistatus(out)
    (resp,) = root.findall(f"{D}response")
    assert resp.findtext(f"{D}href") == "/dav/u/cal/e1.ics"
    assert "404" in resp.findtext(f"{D}status")

    # stale/garbage token -> 403 with DAV:valid-sync-token error
    status, _, out = sync("urn:exocalendar:sync:999999")
    assert status == 403
    err = ET.fromstring(out)
    assert err.find(f"{D}valid-sync-token") is not None


def test_report_unknown_type_rejected(dav, store):
    store.create_calendar("cal", "Cal", "#111111")
    status, _, _ = req(
        dav, "REPORT", "/dav/u/cal/",
        '<?xml version="1.0"?><x:weird xmlns:x="urn:x"/>', depth="1",
    )
    assert status == 403
