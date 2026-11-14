import json

import pytest

from exocalendar.store import Store
from exocalendar.webapi import WebApi


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def api(store):
    return WebApi(store)


def call(api, method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else b""
    status, headers, out = api.handle_api(method, path, body)
    data = json.loads(out) if out and "json" in headers.get("Content-Type", "") else out
    return status, data


# --- calendars ---------------------------------------------------------------

def test_calendar_crud(api, store):
    status, data = call(api, "POST", "/api/calendars", {"displayname": "Work"})
    assert status == 201
    cal_id = data["id"]
    assert data["displayname"] == "Work"
    assert data["color"].startswith("#")
    assert data["feed_token"]

    status, data = call(api, "GET", "/api/calendars")
    assert status == 200
    assert [c["id"] for c in data] == [cal_id]

    status, data = call(api, "PATCH", f"/api/calendars/{cal_id}", {"color": "#d95f02"})
    assert status == 200
    assert data["color"] == "#d95f02"

    status, _ = call(api, "DELETE", f"/api/calendars/{cal_id}")
    assert status == 204
    assert store.list_calendars() == []


def test_create_calendar_distinct_colors(api):
    ids = []
    for name in ("A", "B", "C"):
        _, data = call(api, "POST", "/api/calendars", {"displayname": name})
        ids.append(data["color"])
    assert len(set(ids)) == 3


def test_rotate_feed_token(api, store):
    _, cal = call(api, "POST", "/api/calendars", {"displayname": "W"})
    old = cal["feed_token"]
    status, data = call(api, "POST", f"/api/calendars/{cal['id']}/rotate-feed-token")
    assert status == 200
    assert data["feed_token"] != old


def test_unknown_route_404(api):
    status, _ = call(api, "GET", "/api/nope")
    assert status == 404


# --- events ------------------------------------------------------------------

def make_cal(api):
    _, data = call(api, "POST", "/api/calendars", {"displayname": "Cal"})
    return data["id"]


def make_event(api, cal, **kw):
    payload = {
        "cal": cal,
        "summary": "Standup",
        "start": "2026-06-01T10:00:00+00:00",
        "end": "2026-06-01T10:30:00+00:00",
        "all_day": False,
        **kw,
    }
    status, data = call(api, "POST", "/api/events", payload)
    assert status == 201, data
    return data


def get_occurrences(api, cal, start="2026-06-01T00:00:00+00:00", end="2026-07-01T00:00:00+00:00"):
    from urllib.parse import quote

    status, data = call(
        api, "GET",
        f"/api/occurrences?start={quote(start)}&end={quote(end)}&calendars={cal}",
    )
    assert status == 200, data
    return data


def test_create_and_list_event(api):
    cal = make_cal(api)
    created = make_event(api, cal)
    assert created["href"].endswith(".ics")
    occs = get_occurrences(api, cal)
    assert len(occs) == 1
    occ = occs[0]
    assert occ["summary"] == "Standup"
    assert occ["cal"] == cal
    assert occ["all_day"] is False
    assert occ["etag"]
    assert occ["is_recurring"] is False
    assert occ["start"] == "2026-06-01T10:00:00+00:00"


def test_create_all_day_event(api):
    cal = make_cal(api)
    payload = {
        "cal": cal, "summary": "Trip",
        "start": "2026-06-10", "end": "2026-06-12", "all_day": True,
    }
    status, _ = call(api, "POST", "/api/events", payload)
    assert status == 201
    occs = get_occurrences(api, cal)
    assert occs[0]["all_day"] is True
    assert occs[0]["start"] == "2026-06-10"
    assert occs[0]["end"] == "2026-06-12"


def test_create_recurring_with_tzid(api):
    cal = make_cal(api)
    make_event(
        api, cal,
        start="2026-06-01T09:00:00+02:00", end="2026-06-01T09:30:00+02:00",
        tzid="Europe/Berlin", rrule="FREQ=WEEKLY;BYDAY=MO,WE",
    )
    occs = get_occurrences(api, cal)
    # Mon/Wed weekly across June = 9 occurrences
    assert len(occs) == 9
    assert all(o["is_recurring"] for o in occs)
    assert occs[0]["start"] == "2026-06-01T09:00:00+02:00"
    assert occs[0]["rrule"] == "FREQ=WEEKLY;BYDAY=MO,WE"


def test_bad_rrule_rejected(api):
    cal = make_cal(api)
    payload = {
        "cal": cal, "summary": "X",
        "start": "2026-06-01T10:00:00+00:00", "end": "2026-06-01T11:00:00+00:00",
        "all_day": False, "rrule": "FREQ=NONSENSE",
    }
    status, data = call(api, "POST", "/api/events", payload)
    assert status == 400
    assert "error" in data


def test_edit_all(api):
    cal = make_cal(api)
    created = make_event(api, cal)
    occ = get_occurrences(api, cal)[0]
    payload = {
        "etag": occ["etag"], "scope": "all",
        "summary": "Renamed",
        "start": "2026-06-01T11:00:00+00:00", "end": "2026-06-01T11:45:00+00:00",
        "all_day": False,
    }
    status, _ = call(api, "PUT", f"/api/events/{cal}/{created['href']}", payload)
    assert status == 200
    occ = get_occurrences(api, cal)[0]
    assert occ["summary"] == "Renamed"
    assert occ["start"] == "2026-06-01T11:00:00+00:00"


def test_etag_conflict_409(api):
    cal = make_cal(api)
    created = make_event(api, cal)
    payload = {
        "etag": "stale", "scope": "all", "summary": "X",
        "start": "2026-06-01T10:00:00+00:00", "end": "2026-06-01T10:30:00+00:00",
        "all_day": False,
    }
    status, _ = call(api, "PUT", f"/api/events/{cal}/{created['href']}", payload)
    assert status == 409


def test_edit_this_occurrence(api):
    cal = make_cal(api)
    created = make_event(api, cal, rrule="FREQ=DAILY;COUNT=5")
    occs = get_occurrences(api, cal)
    target = occs[2]  # June 3
    payload = {
        "etag": target["etag"], "scope": "this",
        "recurrence_id": target["recurrence_id"],
        "summary": "Moved standup",
        "start": "2026-06-03T15:00:00+00:00", "end": "2026-06-03T15:30:00+00:00",
        "all_day": False,
    }
    status, _ = call(api, "PUT", f"/api/events/{cal}/{created['href']}", payload)
    assert status == 200
    occs = get_occurrences(api, cal)
    assert len(occs) == 5
    summaries = {o["start"]: o["summary"] for o in occs}
    assert summaries["2026-06-03T15:00:00+00:00"] == "Moved standup"
    assert "2026-06-03T10:00:00+00:00" not in summaries


def test_edit_future(api):
    cal = make_cal(api)
    created = make_event(api, cal, rrule="FREQ=DAILY;COUNT=10")
    occs = get_occurrences(api, cal)
    target = occs[4]  # June 5
    payload = {
        "etag": target["etag"], "scope": "future",
        "recurrence_id": target["recurrence_id"],
        "summary": "New era",
        "start": "2026-06-05T14:00:00+00:00", "end": "2026-06-05T14:30:00+00:00",
        "all_day": False,
    }
    status, _ = call(api, "PUT", f"/api/events/{cal}/{created['href']}", payload)
    assert status == 200
    occs = get_occurrences(api, cal)
    old = [o for o in occs if o["summary"] == "Standup"]
    new = [o for o in occs if o["summary"] == "New era"]
    assert len(old) == 4  # June 1-4
    assert len(new) == 6  # June 5-10 at the new time
    assert new[0]["start"] == "2026-06-05T14:00:00+00:00"
    assert old[-1]["start"] == "2026-06-04T10:00:00+00:00"


def test_delete_this(api):
    cal = make_cal(api)
    created = make_event(api, cal, rrule="FREQ=DAILY;COUNT=5")
    occs = get_occurrences(api, cal)
    target = occs[1]
    status, _ = call(
        api, "DELETE", f"/api/events/{cal}/{created['href']}",
        {"scope": "this", "recurrence_id": target["recurrence_id"]},
    )
    assert status == 204
    occs = get_occurrences(api, cal)
    assert len(occs) == 4
    assert target["start"] not in [o["start"] for o in occs]


def test_delete_future(api):
    cal = make_cal(api)
    created = make_event(api, cal, rrule="FREQ=DAILY;COUNT=5")
    occs = get_occurrences(api, cal)
    status, _ = call(
        api, "DELETE", f"/api/events/{cal}/{created['href']}",
        {"scope": "future", "recurrence_id": occs[3]["recurrence_id"]},
    )
    assert status == 204
    assert len(get_occurrences(api, cal)) == 3


def test_delete_all(api):
    cal = make_cal(api)
    created = make_event(api, cal, rrule="FREQ=DAILY;COUNT=5")
    status, _ = call(api, "DELETE", f"/api/events/{cal}/{created['href']}", {"scope": "all"})
    assert status == 204
    assert get_occurrences(api, cal) == []


# --- import / export / feed --------------------------------------------------

IMPORT_ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//EN\r\n"
    "BEGIN:VEVENT\r\nUID:imp1\r\nDTSTART:20260610T100000Z\r\n"
    "DTEND:20260610T110000Z\r\nSUMMARY:One\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:imp2\r\nDTSTART:20260611T100000Z\r\n"
    "DTEND:20260611T110000Z\r\nSUMMARY:Two\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:imp2\r\nRECURRENCE-ID:20260611T100000Z\r\n"
    "DTSTART:20260611T120000Z\r\nDTEND:20260611T130000Z\r\n"
    "SUMMARY:Two moved\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_import_splits_by_uid(api, store):
    cal = make_cal(api)
    status, headers, out = api.handle_api(
        "POST", f"/api/import?calendar={cal}", IMPORT_ICS.encode()
    )
    assert status == 200
    data = json.loads(out)
    assert data["imported"] == 2  # two UIDs
    resources = store.list_resources(cal)
    assert len(resources) == 2
    # the override travels with its master
    imp2 = next(r for r in resources if r.uid == "imp2")
    assert "RECURRENCE-ID" in imp2.ics_text


def test_export_merges(api):
    cal = make_cal(api)
    call(api, "POST", f"/api/import?calendar={cal}", None) if False else None
    api.handle_api("POST", f"/api/import?calendar={cal}", IMPORT_ICS.encode())
    status, headers, out = api.handle_api("GET", f"/api/export/{cal}.ics", b"")
    assert status == 200
    assert "text/calendar" in headers["Content-Type"]
    text = out.decode()
    assert text.count("BEGIN:VEVENT") == 3
    assert text.count("BEGIN:VCALENDAR") == 1


def test_feed_token_auth(api, store):
    cal = make_cal(api)
    api.handle_api("POST", f"/api/import?calendar={cal}", IMPORT_ICS.encode())
    token = store.get_calendar(cal).feed_token
    status, headers, out = api.handle_feed(f"/feed/{cal}.ics?t={token}")
    assert status == 200
    assert "text/calendar" in headers["Content-Type"]
    assert b"BEGIN:VEVENT" in out
    status, _, _ = api.handle_feed(f"/feed/{cal}.ics?t=wrong")
    assert status == 404
    status, _, _ = api.handle_feed(f"/feed/{cal}.ics")
    assert status == 404


def test_static_serves_index(api):
    status, headers, out = api.handle_static("GET", "/")
    assert status == 200
    assert "text/html" in headers["Content-Type"]
    assert b"exocalendar" in out
    status, _, _ = api.handle_static("GET", "/app.js")
    assert status == 200
    status, _, _ = api.handle_static("GET", "/../secrets")
    assert status == 404
