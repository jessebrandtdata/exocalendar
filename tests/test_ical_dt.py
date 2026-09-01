from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from exocalendar.ical import (
    Component,
    ContentLine,
    DTValue,
    TZResolver,
    TZUnknown,
    event_span,
    parse_dt,
    parse_duration,
    serialize_dt,
    serialize_duration,
)

CORPUS = Path(__file__).parent / "corpus"


def resolver_for(fname: str) -> TZResolver:
    cal = Component.parse((CORPUS / fname).read_text())
    return TZResolver.from_calendar(cal)


def test_parse_utc_datetime():
    cl = ContentLine.parse("DTSTART:20260401T120000Z")
    dtv = parse_dt(cl, TZResolver.from_calendar(None))
    assert dtv.is_date is False
    assert dtv.tzid is None
    assert dtv.value == datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)


def test_parse_date_value():
    cl = ContentLine.parse("DTSTART;VALUE=DATE:20260401")
    dtv = parse_dt(cl, TZResolver.from_calendar(None))
    assert dtv.is_date is True
    assert dtv.value == date(2026, 4, 1)


def test_parse_floating_datetime():
    cl = ContentLine.parse("DTSTART:20260401T120000")
    dtv = parse_dt(cl, TZResolver.from_calendar(None))
    assert dtv.is_date is False
    assert dtv.tzid is None
    assert dtv.value == datetime(2026, 4, 1, 12, 0)
    assert dtv.value.tzinfo is None


def test_parse_tzid_via_zoneinfo():
    cl = ContentLine.parse("DTSTART;TZID=America/New_York:20260415T090000")
    dtv = parse_dt(cl, TZResolver.from_calendar(None))
    assert dtv.tzid == "America/New_York"
    assert dtv.value.utcoffset() == timedelta(hours=-4)  # EDT in April


def test_dst_transition_offsets():
    tz = TZResolver.from_calendar(None)
    before = parse_dt(ContentLine.parse("DTSTART;TZID=America/New_York:20260307T013000"), tz)
    after = parse_dt(ContentLine.parse("DTSTART;TZID=America/New_York:20260309T013000"), tz)
    assert before.value.utcoffset() == timedelta(hours=-5)  # EST
    assert after.value.utcoffset() == timedelta(hours=-4)  # EDT after Mar 8 2026


def test_custom_vtimezone_resolves_nonolson_tzid():
    tz = resolver_for("outlook-invite.ics")
    cl = ContentLine.parse("DTSTART;TZID=W. Europe Standard Time:20261007T140000")
    dtv = parse_dt(cl, tz)
    # October 7 is still daylight time in Europe (+02:00)
    assert dtv.value.utcoffset() == timedelta(hours=2)
    winter = parse_dt(
        ContentLine.parse("DTSTART;TZID=W. Europe Standard Time:20261207T140000"), tz
    )
    assert winter.value.utcoffset() == timedelta(hours=1)


def test_vtimezone_preferred_over_zoneinfo_but_falls_back():
    tz = resolver_for("google-export.ics")
    dtv = parse_dt(
        ContentLine.parse("DTSTART;TZID=America/New_York:20260415T090000"), tz
    )
    assert dtv.value.utcoffset() == timedelta(hours=-4)


def test_unknown_tzid_raises():
    with pytest.raises(TZUnknown):
        parse_dt(
            ContentLine.parse("DTSTART;TZID=Not/AZone:20260101T000000"),
            TZResolver.from_calendar(None),
        )


def test_serialize_dt_round_trips():
    tz = TZResolver.from_calendar(None)
    for text in [
        "DTSTART:20260401T120000Z",
        "DTSTART;VALUE=DATE:20260401",
        "DTSTART:20260401T120000",
        "DTSTART;TZID=America/New_York:20260415T090000",
    ]:
        cl = ContentLine.parse(text)
        dtv = parse_dt(cl, tz)
        value, params = serialize_dt(dtv)
        rebuilt = ContentLine(name="DTSTART", params=params, value=value)
        assert parse_dt(rebuilt, tz) == dtv


def test_parse_duration():
    assert parse_duration("PT1H30M") == timedelta(hours=1, minutes=30)
    assert parse_duration("P1DT12H") == timedelta(days=1, hours=12)
    assert parse_duration("-P1DT12H") == timedelta(days=-1, hours=-12)
    assert parse_duration("P2W") == timedelta(weeks=2)
    assert parse_duration("PT15M") == timedelta(minutes=15)
    with pytest.raises(ValueError):
        parse_duration("1H")


def test_serialize_duration_round_trip():
    for td in [
        timedelta(minutes=15),
        timedelta(hours=1, minutes=30),
        timedelta(days=1, hours=12),
        timedelta(weeks=2),
        timedelta(0),
        -timedelta(hours=2),
    ]:
        assert parse_duration(serialize_duration(td)) == td


def _vevent(*lines: str) -> Component:
    body = "\r\n".join(lines)
    return Component.parse(f"BEGIN:VEVENT\r\nUID:u\r\n{body}\r\nEND:VEVENT\r\n")


def test_event_span_dtend():
    ev = _vevent("DTSTART:20260401T090000Z", "DTEND:20260401T100000Z")
    start, end = event_span(ev, TZResolver.from_calendar(None))
    assert (end.value - start.value) == timedelta(hours=1)


def test_event_span_duration():
    ev = _vevent("DTSTART:20260401T090000Z", "DURATION:PT45M")
    start, end = event_span(ev, TZResolver.from_calendar(None))
    assert (end.value - start.value) == timedelta(minutes=45)


def test_event_span_date_default_one_day():
    ev = _vevent("DTSTART;VALUE=DATE:20260401")
    start, end = event_span(ev, TZResolver.from_calendar(None))
    assert start.value == date(2026, 4, 1)
    assert end.value == date(2026, 4, 2)


def test_event_span_datetime_default_zero_length():
    ev = _vevent("DTSTART:20260401T090000Z")
    start, end = event_span(ev, TZResolver.from_calendar(None))
    assert start.value == end.value


def test_dtvalue_is_dataclass_with_fields():
    dtv = DTValue(value=date(2026, 1, 1), is_date=True, tzid=None)
    assert dtv.is_date
