"""Regression tests for the findings of the PR #1 code review."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from exocalendar.ical import (
    Component,
    ContentLine,
    DTValue,
    TZResolver,
    escape_text,
    parse_dt,
    parse_duration,
    serialize_dt,
)
from exocalendar.rrule import RRule, expand

UTC = timezone.utc


def _vtz(tzid: str, body: str) -> TZResolver:
    cal = Component.parse(
        f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTIMEZONE\r\nTZID:{tzid}\r\n"
        + body
        + "END:VTIMEZONE\r\nEND:VCALENDAR\r\n"
    )
    return TZResolver.from_calendar(cal)


EXPIRED_DST = (
    "BEGIN:DAYLIGHT\r\nTZOFFSETFROM:+0200\r\nTZOFFSETTO:+0300\r\nTZNAME:EEST\r\n"
    "DTSTART:19970330T030000\r\n"
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU;UNTIL=20160327T010000Z\r\nEND:DAYLIGHT\r\n"
    "BEGIN:STANDARD\r\nTZOFFSETFROM:+0300\r\nTZOFFSETTO:+0200\r\nTZNAME:EET\r\n"
    "DTSTART:19971026T040000\r\n"
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU;UNTIL=20161030T010000Z\r\nEND:STANDARD\r\n"
)


def test_expired_dst_rules_settle_on_standard_offset():
    # a zone that stopped observing DST must not report the daylight offset
    # for dates years after the last transition (review finding 1a)
    tz = _vtz("Legacy/EET", EXPIRED_DST)
    for year in (2019, 2026):
        dtv = parse_dt(
            ContentLine.parse(f"DTSTART;TZID=Legacy/EET:{year}0601T120000"), tz
        )
        assert dtv.value.utcoffset() == timedelta(hours=2), year


def test_before_all_transitions_uses_from_offset():
    # review finding 1b: pre-history must use the FROM side, not TO
    tz = _vtz(
        "X/NY",
        "BEGIN:DAYLIGHT\r\nTZOFFSETFROM:-0500\r\nTZOFFSETTO:-0400\r\n"
        "DTSTART:20070311T020000\r\nRRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU\r\nEND:DAYLIGHT\r\n"
        "BEGIN:STANDARD\r\nTZOFFSETFROM:-0400\r\nTZOFFSETTO:-0500\r\n"
        "DTSTART:20071104T020000\r\nRRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\r\nEND:STANDARD\r\n",
    )
    dtv = parse_dt(ContentLine.parse("DTSTART;TZID=X/NY:20050115T120000"), tz)
    assert dtv.value.utcoffset() == timedelta(hours=-5)


def test_fromutc_round_trips_across_both_dst_transitions():
    # review finding 2: astimezone through the fall-back repeated hour
    from pathlib import Path

    corpus = Path(__file__).parent / "corpus" / "outlook-invite.ics"
    tz = TZResolver.from_calendar(Component.parse(corpus.read_text()))
    zone = tz.resolve("W. Europe Standard Time")
    for probe in [
        datetime(2026, 3, 29, 0, 30, tzinfo=UTC),   # around spring-forward (01:00Z)
        datetime(2026, 3, 29, 1, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),  # around fall-back (01:00Z)
        datetime(2026, 10, 25, 1, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 2, 0, tzinfo=UTC),
    ]:
        local = probe.astimezone(zone)
        assert local.astimezone(UTC) == probe, probe
        # and agree with the host zoneinfo db for this real-world zone
        assert local.replace(tzinfo=None) == probe.astimezone(
            ZoneInfo("Europe/Berlin")
        ).replace(tzinfo=None), probe


def test_vtimezone_agrees_with_zoneinfo_across_a_year():
    from pathlib import Path

    corpus = Path(__file__).parent / "corpus" / "google-export.ics"
    tz = TZResolver.from_calendar(Component.parse(corpus.read_text()))
    zone = tz.resolve("America/New_York")
    real = ZoneInfo("America/New_York")
    probe = datetime(2026, 1, 1, tzinfo=UTC)
    while probe < datetime(2027, 1, 1, tzinfo=UTC):
        assert probe.astimezone(zone).replace(tzinfo=None) == probe.astimezone(
            real
        ).replace(tzinfo=None), probe
        probe += timedelta(hours=6)


def test_unmodelable_vtimezone_falls_back_to_zoneinfo():
    # review finding 4: weird VTIMEZONEs must not abort the calendar
    for rule in (
        "RRULE:FREQ=YEARLY;BYDAY=-1SU",         # no BYMONTH (legal): modeled via DTSTART month
        "RRULE:FREQ=WEEKLY;BYDAY=SU",           # unsupported FREQ: skipped
        "RRULE:FREQ=YEARLY;BYMONTH=x;BYDAY=1SU",  # garbage: skipped
    ):
        tz = _vtz(
            "America/New_York",
            "BEGIN:STANDARD\r\nTZOFFSETFROM:-0400\r\nTZOFFSETTO:-0500\r\n"
            f"DTSTART:19701101T020000\r\n{rule}\r\nEND:STANDARD\r\n",
        )
        dtv = parse_dt(
            ContentLine.parse("DTSTART;TZID=America/New_York:20260715T120000"), tz
        )
        # resolution must succeed one way or the other
        assert dtv.value.utcoffset() in (timedelta(hours=-4), timedelta(hours=-5))


def test_serialize_dt_quotes_awkward_tzids():
    # review finding 5
    dtv = DTValue(
        value=datetime(2026, 10, 7, 14, 0, tzinfo=UTC),
        is_date=False,
        tzid="(UTC+01:00) Amsterdam, Berlin",
    )
    value, params = serialize_dt(dtv)
    line = ContentLine(name="DTSTART", params=params, value=value)
    reparsed = ContentLine.parse(line.serialize())
    assert reparsed.param("TZID") == "(UTC+01:00) Amsterdam, Berlin"
    assert reparsed.value == "20261007T140000"


def test_expand_reaches_windows_beyond_the_budget():
    # review finding 3: a daily series decades old must still show this week
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:old\r\n"
        "DTSTART:19960105T090000Z\r\nDTEND:19960105T093000Z\r\n"
        "RRULE:FREQ=DAILY\r\nSUMMARY:Ancient standup\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    cal = Component.parse(text)
    occs = expand(
        cal.find_children("VEVENT"),
        TZResolver.from_calendar(cal),
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 6, 8, tzinfo=UTC),
    )
    assert len(occs) == 7


def test_escape_text_normalizes_crlf():
    # review finding 6: CRLF from a textarea must survive a round-trip
    ev = Component(name="VEVENT")
    ev.set("UID", "x")
    ev.set("DESCRIPTION", escape_text("line1\r\nline2\rline3"))
    reparsed = Component.parse(ev.serialize())
    from exocalendar.ical import unescape_text

    assert unescape_text(reparsed.get("DESCRIPTION").value) == "line1\nline2\nline3"


def test_naive_until_read_in_dtstart_zone():
    # review finding 7: UNTIL without Z + aware DTSTART = DTSTART's local time
    zone = ZoneInfo("America/New_York")
    dtstart = datetime(2026, 6, 1, 20, 0, tzinfo=zone)
    rule = RRule.parse("FREQ=DAILY;UNTIL=20260605T235959")
    got = [d.day for d in rule.iterate(dtstart)]
    assert got == [1, 2, 3, 4, 5]


def test_bysecond_60_does_not_crash():
    # review finding 8
    rule = RRule.parse("FREQ=DAILY;BYSECOND=60;COUNT=2")
    got = list(rule.iterate(datetime(2026, 6, 1, 10, 0, 30)))
    assert len(got) == 2


def test_signed_empty_durations_rejected():
    # review finding 9
    for bad in ("-P", "+P", "-PT", "+PT"):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_all_day_expand_still_works():
    # guard: the retained-occurrence budget must not break date-mode duration math
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:d\r\n"
        "DTSTART;VALUE=DATE:20200101\r\nRRULE:FREQ=DAILY\r\nSUMMARY:D\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    cal = Component.parse(text)
    occs = expand(
        cal.find_children("VEVENT"),
        TZResolver.from_calendar(cal),
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 6, 4, tzinfo=UTC),
    )
    assert [o.start.value for o in occs] == [
        date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)
    ]


# --- import: UIDs that do not make a valid file name -------------------------

def _import_ics(*uids):
    events = "".join(
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTART:2026111{n}T230000Z\r\n"
        f"DTEND:2026111{n}T233000Z\r\nSUMMARY:E{n}\r\nEND:VEVENT\r\n"
        for n, uid in enumerate(uids)
    )
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//EN\r\n" + events
            + "END:VCALENDAR\r\n").encode()


def _import(store, body):
    import json

    from exocalendar.webapi import WebApi

    status, _, out = WebApi(store).handle_api("POST", "/api/import?calendar=c", body)
    return status, json.loads(out)


@pytest.fixture
def import_store(tmp_path):
    from exocalendar.store import Store

    store = Store(tmp_path / "data")
    store.create_calendar("c", "C", "#1b9e77")
    return store


def test_import_keeps_google_uids_that_start_with_an_underscore(import_store):
    # Google Calendar's own events have ids like "_8co48ga2...@google.com"
    uid = "_8co48ga27524ab9j6kp42b9k7523cba18grjgb9m6gs46c9g8grj8d1g74@google.com"
    status, data = _import(import_store, _import_ics(uid))
    assert status == 200 and data["imported"] == 1 and data["skipped"] == 0
    (res,) = import_store.list_resources("c")
    assert res.uid == uid
    assert res.href == uid.lstrip("_") + ".ics"


@pytest.mark.parametrize("uid", ["...", "-leading-dash", "_", "%41"])
def test_import_any_uid_gets_a_valid_file_name(import_store, uid):
    _, data = _import(import_store, _import_ics(uid))
    assert data["imported"] == 1, data
    assert import_store.list_resources("c")[0].uid == uid


def test_import_two_uids_with_the_same_file_name_both_land(import_store):
    # both reduce to "a_b.ics"; the second must neither overwrite nor be refused
    _, data = _import(import_store, _import_ics("a/b", "a:b"))
    assert data["imported"] == 2, data
    assert {r.uid for r in import_store.list_resources("c")} == {"a/b", "a:b"}


def test_reimporting_updates_in_place(import_store):
    _import(import_store, _import_ics("_g@google.com", "a/b", "a:b"))
    _, data = _import(import_store, _import_ics("_g@google.com", "a/b", "a:b"))
    assert data["imported"] == 3
    assert len(import_store.list_resources("c")) == 3


def test_import_says_why_it_skipped(import_store, monkeypatch):
    from exocalendar.store import BadResource

    def refuse(*args, **kwargs):
        raise BadResource("no thanks")

    monkeypatch.setattr(import_store, "put", refuse)
    _, data = _import(import_store, _import_ics("x"))
    assert data["skipped"] == 1
    assert data["errors"] == [{"uid": "x", "reason": "no thanks"}]
