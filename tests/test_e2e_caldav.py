"""End-to-end: boot the real server, sync against the `caldav` client library.

This is the strongest interop signal short of a phone: the caldav package
exercises real discovery, REPORT, and sync-collection flows over HTTP.
"""

import threading
from datetime import datetime, timezone

import caldav
import pytest

from exocalendar.auth import hash_password
from exocalendar.config import Config
from exocalendar.server import ExoCalendarServer

PASSWORD = "e2e-pw"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    cfg = Config(
        username="u",
        password_hash=hash_password(PASSWORD),
        data_dir=tmp_path_factory.mktemp("data"),
        bind="127.0.0.1",
        port=0,
    )
    srv = ExoCalendarServer(cfg)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()


@pytest.fixture(scope="module")
def client(server):
    url = f"http://127.0.0.1:{server.server_address[1]}/dav/"
    with caldav.DAVClient(url=url, username="u", password=PASSWORD) as client:
        yield client


@pytest.fixture(scope="module")
def calendar(client):
    principal = client.principal()
    return principal.make_calendar(name="E2E", cal_id="e2e")


EVENT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//e2e//EN
BEGIN:VEVENT
UID:e2e-single
DTSTAMP:20260101T000000Z
DTSTART:20260701T100000Z
DTEND:20260701T110000Z
SUMMARY:Dentist
END:VEVENT
END:VCALENDAR
"""

RECURRING = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//e2e//EN
BEGIN:VEVENT
UID:e2e-weekly
DTSTAMP:20260101T000000Z
DTSTART:20260706T090000Z
DTEND:20260706T093000Z
RRULE:FREQ=WEEKLY;BYDAY=MO,FR
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""


def test_discovery(client, server):
    principal = client.principal()
    assert principal is not None
    # calendar home is discoverable and listable
    assert isinstance(principal.calendars(), list)


def test_make_calendar_visible(client, calendar, server):
    names = [c.get_display_name() for c in client.principal().calendars()]
    assert "E2E" in names
    assert server.store.get_calendar("e2e") is not None


def test_event_round_trip(calendar):
    calendar.save_event(EVENT)
    events = calendar.events()
    assert len(events) == 1
    assert "Dentist" in events[0].data


def test_date_search_expands_recurrence(calendar):
    calendar.save_event(RECURRING)
    found = calendar.search(
        start=datetime(2026, 7, 13, tzinfo=timezone.utc),
        end=datetime(2026, 7, 18, tzinfo=timezone.utc),
        event=True,
    )
    # week of Jul 13: Mon 13 + Fri 17 occurrences exist -> the resource matches
    uids = {e.icalendar_component.get("UID") for e in found}
    assert "e2e-weekly" in str(uids)
    assert "e2e-single" not in str(uids)  # July 1 event is outside the window


def test_edit_event(calendar):
    (event,) = [e for e in calendar.events() if "e2e-single" in e.data]
    event.data = event.data.replace("SUMMARY:Dentist", "SUMMARY:Dentist (moved)")
    event.save()
    (again,) = [e for e in calendar.events() if "e2e-single" in e.data]
    assert "Dentist (moved)" in again.data


def test_sync_collection_incremental(calendar):
    # initial sync grabs everything, delta reports the change
    objects = calendar.objects(load_objects=False)
    token1 = objects.sync_token
    assert token1
    initial = {o.url.path.rsplit("/", 1)[-1] for o in objects}
    assert "e2e-single.ics" in initial

    calendar.save_event(EVENT.replace("e2e-single", "e2e-second"))
    updated, deleted = objects.sync()
    changed = {u.url.path.rsplit("/", 1)[-1] for u in updated}
    assert "e2e-second.ics" in changed
    assert list(deleted) == []

    # delete propagates as a deletion on the next delta
    (victim,) = [e for e in calendar.events() if "e2e-second" in e.data]
    victim.delete()
    updated, deleted = objects.sync()
    gone = {str(d).rsplit("/", 1)[-1] for d in deleted}
    assert "e2e-second.ics" in gone


def test_delete_event(calendar):
    for e in calendar.events():
        e.delete()
    assert calendar.events() == []
